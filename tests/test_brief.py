from collections import Counter
from datetime import date

import pytest

import brief
from brief import RunStats
from helpers import fact, res

TODAY = date(2026, 10, 19)

SOURCES = {
    "S1": {**res(1, url="https://stripe.com/about"), "kind": "company", "group": "company", "full_text": "t"},
    "S2": {**res(2, url="https://www.reddit.com/r/x/2"), "kind": "forum", "group": "interview", "full_text": "t"},
    "S3": {**res(3, url="https://www.reddit.com/r/x/3"), "kind": "forum", "group": "interview", "full_text": "t"},
    "S4": {**res(4, url="https://www.reuters.com/x", published="2026-02-24"), "kind": "reputable", "group": "company", "full_text": "t"},
}
FACTS = [
    fact("Stripe builds payments infrastructure.", "Stripe builds payments infrastructure", "company", "S1"),
    fact("The loop has 4 rounds.", "the loop has 4 rounds", "interview", "S2", is_experience=True),
    fact("String parsing questions are common.", "string parsing questions come up a lot", "interview", "S3", is_experience=True),
    fact("Stripe was valued at $159 billion.", "valued at $159 billion", "company", "S4", published="2026-02-24"),
]


def check(body):
    stats = RunStats()
    return brief.check_body(body, FACTS, stats), stats


# ---- writer prompt ----

def test_writer_prompt_lists_only_grounded_facts_by_section():
    _, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", FACTS, SOURCES, TODAY)
    company_block = user.split("### company facts")[1].split("### interview facts")[0]
    assert "[F1]" in company_block and "[F4]" in company_block and "published 2026-02-24" in company_block
    assert "[F2] (source S2, CANDIDATE-REPORT)" not in user and "[F2] (source S2, stage unknown, CANDIDATE-REPORT)" in user
    assert "### role facts\n(none)" in user
    assert "Inferred, not from a JD" in user  # no posting/JD source with text
    tldr = user.split("## TL;DR")[1].split("## 1.")[0]
    assert "only 2 interview fact(s)" in tldr and "do NOT write one" in tldr  # FACTS has just 2 interview facts

    three = FACTS + [fact("A third interview fact.", "a third interview fact", "interview", "S3")]
    _, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", three, SOURCES, TODAY)
    tldr = user.split("## TL;DR")[1].split("## 1.")[0]
    assert "ONLY from interview facts" in tldr and "no company news" in tldr

    role_only = [fact("You will build APIs.", "you will build APIs for merchants", "role", "S1")]
    _, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", role_only, SOURCES, TODAY)
    tldr = user.split("## TL;DR")[1].split("## 1.")[0]
    assert "NO interview facts" in tldr and "Never describe interview stages" in tldr

    with_posting = {**SOURCES, "S5": {**res(5), "kind": "company", "group": "posting", "full_text": "jd text",
                                      "type": "official-posting", "official": True}}
    _, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", FACTS, with_posting, TODAY)
    assert "Inferred" not in user


# ---- finalize checks ----

def test_uncited_unknown_and_number_padded_sentences_are_dropped():
    body = (
        "## 1. Company overview + recent news\n"
        "Stripe builds payments infrastructure [F1]. Stripe is the best company. Made up [F99].\n"
        "Stripe was valued at $159 billion (reported Feb 2026) [F4]. Stripe has 9000 staff [F1].\n"
    )
    out, stats = check(body)
    assert "payments infrastructure [F1]" in out and "$159 billion (reported Feb 2026) [F4]" in out
    assert "best company" not in out and "Made up" not in out and "9000" not in out
    assert [(d.code, d.detail) for d in stats.drops] == [("uncited", ""), ("unknown-fact", "F99"), ("number-not-in-facts", "9000")]


def test_forum_facts_rewritten_except_in_derived_sections():
    body = (
        "## 2. Interview process + common questions\n"
        "- The loop has 4 rounds [F2].\n"
        "- Candidates report string parsing is common [F3].\n"
        "## 4. How to prepare\n"
        "1. Practice string parsing under time pressure [F3].\n"
    )
    out, stats = check(body)
    assert "- Candidates report that the loop has 4 rounds [F2]." in out
    assert "- Candidates report string parsing is common [F3]." in out  # already attributed
    assert "1. Practice string parsing under time pressure [F3]." in out  # advice: not rewritten
    assert len(stats.rewrites) == 1


def test_questions_must_cite_and_lists_are_renumbered_after_drops():
    body = (
        "## 4. How to prepare\n"
        "1. Practice string parsing [F3].\n"
        "2. Do 6 mock interviews [F2].\n"
        "3. Rehearse a 4-round day [F2].\n"
        "## 5. Questions to ask the interviewer\n"
        "1. What's the culture like?\n"
        "2. How does the team use payments infrastructure in production [F1]?\n"
    )
    role = fact("Interns own small features.", "interns own small features", "role", "S1")
    stats = RunStats()
    out = brief.check_body(body.replace("[F1]?", "[F5]?"), FACTS + [role], stats)
    assert "1. Practice string parsing [F3]." in out
    assert "2. Rehearse a 4-round day [F2]." in out  # was 3.
    assert "culture" not in out
    assert "1. How does the team use payments infrastructure in production [F5]?" in out  # was 2.
    assert [d.code for d in stats.drops] == ["number-not-in-facts", "uncited"]


HEADINGS = ["## TL;DR", "## 1. Company overview + recent news", "## 2. Interview process + common questions",
            "## 3. What the role really needs", "## 4. How to prepare", "## 5. Questions to ask the interviewer"]


@pytest.mark.parametrize("body", [
    "",  # writer returned nothing
    "## TL;DR\n- The loop has 4 rounds [F2].\n## 4. How to prepare\n1. Prepare stories that show you",  # cut off
    "## 5. Questions to ask\n1. Q [F1]?\n## TL;DR\n- The loop has 4 rounds [F2].",  # wrong order, odd heading text
    "## TLDR\n- Uncited.\n## 1. Company\nStripe builds payments infrastructure [F1].\n## Extra notes\nignored [F1].",
])
def test_all_six_headings_always_render_in_order(body):
    out, _ = check(body)
    positions = [out.find(h + "\n") for h in HEADINGS]
    assert all(p >= 0 for p in positions), out
    assert positions == sorted(positions)
    assert out.count("\n## ") + out.startswith("## ") == 6  # nothing else at heading level


def test_full_brief_output_has_all_six_headings_even_when_writer_stops_early():
    stats = RunStats()
    truncated = "## TL;DR\n- The loop has 4 rounds [F2].\n## 1. Company overview + recent news\nStripe builds"
    text = brief.finalize_brief("Stripe", "SWE", "new-grad", truncated, FACTS, SOURCES, stats).text
    for h in HEADINGS[:3] + ["## 3. What the role really needs (Inferred, not from a JD)"] + HEADINGS[4:]:
        assert h + "\n" in text
    assert text.count(brief.PLACEHOLDER) == 5  # sections 1-5: the cut-off "Stripe builds" is uncited -> dropped
    assert "No official posting text found.\n" + brief.PLACEHOLDER in text  # SOURCES here has no official posting


def test_tldr_interview_claims_need_interview_facts():
    role_fact = fact("Stripe wants people who move with urgency.", "move with urgency", "role", "S1")
    facts = FACTS + [role_fact]  # F5
    body = (
        "## TL;DR\n"
        "- Interview stages assess your urgency [F5].\n"  # posting marketing dressed up as interview info
        "- The loop has 4 rounds [F2].\n"
        "- Stripe wants people who move with urgency [F5].\n"
    )
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert "assess your urgency" not in out
    assert "4 rounds" in out and "move with urgency [F5]" in out
    assert [d.code for d in stats.drops] == ["tldr-interview-without-interview-fact"]

    # no interview facts at all -> say so instead of inferring
    stats = RunStats()
    out = brief.check_body(body.replace("- The loop has 4 rounds [F2].\n", ""), [FACTS[0], role_fact], stats)
    tldr = out.split("## TL;DR\n")[1].split("\n\n## ")[0]
    assert tldr.startswith("- Limited interview data (0 sources).\n- No interview reports found.")
    assert "assess" not in tldr


def test_company_section_capped_at_five_sentences():
    body = "## 1. Company overview + recent news\n" + "\n".join(f"Stripe builds payments infrastructure {n} [F1]." for n in range(7))
    stats = RunStats()
    many = [fact("Stripe builds payments infrastructure 0 1 2 3 4 5 6.", "q 0 1 2 3 4 5 6", "company", "S1")]
    out = brief.check_body(body, many, stats)
    assert out.count("Stripe builds payments infrastructure") == 5
    assert [d.code for d in stats.drops] == ["company-section-cap"] * 2


def test_select_for_writing_caps_company_facts_per_article_and_total():
    company = [fact(f"News {n}.", f"news {n} q", "company", f"S{n % 4}") for n in range(16)]
    interview = [fact("I.", "i q", "interview", "S9")]
    selected = brief.select_for_writing(company + interview)
    sel_company = [f for f in selected if f["section"] == "company"]
    assert len(sel_company) == brief.COMPANY_FACTS_FOR_WRITER
    assert max(sum(f["source_id"] == sid for f in sel_company) for sid in ("S0", "S1", "S2", "S3")) <= brief.COMPANY_FACTS_PER_SOURCE
    assert interview[0] in selected


# ---- rendering ----

def test_citations_become_one_link_per_source_numbered_by_first_use():
    two_from_s2 = FACTS + [fact("Rounds are an hour.", "each round is an hour", "interview", "S2", is_experience=True)]
    body = "Stripe builds payments [F1]. Loop facts [F2][F5][F3]."
    out, cited = brief.render(body, two_from_s2, SOURCES)
    assert out == ("Stripe builds payments [1](https://stripe.com/about). Loop facts "
                   "[2](https://www.reddit.com/r/x/2)[3](https://www.reddit.com/r/x/3).")
    assert [s["url"] for s in cited] == ["https://stripe.com/about", "https://www.reddit.com/r/x/2", "https://www.reddit.com/r/x/3"]


def test_finalize_prunes_sources_and_reports_counts():
    # 4 facts from 3 distinct sources; only 1 of the 4 sources has full text (the rest are snippets)
    stats = RunStats(facts_grounded=4, sources_with_text=1, grounded=FACTS[:3] + [FACTS[0]], rejected=[{"reason": "quote-not-found"}])
    body = "## 1. Company overview + recent news\nStripe builds payments infrastructure [F1]. Uncited.\n"
    result = brief.finalize_brief("Stripe", "SWE", "new-grad", body, FACTS, SOURCES, stats)
    sources_part = result.text.split("## Sources")[1]
    assert "https://stripe.com/about" in sources_part and "reddit" not in sources_part
    assert "_Grounding: 4 facts grounded from 3 sources (1 rejected). Writing: 2 sentences checked, 1 dropped (1 uncited)._" in result.text


def test_debug_report_lists_rejected_facts_drops_and_rewrites():
    stats = RunStats(steps=["[search] 4 sources"], rejected=[
        {"fact": "Five rounds.", "exact_quote": "there were five rounds", "section": "interview", "source_id": "S2",
         "reason": "quote-not-found"},
    ])
    brief.check_body("## 2. Interview\n- The loop has 4 rounds [F2]. Uncited claim.\n", FACTS, stats)
    report = stats.debug_report(SOURCES)
    assert "- `[search] 4 sources`" in report
    assert "[pipeline]" not in report and "[finalize]" not in report  # only what the steps logged, once
    assert "[quote-not-found] exact_quote does not appear in the source text" in report
    assert 'Quote: "there were five rounds"' in report and "Source: https://www.reddit.com/r/x/2" in report
    assert "> Uncited claim." in report
    assert "After: Candidates report that the loop has 4 rounds [F2]." in report


# ---- interview stages ----

OA = fact("The online assessment is one coding question.", "the OA is one coding question", "interview", "S2", is_experience=True, stage="oa")
ONSITE = fact("The onsite has four rounds.", "the onsite has four rounds", "interview", "S3", is_experience=True, stage="onsite")
GENERAL = fact("Clarify requirements before coding.", "clarify requirements before coding", "interview", "S3", is_experience=True)
STAGE_FACTS = [OA, ONSITE, GENERAL]  # F1, F2, F3


def test_writer_prompt_groups_interview_facts_by_stage_in_order():
    shuffled = [GENERAL, ONSITE, OA]
    _, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", shuffled, SOURCES, TODAY)
    interview = user.split("### interview facts")[1].split("### role facts")[0]
    assert interview.index("stage oa") < interview.index("stage onsite") < interview.index("stage unknown")
    assert '"Online assessment", "Phone screen", "Team screen", "Onsite", "Stage not specified"' in user
    assert "group by stage" in user and "THAT stage only" in user
    # one bullet per stage in the TL;DR, no mixing
    tldr = user.split("## TL;DR")[1].split("## 1.")[0]
    assert "FORMAT" in tldr and "KEY TOPICS" in tldr and "STANDOUT INSIGHT" in tldr and "never mixes stages" in tldr


def test_section2_sentence_mixing_stages_is_split_into_one_sentence_per_fact():
    body = "## 2. Interview process + common questions\n- The OA has one coding question and the onsite has four rounds [F1][F2].\n"
    stats = RunStats()
    out = brief.check_body(body, STAGE_FACTS, stats)
    assert "The OA has one coding question and the onsite" not in out
    assert "Candidates report that the online assessment is one coding question [F1]." in out
    assert "Candidates report that the onsite has four rounds [F2]." in out
    assert [r.kind for r in stats.rewrites].count("stage-split") == 1
    assert stats.checked == 1 and not stats.drops


def test_unknown_stage_facts_can_share_a_sentence_with_one_stage():
    body = "## 2. Interview process + common questions\n- Clarify requirements, then code the onsite problem [F2][F3].\n"
    stats = RunStats()
    out = brief.check_body(body, STAGE_FACTS, stats)
    assert "[F2][F3]" in out and not any(r.kind == "stage-split" for r in stats.rewrites)


def test_tldr_bullet_mixing_stages_is_trimmed_to_its_first_fact():
    body = "## TL;DR\n- Expect an OA then a four-round onsite [F1][F2].\n- Clarify requirements before coding [F3].\n"
    stats = RunStats()
    out = brief.check_body(body, STAGE_FACTS, stats)
    tldr = out.split("## TL;DR\n")[1].split("\n\n## 1.")[0]
    assert "onsite" not in tldr and "online assessment is one coding question" in tldr
    assert len([l for l in tldr.splitlines() if l.startswith("- ")]) == 2
    assert [r.kind for r in stats.rewrites if r.kind == "stage-trim"] == ["stage-trim"]


def test_bold_stage_labels_are_not_dropped_as_uncited():
    body = "## 2. Interview process + common questions\n**Online assessment**\n- The OA is one coding question [F1].\n"
    out = brief.check_body(body, STAGE_FACTS, RunStats())
    assert "**Online assessment**\n- Candidates report that the OA is one coding question [F1]." in out


# ---- hedges ----

def test_hedged_facts_keep_their_hedge_in_the_brief():
    facts = [
        fact("Stripe may acquire OpenRouter for over $7 billion.", "may acquire OpenRouter for over $7 billion", "company", "S4", hedged=True),
        fact("Stripe builds payments infrastructure.", "Stripe builds payments infrastructure", "company", "S1"),
    ]
    body = (
        "## 1. Company overview + recent news\n"
        "Stripe acquired OpenRouter for over $7 billion (reported Aug 2026) [F1].\n"  # 'reported' label is not a hedge
        "Stripe may acquire OpenRouter [F1].\n"
        "Stripe builds payments infrastructure [F2].\n"
    )
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert "Reportedly, Stripe acquired OpenRouter for over $7 billion (reported Aug 2026) [F1]." in out
    assert "\nStripe may acquire OpenRouter [F1]." in out  # already hedged: untouched
    assert "\nStripe builds payments infrastructure [F2]." in out  # unhedged source: untouched
    assert [r.kind for r in stats.rewrites] == ["hedge-added"]


def test_writer_prompt_marks_hedged_facts():
    facts = [fact("Stripe may acquire OpenRouter.", "may acquire OpenRouter", "company", "S4", hedged=True)]
    system, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", facts, SOURCES, TODAY)
    assert "(source S4, HEDGED)" in user and "Keep the hedge" in system


# ---- questions to ask ----

def test_questions_never_about_logistics_rumors_or_interview_facts():
    facts = [
        fact("You will build APIs for merchants.", "you will build APIs for merchants", "role", "S1"),
        fact("Stripe may acquire OpenRouter.", "may acquire OpenRouter", "company", "S4", hedged=True),
        fact("The onsite has four rounds.", "the onsite has four rounds", "interview", "S3", stage="onsite"),
    ]  # F1 role, F2 hedged company news, F3 interview
    body = (
        "## 5. Questions to ask the interviewer\n"
        "1. What kinds of merchant APIs would a new grad own in their first year [F1]?\n"
        "2. How will the OpenRouter acquisition change the team [F2]?\n"
        "3. How many rounds should I expect in the onsite [F3]?\n"
        "4. What is the interview timeline from here?\n"
        "5. How does the team support mentorship and growth [F1]?\n"
    )
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    questions = out.split("## 5. Questions to ask the interviewer\n")[1]
    assert "1. What kinds of merchant APIs" in questions and "2. How does the team support mentorship" in questions
    assert "OpenRouter" not in questions and "rounds" not in questions and "timeline" not in questions
    assert [d.code for d in stats.drops] == ["question-logistics-or-rumor"] * 3


# ---- citation formats ----

@pytest.mark.parametrize("written, canonical", [
    ("Fact [F3].", "Fact [F3]."),
    ("Fact 【F12】.", "Fact [F12]."),  # what the Google eval's writer produced
    ("Fact 【F10】【F14】【F19】.", "Fact [F10][F14][F19]."),
    ("Fact [F1, F2].", "Fact [F1][F2]."),
    ("Fact [F1,F2].", "Fact [F1][F2]."),
    ("Fact [F1; F3].", "Fact [F1][F3]."),
    ("Fact [F1 and F3].", "Fact [F1][F3]."),
    ("Fact [F1 F3].", "Fact [F1][F3]."),
    ("Fact [F1-F3].", "Fact [F1][F2][F3]."),
    ("Fact [F1–F3].", "Fact [F1][F2][F3]."),  # en dash
    ("Fact [F2-4].", "Fact [F2][F3][F4]."),
    ("Fact [F1, F3-F5].", "Fact [F1][F3][F4][F5]."),
    ("Fact (F2).", "Fact [F2]."),
    ("Fact （F2）.", "Fact [F2]."),  # full-width parentheses
    ("Fact (F1, F2).", "Fact [F1][F2]."),
    ("Fact [f2].", "Fact [F2]."),
    ("Fact [ F2 ].", "Fact [F2]."),
    ("Fact F2.", "Fact [F2]."),  # bare id at the end of the sentence
    ("Fact F1, F3.", "Fact [F1][F3]."),
    ("- Fact F2", "- Fact [F2]"),  # bare id at end of line, no period
    ("Fact [F1][F2].", "Fact [F1][F2]."),  # already canonical: unchanged
])
def test_every_citation_variant_is_normalized(written, canonical):
    assert brief.normalize_citations(written, n_facts=10) == canonical


@pytest.mark.parametrize("text", [
    "A Formula F1 car is fast.",  # bare F1 mid-sentence is not a citation
    "The F1 visa lets students work.",
    "Section F2 covers it, and more.",
    "Fact F99.",  # bare id beyond the number of facts
    "Call (555) 123-4567.",
    "See (Appendix F).",
])
def test_ordinary_text_is_left_alone(text):
    assert brief.normalize_citations(text, n_facts=10) == text


def test_out_of_range_bracketed_ids_stay_citations_so_they_are_reported_as_unknown():
    assert brief.normalize_citations("Fact 【F99】.", n_facts=10) == "Fact [F99]."
    stats = RunStats()
    out = brief.check_body("## 1. Company overview + recent news\nMade up 【F99】.\n", FACTS, stats)
    assert [(d.code, d.detail) for d in stats.drops] == [("unknown-fact", "F99")]


def test_the_google_eval_draft_style_is_no_longer_dropped_as_uncited():
    body = (
        "## 2. Interview process + common questions\n"
        "- Online assessments are included in the process【F1】.\n"
        "- Phone interviews are technical (feedback goes to the hiring committee)【F2】【F3】.\n"
        "## 5. Questions to ask the interviewer\n"
        "1. What mentorship is available for interns working with Google's full-stack AI tools?【F5】\n"
    )
    facts = [
        fact("Online assessments are included in the interview process.", "online assessments", "interview", "S2", stage="oa"),
        fact("Technical phone interviews feed the hiring committee.", "phone interviews", "interview", "S3", stage="phone-screen"),
        fact("Phone interview is a common stage.", "phone interview is common", "interview", "S3", stage="phone-screen"),
        fact("x", "x y z", "company", "S1"),
        fact("Interns work with Google's full-stack AI tools.", "full-stack AI", "role", "S1"),
    ]
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert not [d for d in stats.drops if d.code == "uncited"]
    assert "[F1]" in out and "[F2][F3]" in out and "[F5]" in out
    text, cited = brief.render(out, facts, SOURCES)
    assert "【" not in text and "](https://www.reddit.com/r/x/2)" in text


# ---- capping facts for the writer ----

def _many(section, n_sources, per_source, start_sid=1, **kw):
    return [fact(f"{section} fact {s}-{i}.", f"{section} quote {s} {i}", section, f"S{start_sid + s}", **kw)
            for s in range(n_sources) for i in range(per_source)]


def test_writer_gets_at_most_30_facts_posting_and_interview_first_company_reserved():
    company = _many("company", 4, 8, start_sid=1)  # 32 company facts
    interview = _many("interview", 6, 6, start_sid=10)  # 36
    role = _many("role", 2, 11, start_sid=20)  # 22
    facts = company + interview + role
    chosen = brief.select_for_writing(facts)
    by_section = Counter(f["section"] for f in chosen)
    assert len(chosen) == brief.MAX_WRITER_FACTS == 30
    assert by_section["company"] == brief.COMPANY_RESERVE == 5
    assert by_section["interview"] + by_section["role"] == 25 and by_section["interview"] > by_section["role"]
    assert chosen == [f for f in facts if f in chosen]  # original order kept, so fact ids are stable
    # interleaved across sources: every interview source contributes before any contributes a 3rd fact
    per_source = Counter(f["source_id"] for f in chosen if f["section"] == "interview")
    assert len(per_source) == 6 and max(per_source.values()) - min(per_source.values()) <= 1


def test_small_fact_sets_are_not_trimmed_but_company_stays_capped():
    facts = _many("company", 4, 8) + _many("interview", 1, 2, start_sid=10) + _many("role", 1, 3, start_sid=20)
    chosen = brief.select_for_writing(facts)
    by_section = Counter(f["section"] for f in chosen)
    assert by_section["interview"] == 2 and by_section["role"] == 3  # everything kept
    assert by_section["company"] == brief.COMPANY_FACTS_FOR_WRITER  # 4 sources x 3 per source = 12, capped to 10
    assert max(Counter(f["source_id"] for f in chosen if f["section"] == "company").values()) <= brief.COMPANY_FACTS_PER_SOURCE


def test_unused_role_budget_flows_to_interview():
    facts = _many("interview", 5, 8, start_sid=10) + _many("company", 2, 3)
    chosen = brief.select_for_writing(facts)
    assert Counter(f["section"] for f in chosen) == {"interview": 25, "company": 5}


# ---- sentence splitting ----

@pytest.mark.parametrize("text, expected", [
    ("Candidates need a B.Tech or M.S. in CS from a U.S. university, e.g. IIT [F1]. Next sentence [F2].",
     ["Candidates need a B.Tech or M.S. in CS from a U.S. university, e.g. IIT [F1].", "Next sentence [F2]."]),
    ("Cummins Inc. is based in Indiana [F1]. It makes engines [F2].",
     ["Cummins Inc. is based in Indiana [F1].", "It makes engines [F2]."]),
    ("Open to Ph.D. students. Apply by May 1 [F1].", ["Open to Ph.D. students.", "Apply by May 1 [F1]."]),
    ("Dr. Smith leads the team [F1]. Expect a call.", ["Dr. Smith leads the team [F1].", "Expect a call."]),
    ("Use Python, Java, etc. in the loop [F1]. Then wait.", ["Use Python, Java, etc. in the loop [F1].", "Then wait."]),
    ("A $1.5 billion deal closed in v2.0 of the plan [F1]!", ["A $1.5 billion deal closed in v2.0 of the plan [F1]!"]),
    ("First one. Second one? Third one!", ["First one.", "Second one?", "Third one!"]),
    ("Interview with J. Smith went well [F3]. Offer came later [F4].", ["Interview with J. Smith went well [F3].", "Offer came later [F4]."]),
    ("No terminal punctuation [F1]", ["No terminal punctuation [F1]"]),
    ("Done. [F1] Then more [F2].", ["Done. [F1]", "Then more [F2]."]),  # a citation after the period closes the sentence
])
def test_sentences_are_not_split_inside_abbreviations(text, expected):
    assert brief.split_sentences(text) == expected


def test_abbreviations_in_a_real_sentence_stay_one_cited_sentence():
    facts = [fact("Candidates need a B.Tech or M.S. degree.", "a B.Tech or M.S. in CS", "role", "S1")]
    body = "## 3. What the role really needs\nCandidates need a B.Tech or M.S. in CS, e.g. from a U.S. school [F1].\n"
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert stats.checked == 1 and not stats.drops
    assert "B.Tech or M.S. in CS, e.g. from a U.S. school [F1]." in out


# ---- who is reporting ----

def test_candidates_report_only_for_first_person_interview_facts_not_job_reposts():
    facts = [
        fact("The loop has four rounds.", "the loop has four rounds", "interview", "S2", is_experience=True),
        fact("You will build APIs for merchants.", "build APIs for merchants", "role", "S2", is_experience=False),  # a repost on Reddit
    ]
    body = ("## 2. Interview process + common questions\n- The loop has four rounds [F1].\n"
            "## 3. What the role really needs\n- You will build APIs for merchants [F2].\n")
    out = brief.check_body(body, facts, RunStats())
    assert "Candidates report that the loop has four rounds [F1]." in out
    assert "\n- You will build APIs for merchants [F2]." in out  # not rewritten


def test_writer_prompt_tags_only_experience_facts_as_candidate_reports():
    facts = [fact("The loop has four rounds.", "q 1", "interview", "S2", is_experience=True),
             fact("You will build APIs.", "q 2", "role", "S2")]
    system, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", facts, SOURCES, TODAY)
    assert "[F1] (source S2, stage unknown, CANDIDATE-REPORT)" in user and "[F2] (source S2)" in user
    assert "first-person interview experiences" in system


# ---- interview-fact level ----

def mixed_levels():
    return [
        fact("New-grad loop has 4 rounds.", "q a", "interview", "S2", level="new-grad", stage="onsite"),
        fact("Full-time loop has 6 rounds.", "q b", "interview", "S3", level="experienced", stage="onsite"),
        fact("Tip: clarify requirements.", "q c", "interview", "S3", level="unknown"),
        fact("Intern loop has 2 rounds.", "q d", "interview", "S2", level="intern", stage="onsite"),
    ]


def test_mismatched_levels_are_labelled_matching_and_unknown_are_not():
    body = ("## 2. Interview process + common questions\n"
            "- The loop has 4 rounds [F1].\n- The loop has 6 rounds [F2].\n- Clarify requirements [F3].\n"
            "- The loop has 2 rounds. [F4]\n")
    stats = RunStats()
    out = brief.check_body(body, mixed_levels(), stats, level="new-grad")
    assert "- The loop has 4 rounds [F1]." in out and "Clarify requirements [F3]." in out and "(new-grad process)" not in out
    assert "- The loop has 6 rounds (full-time process) [F2]." in out
    assert "- The loop has 2 rounds (internship process). [F4]" in out  # label goes before the period/citations
    assert [r.kind for r in stats.rewrites] == ["level-label", "level-label"]


def test_a_label_the_writer_already_wrote_is_not_doubled_and_no_level_means_no_labels():
    body = "## 2. Interview process + common questions\n- The loop has 6 rounds (full-time process) [F2].\n"
    out = brief.check_body(body, mixed_levels(), RunStats(), level="new-grad")
    assert out.count("process)") == 1
    out = brief.check_body("## 2. Interview process + common questions\n- The loop has 6 rounds [F2].\n", mixed_levels(), RunStats())
    assert "process)" not in out


def test_sentence_mixing_two_other_levels_gets_a_generic_label():
    body = "## 2. Interview process + common questions\n- Loops run 2 to 6 rounds [F2][F4].\n"
    out = brief.check_body(body, mixed_levels(), RunStats(), level="new-grad")
    assert "(other-level processes)" in out


def test_writer_prompt_marks_level_mismatches_with_their_label():
    _, user = brief.build_writer_prompt("Cummins", "SWE", "new-grad", mixed_levels(), SOURCES, TODAY)
    assert 'level experienced, LEVEL-MISMATCH, label "(full-time process)"' in user
    assert 'level intern, LEVEL-MISMATCH, label "(internship process)"' in user
    assert "[F1] (source S2, stage onsite, level new-grad)" in user  # the matching one carries its level but no mismatch flag
    assert "[F3] (source S3, stage unknown)" in user  # unknown level: no tag


def test_level_matching_interview_facts_are_preferred_for_the_writer():
    matching = [fact(f"match {i}.", f"mq {i}", "interview", f"S{10 + i % 3}", level="new-grad") for i in range(12)]
    unknown = [fact(f"unknown {i}.", f"uq {i}", "interview", "S20", level="unknown") for i in range(4)]
    other = [fact(f"other {i}.", f"oq {i}", "interview", "S30", level="experienced") for i in range(20)]
    chosen = brief.select_for_writing(other + matching + unknown, limit=20, level="new-grad")
    interview = [f for f in chosen if f["section"] == "interview"]
    assert all(f in interview for f in matching + unknown)  # every fitting fact is in...
    assert sum(f["level"] == "experienced" for f in interview) == len(interview) - 16  # ...before any other-level fact fills in
    only_other = brief.select_for_writing(other[:5], limit=20, level="new-grad")
    assert len(only_other) == 5  # other-level facts are still used when nothing better exists (and then labelled)


# ---- limited interview data ----

def tl_dr(out):
    return out.split("## TL;DR\n")[1].split("\n\n## ")[0]


def test_tldr_warns_when_there_are_fewer_than_three_interview_facts():
    two = [fact("The loop has 4 rounds.", "q a", "interview", "S2"), fact("Clarify requirements.", "q b", "interview", "S3")]
    body = "## TL;DR\n- The loop has 4 rounds [F1].\n- Limited interview data (9 sources).\n"  # the writer's own line is replaced
    out = brief.check_body(body, two, RunStats())
    assert tl_dr(out) == "- Limited interview data (2 sources).\n- The loop has 4 rounds [F1]."
    assert tl_dr(out).count("Limited interview data") == 1

    one_source = [fact("A.", "q a", "interview", "S2"), fact("B.", "q b", "interview", "S2")]
    assert tl_dr(brief.check_body("## TL;DR\n- A [F1].\n", one_source, RunStats())).startswith("- Limited interview data (1 source).")

    none = tl_dr(brief.check_body("## TL;DR\n- No interview reports found.\n", [], RunStats()))
    assert none == "- Limited interview data (0 sources).\n- No interview reports found."

    three = [fact(f"Fact {i}.", f"q {i}", "interview", "S2") for i in range(3)]
    assert "Limited interview data" not in brief.check_body("## TL;DR\n- Fact 0 [F1].\n", three, RunStats())


# ---- "How to prepare" / "Questions" cite role or interview facts only ----

def test_prep_and_questions_cannot_cite_company_news():
    facts = [
        fact("Stripe acquired Bridge.", "acquired Bridge", "company", "S1", published="2026-02-24"),
        fact("You will build APIs for merchants.", "build APIs for merchants", "role", "S1"),
        fact("Clarify requirements before coding.", "clarify requirements", "interview", "S3"),
    ]
    body = (
        "## 4. How to prepare\n"
        "1. Read up on the Bridge acquisition [F1].\n"
        "2. Practice API design [F2].\n"
        "3. Practice clarifying requirements [F3].\n"
        "4. Connect APIs to the Bridge deal [F1][F2].\n"
        "## 5. Questions to ask the interviewer\n"
        "1. How did the Bridge acquisition change the roadmap [F1]?\n"
        "2. Which merchant APIs would I own [F2]?\n"
        "3. How does the team coach new engineers on clarifying requirements [F3]?\n"
    )
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert "Bridge" not in out.split("## 4.")[1]
    assert "1. Practice API design [F2]." in out and "2. Practice clarifying requirements [F3]." in out  # renumbered
    assert "1. Which merchant APIs would I own [F2]?" in out and "2. How does the team coach" in out  # interview facts are fine
    assert [d.code for d in stats.drops] == ["derived-cites-company-news"] * 3
    assert "derived-cites-company-news" in brief.DROP_REASONS


def test_writer_prompt_restricts_prep_and_questions_to_role_and_interview_facts():
    system, user = brief.build_writer_prompt("Stripe", "SWE", "new-grad", FACTS, SOURCES, TODAY)
    assert "must be ROLE or INTERVIEW facts. Never base them on company news" in system
    assert "each citing a ROLE or INTERVIEW fact; no company news" in user


# ---- section 3 when there is no official posting text ----

def srcs(**types):
    """Sources S1.. with the given type each (official ones get official=True)."""
    out = {}
    for n, (sid, t) in enumerate(types.items(), 1):
        out[sid.upper()] = {**res(n, url=f"https://example.com/{n}"), "kind": "other", "group": "posting", "type": t,
                            "official": t == "official-posting", "full_text": "x" * 600}
    return out


def role_facts(*types):
    return [fact(f"Role fact {n}.", f"role quote {n}", "role", f"S{n}") | {"source_type": t} for n, t in enumerate(types, 1)]


def test_section3_says_when_there_is_no_official_posting_and_labels_each_fact_by_source():
    facts = role_facts("third-party-guide", "job-aggregator")
    body = "## 3. What the role really needs\n- Role fact 1 [F1].\n- Role fact 2 [F2].\n- Both together [F1][F2].\n"
    sources = srcs(s1="third-party-guide", s2="job-aggregator")
    stats = RunStats()
    out = brief.check_body(body, facts, stats, sources)
    s3 = out.split("## 3. ")[1].split("## 4.")[0]
    lines = [l for l in s3.splitlines()[1:] if l.strip()]
    assert lines[0] == "No official posting text found; based on third-party guide and job aggregator."
    assert lines[1:] == ["- Role fact 1 (third-party guide) [F1].", "- Role fact 2 (job aggregator) [F2].",
                         "- Both together (third-party guide and job aggregator) [F1][F2]."]
    assert [r.kind for r in stats.rewrites] == ["source-label"] * 3
    assert "(Inferred, not from a JD)" in out.split("## 3. ")[1].split("\n")[0]


def test_no_note_or_labels_when_an_official_source_exists_or_a_jd_was_given():
    facts = role_facts("official-posting", "third-party-guide")
    body = "## 3. What the role really needs\n- Role fact 1 [F1].\n- Role fact 2 [F2].\n"
    out = brief.check_body(body, facts, RunStats(), srcs(s1="official-posting", s2="third-party-guide"))
    assert "No official posting text" not in out and "(third-party guide)" not in out and "## 3. What the role really needs\n" in out

    jd = brief.collect_sources({"company": []}, "Acme", jd="my pasted job description " * 40)
    assert jd["S1"]["type"] == "official-posting" and brief.has_role_document(jd)


def test_note_without_any_role_facts_keeps_the_placeholder():
    out = brief.check_body("## 3. What the role really needs\n", [], RunStats(), srcs(s1="job-aggregator"))
    assert "No official posting text found.\n" + brief.PLACEHOLDER in out


def test_a_careers_page_counts_as_official_text_for_section_3():
    sources = brief.collect_sources({"careers": [res(1, url="https://www.acme.com/company/careers/university/apm", full_text="x" * 600)]}, "Acme")
    assert sources["S1"]["type"] == "company-site" and sources["S1"]["official"] is True and brief.has_role_document(sources)
    news = brief.collect_sources({"company": [res(1, url="https://www.acme.com/news/q4", full_text="x" * 600)]}, "Acme")
    assert news["S1"]["official"] is False and not brief.has_role_document(news)
    guide = brief.collect_sources({"posting": [res(1, url="https://jobright.ai/jobs/apm", full_text="x" * 600)]}, "Acme")
    assert guide["S1"]["type"] == "job-aggregator" and not brief.has_role_document(guide)


def test_official_sources_are_preferred_when_the_writer_budget_is_tight():
    official = [fact(f"official {i}.", f"oq {i}", "role", "S1") | {"official": True} for i in range(4)]
    other = [fact(f"other {i}.", f"xq {i}", "role", f"S{2 + i % 3}") | {"official": False} for i in range(10)]
    chosen = brief.select_for_writing(other + official, limit=6)
    assert [f for f in chosen if f["official"]] == official  # every official fact is in...
    assert len([f for f in chosen if not f["official"]]) == 2  # ...before the others fill what is left

    interview = [fact(f"official iv {i}.", f"ivq {i}", "interview", "S1", level="new-grad") | {"official": True} for i in range(3)]
    forum = [fact(f"forum iv {i}.", f"fq {i}", "interview", "S5", level="new-grad") | {"official": False} for i in range(10)]
    chosen = brief.select_for_writing(forum + interview, limit=6)
    assert all(f in chosen for f in interview)


# ---- timeline ----

TL = fact("Applications open in early July.", "applications open in early July", "timeline", "S3") | {"source_type": "third-party-guide"}
IV = [fact(f"Interview fact {n}.", f"iq {n}", "interview", "S2", stage="onsite") for n in range(3)]
RL = fact("You will build APIs.", "build APIs", "role", "S1")


def test_timeline_facts_do_not_belong_in_the_tldr_or_in_prep_and_questions():
    facts = IV + [TL, RL]  # F1-F3 interview, F4 timeline, F5 role
    body = (
        "## TL;DR\n- Interview fact 0 [F1].\n- Applications open in July [F4].\n"
        "## 4. How to prepare\n1. Practice APIs [F5].\n2. Apply by July [F4].\n"
        "## 5. Questions to ask the interviewer\n1. Which APIs would I build [F5]?\n2. When do offers go out [F4]?\n"
    )
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert "July" not in out
    assert [d.code for d in stats.drops] == ["tldr-timeline", "derived-cites-timeline", "derived-cites-timeline"]
    assert "tldr-timeline" in brief.DROP_REASONS and "derived-cites-timeline" in brief.DROP_REASONS


def test_timeline_stays_in_section_2_under_its_own_sub_heading():
    facts = IV + [TL, RL]
    body = ("## 2. Interview process + common questions\n**Onsite**\n- Interview fact 0 [F1].\n"
            "**Timeline**\n- Applications open in early July [F4].\n")
    out = brief.check_body(body, facts, RunStats())
    s2 = out.split("## 2. ")[1].split("## 3.")[0]
    assert "**Timeline**\n- Applications open in early July [F4]." in s2 and "**Onsite**" in s2


def test_timeline_facts_do_not_count_as_interview_data():
    out = brief.check_body("## TL;DR\n- Role stuff [F2].\n", [TL, RL], RunStats())
    assert "Limited interview data (0 sources)." in out and "No interview reports found." in out


def test_section_2_with_only_a_timeline_still_says_no_interview_reports_were_found():
    out = brief.check_body("## 2. Interview process + common questions\n**Timeline**\n- Applications open in early July [F1].\n", [TL], RunStats())
    s2 = out.split("## 2. ")[1].split("## 3.")[0]
    assert s2.splitlines()[1] == "No interview reports found." and "**Timeline**" in s2


def test_writer_prompt_has_a_timeline_block_and_rules():
    system, user = brief.build_writer_prompt("Acme", "APM", "new-grad", IV + [TL, RL], SOURCES, TODAY)
    assert "### timeline facts\n[F4]" in user
    assert 'final bold label line "**Timeline**"' in user and "no timeline/deadline facts" in user
    assert "NOT interview content" in system and "never in the TL;DR" in system


def test_up_to_four_timeline_facts_are_always_sent_beyond_the_cap():
    tl = [fact(f"Date {n}.", f"tq {n}", "timeline", f"S{n % 2}") for n in range(7)]
    interview = [fact(f"i {n}.", f"iq {n}", "interview", f"S{10 + n % 5}") for n in range(40)]
    chosen = brief.select_for_writing(tl + interview)
    assert sum(f["section"] == "timeline" for f in chosen) == brief.TIMELINE_MAX == 4
    assert len(chosen) <= brief.MAX_WRITER_FACTS


# ---- years that come from a source's date ----

def test_a_year_is_fine_when_a_cited_source_is_dated_that_year_or_the_label_says_so():
    facts = [fact("Salesforce closed the last mile of AI integration.", "closed the last mile", "company", "S1", published="2026-02-25")]
    body = ("## 1. Company overview + recent news\n"
            "In early 2026 Salesforce closed the last mile of AI integration (reported Feb 2026) [F1].\n"
            "In 2025 Salesforce closed the last mile of AI integration [F1].\n"
            "Salesforce closed the last mile of AI integration by 2030 [F1].\n")
    stats = RunStats()
    out = brief.check_body(body, facts, stats)
    assert "In early 2026" in out and "(reported Feb 2026)" in out
    assert [(d.code, d.detail) for d in stats.drops] == [("number-not-in-facts", "2025"), ("number-not-in-facts", "2030")]


def test_the_year_allowance_needs_a_dated_source():
    facts = [fact("Salesforce closed the last mile.", "closed the last mile", "company", "S1")]  # undated
    stats = RunStats()
    brief.check_body("## 1. Company overview + recent news\nIn early 2026 Salesforce closed the last mile [F1].\n", facts, stats)
    assert [d.code for d in stats.drops] == ["number-not-in-facts"]


# ---- only company facts: general guidance instead of a collapsed brief ----

COMPANY_ONLY = [FACTS[0], FACTS[3]]
COMPANY_ONLY_DRAFT = ("## TL;DR\nNo grounded information found for this section.\n\n"
                      "## 1. Company overview + recent news\nStripe builds payments infrastructure [F1].\n\n"
                      "## 2. Interview process + common questions\nNo grounded information found for this section.\n\n"
                      "## 3. What the role really needs\nNo grounded information found for this section.\n\n"
                      "## 4. How to prepare\nNo grounded information found for this section.\n\n"
                      "## 5. Questions to ask the interviewer\nNo grounded information found for this section.\n")


def test_company_only_briefs_get_labelled_general_guidance_for_the_role_family():
    """Salesforce APM: only company facts survived, and 5 of 6 sections were placeholders."""
    stats = RunStats()
    text = brief.finalize_brief("Stripe", "Associate Product Manager", "new-grad", COMPANY_ONLY_DRAFT, COMPANY_ONLY, SOURCES, stats).text
    prep = text.split("## 4. How to prepare\n")[1].split("\n\n## 5.")[0]
    questions = text.split("## 5. Questions to ask the interviewer\n")[1].split("\n\n## Sources")[0]
    for section in (prep, questions):
        assert section.startswith("**General guidance (no company-specific data found)**, typical for product management roles:")
        assert section.count("\n- ") >= 3 and "[" not in section  # bullets, no citations: nothing pretends to come from a source
    assert "product-sense" in prep
    tldr = text.split("## TL;DR\n")[1].split("\n\n## 1.")[0]
    assert tldr.splitlines() == ["- Limited interview data (0 sources).", "- No interview reports found.",
                                 "- Only company information was found; sections 4 and 5 give general guidance for product management roles."]
    assert brief.PLACEHOLDER not in tldr
    assert "No grounded information found for this section." in text.split("## 2.")[1].split("## 3.")[0]  # 2 and 3 stay honest
    assert stats.general_sections == ["4. How to prepare", "5. Questions to ask the interviewer"]
    assert "# General guidance used (2)" in stats.debug_report()


def test_general_guidance_is_not_used_when_interview_or_role_facts_exist():
    stats = RunStats()
    text = brief.finalize_brief("Stripe", "Software Engineer", "new-grad", COMPANY_ONLY_DRAFT, FACTS, SOURCES, stats).text
    assert "General guidance" not in text and stats.general_sections == []
    assert "## 4. How to prepare\nNo grounded information found for this section." in text


def test_a_role_with_no_known_family_gets_generic_guidance():
    assert "typical for roles like this one" in brief.guidance.section_text("questions", "Chief Happiness Officer")
    assert "typical for software engineering roles" in brief.guidance.section_text("prep", "Backend Engineer")
