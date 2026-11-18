"""The real FastAPI app (src/api.py) with a scripted pipeline: no Tavily or LLM calls, no credits spent.

For trying the web app (web/) and for its end-to-end test:
    INTERNAL_API_KEY=<key> .venv/bin/python scripts/fake_api.py            # http://127.0.0.1:8000

Env: FAKE_API_PORT (8000), FAKE_STEP_SECONDS (1.0 per step), FAKE_DB (a fresh temp file each start, so a restart
"expires" old briefs like the real server's disk wipe). Company "Fail Co" makes the run fail (to see the error state).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn  # noqa: E402

import api  # noqa: E402
import llm  # noqa: E402
from brief import BriefResult, RunStats  # noqa: E402
from jobs import JobStore  # noqa: E402

STEP_SECONDS = float(os.getenv("FAKE_STEP_SECONDS", "1.0"))
# The real order with one follow-up round (graph.py): search_more re-runs fetch -> extract -> ground -> coverage.
SCRIPT = [
    ("search", "[search] 14 sources (5 company, 3 careers, 5 interview, 1 posting); tavily credits: 6"),
    ("fetch", "[fetch] 10 pages fetched, 1 failed; 11/14 sources have full text; tavily credits: 3"),
    ("extract", "[extract] 52 candidate facts from 14 sources (3 snippet-only; 0 cached) in 6.1s, 4 at a time"),
    ("ground", "[ground] 31 grounded (4 from snippets), 21 rejected"),
    ("coverage", "[coverage] 1 interview, 6 role -> search more"),
    ("search_more", "[search_more] new sources: 3 interview; tavily credits: 2"),
    ("fetch", "[fetch] 2 pages fetched, 0 failed"),
    ("extract", "[extract] 9 candidate facts from 3 sources"),
    ("ground", "[ground] 7 grounded, 2 rejected"),
    ("coverage", "[coverage] 5 interview, 6 role -> enough"),
    ("write", "[write] draft written from 30 of 38 grounded facts"),
    ("finalize", "[finalize] 34 sentences checked, 1 dropped, 4 rewritten"),
]


def fake_brief(company: str, role: str, level: str) -> str:
    s = [
        ("About us", f"https://{company.lower().replace(' ', '')}.example/about"),
        (f"{role} ({level}) careers page", f"https://{company.lower().replace(' ', '')}.example/careers/123"),
        (f"My {company} interview experience", "https://forum.example/posts/42"),
        (f"{company} new grad loop, rejected", "https://forum.example/posts/77"),
        (f"{company} launches a new developer platform [video]", "https://news.example/2026/10/launch"),
    ]
    c = lambda n: f"[{n}]({s[n - 1][1]})"  # noqa: E731
    esc = lambda t: t.replace("[", "\\[").replace("]", "\\]")  # noqa: E731  as brief.py escapes titles
    sources = "\n".join(f"{i}. [{esc(title)}]({url})" + (" (2026-10-15)" if i == 5 else "") for i, (title, url) in enumerate(s, 1))
    return f"""# Interview brief: {company} — {role} ({level})

## TL;DR
- Candidates report an online assessment followed by three technical rounds and one behavioral round. {c(3)}{c(4)}
- The posting asks for strong data structures and algorithms fundamentals. {c(2)}
- {company} recently launched a developer platform. {c(5)}

## 1. Company overview + recent news
{company} builds software infrastructure for businesses. {c(1)}
In October 2026 it launched a new developer platform. {c(5)}

## 2. Interview process + common questions
Candidates report: The online assessment has two LeetCode medium problems in 90 minutes. {c(3)}
Candidates report: The final loop is four 45-minute interviews on one day. {c(4)}

## 3. What the role really needs
The role involves building and operating backend services. {c(2)}
The posting lists experience with **Python or Go** as a plus. {c(2)}

## 4. How to prepare
- Practice graph and interval problems under time pressure. {c(3)}
- Prepare stories about debugging a production issue. {c(4)}

## 5. Questions to ask the interviewer
- How does the team decide what goes into the developer platform? {c(5)}

## Sources
{sources}

_Grounding: 38 facts grounded from 17 sources (23 rejected). Writing: 34 sentences checked, 1 dropped._
"""


def runner(company, role, level, jd, location, stats: RunStats) -> BriefResult:
    for i, (step, line) in enumerate(SCRIPT):
        stats.on_progress(step, None)
        time.sleep(STEP_SECONDS)
        if company.lower() == "fail co" and step == "extract":
            raise llm.QuotaExceededError("Groq daily token limit reached for openai/gpt-oss-20b and no fallback is configured.")
        stats.on_progress(None, line)
    return BriefResult(fake_brief(company, role, level), stats, {})


if __name__ == "__main__":
    os.environ.setdefault("MAX_BRIEFS_PER_DAY", "0")  # no daily cap for a fake
    db = os.getenv("FAKE_DB") or str(Path(tempfile.mkdtemp(prefix="fake_api_")) / "jobs.db")
    print(f"[fake_api] jobs db {db}; internal key {'required' if os.getenv('INTERNAL_API_KEY') else 'NOT required'}", file=sys.stderr)
    app = api.create_app(store=JobStore(db), runner=runner)
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("FAKE_API_PORT", "8000")), log_level="warning")
