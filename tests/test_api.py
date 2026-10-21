"""The web API: jobs, the worker, reuse, restart, access code. A fake pipeline, a temporary SQLite file, no network."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
import llm
from brief import BriefResult
from jobs import JobStore

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
REQ = {"company": "Acme", "role": "Software Engineer", "level": "new-grad", "location": "United States"}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def fake_runner(calls=None, fail_with=None):
    """Walks through the steps like the real pipeline (progress callbacks included) and returns a tiny brief."""
    def run(company, role, level, jd, location, stats):
        if calls is not None:
            calls.append((company, role, level, jd, location))
        for step, line in (("search", "[search] 3 sources"), ("extract", "[extract] 5 candidate facts"), ("write", "[write] draft")):
            stats.on_progress(step, None)
            stats.steps.append(line)
            stats.on_progress(None, line)
        if fail_with:
            raise fail_with
        stats.sources_total, stats.facts_grounded = 3, 5
        text = f"# Interview brief: {company} — {role}\n\n## TL;DR\n- Fact [1](https://acme.com/about)\n\n## Sources\n1. [About](https://acme.com/about)\n"
        return BriefResult(text, stats, {"S1": {"url": "https://acme.com/about"}})
    return run


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ACCESS_CODE", raising=False)
    clock, calls = Clock(), []
    store = JobStore(str(tmp_path / "jobs.db"), now=clock)
    app = api.create_app(store=store, runner=fake_runner(calls), start_worker=False)
    with TestClient(app) as client:
        yield {"client": client, "store": store, "worker": app.state.worker, "clock": clock, "calls": calls, "tmp": tmp_path}


def post(env, **kw):
    r = env["client"].post("/briefs", json={**REQ, **kw})
    assert r.status_code == 202, r.text
    return r.json()


def get(env, job_id):
    r = env["client"].get(f"/briefs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ---- the job lifecycle ----

def test_post_returns_a_queued_job_at_once_and_the_worker_runs_it(env):
    job = post(env, jd="  We need a backend engineer.  ")
    assert job["status"] == "queued" and job["reused"] is False and len(job["job_id"]) == 32
    assert env["calls"] == []  # nothing ran during the request

    assert env["worker"].process_next() is True
    done = get(env, job["job_id"])
    assert done["status"] == "done" and done["step"] == "done"
    assert done["brief_markdown"].startswith("# Interview brief: Acme") and "[About](https://acme.com/about)" in done["brief_markdown"]
    assert done["steps"] == ["[search] 3 sources", "[extract] 5 candidate facts", "[write] draft"]
    assert done["debug_summary"]["sources"] == 3 and done["debug_summary"]["facts_grounded"] == 5
    assert done["has_jd"] is True and env["calls"] == [("Acme", "Software Engineer", "new-grad", "We need a backend engineer.", "United States")]
    assert env["worker"].process_next() is False  # queue empty

    debug = env["client"].get(f"/briefs/{job['job_id']}/debug").json()
    assert debug["debug_markdown"].startswith("# Run report")


def test_progress_is_visible_while_the_job_runs(env):
    job = post(env)
    seen = []

    def spy(*args):
        seen.append(get(env, job["job_id"]))  # what a polling client sees mid-run
        return fake_runner()(*args)

    env["worker"].runner = spy
    env["worker"].process_next()
    assert seen[0]["status"] == "running" and seen[0]["started_at"]


def test_a_failed_run_is_reported_with_its_message(env):
    env["worker"].runner = fake_runner(fail_with=llm.QuotaExceededError("groq daily limit reached; try again in 5m"))
    job = post(env)
    env["worker"].process_next()
    failed = get(env, job["job_id"])
    assert failed["status"] == "failed" and "daily limit" in failed["error"] and "brief_markdown" not in failed
    assert failed["step"] == "write" and failed["debug_summary"]["steps"][-1] == "[write] draft"  # where it stopped


def test_a_crash_fails_the_job_but_not_the_worker(env):
    env["worker"].runner = fake_runner(fail_with=KeyError("boom"))
    first, second = post(env), post(env, company="Other")
    env["worker"].process_next()
    assert get(env, first["job_id"])["error"] == "Internal error: KeyError: 'boom'"
    assert "Traceback" in env["client"].get(f"/briefs/{first['job_id']}/debug").json()["debug_markdown"]
    env["worker"].runner = fake_runner()
    env["worker"].process_next()
    assert get(env, second["job_id"])["status"] == "done"


def test_jobs_run_oldest_first(env):
    a = post(env, company="A")
    env["clock"].t += timedelta(seconds=1)
    b = post(env, company="B")
    env["worker"].process_next()
    assert get(env, a["job_id"])["status"] == "done" and get(env, b["job_id"])["status"] == "queued"


# ---- reuse ----

def test_a_recent_brief_with_the_same_inputs_is_reused_not_rerun(env):
    first = post(env)
    env["worker"].process_next()
    env["clock"].t += timedelta(days=6, hours=23)
    again = post(env, company="  acme ", role="software engineer")  # case and spaces don't matter
    assert again["job_id"] == first["job_id"] and again["reused"] is True and again["status"] == "done"
    assert "brief_markdown" in again
    assert env["worker"].process_next() is False and len(env["calls"]) == 1


def test_no_reuse_after_7_days_with_other_inputs_or_when_forced(env):
    first = post(env)
    env["worker"].process_next()
    assert post(env, level="intern")["job_id"] != first["job_id"]
    assert post(env, location="Canada")["job_id"] != first["job_id"]
    assert post(env, jd="a pasted JD")["job_id"] != first["job_id"]
    assert post(env, force=True)["job_id"] != first["job_id"]
    for _ in range(4):
        env["worker"].process_next()  # the forced rerun finishes now, so it is the newest brief for these inputs
    env["clock"].t += timedelta(days=7, seconds=1)
    stale = post(env)
    assert stale["job_id"] != first["job_id"] and stale["reused"] is False


def test_a_failed_brief_is_not_reused(env):
    env["worker"].runner = fake_runner(fail_with=llm.LLMError("nope"))
    first = post(env)
    env["worker"].process_next()
    assert post(env)["job_id"] != first["job_id"]


def test_an_identical_job_still_in_progress_is_returned_instead_of_a_duplicate(env):
    first = post(env)
    second = post(env)
    assert second["job_id"] == first["job_id"] and second["status"] == "queued" and second["reused"] is True
    assert env["store"].count("queued") == 1


def test_the_queue_is_capped(env, monkeypatch):
    monkeypatch.setattr(api, "MAX_QUEUED", 2)
    post(env, company="A")
    post(env, company="B")
    r = env["client"].post("/briefs", json={**REQ, "company": "C"})
    assert r.status_code == 429


# ---- restart ----

def test_jobs_survive_a_restart_and_interrupted_ones_run_again(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ACCESS_CODE", raising=False)
    path = str(tmp_path / "jobs.db")
    store = JobStore(path)
    done, interrupted, waiting = (store.create(c, "SWE", "new-grad", "United States", None) for c in ("Done", "Interrupted", "Waiting"))
    store.claim_next()
    store.finish(done["id"], "# brief", "# debug", {})
    store.claim_next()  # "Interrupted" is running when the server dies
    store.set_step(interrupted["id"], "extract")

    calls = []
    app = api.create_app(db_path=path, runner=fake_runner(calls), start_worker=False)  # a new process, same file
    with TestClient(app) as client:
        assert client.get(f"/briefs/{done['id']}").json()["brief_markdown"] == "# brief"
        assert client.get(f"/briefs/{interrupted['id']}").json()["status"] == "queued"  # re-queued at startup
        app.state.worker.process_next()
        app.state.worker.process_next()
        assert [c[0] for c in calls] == ["Interrupted", "Waiting"]  # oldest first
        assert client.get(f"/briefs/{waiting['id']}").json()["status"] == "done"


def test_the_background_worker_thread_runs_jobs_by_itself(tmp_path, monkeypatch):
    import time

    monkeypatch.delenv("APP_ACCESS_CODE", raising=False)
    app = api.create_app(db_path=str(tmp_path / "jobs.db"), runner=fake_runner(), start_worker=True)
    with TestClient(app) as client:
        job_id = client.post("/briefs", json=REQ).json()["job_id"]
        deadline = time.time() + 5
        while client.get(f"/briefs/{job_id}").json()["status"] != "done" and time.time() < deadline:
            time.sleep(0.02)
        assert client.get(f"/briefs/{job_id}").json()["status"] == "done"


# ---- listing, errors, validation ----

def test_recent_briefs_newest_first_without_the_big_text(env):
    for i, company in enumerate(("A", "B", "C")):
        env["clock"].t = T0 + timedelta(minutes=i)
        post(env, company=company)
    rows = env["client"].get("/briefs?limit=2").json()
    assert [r["company"] for r in rows] == ["C", "B"]
    assert set(rows[0]) >= {"job_id", "status", "role", "level", "location", "created_at"} and "brief_md" not in rows[0] and "id" not in rows[0]
    assert env["client"].get("/briefs?limit=0").status_code == 422


def test_unknown_jobs_are_404(env):
    assert env["client"].get("/briefs/nope").status_code == 404
    assert env["client"].get("/briefs/nope/debug").status_code == 404


@pytest.mark.parametrize("bad", [
    {"company": ""}, {"company": "   "}, {"role": ""}, {"level": "senior"}, {"location": " "},
    {"company": "x" * 201}, {"jd": "x" * 20001},
])
def test_bad_requests_are_422(env, bad):
    assert env["client"].post("/briefs", json={**REQ, **bad}).status_code == 422
    assert env["store"].count("queued") == 0


def test_defaults_and_blank_jd(env):
    job = env["client"].post("/briefs", json={"company": "Acme", "role": "SWE", "jd": "   "}).json()
    assert (job["level"], job["location"], job["has_jd"]) == ("new-grad", "United States", False)


# ---- access code ----

def test_no_access_code_set_means_open(env):
    assert env["client"].get("/health").json() == {"status": "ok", "access_code_required": False}
    assert env["client"].get("/auth").status_code == 200


def test_an_access_code_is_required_when_set(env, monkeypatch):
    monkeypatch.setenv("APP_ACCESS_CODE", "s3cret")
    c = env["client"]
    assert c.get("/health").json()["access_code_required"] is True  # health stays open
    for method, url in (("post", "/briefs"), ("get", "/briefs"), ("get", "/briefs/x"), ("get", "/briefs/x/debug"), ("get", "/auth")):
        assert getattr(c, method)(url, **({"json": REQ} if method == "post" else {})).status_code == 401, url
    assert c.post("/briefs", json=REQ, headers={"X-Access-Code": "wrong"}).status_code == 401
    assert env["store"].count("queued") == 0  # nothing was queued without the code
    ok = {"X-Access-Code": "s3cret"}
    assert c.get("/auth", headers=ok).status_code == 200
    assert c.post("/briefs", json=REQ, headers=ok).status_code == 202
