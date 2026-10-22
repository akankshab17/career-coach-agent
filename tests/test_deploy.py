"""Production settings: the access code is mandatory, briefs are private, a daily cap on new briefs, and the image files
keep secrets and the API out of reach."""
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

import api
import httpx
from jobs import JobStore
from test_api import REQ, T0, Clock, fake_runner

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = JobStore(str(tmp_path / "jobs.db"), now=clock)
    app = api.create_app(store=store, runner=fake_runner(), start_worker=False)
    with TestClient(app) as client:
        yield {"client": client, "store": store, "worker": app.state.worker, "clock": clock}


def post(env, **kw):
    return env["client"].post("/briefs", json={**REQ, **kw})


# ---- the access code is mandatory in production ----

@pytest.mark.parametrize("code, ok", [(None, False), ("short", False), ("x" * 15, False), ("x" * 16, True)])
def test_the_server_refuses_to_start_without_a_long_enough_code_when_required(monkeypatch, code, ok):
    monkeypatch.setattr(api, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(api, "create_app", lambda: "app")  # no database, no worker thread
    monkeypatch.setenv("REQUIRE_ACCESS_CODE", "1")
    if code is not None:
        monkeypatch.setenv("APP_ACCESS_CODE", code)
    if ok:
        assert api.serve() == "app"
    else:
        with pytest.raises(RuntimeError, match="APP_ACCESS_CODE"):
            api.serve()


def test_without_the_requirement_a_missing_code_is_allowed_for_local_use(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(api, "create_app", lambda: "app")
    assert api.serve() == "app"


# ---- a daily cap on new briefs ----

def test_new_briefs_are_capped_per_utc_day_with_a_clear_message(env, monkeypatch):
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "2")
    env["clock"].t = T0.replace(hour=21, minute=30)
    assert post(env, company="A").status_code == 202
    assert post(env, company="B").status_code == 202
    r = post(env, company="C")
    assert r.status_code == 429
    assert r.json()["detail"].startswith("Daily limit reached: 2 new briefs have been started today (limit 2).")
    assert "in 2h 30m" in r.json()["detail"]
    assert env["store"].count("queued") == 2

    assert post(env, company="A").status_code == 202 and post(env, company="A").json()["reused"] is True  # reuse is free
    env["clock"].t = T0.replace(hour=0, minute=1) + timedelta(days=1)  # a new UTC day
    assert post(env, company="C").status_code == 202


def test_failed_briefs_count_too_and_zero_means_no_limit(env, monkeypatch):
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "1")
    env["worker"].runner = fake_runner(fail_with=RuntimeError("boom"))
    post(env, company="A")
    env["worker"].process_next()  # it failed, but it used searches and tokens
    assert post(env, company="B").status_code == 429
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "0")
    assert post(env, company="B").status_code == 202


def test_the_default_is_30_and_a_bad_value_fails_at_startup(tmp_path, monkeypatch):
    assert api.daily_limit() == 30
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "thirty")
    with pytest.raises(ValueError, match="MAX_BRIEFS_PER_DAY"):
        api.create_app(db_path=str(tmp_path / "jobs.db"), start_worker=False)


# ---- private briefs ----

def test_private_mode_turns_off_the_shared_list(env, monkeypatch):
    job_id = post(env).json()["job_id"]
    monkeypatch.setenv("PRIVATE_BRIEFS", "1")
    assert env["client"].get("/health").json()["private_briefs"] is True
    r = env["client"].get("/briefs")
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    assert env["client"].get(f"/briefs/{job_id}").status_code == 200  # a brief's own (unguessable) id still opens it


def test_the_app_lists_only_this_sessions_briefs_in_private_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_BRIEFS", "1")
    monkeypatch.setenv("BRIEF_API_URL", "http://api.test")
    app = api.create_app(store=JobStore(str(tmp_path / "jobs.db")), runner=fake_runner(), start_worker=True)
    with TestClient(app) as client:
        monkeypatch.setattr(httpx, "request", lambda method, url, headers=None, timeout=None, **kw:
                            client.request(method, url.removeprefix("http://api.test"), headers=headers, **kw))
        client.post("/briefs", json={**REQ, "company": "SomeoneElse"})  # another coach's brief
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
        assert not at.exception and not at.sidebar.warning
        assert at.sidebar.header[0].value == "Your briefs"
        at.text_input[0].input("Acme")
        at.text_input[1].input("Software Engineer")
        at.button[0].click()
        at = at.run(timeout=20)
        labels = [b.label for b in at.sidebar.button]
        assert any(l.startswith("Acme · Software Engineer") for l in labels)
        assert not any("SomeoneElse" in l for l in labels)


def test_the_app_shows_the_daily_limit_message_as_is(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_BRIEFS_PER_DAY", "1")
    monkeypatch.setenv("BRIEF_API_URL", "http://api.test")
    app = api.create_app(store=JobStore(str(tmp_path / "jobs.db")), runner=fake_runner(), start_worker=False)
    with TestClient(app) as client:
        monkeypatch.setattr(httpx, "request", lambda method, url, headers=None, timeout=None, **kw:
                            client.request(method, url.removeprefix("http://api.test"), headers=headers, **kw))
        client.post("/briefs", json={**REQ, "company": "First"})
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
        at.text_input[0].input("Acme")
        at.text_input[1].input("Software Engineer")
        at.button[0].click()
        at = at.run(timeout=20)
        assert any(e.value.startswith("Daily limit reached") for e in at.error)


# ---- the image files ----

def test_the_image_keeps_secrets_out_and_production_settings_on():
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    for entry in (".env", ".cache/", "evals/", "tests/", ".venv/", "*.debug.md"):
        assert entry in ignore, entry
    docker = (ROOT / "Dockerfile").read_text()
    for setting in ("REQUIRE_ACCESS_CODE=1", "PRIVATE_BRIEFS=1", "MAX_BRIEFS_PER_DAY=30", "USER user", "--uid 1000"):
        assert setting in docker, setting
    assert ".env" not in [line.split()[1] for line in docker.splitlines() if line.startswith("COPY")]
    start = (ROOT / "start.sh").read_text()
    assert "--host 127.0.0.1 --port 8000" in start and "--server.address 0.0.0.0" in start  # only Streamlit is reachable
    readme = (ROOT / "README.md").read_text()
    assert readme.startswith("---\n") and "sdk: docker" in readme and "app_port: 7860" in readme  # the Space's config
