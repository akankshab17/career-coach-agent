import json
import os

import pytest

import facts
import helpers
import llm

PAGE = (
    "Stripe’s new-grad loop has 4 rounds — a phone screen, then an onsite.\n\n"
    "Interviewers   asked a **string-parsing** problem in the first round. Stripe was valued at $91.5 billion."
)


def sources(published=None, kind="other", text=PAGE, title="Some page"):
    return {"S1": {"url": "https://example.com/1", "title": title, "full_text": text, "kind": kind, "published_date": published}}


def cand(fact, quote, section="interview"):
    return {"fact": fact, "exact_quote": quote, "section": section, "is_forum": False, "source_id": "S1"}


# ---- grounding ----

def test_normalize_ignores_case_whitespace_punctuation_quotes_and_markdown():
    assert facts.normalize("Stripe’s  loop — has\n4 **rounds**.") == facts.normalize("stripe's loop - has 4 rounds")


def test_quote_must_appear_in_source_text():
    ok = cand("The first round included a string-parsing problem.",
              "Interviewers asked a string-parsing problem in the first round")  # spacing/markdown differ: still matches
    invented = cand("The loop has a system design round.", "The onsite includes a system design round")
    grounded, rejected = facts.ground([ok, invented], sources())
    assert [g["fact"] for g in grounded] == [ok["fact"]]
    assert [(r["fact"], r["reason"]) for r in rejected] == [(invented["fact"], "quote-not-found")]


def test_numbers_in_fact_must_be_in_its_quote():
    good = cand("The loop has 4 rounds.", "new-grad loop has 4 rounds")
    padded = cand("The loop has 4 rounds of 45 minutes.", "new-grad loop has 4 rounds")
    grounded, rejected = facts.ground([good, padded], sources())
    assert len(grounded) == 1 and rejected[0]["reason"] == "number-not-in-quote"


def test_time_sensitive_fact_needs_a_dated_source():
    c = cand("Stripe was valued at $91.5 billion.", "Stripe was valued at $91.5 billion", "company")
    assert facts.ground([c], sources(published=None))[1][0]["reason"] == "undated-time-sensitive"
    assert len(facts.ground([c], sources(published="2026-02-01"))[0]) == 1


def test_short_malformed_and_duplicate_candidates_rejected():
    good = cand("The loop has 4 rounds.", "new-grad loop has 4 rounds")
    candidates = [
        cand("Four rounds.", "4 rounds"),  # too short to be meaningful evidence
        {"fact": "x", "source_id": "S1"},  # no quote/section
        cand("Bad section.", "new-grad loop has 4 rounds", section="culture"),
        good,
        {**good},  # same fact twice
    ]
    grounded, rejected = facts.ground(candidates, sources())
    assert len(grounded) == 1
    assert [r["reason"] for r in rejected] == ["quote-too-short", "malformed", "malformed", "duplicate"]

    # facts grounded in an earlier round count as duplicates too
    assert facts.ground([good], sources(), already=grounded)[1][0]["reason"] == "duplicate"


def test_is_forum_comes_from_the_source_domain_not_the_llm():
    c = {**cand("The loop has 4 rounds.", "new-grad loop has 4 rounds"), "is_forum": False}
    grounded, _ = facts.ground([c], sources(kind="forum"))
    assert grounded[0]["is_forum"] is True


# ---- parsing ----

def test_parse_facts_tolerates_prose_and_rejects_garbage():
    out = 'Here you go:\n```json\n{"facts": [{"fact": "a", "exact_quote": "b", "section": "role"}, "junk"]}\n```'
    assert facts.parse_facts(out) == [{"fact": "a", "exact_quote": "b", "section": "role"}]
    with pytest.raises(ValueError):
        facts.parse_facts("I could not find anything.")


# ---- cache ----

def _counting_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "generate", helpers.fake_llm({"https://example.com/1": [cand("f", "q")]}, "", calls))
    return calls


def test_extraction_is_cached_per_url_and_prompt_version(monkeypatch):
    calls = _counting_llm(monkeypatch)
    src = sources()["S1"]

    first, cached1 = facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    second, cached2 = facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    assert (cached1, cached2) == (False, True) and first == second
    assert len(calls) == 1

    files = os.listdir(facts.CACHE_DIR)
    with open(os.path.join(facts.CACHE_DIR, files[0])) as f:
        assert json.load(f)["prompt_version"] == facts.PROMPT_VERSION

    monkeypatch.setattr(facts, "PROMPT_VERSION", "extract-v999")  # prompt changed -> cache miss
    facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    assert len(calls) == 2


def test_no_cache_always_calls_the_llm(monkeypatch):
    calls = _counting_llm(monkeypatch)
    src = sources()["S1"]
    facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    _, cached = facts.extract_facts(src, "Stripe", "SWE", "new-grad", use_cache=False)
    assert cached is False and len(calls) == 2


def test_pasted_jds_with_the_same_pseudo_url_dont_share_a_cache_entry(monkeypatch):
    calls = _counting_llm(monkeypatch)
    facts.extract_facts({"url": "jd://user-provided", "full_text": "JD one"}, "Stripe", "SWE", "new-grad")
    facts.extract_facts({"url": "jd://user-provided", "full_text": "JD two"}, "Stripe", "SWE", "new-grad")
    assert len(calls) == 2


# Excerpts of the real pages (Tavily extract output) behind test6's three quote-not-found rejections.
GIGAZINE = ("Bloomberg reports that financial services platform **[Stripe](https://stripe.com/jp)** has signed an "
            "agreement to acquire AI gateway service **[OpenRouter](https://openrouter.ai/)** . The acquisition is expected")
STRIPE_JOB = ("More than 10 million [businesses](https://stripe.com/customers) build on Stripe, spanning the economic "
              "frontier—from solo founders to established enterprises—united by a practical focus on growth. "
              "We maintain some of the most [reliable APIs](https://status.stripe.com/) on the internet.")


@pytest.mark.parametrize("text, quote", [
    (GIGAZINE, "financial services platform Stripe has signed an agreement to acquire AI gateway service OpenRouter."),
    (STRIPE_JOB, "More than 10 million businesses build on Stripe, spanning the economic frontier—from solo founders "
                 "to established enterprises—united by a practical focus on growth."),
    (STRIPE_JOB, "We maintain some of the most reliable APIs on the internet."),
])
def test_quotes_across_markdown_links_now_ground(text, quote):
    grounded, rejected = facts.ground([cand("A fact.", quote, "company")], sources(text=text, published="2026-08-17"))
    assert grounded and not rejected


def test_ellipsis_and_dash_variants_normalize():
    assert facts.normalize("growth… fast–paced") == facts.normalize("growth... fast-paced")


def test_snippet_only_source_grounds_against_its_snippet():
    src = {"S1": {"url": "https://www.reddit.com/r/x/1", "content": "Had 3 rounds, all coding, one on string parsing.",
                  "kind": "forum", "published_date": None}}  # no full_text: Reddit couldn't be fetched
    ok = cand("There were 3 coding rounds.", "Had 3 rounds, all coding")
    bad = cand("There was a system design round.", "one round was system design")
    grounded, rejected = facts.ground([ok, bad], src)
    assert [(g["fact"], g["grounded_in"]) for g in grounded] == [("There were 3 coding rounds.", "snippet")]
    assert rejected[0]["reason"] == "quote-not-found"


def test_snippet_and_full_text_extractions_are_cached_separately(monkeypatch):
    calls = _counting_llm(monkeypatch)
    snippet_only = {"url": "https://example.com/1", "content": "snippet text"}
    facts.extract_facts(snippet_only, "Stripe", "SWE", "new-grad")
    facts.extract_facts({**snippet_only, "full_text": PAGE}, "Stripe", "SWE", "new-grad")
    assert len(calls) == 2


# ---- interview stage ----

def test_stage_is_kept_for_interview_facts_and_defaults_to_unknown():
    onsite = {**cand("The onsite has 4 rounds.", "new-grad loop has 4 rounds"), "stage": "onsite"}
    invented_stage = {**cand("A phone screen came first.", "a phone screen, then an onsite"), "stage": "final-boss"}
    company = {**cand("Stripe was valued at $91.5 billion.", "Stripe was valued at $91.5 billion", "company"), "stage": "onsite"}
    grounded, _ = facts.ground([onsite, invented_stage, company], sources(published="2026-01-01"))
    assert [(g["section"], g["stage"]) for g in grounded] == [("interview", "onsite"), ("interview", "unknown"), ("company", "")]


def test_extraction_prompt_asks_for_stage_and_cache_version_bumped():
    assert '"stage": "oa|phone-screen|team-screen|onsite|unknown"' in facts.EXTRACT_SYSTEM_PROMPT
    assert facts.PROMPT_VERSION != "extract-v1"  # old cached facts have no stage field


# ---- hedged sources ----

def test_hedged_title_makes_the_fact_keep_a_hedge():
    c = cand("Stripe has signed an agreement to acquire OpenRouter.", "has signed an agreement to acquire", "company")
    grounded, _ = facts.ground([c], sources(text="Stripe has signed an agreement to acquire OpenRouter.",
                                            title="Stripe may acquire AI gateway service provider OpenRouter"))
    assert grounded[0]["hedged"] is True
    assert grounded[0]["fact"] == "Reportedly, Stripe has signed an agreement to acquire OpenRouter."


def test_already_hedged_or_unhedged_titles_are_left_alone():
    text = "Stripe may buy OpenRouter. The loop has 4 rounds."
    already = cand("Stripe may buy OpenRouter.", "Stripe may buy OpenRouter", "company")
    g, _ = facts.ground([already], sources(text=text, title="Stripe may acquire OpenRouter"))
    assert g[0]["fact"] == "Stripe may buy OpenRouter." and g[0]["hedged"]

    plain = cand("The loop has 4 rounds.", "The loop has 4 rounds")
    for title in ("Stripe completes acquisition of Bridge", "May 2026 hiring recap", "Stripe New Grad Interview Experience"):
        g, _ = facts.ground([plain], sources(text=text, title=title))
        assert g[0]["hedged"] is False and g[0]["fact"] == "The loop has 4 rounds."


def test_add_hedge_lowercases_only_common_starters():
    assert facts.add_hedge("The deal is big.") == "Reportedly, the deal is big."
    assert facts.add_hedge("Stripe is buying X.") == "Reportedly, Stripe is buying X."
