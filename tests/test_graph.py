from datetime import date

import pytest

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

    def fake_gather(company, role, level, jd, today=None):
        return {"company": [res(1, url=NEWS)], "interview": [res(2, url=REDDIT)], "posting": []}

    def fake_search_more(company, role, level, section, known_urls, filtered_out=None):
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

    def gather(company, role, level, jd, today=None):
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

    def gather(company, role, level, jd, today=None):
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

    def truncating(system, user):
        out = real(system, user)
        if "interview-prep briefs" in system:
            llm._truncated += 1  # what _chat does when finish_reason == "length"
        return out

    monkeypatch.setattr(llm, "generate", truncating)
    result = graph.run_pipeline("Acme", "Software Engineer", "new-grad", None, today=TODAY)
    assert result.stats.draft_truncated
    assert "(DRAFT WAS CUT OFF by the token limit)" in capsys.readouterr().err
    assert "Draft cut off by the token limit: YES" in result.stats.debug_report(result.sources)
