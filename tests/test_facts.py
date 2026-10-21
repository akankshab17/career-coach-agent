import json
import os
import re

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
    return {"fact": fact, "exact_quote": quote, "section": section, "is_experience": False, "source_id": "S1"}


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


def test_is_experience_comes_from_the_source_domain_not_the_llm():
    c = {**cand("The loop has 4 rounds.", "new-grad loop has 4 rounds"), "is_experience": False}
    grounded, _ = facts.ground([c], sources(kind="forum"))
    assert grounded[0]["is_experience"] is True


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


# ---- model routing + cache key ----

def test_extraction_uses_the_extract_task_and_model_is_part_of_the_cache_key(monkeypatch):
    tasks = []
    calls = []
    monkeypatch.setattr(llm, "generate", helpers.fake_llm({"https://example.com/1": [cand("f", "q")]}, "", calls, tasks))
    src = sources()["S1"]

    facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    assert tasks == ["extract"]

    facts.extract_facts(src, "Stripe", "SWE", "new-grad")  # same model: cached
    assert len(calls) == 1

    monkeypatch.setenv("EXTRACT_MODEL", "openai/gpt-oss-120b")  # different model: its own entry, same prompt version
    _, cached = facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    assert cached is False and len(calls) == 2
    assert facts.PROMPT_VERSION == "extract-v5"  # a model change doesn't bump the prompt version

    monkeypatch.delenv("EXTRACT_MODEL")  # back to the default model: first entry is still there
    _, cached = facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    assert cached is True and len(calls) == 2

    saved = {json.load(open(os.path.join(facts.CACHE_DIR, f)))["model"] for f in os.listdir(facts.CACHE_DIR)}
    assert saved == {"groq/openai/gpt-oss-20b", "groq/openai/gpt-oss-120b"}


# ---- wrong subject ----

EXTERN_PAGE = ("Extern's virtual internship program gives you honest feedback on every answer. "
               "The Cummins interview includes a HireVue recorded behavioral round. Cummins is based in Columbus.")


def extern_sources(kind="other"):
    return {"S1": {"url": "https://www.extern.com/post/cummins-internship-guide", "title": "Cummins Internship Guide",
                   "full_text": EXTERN_PAGE, "kind": kind, "published_date": None}}


def test_facts_about_the_publishing_site_are_rejected_as_wrong_subject():
    own_practice = cand("Extern's virtual internship program gives honest feedback on every answer.",
                        "Extern's virtual internship program gives you honest feedback on every answer")
    about_cummins = cand("The Cummins interview includes a HireVue recorded behavioral round.",
                         "The Cummins interview includes a HireVue recorded behavioral round")
    grounded, rejected = facts.ground([own_practice, about_cummins], extern_sources(), company="Cummins")
    assert [g["fact"] for g in grounded] == [about_cummins["fact"]]
    assert rejected[0]["reason"] == "wrong-subject" and "Extern" in rejected[0]["detail"] or "extern" in rejected[0]["detail"]
    assert "wrong-subject" in facts.REJECT_REASONS


def test_extractor_can_flag_a_fact_as_not_about_the_company():
    flagged = {**cand("The process provides honest feedback on every answer.", "gives you honest feedback on every answer"),
               "about_company": False}
    unflagged = cand("The process provides honest feedback on every answer.", "gives you honest feedback on every answer")
    assert facts.ground([flagged], extern_sources(), company="Cummins")[1][0]["reason"] == "wrong-subject"
    assert len(facts.ground([unflagged], extern_sources(), company="Cummins")[0]) == 1  # field missing: not rejected
    explicit_yes = {**unflagged, "about_company": True}
    assert len(facts.ground([explicit_yes], extern_sources(), company="Cummins")[0]) == 1


def test_the_companys_own_site_is_never_a_wrong_subject():
    src = {"S1": {"url": "https://www.cummins.com/careers", "title": "Careers", "kind": "company", "published_date": None,
                  "full_text": "Cummins careers: Cummins builds engines and hires interns every summer."}}
    c = cand("Cummins hires interns every summer.", "Cummins builds engines and hires interns every summer", "company")
    assert len(facts.ground([c], src, company="Cummins")[0]) == 1


def test_publisher_label():
    assert facts.publisher_label("https://www.extern.com/post/x") == "extern"
    assert facts.publisher_label("https://prep.co.uk/guide") == "prep"
    assert facts.publisher_label("https://interviewing.io/google") == "interviewing"


def test_extraction_prompt_asks_for_level_and_subject():
    assert '"level": "intern|new-grad|experienced|unknown"' in facts.EXTRACT_SYSTEM_PROMPT
    assert "about_company" in facts.EXTRACT_SYSTEM_PROMPT and "website or service that published the page" in facts.EXTRACT_SYSTEM_PROMPT
    assert "is_forum" not in facts.EXTRACT_SYSTEM_PROMPT
    assert facts.PROMPT_VERSION == "extract-v5"


# ---- level tag ----

def test_interview_facts_carry_the_level_their_source_describes():
    new_grad = {**cand("The onsite has 4 rounds.", "new-grad loop has 4 rounds"), "level": "new-grad"}
    bogus = {**cand("A phone screen came first.", "a phone screen, then an onsite"), "level": "senior-staff-wizard"}
    company = {**cand("Stripe was valued at $91.5 billion.", "Stripe was valued at $91.5 billion", "company"), "level": "experienced"}
    grounded, _ = facts.ground([new_grad, bogus, company], sources(published="2026-01-01"))
    assert [(g["section"], g["level"]) for g in grounded] == [("interview", "new-grad"), ("interview", "unknown"), ("company", "")]


# ---- who is "reporting": first-person sources only ----

def test_candidate_reports_are_interview_facts_from_forums_and_blogs_not_job_reposts():
    text = "The loop has 4 rounds. You will build APIs for merchants. Stripe builds payments software."
    interview = cand("The loop has 4 rounds.", "The loop has 4 rounds")
    repost = cand("You will build APIs for merchants.", "You will build APIs for merchants", "role")
    news = cand("Stripe builds payments software.", "Stripe builds payments software", "company")
    cases = {"forum": [True, False, False], "blog": [True, False, False], "other": [False, False, False], "reputable": [False, False, False]}
    for kind, expected in cases.items():
        grounded, _ = facts.ground([interview, repost, news], sources(kind=kind, text=text, published="2026-01-01"))
        assert [g["is_experience"] for g in grounded] == expected, kind


# ---- time-sensitive numbers ----

@pytest.mark.parametrize("fact_text", [
    "The company is a $33.7 billion, ~67,400-employee power-systems manufacturer.",
    "Revenue was $7 million last year.",
    "It was bought for $1.1 billion.",
    "It employs about 8,000 people.",
    "Stripe has 8,000 employees.",
    "The group has a 250-person staff of engineers and 12,000 workers.",
])
def test_bare_money_amounts_and_headcounts_need_a_dated_source(fact_text):
    assert facts.TIME_SENSITIVE_RE.search(fact_text)
    c = cand(fact_text, fact_text.rstrip("."), "company")
    text = fact_text
    assert facts.ground([c], sources(text=text, published=None))[1][0]["reason"] == "undated-time-sensitive"
    assert len(facts.ground([c], sources(text=text, published="2026-03-01"))[0]) == 1


@pytest.mark.parametrize("fact_text", [
    "Three people interviewed me, 2 people on the panel.",
    "Startups get up to US$350,000 in cloud credits.",
    "More than 10 million businesses build on Stripe.",
    "The role pays $120,000 plus equity.",
    "An employee referral helps.",
])
def test_ordinary_numbers_are_not_time_sensitive(fact_text):
    assert not facts.TIME_SENSITIVE_RE.search(fact_text)


def test_reasoning_effort_is_not_part_of_the_cache_key(monkeypatch):
    calls = _counting_llm(monkeypatch)
    src = sources()["S1"]
    facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    monkeypatch.setenv("EXTRACT_REASONING_EFFORT", "high")
    _, cached = facts.extract_facts(src, "Stripe", "SWE", "new-grad")
    assert cached is True and len(calls) == 1 and len(os.listdir(facts.CACHE_DIR)) == 1
    monkeypatch.setenv("WRITE_REASONING_EFFORT", "low")
    assert facts.extract_facts(src, "Stripe", "SWE", "new-grad")[1] is True


# ---- the cache follows the text the extractor read ----

def test_cache_entry_is_not_reused_when_the_page_text_changes(monkeypatch):
    """The Salesforce bug: the extractor read only a site menu and returned []; that empty result stuck in the cache."""
    outputs = iter(['{"facts": []}', '{"facts": [{"fact": "f", "exact_quote": "q one two", "section": "role"}]}'])
    calls = []
    monkeypatch.setattr(llm, "generate", lambda s, u, task="write": calls.append(1) or next(outputs))
    url = "https://www.example.com/careers/new-grads"
    menu_only = {"url": url, "full_text": "Products Sales Service Pricing " * 20}
    content = {"url": url, "full_text": "New graduate jobs: a two-year rotational program. " * 20}

    assert facts.extract_facts(menu_only, "Acme", "APM", "new-grad") == ([], False)
    assert facts.extract_facts(menu_only, "Acme", "APM", "new-grad") == ([], True)  # same text: cached
    got, cached = facts.extract_facts(content, "Acme", "APM", "new-grad")  # new text: asked again
    assert cached is False and len(calls) == 2 and got[0]["fact"] == "f"


# ---- timeline ----

def test_timeline_is_a_fact_section_and_the_extractor_is_told_what_it_is():
    assert "timeline" in facts.SECTIONS
    assert '"section": "company|interview|role|timeline"' in facts.EXTRACT_SYSTEM_PROMPT
    assert "Dates and deadlines are \"timeline\", never \"interview\"" in facts.EXTRACT_SYSTEM_PROMPT
    assert "careers and program pages" in facts.EXTRACT_SYSTEM_PROMPT


def test_timeline_facts_are_grounded_without_stage_or_level():
    page = "Applications open in early July. Deadlines close within 2-4 weeks. The loop has 4 rounds."
    c = {**cand("Applications open in early July.", "Applications open in early July", "timeline"), "stage": "onsite", "level": "intern"}
    grounded, _ = facts.ground([c], sources(text=page))
    assert grounded[0]["section"] == "timeline" and grounded[0]["stage"] == "" and grounded[0]["level"] == ""
    assert grounded[0]["is_experience"] is False


def test_grounded_facts_carry_their_source_type_and_whether_it_is_official():
    src = sources(text="You will build APIs for merchants every day.")
    src["S1"].update({"type": "official-posting", "official": True})
    grounded, _ = facts.ground([cand("You will build APIs.", "You will build APIs for merchants", "role")], src)
    assert grounded[0]["source_type"] == "official-posting" and grounded[0]["official"] is True
    plain, _ = facts.ground([cand("You will build APIs.", "You will build APIs for merchants", "role")],
                            sources(text="You will build APIs for merchants every day."))
    assert plain[0]["source_type"] == "" and plain[0]["official"] is False


# ---- the extractor is told what the page is for ----

def test_the_extractor_is_told_the_pages_purpose_and_it_is_part_of_the_cache_key(monkeypatch):
    """Cummins/Google: told only the role, the extractor returned [] for company profiles and careers pages."""
    prompts = []

    def generate(system_prompt, user_prompt, task="write"):
        prompts.append(user_prompt)
        return '{"facts": []}'

    monkeypatch.setattr(llm, "generate", generate)
    page = {"url": "https://acme.com/about", "full_text": "Acme was founded in 1919.", "kind": "company"}
    facts.extract_facts({**page, "group": "company"}, "Acme", "SWE", "new-grad")
    facts.extract_facts({**page, "group": "careers", "official": True}, "Acme", "SWE", "new-grad")  # same text, other purpose
    facts.extract_facts({"url": "jd://user-provided", "full_text": "JD", "group": "jd"}, "Acme", "SWE", "new-grad")
    assert [re.search(r"^Page purpose: (\w+)$", p, re.M).group(1) for p in prompts] == ["company", "role", "role"]
    assert '"Page purpose"' in facts.EXTRACT_SYSTEM_PROMPT and "even if it never mentions the role" in facts.EXTRACT_SYSTEM_PROMPT


def test_facts_from_the_fallback_model_are_cached_under_that_model_not_groqs(monkeypatch):
    from test_llm import TPD, RateLimitError

    calls = []

    def groq(model, s, u, effort=None, **_):
        calls.append("groq")
        raise RateLimitError(TPD)

    def openai(model, s, u, effort=None, **_):
        calls.append("openai")
        return '{"facts": []}', 10, 10

    monkeypatch.setitem(llm._PROVIDERS, "groq", groq)
    monkeypatch.setitem(llm._PROVIDERS, "openai", openai)
    monkeypatch.setenv("FALLBACK_PROVIDER", "openai")
    monkeypatch.setenv("FALLBACK_EXTRACT_MODEL", "gpt-5.6-luna")
    src = {"url": "https://acme.com/about", "full_text": "Acme was founded in 1919.", "group": "company"}
    assert facts.extract_facts(src, "Acme", "SWE", "new-grad") == ([], False)
    files = os.listdir(facts.CACHE_DIR)
    assert len(files) == 1 and json.load(open(os.path.join(facts.CACHE_DIR, files[0])))["model"] == "openai/gpt-5.6-luna"
    assert facts.extract_facts(src, "Acme", "SWE", "new-grad") == ([], True)  # same run, still on the fallback: cached
    llm.reset_fallback()
    monkeypatch.setitem(llm._PROVIDERS, "groq", lambda model, s, u, effort=None, **_: calls.append("groq") or ('{"facts": []}', 1, 1))
    assert facts.extract_facts(src, "Acme", "SWE", "new-grad") == ([], False)  # back on Groq: the fallback's entry isn't reused
    assert calls == ["groq", "openai", "groq"]
