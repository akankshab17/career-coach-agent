"""The brief pipeline as a LangGraph graph: extract facts first, write second.

search -> fetch -> extract -> ground -> coverage --enough--> write -> finalize
            ^                              |
            +------ search_more <--too few-+   (at most once)
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import llm
from brief import (
    KEY_SECTIONS, BriefResult, RunStats, add_sources, build_writer_prompt, collect_sources, finalize_brief,
    select_for_writing,
)
from facts import extract_facts, ground, location_fact, source_text
from location import DEFAULT_LOCATION
from search import fetch_full_text, gather_research, search_more

MIN_FACTS = 2  # a key section with fewer grounded facts triggers one follow-up search


class PipelineError(llm.LLMError):
    """The run cannot produce a trustworthy brief; stop instead of writing from nothing."""


class BriefState(TypedDict, total=False):
    company: str
    role: str
    level: str
    jd: str | None
    today: date
    use_cache: bool
    location: str  # where the job should be, e.g. "United States"
    location_done: bool  # the posting's location fact has been added
    sources: dict[str, dict]  # S1, S2, ... -> search result (+ full_text, kind, group)
    extracted: list[str]  # source ids already sent through extraction
    candidates: list[dict]  # facts from the latest extract step, not yet grounded
    facts: list[dict]  # grounded facts so far
    selected: list[dict]  # the grounded facts given to the writer (F1, F2, ... in this order)
    lacking: list[str]  # key sections still under MIN_FACTS
    retry_used: bool
    body: str  # the writer's draft
    stats: RunStats
    result: BriefResult


def _log(stats: RunStats, line: str) -> None:
    stats.steps.append(line)
    print(line, file=sys.stderr)


def _filtered_note(filtered: list[dict]) -> str:
    if not filtered:
        return ""
    reasons = Counter(f["reason"].split(" (")[0] for f in filtered)
    return f"; filtered {len(filtered)} irrelevant interview results ({', '.join(f'{n} {r}' for r, n in reasons.items())})"


def _section_counts(facts: list[dict]) -> Counter:
    return Counter(f["section"] for f in facts)


# ---------- nodes: each takes the state and returns the keys it changes ----------

def search_node(state: BriefState) -> dict:
    stats = state["stats"]
    research = gather_research(state["company"], state["role"], state["level"], state["jd"], today=state["today"],
                               location=state["location"])
    sources = collect_sources(research, state["company"], state["jd"])
    stats.sources_total = len(sources)
    stats.filtered += research.get("filtered", [])
    groups = Counter(s["group"] for s in sources.values())
    _log(stats, f"[search] {len(sources)} sources ({', '.join(f'{g} {n}' for g, n in groups.items())})"
                + _filtered_note(stats.filtered))
    return {"sources": sources, "extracted": [], "facts": [], "retry_used": False, "location_done": False}


def fetch_node(state: BriefState) -> dict:
    stats, sources = state["stats"], state["sources"]
    fetched, failed = fetch_full_text(sources)
    stats.sources_with_text = sum(1 for s in sources.values() if s.get("full_text"))
    _log(stats, f"[fetch] {fetched} pages fetched, {failed} failed; {stats.sources_with_text}/{len(sources)} sources have full text")
    return {"sources": sources}


def _extractable(source: dict) -> bool:
    """Full page text, or a snippet from any non-company source (Reddit posts usually can't be fetched).

    Company sources outside the top FULL_TEXT_COMPANY were deliberately not fetched; skipping their
    snippets saves an LLM call each.
    """
    if source.get("full_text"):
        return True
    return bool(source.get("content")) and (source.get("group") != "company" or source.get("kind") in ("forum", "blog"))


def extract_node(state: BriefState) -> dict:
    stats, sources = state["stats"], state["sources"]
    todo = [sid for sid, s in sources.items() if sid not in state["extracted"] and _extractable(s)]
    candidates, cached, failed = [], 0, 0
    for sid in todo:
        text, kind = source_text(sources[sid])
        row = stats.per_source.setdefault(sid, {"url": sources[sid]["url"], "text": kind, "chars": len(text),
                                                "extracted": 0, "grounded": Counter(), "rejected": 0, "error": ""})
        try:
            facts, from_cache = extract_facts(sources[sid], state["company"], state["role"], state["level"], state["use_cache"])
        except llm.FatalLLMError:
            raise  # daily quota / bad key: every other call would fail too, so stop the whole run
        except (ValueError, llm.LLMError) as exc:  # one bad source shouldn't sink the run; it's logged
            stats.extract_errors.append((sources[sid]["url"], str(exc)))
            row["error"] = str(exc)
            failed += 1
            continue
        cached += from_cache
        row["extracted"] = len(facts)
        candidates += [{**f, "source_id": sid} for f in facts]
    stats.facts_extracted += len(candidates)
    first_error = stats.extract_errors[0][1] if stats.extract_errors else ""
    snippets = sum(1 for sid in todo if not sources[sid].get("full_text"))
    _log(stats, f"[extract] {len(candidates)} candidate facts from {len(todo)} sources ({snippets} snippet-only; "
                f"{cached} from cache, {len(todo) - cached - failed} via LLM, {failed} failed)")
    if todo and failed == len(todo) and not state["facts"]:
        raise PipelineError(
            f"Every fact extraction failed ({failed} of {failed} sources), so there is nothing to build a brief from. "
            f"Stopping before any further search or writing. First error: {first_error[:300]}"
        )
    return {"candidates": candidates, "extracted": state["extracted"] + todo}


def ground_node(state: BriefState) -> dict:
    stats = state["stats"]
    grounded, rejected = ground(state["candidates"], state["sources"], already=state["facts"], company=state["company"])
    location_done = state["location_done"]
    if not location_done:  # the primary posting's location, read straight from its page (code only)
        posting = next((sid for g in ("posting", "role") for sid, s in state["sources"].items()
                        if s.get("group") == g and s.get("full_text")), None)
        if posting:
            location_done = True
            loc = location_fact(posting, state["sources"][posting], state["location"])
            if loc:
                grounded.append(loc)
                stats.per_source.setdefault(posting, {"url": state["sources"][posting]["url"], "text": "full", "chars": 0,
                                                     "extracted": 0, "grounded": Counter(), "rejected": 0, "error": ""})
    for f in grounded:
        stats.per_source[f["source_id"]]["grounded"][f["section"]] += 1
    for r in rejected:
        stats.per_source[r["source_id"]]["rejected"] += 1
    facts = state["facts"] + grounded
    stats.facts_grounded = len(facts)
    stats.grounded = facts
    stats.rejected += rejected
    reasons = Counter(r["reason"] for r in rejected)
    detail = f" ({', '.join(f'{n} {r}' for r, n in reasons.items())})" if rejected else ""
    from_snippets = sum(1 for f in grounded if f["grounded_in"] == "snippet")
    _log(stats, f"[ground] {len(grounded)} grounded ({from_snippets} from snippets), {len(rejected)} rejected{detail}")
    return {"facts": facts, "candidates": [], "location_done": location_done}


def coverage_node(state: BriefState) -> dict:
    stats = state["stats"]
    counts = _section_counts(state["facts"])
    lacking = [s for s in KEY_SECTIONS if counts[s] < MIN_FACTS]
    totals = " ".join(f"{s}={counts[s]}" for s in ("company", *KEY_SECTIONS))
    if lacking and not state["retry_used"]:
        decision = f"too few for {', '.join(lacking)} -> searching once more"
    else:
        decision = f"writing (retry used: {'yes' if state['retry_used'] else 'no'})"
    _log(stats, f"[coverage] {totals} -> {decision}")
    return {"lacking": lacking}


def route_after_coverage(state: BriefState) -> str:
    return "search_more" if state["lacking"] and not state["retry_used"] else "write"


def search_more_node(state: BriefState) -> dict:
    stats, sources = state["stats"], state["sources"]
    known = {s["url"] for s in sources.values()}
    added = []
    for section in state["lacking"]:
        results = search_more(state["company"], state["role"], state["level"], section, known, filtered_out=stats.filtered,
                              location=state["location"])
        new_ids = add_sources(sources, results, section, state["company"])
        known |= {sources[sid]["url"] for sid in new_ids}
        added.append(f"{section} +{len(new_ids)}")
    stats.retry_sections = list(state["lacking"])
    stats.sources_total = len(sources)
    _log(stats, f"[search_more] new sources: {', '.join(added)}" + _filtered_note(stats.filtered))
    return {"sources": sources, "retry_used": True}


def write_node(state: BriefState) -> dict:
    selected = select_for_writing(state["facts"], level=state["level"])
    system, user = build_writer_prompt(state["company"], state["role"], state["level"], selected, state["sources"], state["today"])
    cut_before = llm.truncation_count()
    body = llm.generate(system, user, task="write")
    stats = state["stats"]
    stats.draft, stats.selected = body, len(selected)
    stats.draft_truncated = llm.truncation_count() > cut_before
    _log(stats, f"[write] draft written from {len(selected)} of {len(state['facts'])} grounded facts"
                + (" (DRAFT WAS CUT OFF by the token limit)" if stats.draft_truncated else ""))
    return {"body": body, "selected": selected}


def finalize_node(state: BriefState) -> dict:
    stats = state["stats"]
    result = finalize_brief(state["company"], state["role"], state["level"], state["body"], state["selected"], state["sources"], stats)
    _log(stats, stats.log_line())
    return {"result": result}


# ---------- wiring ----------

def build_graph():
    g = StateGraph(BriefState)
    for name, fn in [
        ("search", search_node), ("fetch", fetch_node), ("extract", extract_node), ("ground", ground_node),
        ("coverage", coverage_node), ("search_more", search_more_node), ("write", write_node), ("finalize", finalize_node),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "search")
    g.add_edge("search", "fetch")
    g.add_edge("fetch", "extract")
    g.add_edge("extract", "ground")
    g.add_edge("ground", "coverage")
    g.add_conditional_edges("coverage", route_after_coverage, {"search_more": "search_more", "write": "write"})
    g.add_edge("search_more", "fetch")
    g.add_edge("write", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def run_pipeline(company: str, role: str, level: str, jd: str | None, use_cache: bool = True, today: date | None = None,
                 stats: RunStats | None = None, location: str = DEFAULT_LOCATION) -> BriefResult:
    """Run the graph. Pass `stats` to keep the run log even when the run stops early with an LLMError."""
    final = build_graph().invoke({
        "company": company, "role": role, "level": level, "jd": jd,
        "today": today or date.today(), "use_cache": use_cache, "stats": stats or RunStats(), "location": location,
    })
    _log(final["stats"], final["stats"].pipeline_line())
    return final["result"]
