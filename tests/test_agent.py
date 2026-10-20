import json
import re
from datetime import date

import brief
import llm
import main
import search

TODAY = date(2026, 10, 19)


def res(n, title=None, url=None, content=None, published=None):
    return {
        "title": title or f"T{n}",
        "url": url or f"https://example.com/{n}",
        "content": content or f"content {n}",
        "published_date": published,
    }


def fake_llm(writer_output, unsupported_marker="BAD", verifier_calls=None):
    """Fake llm.generate: the writer returns `writer_output`; the verifier flags claims containing the marker."""
    def generate(system_prompt, user_prompt):
        if "fact-checker" in system_prompt:
            if verifier_calls is not None:
                verifier_calls.append(user_prompt)
            claims = re.findall(r"^\[(\d+)\] (.*)$", user_prompt.split("Claims:")[1], re.M)
            return json.dumps({"unsupported": [int(n) for n, t in claims if unsupported_marker in t]})
        return writer_output
    return generate


# ---- level filter ----

def test_internal_is_not_intern_and_levels_classified():
    assert search.classify_level(res(1, title="Software Engineer, Internal Systems")) == "experienced"
    assert search.classify_level(res(2, title="Software Engineer Intern, Summer 2027")) == "intern"
    assert search.classify_level(res(3, title="Software Engineer, New Grad")) == "new-grad"
    assert search.classify_level(res(4, url="https://x.com/jobs/university-graduate-swe")) == "new-grad"


def test_pick_posting_keeps_one_posting_at_requested_level():
    postings = [
        res(1, "Software Engineer, New Grad", "https://stripe.com/jobs/1"),
        res(2, "Software Engineer Intern", "https://stripe.com/jobs/2"),
        res(3, "Backend Engineer, Core Technology", "https://stripe.com/jobs/3"),
        res(4, "Software Engineer, Money Movement", "https://stripe.com/jobs/4"),
    ]
    assert [r["url"] for r in search.pick_posting(postings, "Stripe", "Software Engineer", "intern")] == ["https://stripe.com/jobs/2"]
    assert [r["url"] for r in search.pick_posting(postings, "Stripe", "Software Engineer", "new-grad")] == ["https://stripe.com/jobs/1"]
    # two experienced postings -> exactly one comes back, the better title match
    picked = search.pick_posting(postings, "Stripe", "Software Engineer", "experienced")
    assert [r["url"] for r in picked] == ["https://stripe.com/jobs/4"]
    assert search.pick_posting(postings[:2], "Stripe", "Software Engineer", "experienced") == []


def test_posting_search_skipped_when_jd_given_and_reddit_queried(monkeypatch):
    queries = []
    monkeypatch.setattr(search, "search", lambda q, client=None, **kw: queries.append(q) or [])

    search.gather_research("Acme", "SRE", "new-grad", jd="We need an SRE", today=TODAY)
    assert not any("job posting" in q for q in queries)
    assert any(q.startswith("site:reddit.com") for q in queries)

    queries.clear()
    search.gather_research("Acme", "SRE", "new-grad", jd=None, today=TODAY)
    assert any("job posting" in q and "new grad" in q for q in queries)


# ---- recency + source ranking ----

def test_dates_parsed_stale_dropped_and_company_ranked_first():
    assert search._parse_date("Tue, 03 Feb 2026 12:00:00 GMT") == "2026-02-03"
    assert search._parse_date("2026-02-03T10:00:00Z") == "2026-02-03"
    assert search._parse_date(None) is None

    old, new, undated = res(1, published="2025-01-01"), res(2, published="2026-08-01"), res(3)
    assert search.drop_stale([old, new, undated], TODAY) == [new, undated]

    other = res(4, url="https://blog.example.com/x", published="2026-09-01")
    reuters = res(5, url="https://www.reuters.com/x", published="2026-01-01")
    own = res(6, url="https://stripe.com/newsroom", published="2025-12-01")
    assert search.rank([other, reuters, own], "Stripe") == [own, reuters, other]


# ---- verification ----

def make_sources():
    research = {
        "company": [res(1, url="https://stripe.com/about", content="Stripe was founded in 2009.", published="2026-02-03")],
        "interview": [res(2, url="https://www.reddit.com/r/x/1", content="The loop had 4 rounds.")],
        "posting": [],
    }
    return research, brief.collect_sources(research, "Stripe")


def test_verification_drops_unsupported_uncited_and_unattributed_forum_claims(monkeypatch):
    monkeypatch.setattr(brief.llm, "generate", fake_llm(""))
    research, sources = make_sources()
    body = (
        "## 1. Overview\n"
        "Stripe was founded in 2009 [S1]. BAD invented valuation of $1T [S1].\n"
        "An uncited claim about culture.\n"
        "## 2. Interview\n"
        "* The loop has 4 rounds [S2].\n"
        "* Candidates report the loop had 4 rounds [S2].\n"
        "* What would success look like here?\n"
    )
    out, stats = brief.verify_body(body, sources)
    assert "founded in 2009" in out
    assert "BAD" not in out and "uncited claim" not in out
    assert "The loop has 4 rounds" not in out  # forum claim stated as fact
    assert "Candidates report" in out
    assert "What would success look like" in out  # questions need no citation
    assert (stats.dropped_unsupported, stats.dropped_uncited, stats.dropped_forum) == (1, 1, 1)
    assert stats.verified


def test_verifier_failure_is_loud_not_silent(monkeypatch):
    research, sources = make_sources()
    monkeypatch.setattr(brief.llm, "generate", lambda s, u: "not json at all")
    out, stats = brief.verify_body("## 1. Overview\nStripe was founded in 2009 [S1].\n", sources)
    assert not stats.verified
    assert "[verify] FAILED" in stats.log_line()


def test_only_cited_sources_listed_and_numbered_by_first_use(monkeypatch):
    research = {
        "company": [res(1), res(2), res(3)],
        "interview": [],
        "posting": [],
    }
    writer = "## 1. A\nFact one [S3]. Fact two [S1][S9].\n"  # S2 never cited, S9 doesn't exist
    monkeypatch.setattr(brief.llm, "generate", fake_llm(writer))
    result = brief.generate_brief("Acme", "SRE", "experienced", None, research, today=TODAY)
    text = result.text
    assert "[1](https://example.com/3)" in text and "[2](https://example.com/1)" in text
    assert "S9" not in text
    sources_part = text.split("## Sources")[1]
    assert "https://example.com/3" in sources_part and "https://example.com/1" in sources_part
    assert "https://example.com/2" not in sources_part  # uncited source pruned


def test_empty_section_after_drops_gets_placeholder(monkeypatch):
    research = {"company": [res(1)], "interview": [], "posting": []}
    writer = "## 1. A\nBAD claim [S1].\n## 2. B\nGood fact [S1].\n"
    monkeypatch.setattr(brief.llm, "generate", fake_llm(writer))
    text = brief.generate_brief("Acme", "SRE", "experienced", None, research, today=TODAY).text
    assert "No supported information found." in text.split("## 2. B")[0]


# ---- prompts ----

def test_section3_label_and_posting_scope():
    research = {"company": [res(1)], "interview": [], "posting": []}
    sources = brief.collect_sources(research, "Acme")
    _, user = brief.build_prompts("Acme", "SRE", "intern", None, research, sources, TODAY)
    assert "Inferred, not from a JD" in user
    assert "Candidate level: intern" in user and "2026-10-19" in user

    research["posting"] = [res(9, url="https://acme.com/jobs/9")]
    sources = brief.collect_sources(research, "Acme")
    _, user = brief.build_prompts("Acme", "SRE", "intern", None, research, sources, TODAY)
    assert "Inferred" not in user
    assert "ONLY on the single job posting https://acme.com/jobs/9" in user

    sources = brief.collect_sources(research, "Acme", jd="my jd")
    _, user = brief.build_prompts("Acme", "SRE", "intern", "my jd", research, sources, TODAY)
    assert "job description source" in user


# ---- llm provider ----

def _fake_provider(name, calls):
    def fn(model, system_prompt, user_prompt):
        calls.append((name, model))
        return "text", 1000, 500
    return fn


def test_provider_switch(monkeypatch):
    calls = []
    monkeypatch.setitem(llm._PROVIDERS, "groq", _fake_provider("groq", calls))
    monkeypatch.setitem(llm._PROVIDERS, "openai", _fake_provider("openai", calls))
    llm.reset_usage()

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm.generate("s", "u")  # default -> groq
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    llm.generate("s", "u")
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    llm.generate("s", "u")

    assert calls == [("groq", "openai/gpt-oss-120b"), ("openai", "gpt-4o-mini"), ("openai", "custom-model")]
    assert "provider=openai model=custom-model calls=3 in=3000" in llm.summary_line()


def test_retries_then_clear_error(monkeypatch):
    class RateLimitError(Exception):
        pass

    attempts = []

    def flaky(model, s, u):
        attempts.append(1)
        raise RateLimitError("429")

    monkeypatch.setitem(llm._PROVIDERS, "groq", flaky)
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    try:
        llm.generate("s", "u")
        assert False, "should have raised"
    except llm.LLMError as exc:
        assert "after 3 attempts" in str(exc)
    assert len(attempts) == 3


# ---- CLI ----

def test_end_to_end_with_mocks(monkeypatch, capsys):
    seen = {}

    def fake_gather(company, role, level, jd):
        seen["level"] = level
        return {"company": [res(1)], "interview": [], "posting": []}

    monkeypatch.setattr(main, "gather_research", fake_gather)
    monkeypatch.setattr(brief.llm, "generate", fake_llm("## 1. A\nFact [S1].\n"))
    main.main(["--company", "Acme", "--role", "SRE", "--level", "intern"])
    captured = capsys.readouterr()
    assert seen["level"] == "intern"
    assert "Fact [1](https://example.com/1)" in captured.out
    assert "[verify]" in captured.err
