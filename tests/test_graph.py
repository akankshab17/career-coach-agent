from datetime import date

import pytest

import brief
import graph
import helpers
import llm
import main
import search
from helpers import res

TODAY = date(2026, 10, 19)
NEWS, REDDIT, POSTING = "https://news.example.com/a", "https://www.reddit.com/r/cs/1", "https://acme.com/careers/listing/1"

helpers_texts = {
    NEWS: "Acme builds payment software for online businesses. Acme acquired Widget Co in March 2026.",
    REDDIT: "The interview had three rounds. The first round was a coding screen on string parsing. Final round was a system design chat.",
    POSTING: "You will build APIs for merchants. You have a degree in computer science or equivalent experience.",
}

EXTRACTED = {
    NEWS: [{"fact": "Acme builds payment software.", "exact_quote": "Acme builds payment software for online businesses", "section": "company"}],
    REDDIT: [
        {"fact": "The interview had three rounds.", "exact_quote": "The interview had three rounds", "section": "interview"},
        {"fact": "Round one was a string parsing coding screen.", "exact_quote": "The first round was a coding screen on string parsing", "section": "interview"},
        {"fact": "There were five rounds.", "exact_quote": "There were five rounds in total", "section": "interview"},  # invented
    ],
    POSTING: [
        {"fact": "You will build APIs for merchants.", "exact_quote": "You will build APIs for merchants", "section": "role"},
        {"fact": "A CS degree or equivalent experience is required.", "exact_quote": "a degree in computer science or equivalent experience", "section": "role"},
    ],
}

# F1 company (NEWS), F2-F3 interview (REDDIT), F4-F5 role (POSTING, found on the retry)
WRITER = """## TL;DR
- The interview had three rounds [F2].
- Round one is a string parsing screen [F3].
- The role builds APIs for merchants [F4].
## 1. Company overview + recent news
Acme builds payment software [F1].
## 2. Interview process + common questions
The interview had three rounds [F2].
## 3. What the role really needs
- You will build APIs for merchants [F4].
- A CS degree or equivalent experience is required [F5].
## 4. How to prepare
1. Practice string parsing problems [F3].
2. Do 5 mock rounds [F2].
3. Review API design basics [F4].
## 5. Questions to ask the interviewer
1. Which merchant APIs does the team own [F4]?
2. What is the culture like?
"""


@pytest.fixture
def world(monkeypatch):
    """Mock search, Tavily extract and the LLM. Returns a dict recording what was called."""
    seen = {"search_more": [], "llm": []}
    helpers.TEXTS.update(helpers_texts)

    def fake_gather(company, role, level, jd, today=None, **kw):
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT)], "posting": []}

    def fake_search_more(company, role, level, section, known_urls, filtered_out=None, **kw):
        seen["search_more"].append(section)
        return [res(3, "Software Engineer, New Grad", POSTING)] if section == "role" else []

    monkeypatch.setattr(graph, "gather_research", fake_gather)
    monkeypatch.setattr(graph, "search_more", fake_search_more)
    monkeypatch.setattr(search, "extract", helpers.fake_extract)
    monkeypatch.setattr(llm, "generate", helpers.fake_llm(EXTRACTED, WRITER, seen["llm"]))
    return seen


def test_graph_retries_once_for_a_thin_section_then_writes(world, capsys):
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    err = capsys.readouterr().err

    assert world["search_more"] == ["role"]  # interview had 2 grounded facts, role had 0
    assert world["llm"] == [("extract", NEWS), ("extract", REDDIT), ("extract", POSTING), ("write", None)]

    # one line per step, in order, with the retry visible
    steps = [line.split("]")[0] + "]" for line in err.splitlines() if line.startswith("[")]
    assert steps == ["[search]", "[fetch]", "[extract]", "[ground]", "[coverage]", "[search_more]",
                     "[fetch]", "[extract]", "[ground]", "[coverage]", "[write]", "[finalize]", "[pipeline]"]
    assert "[ground] 3 grounded (0 from snippets), 1 rejected (1 quote-not-found)" in err
    assert "company=1 interview=2 role=0 -> too few for role -> searching once more" in err
    assert "company=1 interview=2 role=2 -> writing (retry used: yes)" in err
    assert "[pipeline] sources=3 with_text=3 extracted=6 grounded=5 rejected=1 retry=yes (role)" in err

    text = result.text
    assert "## 3. What the role really needs\n" in text  # posting found on retry -> not "Inferred"
    assert "Candidates report that the interview had three rounds [1](https://www.reddit.com/r/cs/1)." in text
    assert "5 mock rounds" not in text and "culture" not in text
    assert "2. Review API design basics [2](https://acme.com/careers/listing/1)." in text  # renumbered
    assert "1. Which merchant APIs does the team own [2](https://acme.com/careers/listing/1)?" in text
    assert "There were five rounds" not in text


def test_retry_happens_at_most_once(world, monkeypatch, capsys):
    monkeypatch.setattr(graph, "search_more", lambda *a, **k: world["search_more"].append(a[3]) or [])
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    err = capsys.readouterr().err
    assert world["search_more"] == ["role"]  # still short after the retry, but no second loop
    assert "role=0 -> writing (retry used: yes)" in err
    assert "[write]" in err


def test_cli_end_to_end_writes_brief_debug_file_and_respects_no_cache(world, tmp_path, capsys):
    out_file = tmp_path / "brief.md"
    argv = ["--company", "Acme", "--role", "Software Engineer", "-o", str(out_file)]

    main.main(argv)
    extract_calls = lambda: sum(1 for kind, _ in world["llm"] if kind == "extract")  # noqa: E731
    assert extract_calls() == 3

    main.main(argv)  # cached: no new extraction calls
    assert extract_calls() == 3

    main.main(argv + ["--no-cache"])
    assert extract_calls() == 6

    assert "Acme builds payment software [3](https://news.example.com/a)."  # TL;DR cites Reddit + posting first in out_file.read_text()
    debug = (tmp_path / "brief.debug.md").read_text()
    assert "[quote-not-found]" in debug and "There were five rounds" in debug
    assert "> What is the culture like?" in debug
    err = capsys.readouterr().err
    assert "2 from cache, 0 via LLM" in err and "1 from cache, 0 via LLM" in err  # second run: before + after retry
    assert "[debug] details in" in err


def test_unfetchable_reddit_post_is_grounded_from_its_snippet(world, monkeypatch, capsys):
    del helpers.TEXTS[REDDIT]  # Tavily can't extract Reddit
    snippet = helpers_texts[REDDIT]

    def gather(company, role, level, jd, today=None, **kw):
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT, content=snippet)], "posting": []}

    monkeypatch.setattr(graph, "gather_research", gather)
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    err = capsys.readouterr().err
    assert "(1 snippet-only;" in err
    assert "[ground] 3 grounded (2 from snippets), 1 rejected" in err
    assert "Candidates report that the interview had three rounds" in result.text
    debug = result.stats.debug_report(result.sources)
    assert "[snippet-grounded] The interview had three rounds." in debug
    assert f"| S2 {REDDIT} | snippet |" in debug


def test_pipeline_and_finalize_lines_appear_once(world, capsys):
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    err = capsys.readouterr().err
    debug = result.stats.debug_report(result.sources)
    for text in (err, debug):
        assert text.count("[pipeline]") == 1
        assert text.count("[finalize]") == 1


def test_filtered_sources_are_logged_and_listed_in_the_debug_file(world, monkeypatch, capsys):
    off_topic = {"url": "https://www.reddit.com/r/FPGA/1", "title": "Google Silicon Engineering Intern interview",
                 "reason": "other-role-family (wanted software)"}

    def gather(company, role, level, jd, today=None, **kw):
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT)], "posting": [], "filtered": [off_topic]}

    monkeypatch.setattr(graph, "gather_research", gather)
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    err = capsys.readouterr().err
    assert "filtered 1 irrelevant interview results (1 other-role-family)" in err
    assert len(result.sources) == 3  # the filtered note is not turned into a source
    debug = result.stats.debug_report(result.sources)
    assert "# Filtered interview sources (1)" in debug and "Silicon Engineering Intern" in debug
    assert "https://www.reddit.com/r/FPGA/1" not in result.text


def test_debug_file_keeps_the_raw_draft_and_says_whether_it_was_cut_off(world):
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    debug = result.stats.debug_report(result.sources)
    assert "# Writer draft (raw, before checking)" in debug and "Draft cut off by the token limit: no" in debug
    assert "- You will build APIs for merchants [F4]." in debug


def test_cut_off_draft_is_reported(world, monkeypatch, capsys):
    real = llm.generate

    def truncating(system, user, **kw):
        out = real(system, user, **kw)
        if "interview-prep briefs" in system:
            llm._truncated += 1  # what _chat does when finish_reason == "length"
        return out

    monkeypatch.setattr(llm, "generate", truncating)
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert result.stats.draft_truncated
    assert "(DRAFT WAS CUT OFF by the token limit)" in capsys.readouterr().err
    assert "Draft cut off by the token limit: YES" in result.stats.debug_report(result.sources)


# ---- stopping early ----

def test_writer_uses_the_write_task_and_extraction_the_extract_task(world, monkeypatch):
    tasks = []
    monkeypatch.setattr(llm, "generate", helpers.fake_llm(EXTRACTED, WRITER, tasks=tasks))
    graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert tasks == ["extract", "extract", "extract", "write"]


def test_daily_limit_mid_extraction_stops_the_run_immediately(world, monkeypatch, capsys, tmp_path):
    extracted = []

    def generate(system, user, task="write"):
        if task == "extract":
            extracted.append(task)
            if len(extracted) == 2:
                raise llm.QuotaExceededError("groq daily limit reached on openai/gpt-oss-20b. Retrying won't help: try again in 8m 35s.")
            return '{"facts": []}'
        pytest.fail("must not write")

    monkeypatch.setattr(llm, "generate", generate)
    out_file = tmp_path / "brief.md"
    with pytest.raises(SystemExit) as stop:
        main.main(["--company", "Acme", "--role", "Software Engineer", "-o", str(out_file)])
    assert "Run stopped: groq daily limit reached" in str(stop.value) and "8m 35s" in str(stop.value)
    assert len(extracted) == 2  # the third source was never tried
    assert world["search_more"] == []  # no follow-up search either
    assert not out_file.exists()
    debug = (tmp_path / "brief.debug.md").read_text()
    assert "[search]" in debug and "[fetch]" in debug and "# Run stopped" in debug and "8m 35s" in debug
    err = capsys.readouterr().err
    assert "[llm]" in err and "partial run report" in err


def test_all_extractions_failing_stops_before_search_more_and_write(world, monkeypatch):
    calls = []

    def generate(system, user, task="write"):
        calls.append(task)
        if task == "extract":
            return "sorry, I cannot produce JSON"
        pytest.fail("must not write")

    monkeypatch.setattr(llm, "generate", generate)
    stats = brief.RunStats()
    with pytest.raises(graph.PipelineError) as err:
        graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY, stats=stats)
    assert "Every fact extraction failed (2 of 2 sources)" in str(err.value) and "did not return" in str(err.value)
    assert isinstance(err.value, llm.LLMError)
    assert calls == ["extract", "extract"] and world["search_more"] == []
    assert len(stats.extract_errors) == 2 and stats.steps[-1].startswith("[extract]")  # the log survives for debugging


def test_all_extractions_failing_in_the_retry_round_still_writes_from_earlier_facts(world, monkeypatch):
    base = helpers.fake_llm(EXTRACTED, WRITER)

    def generate(system, user, task="write"):
        if task == "extract" and POSTING in user:
            raise llm.LLMError("boom")  # only the retry round's source fails
        return base(system, user, task)

    monkeypatch.setattr(llm, "generate", generate)
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert world["search_more"] == ["role"] and "Acme builds payment software" in result.text
    assert len(result.stats.extract_errors) == 1


def test_a_bad_key_stops_extraction_after_one_call(world, monkeypatch):
    calls = []

    def generate(system, user, task="write"):
        calls.append(task)
        raise llm.FatalLLMError("GROQ_API_KEY is not set. Add it to your .env file.")

    monkeypatch.setattr(llm, "generate", generate)
    with pytest.raises(llm.FatalLLMError):
        graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert calls == ["extract"]


# ---- location ----

def research_with_posting(page_text, seen=None):
    def gather(company, role, level, jd, today=None, **kw):
        if seen is not None:
            seen.update(kw)
        posting = res(3, "Software Engineer, New Grad", POSTING, full_text=page_text)
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT)], "posting": [posting]}
    return gather


def test_the_posting_location_always_appears_first_in_section_3(world, monkeypatch):
    page = helpers_texts[POSTING] + "\nLocation: Wuhan, China\n"
    monkeypatch.setattr(graph, "gather_research", research_with_posting(page))
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)  # default location: United States
    section3 = result.text.split("## 3. ")[1].split("## 4.")[0]
    lines = [l for l in section3.splitlines()[1:] if l.strip()]
    assert lines[0].startswith("Posting location: Wuhan, China (outside United States)")  # the writer never mentioned it
    assert f"({POSTING})" in lines[0]  # cited to the posting
    assert "You will build APIs for merchants" in section3
    debug = result.stats.debug_report(result.sources)
    assert "Posting location: Wuhan, China (outside United States)." in debug


def test_a_posting_in_the_wanted_location_is_shown_without_a_warning(world, monkeypatch):
    page = helpers_texts[POSTING] + "\nLocations: New York, Seattle, South San Francisco\n"
    monkeypatch.setattr(graph, "gather_research", research_with_posting(page))
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert "Posting location: New York, Seattle, South San Francisco" in result.text and "outside" not in result.text


def test_no_location_fact_when_the_page_names_no_place(world, monkeypatch):
    monkeypatch.setattr(graph, "gather_research", research_with_posting(helpers_texts[POSTING]))
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert "Posting location" not in result.text


def test_the_location_reaches_the_searches_and_the_cli_flag_sets_it(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(graph, "gather_research", research_with_posting(helpers_texts[POSTING], seen))
    graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert seen["location"] == "United States"

    seen.clear()
    graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY, location="India")
    assert seen["location"] == "India"


def test_cli_location_flag(world, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(graph, "gather_research", research_with_posting(helpers_texts[POSTING], seen))
    out = tmp_path / "b.md"
    main.main(["--company", "Acme", "--role", "Software Engineer", "-o", str(out)])
    assert seen["location"] == "United States"
    main.main(["--company", "Acme", "--role", "Software Engineer", "--location", "India", "-o", str(out)])
    assert seen["location"] == "India"


def test_search_more_gets_the_location(world, monkeypatch):
    got = {}

    def search_more(company, role, level, section, known_urls, filtered_out=None, location=None):
        got["location"] = location
        return []

    monkeypatch.setattr(graph, "search_more", search_more)
    graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY, location="Canada")
    assert got["location"] == "Canada"


# ---- subject + level flow through the whole pipeline ----

def test_wrong_subject_facts_are_rejected_and_counted_in_the_ground_line(world, monkeypatch, capsys):
    extern = "https://www.extern.com/post/acme-guide"
    helpers.TEXTS[extern] = "Extern's virtual internship program gives honest feedback. Acme interviews include a coding screen."
    extracted = {**EXTRACTED, extern: [
        {"fact": "Extern's virtual program gives honest feedback.", "exact_quote": "Extern's virtual internship program gives honest feedback",
         "section": "interview"},
        {"fact": "Acme interviews include a coding screen.", "exact_quote": "Acme interviews include a coding screen", "section": "interview"},
    ]}
    monkeypatch.setattr(llm, "generate", helpers.fake_llm(extracted, WRITER))

    def gather(company, role, level, jd, today=None, **kw):
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT), res(4, url=extern)], "posting": []}

    monkeypatch.setattr(graph, "gather_research", gather)
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert "1 wrong-subject" in capsys.readouterr().err
    debug = result.stats.debug_report(result.sources)
    assert "[wrong-subject]" in debug and "describes extern's own offering" in debug


# ---- careers pages, timelines and source types through the whole pipeline ----

CAREERS = "https://www.acme.com/company/careers/university/new-grads"
AGGREGATOR = "https://jobright.ai/jobs/associate-product-manager-acme"
MENU_PAGE = "".join(f"## Product {n}\n\nShort blurb about product {n}.\n\n" for n in range(300))
CAREERS_CONTENT = (
    "# New graduate jobs at Acme.\n\nLooking to launch your career at Acme? You are in the right place.\n\n"
    "## Associate Product Manager Program\n\nThis two-year rotational program for new graduates gives you ownership of "
    "products, mentorship from senior leaders, and exposure to the APM community. Eligibility: a degree by 2027.\n\n"
    "## What you will do\n\nYou will own a defined area of a product, run customer discovery interviews, partner with engineers "
    "and designers, and present your product strategy to senior leaders every quarter during the program.\n\n"
    "## Ready to apply?\n\nApplications open in early July. Submit your application promptly."
)
CAREERS_EXTRACTED = {
    CAREERS: [
        {"fact": "The APM program is a two-year rotational program for new graduates.",
         "exact_quote": "This two-year rotational program for new graduates gives you ownership of products", "section": "role"},
        {"fact": "Applications open in early July.", "exact_quote": "Applications open in early July", "section": "timeline"},
    ],
    NEWS: EXTRACTED[NEWS],
    REDDIT: EXTRACTED[REDDIT],
}


def test_a_careers_page_with_a_long_menu_yields_facts_and_timeline_stays_out_of_the_tldr(world, monkeypatch, capsys):
    helpers.TEXTS[CAREERS] = MENU_PAGE + CAREERS_CONTENT
    assert (MENU_PAGE + CAREERS_CONTENT).find("two-year rotational") > search.FULL_TEXT_CHARS  # the old head-cap kept only menu
    writer = """## TL;DR
- Round one is a string parsing screen [F4].
- Applications open in early July [F2].
## 1. Company overview + recent news
Acme builds payment software [F3].
## 2. Interview process + common questions
**Stage not specified**
- The interview had three rounds [F4].
**Timeline**
- Applications open in early July [F2].
## 3. What the role really needs
- The APM program is a two-year rotational program for new graduates [F1].
## 4. How to prepare
1. Practice string parsing [F5].
2. Plan your application for early July [F2].
## 5. Questions to ask the interviewer
1. How does the rotation work for new graduates [F1]?
"""
    monkeypatch.setattr(llm, "generate", helpers.fake_llm(CAREERS_EXTRACTED, writer))

    def gather(company, role, level, jd, today=None, **kw):
        return {"careers": [res(9, url=CAREERS, title="New grads")], "company": [res(1, url=NEWS)],
                "interview": [res(2, url=REDDIT)], "posting": []}

    monkeypatch.setattr(graph, "gather_research", gather)
    result = graph.run_pipeline("Acme", "Associate Product Manager", "new-grad", None, today=TODAY)
    text = result.text

    assert result.sources["S1"]["type"] == "company-site" and result.sources["S1"]["official"]
    section = lambda n: text.split(f"## {n}")[1].split("\n## ")[0]  # noqa: E731
    assert "two-year rotational program" in section("3.")
    assert "No official posting text found" not in text  # the careers page is official text
    assert "**Timeline**\n- Applications open in early July" in section("2.")
    assert "July" not in section("TL;DR") and "July" not in section("4.")
    codes = [d.code for d in result.stats.drops]
    assert "tldr-timeline" in codes and "derived-cites-timeline" in codes
    debug = result.stats.debug_report(result.sources)
    assert "(role, S1 [company-site])" in debug and "(timeline, S1 [company-site])" in debug


def test_without_any_official_text_section_3_says_so_and_labels_its_facts(world, monkeypatch):
    helpers.TEXTS[AGGREGATOR] = "Associate Product Manager. You will own a defined area of a product and run customer discovery. " * 8
    extracted = {**CAREERS_EXTRACTED, AGGREGATOR: [
        {"fact": "You will own a defined area of a product.", "exact_quote": "You will own a defined area of a product", "section": "role"}]}
    writer = "## 3. What the role really needs\n- You will own a defined area of a product [F4].\n"
    monkeypatch.setattr(llm, "generate", helpers.fake_llm(extracted, writer))

    def gather(company, role, level, jd, today=None, **kw):
        posting = res(5, "APM at Acme", AGGREGATOR, full_text=helpers.TEXTS[AGGREGATOR])
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT)], "posting": [posting]}

    monkeypatch.setattr(graph, "gather_research", gather)
    monkeypatch.setattr(graph, "search_more", lambda *a, **k: [])  # the retry finds nothing better
    result = graph.run_pipeline("Acme", "Associate Product Manager", "new-grad", None, today=TODAY)
    s3 = result.text.split("## 3. ")[1].split("\n## ")[0]
    assert s3.startswith("What the role really needs (Inferred, not from a JD)")
    assert "No official posting text found; based on job aggregator." in s3
    assert "You will own a defined area of a product (job aggregator)" in s3
