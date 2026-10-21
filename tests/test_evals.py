"""Tests for the eval harness (evals/harness.py). No network, no real LLM."""
import json
import re
from pathlib import Path

import pytest

import brief
import harness
import helpers
import llm
import search

ROOT = Path(__file__).resolve().parent.parent

ABOUT = "https://www.acme.com/about"
REDDIT = "https://www.reddit.com/r/cs/1"
POSTING = "https://www.acme.com/careers/listing/swe-new-grad"

PAGES = {
    ABOUT: "Acme builds payment software for online businesses. " * 20,
    POSTING: ("Software Engineer, New Grad\n\nLocation: Seattle, WA\n\nYou will build APIs for merchants every day. "
              "You have a degree in computer science or equivalent experience. ") * 6,
}
SNIPPETS = {
    ABOUT: "Acme builds payment software for online businesses.",
    REDDIT: "The interview had three rounds. The first round was a coding screen on string parsing.",
    POSTING: "New grad role in Seattle, WA",
}
EXTRACTED = {
    ABOUT: [{"fact": "Acme builds payment software.", "exact_quote": "Acme builds payment software for online businesses", "section": "company"}],
    REDDIT: [
        {"fact": "The interview had three rounds.", "exact_quote": "The interview had three rounds", "section": "interview"},
        {"fact": "Round one was a string parsing screen.", "exact_quote": "The first round was a coding screen on string parsing", "section": "interview"},
    ],
    POSTING: [
        {"fact": "You will build APIs for merchants.", "exact_quote": "You will build APIs for merchants every day", "section": "role"},
        {"fact": "A CS degree or equivalent experience is required.", "exact_quote": "a degree in computer science or equivalent experience", "section": "role"},
    ],
}


class FakeTavily:
    """The 'real' Tavily for the tests: the same three results for every query, pages for two of the three URLs."""

    def __init__(self, *a, **kw):
        self.search_calls, self.extract_calls = 0, 0

    def search(self, query, max_results=None, **options):
        self.search_calls += 1
        return {"results": [{"title": t, "url": u, "content": SNIPPETS[u]} for u, t in
                            [(ABOUT, "Acme builds payment software"), (REDDIT, "Acme new grad interview experience"),
                             (POSTING, "Software Engineer, New Grad")]]}

    def extract(self, urls):
        self.extract_calls += 1
        return {"results": [{"url": u, "raw_content": PAGES[u]} for u in urls if u in PAGES],
                "failed_results": [{"url": u} for u in urls if u not in PAGES]}  # Reddit can't be fetched


def echo_llm(extracted=EXTRACTED, extract_ok=True):
    """Fake llm.generate: extraction answers from EXTRACTED; the writer echoes the facts it is given, cited."""
    def generate(system, user, task="write"):
        if "extract facts" in system:
            if not extract_ok:
                pytest.fail("extraction must not run")
            url = re.search(r"^URL: (.*)$", user, re.M).group(1)
            return json.dumps({"facts": extracted.get(url, [])})
        sections = {"company": [], "interview": [], "role": [], "timeline": []}
        block = None
        for line in user.splitlines():
            m = re.match(r"### (\w+) facts", line)
            if m:
                block = m.group(1)
            m = re.match(r"\[(F\d+)\] \(.*?\) (.*?)  \| quote:", line)
            if m and block:
                sections[block].append(f"- {m.group(2)} [{m.group(1)}]")
        return ("## TL;DR\n" + "\n".join(sections["interview"][:1]) +
                "\n## 1. Company overview + recent news\n" + "\n".join(sections["company"]) +
                "\n## 2. Interview process + common questions\n" + "\n".join(sections["interview"]) +
                "\n## 3. What the role really needs\n" + "\n".join(sections["role"]) + "\n")
    return generate


CASE = harness.Case(name="acme-new-grad", company="Acme", role="Software Engineer", level="new-grad")


@pytest.fixture
def env(monkeypatch, tmp_path):
    import tavily

    monkeypatch.setenv("TAVILY_API_KEY", "test-key-not-real")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # harness.main must not read the real .env
    monkeypatch.setattr(llm, "generate", echo_llm())
    monkeypatch.setattr(tavily, "TavilyClient", FakeTavily)
    paths = {"fixtures": tmp_path / "fixtures", "results": tmp_path / "results", "baseline_path": tmp_path / "baseline.json"}
    monkeypatch.setattr(harness, "EVALS", tmp_path)
    (tmp_path / "cases.yaml").write_text("cases:\n  - {name: acme-new-grad, company: Acme, role: Software Engineer, level: new-grad}\n"
                                         "  - {name: acme-2, company: Acme, role: Software Engineer, level: new-grad}\n")
    return paths


def go(env, mode, cases=None, **kw):
    return harness.run_all(cases or [CASE], mode, fixtures=env["fixtures"], results=env["results"],
                           baseline_path=env["baseline_path"], **kw)


# ---- record / replay / from-facts ----

def test_record_saves_fixtures_then_replay_needs_no_tavily_and_gives_the_same_numbers(env, monkeypatch):
    import tavily

    rec = go(env, "record")
    m = rec.data["cases"][CASE.name]
    assert m["status"] == "ok" and m["grounded"] >= 5 and m["facts_company"] == 1 and m["facts_interview"] == 2 and m["facts_role"] >= 2
    d = env["fixtures"] / CASE.name
    assert {p.name for p in d.iterdir()} == {"search.json", "pages.json", "meta.json", "facts.json"}
    assert json.loads((d / "pages.json").read_text())[POSTING]["raw_content"].startswith("Software Engineer, New Grad")
    assert json.loads((d / "pages.json").read_text())[REDDIT]["failed"] is True  # failures are recorded too
    assert json.loads((d / "meta.json").read_text())["recorded_on"]

    def boom(*a, **k):
        raise AssertionError("replay must not create a Tavily client")

    monkeypatch.setattr(tavily, "TavilyClient", boom)
    monkeypatch.delenv("TAVILY_API_KEY")  # replay works without a key too
    rep = go(env, "replay")
    r = rep.data["cases"][CASE.name]
    for metric in ("sources", "full_text", "facts_company", "facts_interview", "facts_role", "facts_timeline", "grounded", "rejected"):
        assert r[metric] == m[metric], metric
    assert r["fixture_misses"] == 0
    assert any(k.startswith("previous run") for k in rep.comparisons)
    assert rep.regressions == []


def test_replay_uses_the_recorded_date_not_todays(env, monkeypatch):
    go(env, "record")
    meta_path = env["fixtures"] / CASE.name / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["recorded_on"] = "2020-01-15"
    meta_path.write_text(json.dumps(meta))
    seen = {}
    real = llm.generate

    def spy(system, user, task="write"):
        if task == "write":
            seen["today"] = re.search(r"Today's date: (\S+)", user).group(1)
        return real(system, user, task)

    monkeypatch.setattr(llm, "generate", spy)
    go(env, "replay")
    assert seen["today"] == "2020-01-15"


def test_a_missing_fixture_is_reported_counted_and_flagged(env, capsys):
    go(env, "record")
    pages = env["fixtures"] / CASE.name / "pages.json"
    data = json.loads(pages.read_text())
    del data[ABOUT]  # as if the code started asking for a page that was never recorded
    pages.write_text(json.dumps(data))
    run = go(env, "replay")
    assert run.data["cases"][CASE.name]["fixture_misses"] >= 1
    log = (env["results"] / run.data["timestamp"] / f"{CASE.name}.log").read_text()
    assert "[replay] MISSING page not in fixtures" in log and ABOUT in log
    assert ("fixture_misses" in {row["metric"] for _, _, row in run.regressions})


def test_replay_without_fixtures_says_how_to_record(env):
    with pytest.raises(FileNotFoundError, match="--record"):
        go(env, "replay")


def test_from_facts_reruns_only_writer_and_finalize(env, monkeypatch):
    rec = go(env, "record")
    # nothing but the writer may run: no searching, no fetching, no extraction
    monkeypatch.setattr(llm, "generate", echo_llm(extract_ok=False))
    monkeypatch.setattr(search, "search", lambda *a, **k: pytest.fail("no search in --from-facts"))
    monkeypatch.setattr(search, "extract", lambda *a, **k: pytest.fail("no fetch in --from-facts"))
    run = go(env, "from-facts")
    m, r = rec.data["cases"][CASE.name], run.data["cases"][CASE.name]
    assert r["status"] == "ok" and r["grounded"] == m["grounded"] and r["sources"] == m["sources"] and r["full_text"] == m["full_text"]
    assert r["cache_hits"] is None  # extraction was not run
    brief_text = (env["results"] / run.data["timestamp"] / f"{CASE.name}.md").read_text()
    assert "Acme builds payment software" in brief_text and "## Sources" in brief_text
    # a facts-only run is only compared on writer metrics, and never against full runs' extraction numbers
    assert run.comparisons == {} or all(row["metric"] in {"dropped", "rewrites", "empty_sections", "fixture_misses", "status"}
                                        for comp in run.comparisons.values() for rows in comp.values() for row in rows)


def test_from_facts_needs_a_facts_file(env):
    with pytest.raises(FileNotFoundError, match="facts.json"):
        go(env, "from-facts")


def test_facts_json_is_refreshed_by_a_full_replay(env):
    go(env, "record")
    path = env["fixtures"] / CASE.name / "facts.json"
    path.write_text("{}")
    go(env, "replay")
    saved = json.loads(path.read_text())
    assert len(saved["facts"]) >= 5 and saved["sources"] and saved["case"]["company"] == "Acme"
    assert all("content" not in s or len(s["content"]) <= 300 for s in saved["sources"].values())  # not the whole pages again


def test_repeat_averages_writer_metrics_over_n_writer_runs(env, monkeypatch, capsys):
    """Salesforce dropped 8 sentences, then 0, on the same facts: one writer sample is too noisy to compare."""
    go(env, "record")
    base, runs = echo_llm(), []

    def noisy(system, user, task="write"):
        out = base(system, user, task)
        if "extract facts" not in system:
            runs.append(1)
            out += "\n".join(f"- An uncited claim number {i}." for i in range(3 * (len(runs) - 1)))  # 0, 3, 6 dropped
        return out

    monkeypatch.setattr(llm, "generate", noisy)
    run = go(env, "replay", repeat=3)
    m = run.data["cases"][CASE.name]
    assert len(runs) == 3 and run.data["repeat"] == 3
    assert [r["dropped"] for r in m["writer_runs"]] == [0, 3, 6] and m["dropped"] == 3.0
    first = json.loads((env["fixtures"] / CASE.name / "facts.json").read_text())
    assert m["grounded"] == len(first["facts"])  # extraction numbers come from the one full run
    out = env["results"] / run.data["timestamp"]
    assert (out / f"{CASE.name}.repeat3.md").exists()
    text = harness.render(run)
    assert "mean of 3 writer runs" in text and re.search(r"sentences dropped per run: +0, 3, 6", text)


def test_cli_rejects_a_repeat_below_1(env, capsys):
    assert harness.main(["--repeat", "0"]) == 2


# ---- clients ----

def test_recording_and_replay_clients_roundtrip_and_report_misses(capsys):
    real = FakeTavily()
    rec = harness.RecordingClient(real)
    first = rec.search("acme interview", max_results=5, exclude_domains=["glassdoor.com"])
    rec.extract([ABOUT, REDDIT])
    replay = harness.ReplayClient(rec.searches, rec.pages)
    assert replay.search("acme interview", max_results=5, exclude_domains=["glassdoor.com"]) == first
    ex = replay.extract([ABOUT, REDDIT])
    assert [r["url"] for r in ex["results"]] == [ABOUT] and [f["url"] for f in ex["failed_results"]] == [REDDIT]
    assert replay.misses == []  # a page that failed when recorded fails again, but is not a miss

    assert replay.search("a different query") == {"results": []}
    assert replay.search("acme interview", max_results=10) == {"results": []}  # options are part of the key
    ex = replay.extract(["https://never.recorded/x"])
    assert ex["failed_results"][0]["error"] == "not in fixtures"
    assert [m["kind"] for m in replay.misses] == ["search", "search", "page"]
    assert capsys.readouterr().err.count("[replay] MISSING") == 3


def test_pages_are_capped_in_fixtures():
    class Big:
        def extract(self, urls):
            return {"results": [{"url": urls[0], "raw_content": "x" * (harness.PAGE_CAP + 5000)}]}

    rec = harness.RecordingClient(Big())
    rec.extract(["https://a.example/1"])
    assert len(rec.pages["https://a.example/1"]["raw_content"]) == harness.PAGE_CAP


# ---- secrets ----

def test_fixtures_never_contain_api_keys(env, monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 40)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        harness.write_json(tmp_path / "x.json", {"page": "token gsk_" + "a" * 40})
    with pytest.raises(RuntimeError, match="API key"):
        harness.write_json(tmp_path / "x.json", {"page": "tvly-abcdefgh12345678"})
    with pytest.raises(RuntimeError, match="API key"):
        harness.write_json(tmp_path / "x.json", {"page": "sk-" + "b" * 30})
    harness.write_json(tmp_path / "ok.json", {"page": "an ordinary web page about task-oriented work"})

    go(env, "record")  # a recorded run with keys in the environment writes clean files
    for path in (env["fixtures"] / CASE.name).iterdir():
        text = path.read_text()
        assert "test-key-not-real" not in text and "gsk_" not in text


# ---- metrics ----

BRIEF = """# Interview brief: Acme

## TL;DR
- Limited interview data (1 source).
- No interview reports found.

## 1. Company overview + recent news
No grounded information found for this section.

## 2. Interview process + common questions
No interview reports found.

**Timeline**

## 3. What the role really needs
No official posting text found; based on job aggregator.
You will build APIs. [1](https://x)

## 4. How to prepare
No grounded information found for this section.

## 5. Questions to ask the interviewer
1. Which APIs? [1](https://x)

## Sources
1. A
"""


def test_empty_sections_are_those_with_only_placeholders_and_warnings():
    assert harness.empty_sections(BRIEF) == ["TL;DR", "1. Company overview + recent news", "2. Interview process + common questions",
                                             "4. How to prepare"]
    assert harness.empty_sections("## TL;DR\n- A real bullet [1](u)\n") == []


def test_cache_counts_are_summed_over_the_extract_lines():
    steps = ["[search] 3 sources", "[extract] 4 candidate facts from 3 sources (0 snippet-only; 2 from cache, 1 via LLM, 0 failed)",
             "[extract] 2 candidate facts from 1 sources (0 snippet-only; 1 from cache, 0 via LLM, 0 failed)"]
    assert harness.cache_counts(steps) == (3, 1) and harness.cache_counts(["[search] x"]) is None
    assert harness.cache_rate({"cache_hits": 3, "cache_misses": 1}) == 0.75 and harness.cache_rate({}) is None


def test_usage_totals_cover_every_call(monkeypatch):
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda m, s, u, effort=None, **_: ("t", 100, 50, 30))
    llm.reset_usage()
    llm.generate("s", "u", task="extract")
    llm.generate("s", "u")
    totals = llm.usage_totals()
    assert (totals["calls"], totals["input"], totals["output"], totals["reasoning"]) == (2, 200, 100, 60) and totals["latency"] >= 0


def test_metrics_report_all_requested_numbers(env):
    m = go(env, "record").data["cases"][CASE.name]
    for key in ("sources", "full_text", "facts_company", "facts_interview", "facts_role", "facts_timeline", "grounded", "rejected",
                "dropped", "rewrites", "empty_sections", "fixture_misses", "latency_s", "tokens_in", "tokens_out", "tokens_reasoning"):
        assert key in m, key
    assert m["full_text"] == 2 and m["sources"] == 3  # the Reddit post could not be fetched
    assert m["cache_hits"] == 0 and m["cache_misses"] >= 3


# ---- comparison: what counts as worse ----

def case_m(**kw):
    base = {m: 10 for m in harness.METRICS} | {"fixture_misses": 0, "empty_sections": 0, "cache_hits": 4, "cache_misses": 0, "status": "ok"}
    return base | kw


def verdict(metric, ref, cur, ref_case=None, cur_case=None):
    ref_case = ref_case or case_m(**{metric: ref})
    cur_case = cur_case or case_m(**{metric: cur})
    return harness.judge(metric, ref, cur, ref_case, cur_case)[0]


@pytest.mark.parametrize("metric, ref, cur, expected", [
    ("grounded", 10, 9, "worse"), ("grounded", 10, 11, "better"), ("grounded", 10, 10, "same"),
    ("facts_company", 5, 4, "worse"), ("facts_interview", 5, 6, "better"), ("full_text", 8, 7, "worse"),
    ("rejected", 3, 4, "worse"), ("rejected", 3, 2, "better"),
    ("dropped", 1, 3, "same"), ("dropped", 1, 4, "worse"), ("dropped", 5, 0, "better"),  # writer: only worse by MORE than 2
    ("rewrites", 2, 4, "same"), ("rewrites", 2, 5, "worse"),
    ("empty_sections", 0, 1, "worse"), ("empty_sections", 2, 1, "better"),
    ("fixture_misses", 0, 1, "worse"),
    ("sources", 10, 1, "info"), ("tokens_reasoning", 1, 99, "info"),
    ("tokens_out", 1000, 1200, "same"), ("tokens_out", 1000, 1300, "worse"), ("tokens_in", 1000, 500, "better"),
])
def test_what_counts_as_worse(metric, ref, cur, expected):
    assert verdict(metric, ref, cur) == expected


def test_latency_is_only_judged_when_the_cache_hit_rate_matches():
    same = (case_m(latency_s=10.0), case_m(latency_s=20.0))
    assert harness.judge("latency_s", 10.0, 20.0, *same)[0] == "worse"
    cold = case_m(latency_s=20.0, cache_hits=0, cache_misses=4)
    verdict_, note = harness.judge("latency_s", 10.0, 20.0, case_m(latency_s=10.0), cold)
    assert verdict_ == "info" and "cache hit rate differs" in note


def test_extraction_metrics_get_a_tolerance_only_when_the_cache_hit_rate_differs():
    warm, cold = case_m(grounded=30), case_m(grounded=28, cache_hits=0, cache_misses=4)
    assert harness.judge("grounded", 30, 29, warm, case_m(grounded=29))[0] == "worse"  # same rate: strict
    assert harness.judge("grounded", 30, 28, warm, cold)[0] == "same"  # fresh LLM samples differ a little: within max(2, 10%)
    assert harness.judge("grounded", 30, 26, warm, case_m(grounded=26, cache_hits=0, cache_misses=4))[0] == "worse"
    no_info = ({"grounded": 5}, {"grounded": 4})  # nothing known about caching on either side (e.g. old files): strict
    assert harness.judge("grounded", 5, 4, *no_info)[0] == "worse"


def test_compare_case_only_compares_what_a_facts_run_recomputes():
    ref, cur = case_m(grounded=10, dropped=0), case_m(grounded=3, dropped=9)
    full = {r["metric"]: r["verdict"] for r in harness.compare_case(ref, cur, "replay", "replay")}
    assert full["grounded"] == "worse" and full["dropped"] == "worse"
    facts = {r["metric"]: r["verdict"] for r in harness.compare_case(ref, cur, "replay", "from-facts")}
    assert "grounded" not in facts and "latency_s" not in facts and facts["dropped"] == "worse"
    assert harness.compare_case(None, cur, "replay", "replay") == []
    failed = harness.compare_case(ref, case_m(status="failed", error="boom"), "replay", "replay")
    assert any(r["metric"] == "status" and r["verdict"] == "worse" for r in failed)


# ---- previous run, baseline, exit codes ----

def test_previous_run_skips_aborted_runs_and_other_families(tmp_path):
    def save(ts, mode, aborted=None):
        harness.write_json(tmp_path / f"{ts}.json", {"timestamp": ts, "mode": mode, "aborted": aborted, "cases": {}})

    save("20260101-000000", "replay")
    save("20260102-000000", "from-facts")
    save("20260103-000000", "replay", aborted="daily limit")
    save("20260104-000000", "record")
    (tmp_path / "notes.json").write_text("{}")
    assert harness.previous_run(tmp_path, "replay")["timestamp"] == "20260104-000000"  # record and replay are the same family
    assert harness.previous_run(tmp_path, "replay", exclude="20260104-000000")["timestamp"] == "20260101-000000"
    assert harness.previous_run(tmp_path, "from-facts")["timestamp"] == "20260102-000000"
    assert harness.previous_run(tmp_path / "empty", "replay") is None


def test_a_worse_second_run_is_flagged_against_the_previous_one(env, monkeypatch):
    go(env, "record")
    monkeypatch.setattr(llm, "generate", echo_llm({**EXTRACTED, ABOUT: []}))  # extraction now finds no company fact
    run = go(env, "replay", use_cache=False)
    bad = {row["metric"] for _, _, row in run.regressions}
    assert {"facts_company", "grounded"} <= bad
    text = harness.render(run)
    assert "▼ WORSE" in text and "regression(s):" in text and "facts: company" in text


def test_baseline_holds_metrics_only_and_merges_cases(env):
    run = go(env, "record")
    written = harness.set_baseline(env["baseline_path"], run.data)
    assert written == [CASE.name]
    base = json.loads(env["baseline_path"].read_text())
    entry = base["cases"][CASE.name]
    assert entry["grounded"] >= 5 and "empty_section_names" not in entry and not any(isinstance(v, str) for v in entry.values())
    assert base["mode"] == "replay" and base["models"]["extract"] and "pinned" in base

    other = harness.Case(name="acme-2", company="Acme", role="Software Engineer", level="new-grad")
    harness.set_baseline(env["baseline_path"], go(env, "record", cases=[other]).data)
    assert set(json.loads(env["baseline_path"].read_text())["cases"]) == {CASE.name, "acme-2"}


def test_baseline_refuses_facts_only_aborted_or_failed_runs(env, tmp_path):
    full = go(env, "record").data
    with pytest.raises(ValueError, match="full run"):
        harness.set_baseline(tmp_path / "b.json", {**full, "mode": "from-facts"})
    with pytest.raises(ValueError, match="aborted"):
        harness.set_baseline(tmp_path / "b.json", {**full, "aborted": "daily limit"})
    with pytest.raises(ValueError, match="no case finished"):
        harness.set_baseline(tmp_path / "b.json", {**full, "cases": {CASE.name: {**full["cases"][CASE.name], "status": "failed"}}})


def test_runs_are_compared_with_the_pinned_baseline_too(env, monkeypatch):
    harness.set_baseline(env["baseline_path"], go(env, "record").data)
    monkeypatch.setattr(llm, "generate", echo_llm({**EXTRACTED, POSTING: []}))
    run = go(env, "replay", use_cache=False)
    assert "baseline" in run.comparisons
    assert any(ref == "baseline" and row["metric"] == "facts_role" for ref, _, row in run.regressions)


def test_cli_exit_codes(env, monkeypatch, capsys):
    def cli(*args):
        return harness.main(["--cases", CASE.name, *args])

    assert cli("--record") == 0
    assert cli("--cases", "nope") == 2 and "unknown case" in capsys.readouterr().err
    monkeypatch.setattr(llm, "generate", echo_llm({**EXTRACTED, ABOUT: []}))
    assert cli("--no-cache") == 1  # worse than the recorded run
    assert "regression(s):" in capsys.readouterr().out
    assert cli("--no-cache", "--no-fail") == 0

    assert cli("--from-facts", "--set-baseline") == 2  # a facts-only run cannot become the baseline
    assert not (harness.EVALS / "baseline.json").exists()
    monkeypatch.setattr(llm, "generate", echo_llm())
    assert cli("--no-cache", "--set-baseline") == 0
    assert (harness.EVALS / "baseline.json").exists()


def test_cli_list_and_case_selection(env, capsys):
    assert harness.main(["--list"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("acme-new-grad: Acme / Software Engineer / new-grad")
    with pytest.raises(SystemExit):  # --record and --from-facts exclude each other
        harness.main(["--record", "--from-facts"])


def test_a_daily_limit_aborts_the_run_and_it_is_not_used_for_comparisons(env, monkeypatch, capsys):
    go(env, "record")
    good = harness.previous_run(env["results"], "replay")

    def quota(system, user, task="write"):
        raise llm.QuotaExceededError("groq daily limit reached. try again in 8m 35s")

    monkeypatch.setattr(llm, "generate", quota)
    two = [CASE, harness.Case(name="acme-2", company="Acme", role="Software Engineer", level="new-grad")]
    for c in two:
        (env["fixtures"] / c.name).mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copytree(env["fixtures"] / CASE.name, env["fixtures"] / "acme-2", dirs_exist_ok=True)
    run = go(env, "replay", cases=two, use_cache=False)
    assert run.data["aborted"] and "daily LLM limit" in run.data["aborted"] and list(run.data["cases"]) == []  # stopped at the first case
    assert run.comparisons == {} and "ABORTED" in harness.render(run)
    assert harness.previous_run(env["results"], "replay")["timestamp"] == good["timestamp"]  # the aborted run is skipped
    assert harness.main(["--no-cache"]) == 2


def test_a_failed_case_is_recorded_and_flagged_not_fatal(env, monkeypatch):
    go(env, "record")

    def broken(system, user, task="write"):
        raise llm.LLMError("groq call failed: boom")

    monkeypatch.setattr(llm, "generate", broken)
    run = go(env, "replay", use_cache=False)
    m = run.data["cases"][CASE.name]
    assert m["status"] == "failed" and "Every fact extraction failed" in m["error"]  # every extraction hit the broken LLM
    assert any(row["metric"] == "status" for _, _, row in run.regressions)
    assert "FAILED" in harness.render(run)


# ---- the files themselves ----

def test_cases_yaml_has_the_four_cases():
    cases = harness.load_cases(ROOT / "evals" / "cases.yaml")
    assert [c.name for c in cases] == ["google-intern", "cummins-swe-new-grad", "perplexity-ai-engineer-new-grad", "salesforce-apm-new-grad"]
    by = {c.name: c for c in cases}
    assert (by["google-intern"].company, by["google-intern"].level) == ("Google", "intern")
    assert by["salesforce-apm-new-grad"].role == "Associate Product Manager" and by["perplexity-ai-engineer-new-grad"].role == "AI Engineer"
    assert all(c.location == "United States" for c in cases)


def test_fixtures_and_results_are_gitignored_but_the_baseline_is_not():
    ignored = (ROOT / ".gitignore").read_text().split()
    assert "evals/fixtures/" in ignored and "evals/results/" in ignored and ".env" in ignored
    assert "evals/baseline.json" not in ignored and "baseline.json" not in ignored


def test_render_shows_a_table_per_case_with_every_metric(env):
    text = harness.render(go(env, "record"))
    for label in ("sources", "full-text pages", "facts: company", "facts: interview", "facts: role", "facts: timeline", "grounded",
                  "rejected", "sentences dropped", "empty sections", "latency (s)", "tokens in", "tokens out"):
        assert label in text, label
    assert text.count("=== acme-new-grad") == 1 and "0 regression(s)." in text


# ---- which provider served each case ----

_REAL_GENERATE = llm.generate  # captured at import, before the env fixture swaps in echo_llm


def provider_llm(monkeypatch, groq_daily_limit):
    """Real llm.generate over fake providers that answer like echo_llm; Groq can be made to hit its daily limit."""
    from test_llm import TPD, RateLimitError

    echo = echo_llm()

    def groq(model, s, u, effort=None, **_):
        if groq_daily_limit:
            raise RateLimitError(TPD)
        return echo(s, u), 10, 10

    monkeypatch.setitem(llm._PROVIDERS, "groq", groq)
    monkeypatch.setitem(llm._PROVIDERS, "openai", lambda model, s, u, effort=None, **_: (echo(s, u), 10, 10))
    monkeypatch.setattr(llm, "generate", _REAL_GENERATE)
    monkeypatch.setenv("FALLBACK_PROVIDER", "openai")
    monkeypatch.setenv("FALLBACK_EXTRACT_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("FALLBACK_WRITE_MODEL", "gpt-5.4-mini")


def test_each_case_records_its_provider_and_a_provider_change_is_flagged_not_failed(env, monkeypatch):
    provider_llm(monkeypatch, groq_daily_limit=False)
    first = go(env, "record")
    assert first.data["cases"][CASE.name]["llm"] == {"extract": ["groq/openai/gpt-oss-20b"], "write": ["groq/openai/gpt-oss-120b"]}
    assert first.data["models"]["fallback"] == {"provider": "openai", "extract": "gpt-5.6-luna", "write": "gpt-5.4-mini"}

    provider_llm(monkeypatch, groq_daily_limit=True)
    run = go(env, "replay", use_cache=False)
    m = run.data["cases"][CASE.name]
    assert m["llm"] == {"extract": ["openai/gpt-5.6-luna"], "write": ["openai/gpt-5.4-mini"]} and m["fallback_calls"] > 0
    changed = run.provider_changes
    assert {row["metric"] for _, _, row in changed} == {"provider (extract)", "provider (write)"}
    assert all(row["ref"] == "groq" and row["cur"] == "openai" for _, _, row in changed)
    assert not any(row["verdict"] == "changed" for _, _, row in run.regressions)  # a warning, not a regression
    text = harness.render(run)
    assert "WARNING: 2 provider change(s)" in text and "≠ CHANGED" in text and "openai/gpt-5.6-luna" in text


def test_a_baseline_without_provider_records_is_compared_with_its_configured_provider():
    cur = {"llm": {"extract": ["openai/gpt-5.6-luna"], "write": ["groq/openai/gpt-oss-120b"]}}
    rows = harness.provider_rows({}, cur, {"provider": "groq"}, {})
    assert [(r["metric"], r["ref"], r["cur"]) for r in rows] == [("provider (extract)", "groq", "openai")]
    assert harness.provider_rows({}, cur, {}, {}) == []  # nothing known about the reference: nothing to flag
