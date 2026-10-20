"""Web research via the Tavily search API (no scraping)."""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from tavily import TavilyClient

MAX_RESULTS = 5
POSTING_RESULTS = 10  # fetch more postings so there's a good chance one matches the level
RECENT_DAYS = 365
FULL_TEXT_COMPANY = 3  # company sources fetched in full; every interview/forum/posting source is fetched
EXTRACT_BATCH = 20  # Tavily extract takes at most 20 URLs per call
FULL_TEXT_CHARS = 6000  # cap per page (after cleaning) so each extraction prompt stays small
MIN_POSTING_CHARS = 500  # extracted posting text shorter than this is treated as a failed extract
MAX_POSTING_TRIES = 3

LEVELS = ("intern", "new-grad", "experienced")
LEVEL_QUERY_TERMS = {
    "intern": "intern internship",
    "new-grad": "new grad university graduate early career",
    "experienced": "experienced",
}

# Pages that are noisy or low-signal for company research.
EXCLUDE_DOMAINS = ["youtube.com", "leadiq.com", "zoominfo.com", "dnb.com", "crunchbase.com"]
# Never searched, extracted or cited, whatever the query: we don't scrape these sites.
BLOCKED_DOMAINS = ["glassdoor.com", "glassdoor.co.in", "glassdoor.co.uk", "glassdoor.ca", "glassdoor.com.au"]
REPUTABLE_DOMAINS = (
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "nytimes.com", "apnews.com", "bbc.com",
    "cnbc.com", "forbes.com", "techcrunch.com", "theverge.com", "wired.com", "fortune.com",
    "businessinsider.com", "axios.com", "economist.com", "theinformation.com",
)
FORUM_DOMAINS = ("reddit.com", "teamblind.com", "quora.com", "news.ycombinator.com")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_blocked(url: str) -> bool:
    """True for sites we never touch (any glassdoor.* host)."""
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS) or host.startswith("glassdoor.") or ".glassdoor." in host


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
    options["exclude_domains"] = sorted(set(options.get("exclude_domains") or []) | set(BLOCKED_DOMAINS))
    response = client.search(query=query, max_results=max_results, **options)
    return [
        {
            "title": r.get("title", ""),
            "url": r["url"],
            "content": r.get("content", ""),
            "published_date": _parse_date(r.get("published_date")),
        }
        for r in response.get("results", [])
        if not is_blocked(r["url"])  # belt and braces: Tavily's exclude list doesn't cover every TLD
    ]


def clean_page_text(text: str) -> str:
    """Strip markdown link/image markup that Tavily leaves in pages (keeping link text).

    Forum pages like LeetCode start with ~4k chars of nav links; without this the cap keeps only the nav.
    """
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


MIN_FAMILY_HITS = 2  # a snippet only counts as "about another family" with at least this many keyword hits

# Role families, to keep a "Silicon/FPGA intern" post out of a software-engineering brief.
ROLE_FAMILIES = {
    "software": r"software|\bswes?\b|\bsdes?\b|developer|back-?end|front-?end|full[- ]?stack|programmer",
    "hardware": r"silicon|fpga|asic|\brtl\b|verilog|vhdl|hardware|semiconductor|\bchips?\b|electrical engineer|embedded",
    "data": r"data scien|data analy|machine learning|\bml\b|data engineer|analytics|business intelligence",
    "product": r"product manag|\bpms?\b|\bapm\b|program manag|\btpm\b",
    "design": r"\bux\b|\bui\b|product design|designer|user experience",
    "security": r"security engineer|infosec|cyber ?security",
    "business": r"\bsales\b|marketing|\bfinance\b|consulting|account manager|business analyst",
}
_FAMILY_RES = {name: re.compile(rx, re.I) for name, rx in ROLE_FAMILIES.items()}


def family_scores(text: str) -> Counter:
    return Counter({name: n for name, rx in _FAMILY_RES.items() if (n := len(rx.findall(text)))})


def role_family(role: str) -> str | None:
    """The role family the user asked for, or None if the role matches no known family (then we don't filter)."""
    scores = family_scores(role)
    return scores.most_common(1)[0][0] if scores else None


def mentions_company(result: dict, company: str) -> bool:
    """The title or URL must name the company. (A post that only mentions it in passing, like an HPE
    interview that compares notes with Google, is about another company; generic guides name nobody.)"""
    slug = _slug(company)
    return bool(slug) and slug in re.sub(r"[^a-z0-9]", "", f"{result['title']} {result['url']}".lower())


def matches_role_family(result: dict, role: str) -> bool:
    """Title/URL decide when they name a family; otherwise the snippet's dominant family decides."""
    want = role_family(role)
    if want is None:
        return True
    head = family_scores(f"{result['title']} {result['url']}".replace("-", " ").replace("_", " "))
    if head:
        return want in head
    body = family_scores(result["content"])
    top = max(body.values(), default=0)
    return top < MIN_FAMILY_HITS or body[want] >= top


def interview_filter_reason(result: dict, company: str, role: str) -> str | None:
    """Why an interview result is irrelevant (None = keep it)."""
    if not mentions_company(result, company):
        return "other-company"
    if not matches_role_family(result, role):
        return f"other-role-family (wanted {role_family(role)})"
    return None


def filter_interview(results: list[dict], company: str, role: str) -> tuple[list[dict], list[dict]]:
    """(kept, filtered) where filtered entries are {url, title, reason}."""
    kept, filtered = [], []
    for r in results:
        reason = interview_filter_reason(r, company, role)
        if reason:
            filtered.append({"url": r["url"], "title": r["title"], "reason": reason})
        else:
            kept.append(r)
    return kept, filtered


def extract(urls: list[str], client: TavilyClient | None = None) -> dict[str, str]:
    """Fetch full page text for `urls` via Tavily extract. Returns {url: text}; failures are reported, not hidden."""
    blocked = [u for u in urls if is_blocked(u)]
    if blocked:
        print(f"[search] refusing to extract {len(blocked)} blocked page(s): {', '.join(blocked)}", file=sys.stderr)
    urls = [u for u in urls if not is_blocked(u)]
    if not urls:
        return {}
    client = client or TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    texts: dict[str, str] = {}
    for start in range(0, len(urls), EXTRACT_BATCH):
        batch = urls[start:start + EXTRACT_BATCH]
        try:
            response = client.extract(urls=batch)
        except Exception as exc:
            print(f"[search] extract failed ({type(exc).__name__}: {exc}); {len(batch)} pages skipped", file=sys.stderr)
            continue
        for failed in response.get("failed_results", []):
            print(f"[search] could not extract {failed.get('url')}; skipped", file=sys.stderr)
        for r in response.get("results", []):
            if r.get("raw_content"):
                texts[r["url"]] = clean_page_text(r["raw_content"])[:FULL_TEXT_CHARS]
    return texts


def fetch_full_text(sources: dict[str, dict]) -> tuple[int, int]:
    """Attach `full_text` (in place) to every interview/forum/posting source and the top company sources.

    Returns (fetched, failed). Sources that already have text (the posting, a JD) are left alone.
    """
    company_ids = [sid for sid, s in sources.items() if s.get("group") == "company"][:FULL_TEXT_COMPANY]
    wanted = [
        sid for sid, s in sources.items()
        if "full_text" not in s and not s.get("fetch_attempted")
        and (s.get("group") != "company" or sid in company_ids or s.get("kind") == "forum")
    ]
    texts = extract([sources[sid]["url"] for sid in wanted])
    for sid in wanted:
        sources[sid]["fetch_attempted"] = True
        if sources[sid]["url"] in texts:
            sources[sid]["full_text"] = texts[sources[sid]["url"]]
    fetched = sum(sources[sid]["url"] in texts for sid in wanted)
    return fetched, len(wanted) - fetched


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


def rank_postings(results: list[dict], company: str, role: str, level: str) -> list[dict]:
    """Keep only postings at the requested level, best match first."""
    candidates = [r for r in results if classify_level(r) == level]
    role_words = [w for w in re.findall(r"[a-z]+", role.lower()) if len(w) > 2]

    def score(r: dict) -> int:
        title = r["title"].lower()
        s = 2 if source_kind(r["url"], company) == "company" else 0
        s += sum(w in title for w in role_words)
        s += 1 if re.search(r"career|job", r["url"].lower()) else 0
        return s

    return sorted(candidates, key=score, reverse=True)  # stable: ties keep search relevance order


def pick_posting(results: list[dict], company: str, role: str, level: str) -> list[dict]:
    """The single best posting at the requested level (or [])."""
    return rank_postings(results, company, role, level)[:1]


def normalize_posting_url(url: str) -> str:
    """Stripe's /careers/apply/ page is a form; /careers/listing/ holds the job description."""
    if _host(url).endswith("stripe.com"):
        return url.replace("/careers/apply/", "/careers/listing/")
    return url


def resolve_posting(candidates: list[dict]) -> list[dict]:
    """Return one posting with full text. Try the best candidates in order; fall back to the best snippet."""
    for cand in candidates[:MAX_POSTING_TRIES]:
        url = normalize_posting_url(cand["url"])
        text = extract([url]).get(url, "")
        if len(text.strip()) >= MIN_POSTING_CHARS:
            return [{**cand, "url": url, "full_text": text}]
        print(f"[search] posting text from {url} missing or under {MIN_POSTING_CHARS} chars; trying next result", file=sys.stderr)
    if candidates:
        print(f"[search] no posting page could be extracted; using the snippet of {candidates[0]['url']}", file=sys.stderr)
        return candidates[:1]
    return []


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
    interview_kept, interview_filtered = filter_interview(_dedupe(interview_results), company, role)
    research = {
        "company": rank(drop_stale(_dedupe(company_results), today), company),
        "interview": rank(interview_kept, company),
        "posting": [],
        "filtered": interview_filtered,  # not a source group: reported in the run log and debug file
    }
    if not jd:
        postings = search(f"{company} {role} {terms} job posting", max_results=POSTING_RESULTS)
        research["posting"] = resolve_posting(rank_postings(postings, company, role, level))
    return research


def search_more(company: str, role: str, level: str, section: str, known_urls: set[str],
                filtered_out: list[dict] | None = None) -> list[dict]:
    """Targeted second-round search for a section that came up short. Returns only unseen results.

    Interview results that are irrelevant are appended to `filtered_out` (if given) instead of returned.
    """
    terms = LEVEL_QUERY_TERMS[level]
    if section == "interview":
        results = (
            search(f"site:reddit.com {company} {role} {terms} interview experience rounds")
            + search(f"site:leetcode.com {company} {role} interview experience")
            + search(f"{company} {role} {terms} interview questions what to expect", exclude_domains=EXCLUDE_DOMAINS)
        )
        results, filtered = filter_interview(_dedupe(results), company, role)
        if filtered_out is not None:
            filtered_out += [f for f in filtered if f["url"] not in known_urls]
    elif section == "role":
        postings = search(f"{company} careers {role} {terms} responsibilities qualifications", max_results=POSTING_RESULTS)
        results = [{**r, "url": normalize_posting_url(r["url"])} for r in rank_postings(postings, company, role, level)][:2]
    else:
        raise ValueError(f"no follow-up search for section {section!r}")
    return [r for r in _dedupe(results) if r["url"] not in known_urls]
