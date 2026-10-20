"""Web research via the Tavily search API (no scraping)."""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from tavily import TavilyClient

MAX_RESULTS = 5
POSTING_RESULTS = 10  # fetch more postings so there's a good chance one matches the level
RECENT_DAYS = 365

LEVELS = ("intern", "new-grad", "experienced")
LEVEL_QUERY_TERMS = {
    "intern": "intern internship",
    "new-grad": "new grad university graduate early career",
    "experienced": "experienced",
}

# Pages that are noisy or low-signal for company research.
EXCLUDE_DOMAINS = ["youtube.com", "leadiq.com", "zoominfo.com", "dnb.com", "crunchbase.com"]
REPUTABLE_DOMAINS = (
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "nytimes.com", "apnews.com", "bbc.com",
    "cnbc.com", "forbes.com", "techcrunch.com", "theverge.com", "wired.com", "fortune.com",
    "businessinsider.com", "axios.com", "economist.com", "theinformation.com",
)
FORUM_DOMAINS = ("reddit.com", "teamblind.com", "quora.com", "news.ycombinator.com")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.lower())


def source_kind(url: str, company: str) -> str:
    """company | reputable | forum | other -- used to rank sources and phrase claims."""
    host = _host(url)
    if "leetcode.com" in host and "/discuss" in url:
        return "forum"
    if any(host == d or host.endswith("." + d) for d in FORUM_DOMAINS):
        return "forum"
    if _slug(company) and _slug(company) in host.replace(".", "").replace("-", ""):
        return "company"
    if any(host == d or host.endswith("." + d) for d in REPUTABLE_DOMAINS):
        return "reputable"
    return "other"


def _parse_date(raw: str | None) -> str | None:
    """Tavily gives RFC-2822 or ISO strings (news only). Normalise to YYYY-MM-DD."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def search(query: str, client: TavilyClient | None = None, max_results: int = MAX_RESULTS, **options) -> list[dict]:
    """Run one Tavily query; return [{title, url, content, published_date}, ...]."""
    client = client or TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    response = client.search(query=query, max_results=max_results, **options)
    return [
        {
            "title": r.get("title", ""),
            "url": r["url"],
            "content": r.get("content", ""),
            "published_date": _parse_date(r.get("published_date")),
        }
        for r in response.get("results", [])
    ]


def _dedupe(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out


def drop_stale(results: list[dict], today: date, days: int = RECENT_DAYS) -> list[dict]:
    """Drop results *dated* older than `days`. Undated results are kept (we can't tell)."""
    cutoff = (today - timedelta(days=days)).isoformat()
    return [r for r in results if not r["published_date"] or r["published_date"] >= cutoff]


_TIER = {"company": 0, "reputable": 1, "other": 2, "forum": 3}


def rank(results: list[dict], company: str) -> list[dict]:
    """Company's own site first, then reputable outlets, then the rest; newest first within a tier."""
    by_date = sorted(results, key=lambda r: r["published_date"] or "", reverse=True)
    return sorted(by_date, key=lambda r: _TIER[source_kind(r["url"], company)])


def classify_level(result: dict) -> str:
    """Guess a posting's level from its title and URL (not the body, which mentions interns in passing)."""
    text = f"{result['title']} {result['url']}".lower()
    if re.search(r"\bintern(ship)?s?\b", text):  # \b so "Internal Systems" doesn't match
        return "intern"
    if re.search(r"new[ -]?grad|university[ -]grad|early[ -]career|graduate", text):
        return "new-grad"
    return "experienced"


def pick_posting(results: list[dict], company: str, role: str, level: str) -> list[dict]:
    """Keep only postings at the requested level, then return the single best match (or [])."""
    candidates = [r for r in results if classify_level(r) == level]
    if not candidates:
        return []
    role_words = [w for w in re.findall(r"[a-z]+", role.lower()) if len(w) > 2]

    def score(r: dict) -> int:
        title = r["title"].lower()
        s = 2 if source_kind(r["url"], company) == "company" else 0
        s += sum(w in title for w in role_words)
        s += 1 if re.search(r"career|job", r["url"].lower()) else 0
        return s

    return [max(candidates, key=score)]  # max() keeps the first (most relevant) on ties


def gather_research(company: str, role: str, level: str, jd: str | None, today: date | None = None) -> dict[str, list[dict]]:
    """Run every query the brief needs, grouped by topic."""
    today = today or date.today()
    terms = LEVEL_QUERY_TERMS[level]

    company_results = (
        search(f"{company} company overview", exclude_domains=EXCLUDE_DOMAINS)
        + search(
            f"{company} recent news funding acquisition product launch",
            topic="news", time_range="year", exclude_domains=EXCLUDE_DOMAINS,
        )
    )
    interview_results = (
        search(f"site:reddit.com {company} {role} {terms} interview process questions")
        + search(f"{company} {role} {terms} interview process common questions", exclude_domains=EXCLUDE_DOMAINS)
    )
    research = {
        "company": rank(drop_stale(_dedupe(company_results), today), company),
        "interview": rank(_dedupe(interview_results), company),
        "posting": [],
    }
    if not jd:
        postings = search(f"{company} {role} {terms} job posting", max_results=POSTING_RESULTS)
        research["posting"] = pick_posting(postings, company, role, level)
    return research
