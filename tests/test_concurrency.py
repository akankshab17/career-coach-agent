"""Parallel extraction: bounded concurrency, deterministic output order, thread-safe llm state, stopping on a daily limit."""
import json
import os
import re
import threading
import time

import pytest

import facts
import graph
import llm
from brief import RunStats
from test_llm import TPD, RateLimitError

URLS = [f"https://example.com/{i}" for i in range(8)]


def make_state(n=8, **extra):
    sources = {f"S{i + 1}": {"url": URLS[i], "title": f"T{i}", "full_text": f"Page {i} says fact number {i} is true.",
                             "kind": "other", "group": "interview"} for i in range(n)}
    return {"company": "Acme", "role": "SWE", "level": "new-grad", "use_cache": False, "sources": sources, "extracted": [],
            "facts": [], "stats": RunStats(), **extra}


def answer(user: str) -> str:
    i = int(re.search(r"^URL: https://example.com/(\d+)$", user, re.M).group(1))
    return json.dumps({"facts": [{"fact": f"Fact {i}.", "exact_quote": f"fact number {i} is true", "section": "interview"}]})


# ---- extract_node ----

def test_sources_are_extracted_in_parallel_up_to_the_limit(monkeypatch):
    monkeypatch.setenv("EXTRACT_CONCURRENCY", "4")
    active, peak, lock = [0], [0], threading.Lock()
    four_at_once = threading.Barrier(4, timeout=5)  # would time out (and fail) if calls ran one at a time

    def generate(system, user, task="write"):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if int(re.search(r"/(\d+)$", re.search(r"^URL: (.*)$", user, re.M).group(1)).group(1)) < 4:
                four_at_once.wait()
            time.sleep(0.01)
            return answer(user)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(llm, "generate", generate)
    out = graph.extract_node(make_state())
    assert peak[0] == 4 and len(out["candidates"]) == 8
    assert re.search(r"\[extract\] 8 candidate facts from 8 sources \(0 snippet-only; 0 from cache, 8 via LLM, 0 failed\) "
                     r"in \d+\.\ds, 4 at a time", "\n".join(graph_lines(monkeypatch)))


def graph_lines(monkeypatch):
    """Run extract_node once more with a quick fake and return its log lines."""
    monkeypatch.setattr(llm, "generate", lambda s, u, task="write": answer(u))
    state = make_state()
    graph.extract_node(state)
    return state["stats"].steps


def test_output_is_in_source_order_whatever_order_the_calls_finish_in(monkeypatch):
    def generate(system, user, task="write"):
        i = int(re.search(r"^URL: https://example.com/(\d+)$", user, re.M).group(1))
        time.sleep(0.005 * (8 - i))  # the last source finishes first
        return answer(user)

    monkeypatch.setattr(llm, "generate", generate)
    state = make_state()
    out = graph.extract_node(state)
    assert [c["source_id"] for c in out["candidates"]] == [f"S{i + 1}" for i in range(8)]
    assert list(state["stats"].per_source) == [f"S{i + 1}" for i in range(8)]
    assert out["extracted"] == [f"S{i + 1}" for i in range(8)]


def test_concurrency_1_runs_one_at_a_time_and_bad_values_are_refused(monkeypatch):
    monkeypatch.setenv("EXTRACT_CONCURRENCY", "1")
    names = set()
    monkeypatch.setattr(llm, "generate", lambda s, u, task="write": names.add(threading.current_thread().name) or answer(u))
    state = make_state(3)
    graph.extract_node(state)
    assert len(names) == 1 and "1 at a time" in state["stats"].steps[-1]
    for bad in ("0", "17", "four", "-2"):
        monkeypatch.setenv("EXTRACT_CONCURRENCY", bad)
        with pytest.raises(llm.FatalLLMError, match="EXTRACT_CONCURRENCY"):
            graph.extract_concurrency()
    monkeypatch.delenv("EXTRACT_CONCURRENCY")
    assert graph.extract_concurrency() == 4


def test_one_bad_source_is_logged_the_others_still_count(monkeypatch):
    monkeypatch.setattr(llm, "generate", lambda s, u, task="write": "not json" if "example.com/2" in u else answer(u))
    state = make_state(4)
    out = graph.extract_node(state)
    assert [c["source_id"] for c in out["candidates"]] == ["S1", "S2", "S4"]
    assert state["stats"].per_source["S3"]["error"] and "1 failed" in state["stats"].steps[-1]


def test_a_daily_limit_without_fallback_stops_every_worker(monkeypatch):
    monkeypatch.setenv("EXTRACT_CONCURRENCY", "2")
    started = []

    def generate(system, user, task="write"):
        started.append(user)
        if "example.com/0" in user:
            raise llm.QuotaExceededError("groq daily limit reached on openai/gpt-oss-20b")
        time.sleep(0.05)
        return answer(user)

    monkeypatch.setattr(llm, "generate", generate)
    with pytest.raises(llm.QuotaExceededError, match="daily limit"):
        graph.extract_node(make_state())
    assert len(started) < 8  # the sources not yet started were skipped


# ---- llm.py under threads ----

@pytest.fixture
def providers(monkeypatch):
    """Groq answers until `state['limit']` is True, then raises a daily-limit error; OpenAI always answers."""
    state = {"limit": False}

    def groq(model, s, u, effort=None, **_):
        time.sleep(0.001)
        if state["limit"]:
            raise RateLimitError(TPD)
        return "groq", 10, 5

    def openai(model, s, u, effort=None, **_):
        time.sleep(0.001)
        return "openai", 1000, 100

    monkeypatch.setitem(llm._PROVIDERS, "groq", groq)
    monkeypatch.setitem(llm._PROVIDERS, "openai", openai)
    monkeypatch.setenv("FALLBACK_PROVIDER", "openai")
    monkeypatch.setenv("FALLBACK_EXTRACT_MODEL", "gpt-5.6-luna")
    llm.reset_usage()
    return state


def run_threads(n, fn):
    errors = []

    def wrapped(i):
        try:
            fn(i)
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_usage_counts_stay_exact_under_parallel_calls(providers):
    run_threads(16, lambda i: [llm.generate("s", "u", task="extract") for _ in range(25)])
    totals = llm.usage_totals()
    assert totals["calls"] == 400 and totals["input"] == 4000 and totals["output"] == 2000
    assert "calls=400" in llm.summary_line()


def test_parallel_calls_hitting_the_daily_limit_switch_once_and_all_retry_on_the_fallback(providers, capsys):
    providers["limit"] = True
    served = {}

    def call(i):
        assert llm.generate("s", "u", task="extract") == "openai"
        served[i] = llm.served_by()

    run_threads(12, call)
    assert capsys.readouterr().err.count("FALLBACK:") == 1  # one switch, logged once
    assert set(served.values()) == {("openai", "gpt-5.6-luna")}
    spend = llm.load_spend()
    assert spend["calls"] == 12 and spend["openai_usd"] == pytest.approx(12 * (1000 * 0.20 + 100 * 1.20) / 1e6)  # no lost updates


def test_served_by_is_per_thread(providers):
    llm.generate("s", "u", task="extract")
    assert llm.served_by() == ("groq", "openai/gpt-oss-20b")
    seen = []
    t = threading.Thread(target=lambda: seen.append(llm.served_by()))
    t.start()
    t.join()
    assert seen == [None]  # another thread's call says nothing about this one


def test_a_groq_answer_is_cached_under_groq_even_if_another_thread_switched_meanwhile(providers, monkeypatch):
    """Thread A's Groq call is in flight when thread B hits the daily limit and switches the run to the fallback."""
    a_in_flight, switched = threading.Event(), threading.Event()

    def groq(model, s, u, effort=None, **_):
        if "example.com/0" in u:  # A: answers only after B has switched the run
            a_in_flight.set()
            switched.wait(5)
            return answer(u), 10, 5
        raise RateLimitError(TPD)  # B: the daily limit

    def openai(model, s, u, effort=None, **_):
        switched.set()
        return answer(u), 10, 5

    monkeypatch.setitem(llm._PROVIDERS, "groq", groq)
    monkeypatch.setitem(llm._PROVIDERS, "openai", openai)
    state = make_state(2)
    a = threading.Thread(target=facts.extract_facts, args=(state["sources"]["S1"], "Acme", "SWE", "new-grad"))
    b = threading.Thread(target=facts.extract_facts, args=(state["sources"]["S2"], "Acme", "SWE", "new-grad"))
    a.start()
    assert a_in_flight.wait(5)
    b.start()
    a.join()
    b.join()
    by_url = {json.load(open(os.path.join(facts.CACHE_DIR, f)))["url"]: json.load(open(os.path.join(facts.CACHE_DIR, f)))["model"]
              for f in os.listdir(facts.CACHE_DIR)}
    assert by_url == {URLS[0]: "groq/openai/gpt-oss-20b", URLS[1]: "openai/gpt-5.6-luna"}
    assert llm.get_settings("extract") == ("openai", "gpt-5.6-luna")  # what the old after-the-call check would have used for A
