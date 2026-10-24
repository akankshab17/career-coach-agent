"""Web research via the Tavily search API (no scraping)."""
from __future__ import annotations

import math
import os
import re
import sys
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from tavily import TavilyClient

from location import DEFAULT_LOCATION, MATCH, OTHER, RANK, classify_location, find_location, required_location_elsewhere

MAX_RESULTS = 5
POSTING_RESULTS = 10  # fetch more postings so there's a good chance one matches the level
RECENT_DAYS = 365
FULL_TEXT_COMPANY = 3  # company sources fetched in full; every interview/forum/posting source is fetched
EXTRACT_BATCH = 20  # Tavily extract takes at most 20 URLs per call
FULL_TEXT_CHARS = 6000  # what one extraction prompt sees of a page, chosen by relevance (see focus_text)
MIN_FOCUS_SCORE = 3.0  # a window that scores less than this isn't evidence of anything; use the head
SHORT_BLOCK = 80  # blocks shorter than this (menu entries, headings) are weak evidence of content
MAX_PAGE_CHARS = 80000  # how much of a cleaned page is kept before choosing the relevant part
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
BLOG_DOMAINS = ("medium.com", "substack.com", "dev.to", "hashnode.dev", "blogspot.com", "wordpress.com", "ghost.io")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_blocked(url: str) -> bool:
    """True for sites we never touch (any glassdoor.* host)."""
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS) or host.startswith("glassdoor.") or ".glassdoor." in host


def display_company(company: str) -> str:
    """'uber' -> 'Uber', 'jp morgan' -> 'Jp Morgan': a name typed all in lowercase is shown title-cased. Any capital letter
    means the user chose the spelling ('eBay', 'IBM', 'OpenAI'), so it is kept."""
    name = " ".join(company.split())
    return " ".join(w[:1].upper() + w[1:] for w in name.split(" ")) if name == name.lower() else name


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.lower())


def source_kind(url: str, company: str) -> str:
    """company | reputable | forum | blog | other -- used to rank sources and decide who is "reporting"."""
    host = _host(url)
    if "leetcode.com" in host and "/discuss" in url:
        return "forum"
    if any(host == d or host.endswith("." + d) for d in FORUM_DOMAINS):
        return "forum"
    if any(host == d or host.endswith("." + d) for d in BLOG_DOMAINS):
        return "blog"
    if _slug(company) and _slug(company) in host.replace(".", "").replace("-", ""):
        return "company"
    if any(host == d or host.endswith("." + d) for d in REPUTABLE_DOMAINS):
        return "reputable"
    return "other"


JOB_AGGREGATORS = ("jobright.ai", "indeed.com", "linkedin.com", "ziprecruiter.com", "simplyhired.com", "lensa.com", "talent.com",
                   "wellfound.com", "themuse.com", "builtin.com", "joinhandshake.com", "levels.fyi", "monster.com", "careerbuilder.com",
                   "dice.com", "snagajob.com", "jooble.org", "adzuna.com", "glassdoor.com")
ATS_HOSTS = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "icims.com", "smartrecruiters.com", "ashbyhq.com", "workable.com", "taleo.net")
JOB_PATH_RE = re.compile(r"/jobs?/|/job-|/careers?/(?:listing|apply|jobs?|openings?|positions?)\b|/careers?/list/\d|/listing/|/positions?/|/openings?/|/requisition", re.I)
CAREERS_PATH_RE = re.compile(r"/careers?\b|/jobs\b|/university|/early-career|/students?|/campus|/graduates?|/internships?|/new-grads?|/early-talent", re.I)
SOURCE_TYPES = ("official-posting", "company-site", "third-party-guide", "job-aggregator", "forum/blog", "news")
TYPE_LABEL = {"official-posting": "official posting", "company-site": "company site", "third-party-guide": "third-party guide",
              "job-aggregator": "job aggregator", "forum/blog": "forum/blog posts", "news": "news"}


def _host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def source_type(url: str, company: str, group: str = "", published: str | None = None) -> str:
    """What kind of source this is, for labelling facts: official-posting | company-site | third-party-guide |
    job-aggregator | forum/blog | news. 'Official' means the company's own site (or its applicant-tracking system)."""
    host, kind = _host(url), source_kind(url, company)
    if kind in ("forum", "blog"):
        return "forum/blog"
    if _host_in(host, JOB_AGGREGATORS):
        return "job-aggregator"
    slug = _slug(company)
    on_ats = _host_in(host, ATS_HOSTS) and slug and slug in re.sub(r"[^a-z0-9]", "", url.lower())
    if kind == "company" or on_ats:
        return "official-posting" if (group == "posting" or on_ats or JOB_PATH_RE.search(url)) else "company-site"
    if kind == "reputable" or (published and group == "company"):
        return "news"
    return "third-party-guide"


def is_careers_page(url: str, company: str, group: str = "") -> bool:
    """A page on the company's own site about jobs/programs: top priority for the process and role sections."""
    return source_kind(url, company) == "company" and (group in ("careers", "posting") or bool(CAREERS_PATH_RE.search(urlparse(url).path)))


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


# ---------- Tavily credits ----------
# The free plan is 1,000 credits a month. Tavily reports what each call cost (include_usage); when a response has no usage
# (an old recorded fixture), the cost is estimated from the price list: a basic search is 1 credit, an advanced one 2;
# extract costs 1 credit per 5 pages it returned (failed pages are free).
SEARCH_CREDITS = {"basic": 1, "advanced": 2}
EXTRACT_PAGES_PER_CREDIT = 5
_credits = {"used": 0.0}
_credits_lock = threading.Lock()


def credits_used() -> float:
    """Credits spent since the process started. Callers take the difference around a step."""
    with _credits_lock:
        return _credits["used"]


def _charge(response: dict, estimate: float) -> None:
    usage = response.get("usage") if isinstance(response, dict) else None
    reported = usage.get("credits") if isinstance(usage, dict) else None
    with _credits_lock:
        _credits["used"] += float(reported) if isinstance(reported, (int, float)) else estimate


def format_credits(n: float) -> str:
    return f"{n:g}"


def search(query: str, client: TavilyClient | None = None, max_results: int = MAX_RESULTS, **options) -> list[dict]:
    """Run one Tavily query; return [{title, url, content, published_date}, ...]. Its credit cost is counted."""
    client = client or TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    options["exclude_domains"] = sorted(set(options.get("exclude_domains") or []) | set(BLOCKED_DOMAINS))
    response = client.search(query=query, max_results=max_results, include_usage=True, **options)
    _charge(response, SEARCH_CREDITS.get(options.get("search_depth") or "basic", 1))
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


# Interview-query words by role family, so a product role isn't searched with coding terms only.
FAMILY_INTERVIEW_TERMS = {
    "software": "coding data structures system design",
    "product": "product sense case interview APM interview product design",
    "data": "SQL case study statistics",
    "hardware": "circuit digital design",
    "design": "portfolio design challenge",
    "security": "threat modeling security",
    "business": "case interview",
}
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


def interview_terms(role: str) -> str:
    """Extra interview-query words that fit the role's family ('' if the role matches none)."""
    family = role_family(role)
    return FAMILY_INTERVIEW_TERMS.get(family, "") if family else ""


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


def title_names_company(result: dict, company: str) -> bool:
    """The title (not just the URL) names the company: a page about it, not one that mentions it in passing."""
    slug = _slug(company)
    return bool(slug) and slug in re.sub(r"[^a-z0-9]", "", result["title"].lower())


EMPLOYER_LINE_RE = r"(?:company|employer|hiring (?:company|organi[sz]ation)|organi[sz]ation)\s*[:|]\s*\**\s*"


def posting_belongs_to(result: dict, company: str) -> bool:
    """A posting must be the company's own job: the company is in the title or URL (which covers ATS links like
    jobs.ashbyhq.com/Perplexity), or the snippet has an employer line ("Company: Cummins"). Siemens' early-career page
    once became Cummins' posting because only the level was checked."""
    if mentions_company(result, company):
        return True
    words = re.escape(company.strip()).replace(r"\ ", r"\s+")
    return bool(company.strip()) and bool(re.search(EMPLOYER_LINE_RE + words + r"\b", result.get("content") or "", re.I))


def interview_filter_reason(result: dict, company: str, role: str, level: str | None = None) -> str | None:
    """Why an interview result is irrelevant (None = keep it). For an intern / new-grad candidate, a page about a senior
    candidate (title/URL says L5, SSE, SDE2, Senior...; or a forum/blog post whose text gives 3+ YOE or a level code) is
    another level's evidence."""
    if not mentions_company(result, company):
        return "other-company"
    if not matches_role_family(result, role):
        return f"other-role-family (wanted {role_family(role)})"
    if level in EARLY_LEVELS and interview_level(result) == "experienced":
        return f"other-level (wanted {level}, page is about an experienced candidate)"
    return None


# Forum queries stay short: "site:reddit.com Amazon Software Developer new grad university graduate early career coding
# data structures system design interview process questions" (16 words) returned r/linux, r/AO3 and "Prove your humanity".
FORUM_QUERY_WORDS = 6  # words after the site: operator
FORUM_LEVEL_WORDS = {"intern": "intern", "new-grad": "new grad", "experienced": ""}
ROLE_NOISE = {"intern", "internship", "interns", "new", "grad", "graduate", "graduates", "university", "early", "career",
              "entry", "level", "junior", "summer", "fall", "spring", "winter", "i", "ii", "iii", "iv", "and", "the", "of"}


def role_short(role: str) -> str:
    """The role without level words, years or parentheses: 'Software Engineering Intern (Summer 2027)' -> 'Software Engineering'."""
    words = re.findall(r"[A-Za-z][A-Za-z+#.&-]*", re.sub(r"\([^)]*\)", " ", role))
    return " ".join(w for w in words if w.lower() not in ROLE_NOISE) or role


def forum_query(site: str, company: str, middle: str, tail: str = "interview") -> str:
    """'site:<site> <company> <middle> <tail>' with at most FORUM_QUERY_WORDS words after site: (the middle is cut first)."""
    room = FORUM_QUERY_WORDS - len(company.split()) - len(tail.split())
    words = middle.split()[:max(room, 0)]
    return " ".join([f"site:{site}", company, *words, tail])


def forum_queries(company: str, role: str, level: str, retry: bool = False) -> list[str]:
    """Reddit + LeetCode discuss queries. The retry asks differently (experience reports; LeetCode by role) to find new posts."""
    short = role_short(role)
    if retry:
        return [forum_query("reddit.com", company, short, "interview experience"), forum_query("leetcode.com/discuss", company, short)]
    return [forum_query("reddit.com", company, short), forum_query("leetcode.com/discuss", company, FORUM_LEVEL_WORDS[level])]


def is_forum_listing(url: str) -> bool:
    """A forum page that lists posts instead of being one: a subreddit's home page (no /comments/ in the URL), a LeetCode
    discuss index (no post id)."""
    host, path = _host(url), urlparse(url).path
    if _host_in(host, ("reddit.com",)):
        return "/comments/" not in path
    if _host_in(host, ("leetcode.com",)) and path.startswith("/discuss"):
        return not re.search(r"/\d{3,}(?:/|$)", path)
    return False


def drop_forum_listings(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped) where dropped entries are {url, title, reason} like filter_interview's."""
    kept = [r for r in results if not is_forum_listing(r["url"])]
    dropped = [{"url": r["url"], "title": r["title"], "reason": "forum-listing-page"} for r in results if is_forum_listing(r["url"])]
    return kept, dropped


def forum_counts(found: list[dict], kept: list[dict], company: str) -> dict:
    """How many forum results came back, how many were listing pages, how many survived every filter."""
    forum = [r for r in found if source_kind(r["url"], company) == "forum"]
    return {"found": len(forum), "listing": sum(is_forum_listing(r["url"]) for r in forum),
            "kept": sum(source_kind(r["url"], company) == "forum" for r in kept)}


def interview_search(forum_results: list[dict], other_results: list[dict], company: str, role: str,
                     level: str | None = None) -> tuple[list[dict], list[dict], dict]:
    """Dedupe, drop forum listing pages, filter for relevance. Returns (kept, filtered, forum counts)."""
    found = _dedupe(forum_results + other_results)
    candidates, listings = drop_forum_listings(found)
    kept, filtered = filter_interview(candidates, company, role, level)
    return kept, listings + filtered, forum_counts(found, kept, company)


def filter_interview(results: list[dict], company: str, role: str, level: str | None = None) -> tuple[list[dict], list[dict]]:
    """(kept, filtered) where filtered entries are {url, title, reason}."""
    kept, filtered = [], []
    for r in results:
        reason = interview_filter_reason(r, company, role, level)
        if reason:
            filtered.append({"url": r["url"], "title": r["title"], "reason": reason})
        else:
            kept.append(r)
    return kept, filtered


GENERIC_FOCUS = ("apply", "application", "eligib", "qualif", "requirement", "responsib", "interview", "round", "assessment",
                 "program", "rotation", "deadline", "timeline", "mentor", "benefit", "experience", "skills", "degree", "location")
LEVEL_FOCUS = {"intern": ("intern", "internship", "summer"), "new-grad": ("new grad", "graduate", "university", "early career", "campus"),
               "experienced": ("senior", "years of experience")}


# What a page is *for* decides which part of it matters. A news story about an acquisition has nothing to say about
# "program" or "eligibility"; a careers page has nothing to say about "headquartered". Scoring every page by
# role/interview words made long company pages lose their company content.
COMPANY_FOCUS = ("founded", "headquarter", "product", "acquire", "acquisition", "customer", "ceo", "chief executive", "mission",
                 "employee", "revenue", "launch", "announce", "partner", "platform", "service", "offers", "provides", "business",
                 "industry", "market", "global", "investor", "valuation", "funding", "company")
INTERVIEW_FOCUS = ("interview", "round", "onsite", "on-site", "phone screen", "screen", "assessment", "oa", "coding", "question",
                   "offer", "recruiter", "technical", "behavioral", "system design", "hirevue", "case", "leetcode", "whiteboard",
                   "take-home", "hiring manager", "loop", "prepare", "experience")
PURPOSES = ("company", "role", "interview")


def focus_keywords(company: str, role: str, level: str, purpose: str = "role") -> list[str]:
    """Words that mark the useful part of a page for its purpose: 'role' (careers pages, postings: the role, level and
    application words), 'company' (news and about pages: the company's name and business), 'interview' (guides and
    forum posts: process words plus the role)."""
    role_words = [w for w in re.findall(r"[a-z]+", role.lower()) if len(w) > 2]
    if purpose == "company":
        return list(dict.fromkeys([company.lower()] + list(COMPANY_FOCUS)))
    if purpose == "interview":
        return list(dict.fromkeys(list(INTERVIEW_FOCUS) + role_words + list(LEVEL_FOCUS[level])))
    return list(dict.fromkeys(role_words + list(LEVEL_FOCUS[level]) + list(GENERIC_FOCUS)))


def focus_map(company: str, role: str, level: str) -> dict[str, list[str]]:
    return {p: focus_keywords(company, role, level, p) for p in PURPOSES}


def purpose_of(source: dict, company: str = "") -> str:
    """Why this page is in the brief: 'role' (the company's careers pages, postings), 'interview' (interview guides and
    forum posts), or 'company' (about pages and news)."""
    if source.get("official") and source.get("type") != "news" and (source.get("group") in ("careers", "posting", "role") or
                                                                       is_careers_page(source["url"], company, source.get("group", ""))):
        return "role"
    if source.get("group") in ("posting", "role", "careers", "jd"):
        return "role"
    if source.get("group") == "interview" or source.get("kind") in ("forum", "blog"):
        return "interview"
    return "company"


def focus_text(text: str, keywords: list[str] | None, limit: int = FULL_TEXT_CHARS) -> str:
    """The `limit` characters of `text` most about `keywords`.

    Career pages open with thousands of characters of menu ("Products / Sales / Service ...") and the real content
    comes after; taking the head would hand the extractor only the menu. Blocks (blank-line separated) are scored by
    keyword hits, long prose scores extra, and the best contiguous run of blocks that fits is returned.
    """
    if len(text) <= limit:
        return text
    if not keywords:
        return text[:limit]
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    pats = [re.compile(rf"\b{re.escape(k)}\b" if len(k) <= 3 else re.escape(k), re.I) for k in keywords]  # "oa" must not match "road"
    hits = [min(sum(len(p.findall(b)) for p in pats), 12) for b in blocks]
    # Menu items and headings are short; real content is prose. Short blocks score nothing, however many keywords they
    # contain ("Products", "Product Manager" in a menu) and however many there are; long ones score their hits plus a
    # prose bonus (only if they have a hit at all: prose without the keywords is no evidence of content). (Headings next to good prose still come along, because the window is contiguous.)
    scores = [0.0 if len(b) < SHORT_BLOCK or not h else h + (1.5 if len(b) >= 150 else 0) for h, b in zip(hits, blocks)]
    sizes = [len(b) + 2 for b in blocks]
    best, best_range, lo, window, total = 0.0, None, 0, 0, 0.0
    for hi in range(len(blocks)):  # the contiguous run of blocks, within `limit`, with the highest total score
        window += sizes[hi]
        total += scores[hi]
        while window > limit and lo < hi:
            window -= sizes[lo]
            total -= scores[lo]
            lo += 1
        if total > best:
            best, best_range = total, (lo, hi + 1)
    if best_range is None or best < MIN_FOCUS_SCORE:  # no clear signal about where the content is: keep the head
        return text[:limit]
    first = next(i for i in range(*best_range) if scores[i] > 0)  # shrink to the scoring core...
    last = max(i for i in range(*best_range) if scores[i] > 0) + 1
    size = sum(sizes[first:last])
    while True:  # ...then grow it with the neighbouring headings / closing lines while there is room
        grew = False
        if first > 0 and size + sizes[first - 1] <= limit:
            first -= 1
            size += sizes[first]
            grew = True
        if last < len(blocks) and size + sizes[last] <= limit:
            size += sizes[last]
            last += 1
            grew = True
        if not grew:
            break
    while first < last - 1 and hits[first] <= 1 and len(blocks[first]) < 200:  # drop menu leftovers at the edges
        first += 1
    while last - 1 > first and hits[last - 1] <= 1 and len(blocks[last - 1]) < 200:
        last -= 1
    return "\n\n".join(blocks[first:last])[:limit]


def extract(urls: list[str], client: TavilyClient | None = None) -> dict[str, str]:
    """Fetch page text for `urls` via Tavily extract (cleaned, whole page up to MAX_PAGE_CHARS; callers pick the
    relevant part with focus_text). Returns {url: text}; failures are reported, not hidden."""
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
            response = client.extract(urls=batch, include_usage=True)
        except Exception as exc:
            print(f"[search] extract failed ({type(exc).__name__}: {exc}); {len(batch)} pages skipped", file=sys.stderr)
            continue
        _charge(response, math.ceil(len(response.get("results", [])) / EXTRACT_PAGES_PER_CREDIT))
        for failed in response.get("failed_results", []):
            print(f"[search] could not extract {failed.get('url')}; skipped", file=sys.stderr)
        for r in response.get("results", []):
            if r.get("raw_content"):
                texts[r["url"]] = clean_page_text(r["raw_content"])[:MAX_PAGE_CHARS]
    return texts


def _fit(text: str, focus: list[str] | dict[str, list[str]] | None, source: dict | None = None, company: str = "") -> str:
    """What the extractor reads of a page: all of it if it fits, otherwise the part that matches the page's purpose."""
    if len(text) <= FULL_TEXT_CHARS:
        return text
    if isinstance(focus, dict):
        focus = focus.get(purpose_of(source, company) if source else "role")
    return focus_text(text, focus) if focus else text[:FULL_TEXT_CHARS]


def is_posting(source: dict) -> bool:
    return source.get("group") in ("posting", "role") or source.get("type") == "official-posting"


def is_single_posting(source: dict) -> bool:
    """One job's posting (not a careers page covering several programs, which may ask "5+ years" of another program)."""
    return (source.get("group") == "posting" or source.get("type") == "official-posting"
            or bool(JOB_PATH_RE.search(source.get("url", ""))))


def fetch_full_text(sources: dict[str, dict], focus: list[str] | dict[str, list[str]] | None = None, company: str = "",
                    location: str | None = None, level: str | None = None) -> tuple[int, int]:
    """Attach `full_text` (in place) to every careers/interview/forum/posting source and the top company sources.

    A page that fits in FULL_TEXT_CHARS is kept whole. For a longer one, `focus` (a keyword list, or the {purpose: list}
    map from focus_map) picks the relevant FULL_TEXT_CHARS; without it the page head is used.
    A posting whose text requires the candidate to be outside `location`, or (for an intern / new-grad `level`) asks for
    another level's experience, gets no text and an `excluded` reason instead.
    Returns (fetched, failed). Sources that already have text (the posting, a JD) are left alone.
    """
    company_ids = [sid for sid, s in sources.items() if s.get("group") == "company"][:FULL_TEXT_COMPANY]
    wanted = [
        sid for sid, s in sources.items()
        if "full_text" not in s and not s.get("fetch_attempted")
        and (s.get("group") != "company" or sid in company_ids or s.get("kind") in ("forum", "blog"))
    ]
    texts = extract([sources[sid]["url"] for sid in wanted])
    for sid in wanted:
        sources[sid]["fetch_attempted"] = True
        if sources[sid]["url"] in texts:
            text = texts[sources[sid]["url"]]
            if is_posting(sources[sid]) and (req := required_location_elsewhere(text, location)):
                sources[sid]["excluded"] = f"requires a location outside {location}: \"{req}\""
                continue
            if level and is_single_posting(sources[sid]) and (conflict := posting_level_conflict(sources[sid], level, text)):
                sources[sid]["excluded"] = f"not a {level} posting: {conflict}"
                continue
            sources[sid]["full_text"] = _fit(text, focus, sources[sid], company)
    fetched = sum(sources[sid]["url"] in texts for sid in wanted)
    return fetched, len(wanted) - fetched


# Language segments in URL paths: amazon.jobs/content/en/... and /content/pt/... are one page in two languages.
LANGUAGES = ("en", "pt", "es", "fr", "de", "it", "ja", "ko", "zh", "nl", "pl", "sv", "tr", "ru", "ar", "hi", "id", "th", "vi", "he",
             "da", "fi", "nb", "no", "cs", "hu", "ro", "uk", "el")
LANG_SEGMENT_RE = re.compile(rf"^(?:{'|'.join(LANGUAGES)})(?:[-_][a-z]{{2,4}})?$", re.I)  # en, pt-br, en_GB, zh-hans


def url_language(url: str) -> str | None:
    """The language code in the URL path ('en' for /content/en-us/...), or None."""
    for seg in urlparse(url).path.split("/"):
        if LANG_SEGMENT_RE.match(seg):
            return seg[:2].lower()
    return None


def canonical_url(url: str) -> str:
    """One key for every language/host variant of a page: no scheme, no 'www.', no language segments, no trailing slash."""
    u = urlparse(url)
    path = "/".join(seg for seg in u.path.split("/") if seg and not LANG_SEGMENT_RE.match(seg))
    return f"{_host(url)}/{path}" + (f"?{u.query}" if u.query else "")


def prefers(new_url: str, old_url: str) -> bool:
    """Between two variants of one page, English (or no language marked) beats any other language."""
    return url_language(new_url) in ("en", None) and url_language(old_url) not in ("en", None)


def _dedupe(results: list[dict]) -> list[dict]:
    """One result per page: the same URL, or the same page in another language (the English variant is kept)."""
    index: dict[str, int] = {}
    out: list[dict] = []
    for r in results:
        key = canonical_url(r["url"])
        if key not in index:
            index[key] = len(out)
            out.append(r)
        elif prefers(r["url"], out[index[key]]["url"]):
            out[index[key]] = r
    return out


def drop_stale(results: list[dict], today: date, days: int = RECENT_DAYS) -> list[dict]:
    """Drop results *dated* older than `days`. Undated results are kept (we can't tell)."""
    cutoff = (today - timedelta(days=days)).isoformat()
    return [r for r in results if not r["published_date"] or r["published_date"] >= cutoff]


_TIER = {"company": 0, "reputable": 1, "other": 2, "blog": 2, "forum": 3}


def rank(results: list[dict], company: str) -> list[dict]:
    """Company's own site first, then reputable outlets, then the rest; newest first within a tier."""
    by_date = sorted(results, key=lambda r: r["published_date"] or "", reverse=True)
    return sorted(by_date, key=lambda r: _TIER[source_kind(r["url"], company)])


def classify_level(result: dict) -> str:
    """Guess a posting's level from its title and URL (not the body, which mentions interns in passing)."""
    text = f"{result.get('title', '')} {result.get('url', '')}".lower()
    if re.search(r"\bintern(ship)?s?\b", text):  # \b so "Internal Systems" doesn't match
        return "intern"
    if re.search(r"new[ -]?grad|university[ -]grad|early[ -]career|graduate|entry[ -_]?level", text):
        return "new-grad"
    if re.search(r"\b(?:engineer|developer|analyst|scientist)[\s_-]+(?:i|1)\b", text):  # "Software Engineer I" (not II/III)
        return "new-grad"
    if re.search(r"\bassociate\b(?![\s_-]+(?:director|vice|vp|partner|principal|general))", text) and not re.search(
            r"\b(?:senior|sr)\b", text):  # "Associate Product Manager", not "Associate Director" / "Senior Associate"
        return "new-grad"
    return "experienced"


SENIOR_RE = re.compile(r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head of|vp)\b|"
                       r"\b(?:engineer|developer|scientist|analyst)[\s_-]+(?:ii|iii|iv|[2-5])\b", re.I)


# A title/URL for someone further along than an intern or new grad, even when it also says "Graduate": "Graduate 2026 PhD
# Software Engineer II (AV Labs)" once became Uber's new-grad posting. Senior/staff/principal titles, SSE, "Engineer II",
# "SWE II", "SDE2". "Lead"/"manager" are not here (APM is a new-grad role), nor "Member of Technical Staff".
SENIOR_TITLE_RE = re.compile(
    r"\b(?:senior|sr\.?|(?<!technical )staff|principal|distinguished|mid[- ]level|sse)\b|"
    r"\b(?:engineer|developer|scientist|analyst|sde|swe)[\s_-]*(?:ii|iii|iv|[2-5])\b", re.I)
# Company level codes mean different things per company (Amazon's new-grad SDE I is L4, Uber's L4 is SWE II), so they only
# count when the title has no intern / new-grad marker: "SDE-1 (L4 - New Grad)" is new-grad evidence.
LEVEL_CODE_RE = re.compile(r"\b(?:l[4-9]a?|e[4-9]|ic[3-9])\b", re.I)
PHD_RE = re.compile(r"\bph\.?\s?d\b", re.I)
# "5+ years of experience", "at least 3 years' professional experience", "3-5 yrs exp", "4 YOE", "YOE: 6".
YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:[-–]\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)'?(?:\s+of)?(?:\s+[\w-]+){0,3}?\s+(?:experience|exp)\b|"
    r"\b(\d{1,2})\s*\+?\s*yoe\b|\byoe\s*[:=]?\s*(\d{1,2})\b", re.I)
MIN_SENIOR_YEARS = 3  # asking for (or having) this many years of experience is not an intern or new-grad profile
EARLY_LEVELS = ("intern", "new-grad")


def _head(result: dict) -> str:
    return f"{result.get('title', '')} {result.get('url', '')}".replace("-", " ").replace("_", " ")


def experience_years(text: str) -> int:
    """The most years of experience the text asks for or mentions (0 if none). '3-5 years' counts as 3."""
    return max((int(next(g for g in m.groups() if g)) for m in YEARS_RE.finditer(text or "")), default=0)


def explicit_level(result: dict) -> str | None:
    """The level a title/URL states outright: senior or PhD markers (they win over "Graduate"), intern / new-grad markers
    (as in classify_level) or other senior words (lead, manager...). None when it says nothing (classify_level would guess
    'experienced')."""
    text = _head(result)
    if SENIOR_TITLE_RE.search(text) or PHD_RE.search(text):
        return "experienced"
    level = classify_level(result)
    if level != "experienced":
        return level
    return "experienced" if SENIOR_RE.search(text) or LEVEL_CODE_RE.search(text) else None


def posting_level_conflict(result: dict, level: str, text: str | None = None) -> str | None:
    """Why a posting is for another level than an intern / new-grad candidate's (None = no conflict): its title or URL says
    PhD, senior, staff, principal, II/III or a level code, or its text (snippet, or `text` once fetched) asks for
    MIN_SENIOR_YEARS+ years of experience. A body that merely mentions a PhD ("BS, MS or PhD") is fine."""
    if level not in EARLY_LEVELS:
        return None
    head = _head(result)
    if m := (SENIOR_TITLE_RE.search(head) or PHD_RE.search(head)
             or (classify_level(result) not in EARLY_LEVELS and LEVEL_CODE_RE.search(head))):
        return f"title says {m.group(0).strip()!r}"
    years = experience_years(text if text is not None else result.get("content") or "")
    return f"asks for {years}+ years of experience" if years >= MIN_SENIOR_YEARS else None


def title_level(result: dict) -> str | None:
    """The candidate level an interview page's title/URL names: 'experienced' for L4/L5/SSE/SDE2/SWE II/Senior/Staff...,
    'intern' / 'new-grad' for their markers, None when it says nothing."""
    if SENIOR_TITLE_RE.search(_head(result)):
        return "experienced"
    level = classify_level(result)
    if level in EARLY_LEVELS:
        return level
    return "experienced" if LEVEL_CODE_RE.search(_head(result)) else None


INTERN_NEW_GRAD_RE = re.compile(r"\bintern(?:ship)?s?\b|new[ -]?grad|university[ -]grad|entry[ -]level|campus hire|fresher", re.I)


def text_level(text: str) -> str | None:
    """'experienced' when a first-person post's text gives the writer's seniority away (a level code like L5A or SDE2, or
    MIN_SENIOR_YEARS+ years of experience) and never mentions interns or new grads; otherwise None. Only for forum/blog
    posts: guides advertise their coaches' "10+ years of experience"."""
    if not text or INTERN_NEW_GRAD_RE.search(text):
        return None
    codes = re.search(r"\b(?:l[4-9]a?|e[4-9]|ic[3-9]|sse|(?:sde|swe)[\s_-]*(?:ii|iii|[2-3]))\b", text, re.I)
    return "experienced" if codes or experience_years(text) >= MIN_SENIOR_YEARS else None


def interview_level(result: dict, text: str | None = None) -> str | None:
    """The level an interview source describes: its title/URL first, then (forum/blog posts only) its text."""
    if level := title_level(result):
        return level
    if source_kind(result.get("url", ""), "") in ("forum", "blog"):
        return text_level(text if text is not None else result.get("content") or "")
    return None


def posting_location_class(result: dict, location: str | None) -> str:
    """match / other / unknown from the title, URL and snippet (full text is re-checked once fetched)."""
    return classify_location(f"{result['title']} {result['url']} {result['content'][:800]}", location)


def rank_postings(results: list[dict], company: str, role: str, level: str, location: str | None = None) -> list[dict]:
    """Keep only the company's own postings at the requested level, best first. With a location: postings in it come
    first, unknown ones next, and postings from another country are dropped whenever one in the location exists.
    An intern / new-grad posting whose title or snippet signals another level (PhD, Senior, II, "5+ years") is dropped."""
    candidates = [{**r, "location_class": posting_location_class(r, location)} for r in results
                  if classify_level(r) == level and not posting_level_conflict(r, level)
                  and not looks_like_job_list(r["url"], r["title"]) and posting_belongs_to(r, company)]
    if location and any(c["location_class"] == MATCH for c in candidates):
        candidates = [c for c in candidates if c["location_class"] != OTHER]
    role_words = [w for w in re.findall(r"[a-z]+", role.lower()) if len(w) > 2]

    def score(r: dict) -> int:
        title = r["title"].lower()
        s = 2 if source_kind(r["url"], company) == "company" else 0
        s += sum(w in title for w in role_words)
        s += 1 if re.search(r"career|job", r["url"].lower()) else 0
        return s

    by_score = sorted(candidates, key=score, reverse=True)  # stable: ties keep search relevance order
    return sorted(by_score, key=lambda r: RANK[r["location_class"]]) if location else by_score


JOB_LIST_HOSTS = ("github.com", "gitlab.com", "githubusercontent.com", "gist.github.com")
JOB_LIST_TITLE_RE = re.compile(
    r"\bjobs?\s+(?:in|near|at|for|list|board|tracker|repo)\b|"  # "Jobs in New York, NY", "Glynn Capital Job Board"
    r"\b(?:open|current|available|all|latest)\s+(?:jobs|positions|openings|roles|opportunities)\b|"
    r"\b(?:internship|internships|openings?)\s+(?:list|board|repo|tracker)\b|"
    r"\blist of\b|\btop \d+\b|\b\d[\d,]*\+?\s+(?:jobs|roles|openings|positions)\b|\bhiring now\b", re.I)
JOB_LIST_MARKER_RE = re.compile(
    r"\b(?:easy apply|apply now|save job|show more jobs|(?:posted|updated)\s+\d+\s+(?:hours?|days?|weeks?|months?)\s+ago|\d[\d,]*\s+(?:jobs|results|openings) found)\b",
    re.I)
JOB_LIST_MARKERS = 3  # this many "Apply now"/"Posted 2 days ago" lines on one page means a list of jobs


def looks_like_job_list(url: str, title: str = "", text: str | None = None) -> bool:
    """True for pages that list many jobs (a GitHub repo of postings, "Jobs in New York" aggregator pages, results pages).
    A posting must describe ONE job."""
    host = _host(url)
    if _host_in(host, JOB_LIST_HOSTS):
        return True
    if JOB_LIST_TITLE_RE.search(title) or re.search(r"[-/]jobs-(?:in|near|at)[-/]|/jobs/search|/job-search|/jobs/?$|/q-[^/]*-jobs\.html",
                                                     url, re.I):  # Indeed search results: /q-amazon-entry-level-software-engineer-jobs.html
        return True
    return text is not None and len(JOB_LIST_MARKER_RE.findall(text)) >= JOB_LIST_MARKERS


def pick_posting(results: list[dict], company: str, role: str, level: str, location: str | None = None) -> list[dict]:
    """The single best posting at the requested level (or [])."""
    return rank_postings(results, company, role, level, location)[:1]


def normalize_posting_url(url: str) -> str:
    """Stripe's /careers/apply/ page is a form; /careers/listing/ holds the job description."""
    if _host(url).endswith("stripe.com"):
        return url.replace("/careers/apply/", "/careers/listing/")
    return url


def resolve_posting(candidates: list[dict], location: str | None = None, focus: list[str] | None = None,
                    level: str | None = None) -> list[dict]:
    """Return one posting with full text. Try the best candidates in order; fall back to the best snippet.

    With a location, a fetched page that really is in it wins immediately; otherwise the next candidate is
    tried, and a page in another country is used only if nothing better was found. With a level, a page whose text asks
    for another level's experience ("5+ years") is skipped.
    """
    found: list[tuple[int, int, dict]] = []  # (location rank, order, posting)
    wrong_level: set[str] = set()  # never fall back to the snippet of a posting skipped for its level
    for order, cand in enumerate(candidates[:MAX_POSTING_TRIES]):
        url = normalize_posting_url(cand["url"])
        text = _fit(extract([url]).get(url, ""), focus)
        if len(text.strip()) < MIN_POSTING_CHARS:
            print(f"[search] posting text from {url} missing or under {MIN_POSTING_CHARS} chars; trying next result", file=sys.stderr)
            continue
        if looks_like_job_list(url, cand.get("title", ""), text):
            print(f"[search] {url} lists many jobs instead of describing one; skipping it", file=sys.stderr)
            continue
        if level and (conflict := posting_level_conflict(cand, level, text)):
            print(f"[search] posting {url} is not a {level} posting ({conflict}); skipping it", file=sys.stderr)
            wrong_level.add(cand["url"])
            continue
        if req := required_location_elsewhere(text, location):
            print(f"[search] posting {url} requires a location outside {location} (\"{req}\"); skipping it", file=sys.stderr)
            continue
        cls = classify_location(text, location) if location else MATCH
        posting = {**cand, "url": url, "full_text": text, "location_class": cls}
        if cls == MATCH:
            return [posting]
        print(f"[search] posting {url} is not clearly in {location} (looks {cls}); looking for a better one", file=sys.stderr)
        found.append((RANK[cls], order, posting))
    if found:
        return [min(found, key=lambda t: t[:2])[2]]
    rest = [c for c in candidates if c["url"] not in wrong_level]
    if rest:
        print(f"[search] no posting page could be extracted; using the snippet of {rest[0]['url']}", file=sys.stderr)
        return rest[:1]
    if candidates:
        print(f"[search] no {level} posting found (every candidate was for another level)", file=sys.stderr)
    return []


def posting_location(posting: dict, location: str | None) -> dict | None:
    """{place, quote, class} for where a fetched posting says the job is, or None."""
    text = posting.get("full_text")
    return find_location(text, location) if text else None


LEVEL_SHORT_TERMS = {"intern": "internship", "new-grad": "new grad university", "experienced": ""}
MAX_COMPANY_SITES = 3


def _site(host: str) -> str:
    """'investor.salesforce.com' -> 'salesforce.com', 'jobs.acme.co.uk' -> 'acme.co.uk'."""
    parts = host.split(".")
    keep = 3 if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac") else 2
    return ".".join(parts[-keep:])


def company_sites(urls, company: str) -> list[str]:
    """The company's own domains, from URLs already found on them (investor.salesforce.com -> salesforce.com).
    None found: guess <name>.com (a wrong guess just returns no results)."""
    sites: list[str] = []
    for url in urls:
        if source_kind(url, company) == "company" and (site := _site(_host(url))) not in sites:
            sites.append(site)
    return sites[:MAX_COMPANY_SITES] or ([f"{_slug(company)}.com"] if _slug(company) else [])


def search_company_careers(company: str, role: str, level: str, sites: list[str], extra: str = "") -> list[dict]:
    """Careers/program pages on the company's own site(s). The open-web careers query (long, full of level words) can come
    back with nothing from the company at all: one Salesforce recording returned only Verizon, USAJOBS and job-list pages,
    so the APM program page never became a source. A short query limited to the company's domains can't drift like that."""
    if not sites:
        return []
    query = " ".join(w for w in (company, role, LEVEL_SHORT_TERMS[level], "careers program", extra) if w)
    return [r for r in search(query, include_domains=sites)
            if source_kind(r["url"], company) == "company"
            and classify_level(r) in (level, "experienced")  # titles can only say "intern"/"new grad"; unmarked pass
            and not (level in EARLY_LEVELS and (SENIOR_TITLE_RE.search(_head(r)) or PHD_RE.search(_head(r))))
            and (CAREERS_PATH_RE.search(urlparse(r["url"]).path) or JOB_PATH_RE.search(r["url"])
                 or re.search(r"(?:^|\.)(?:careers?|jobs)\.", _host(r["url"])) or _host(r["url"]).endswith(".jobs"))]


def gather_research(company: str, role: str, level: str, jd: str | None, today: date | None = None,
                    location: str = DEFAULT_LOCATION) -> dict[str, list[dict]]:
    """Run every query the brief needs, grouped by topic. The posting is chosen to be in `location`."""
    today = today or date.today()
    terms = LEVEL_QUERY_TERMS[level]

    family_terms = interview_terms(role)

    company_results = (
        search(f"{company} company overview", exclude_domains=EXCLUDE_DOMAINS)
        + search(
            f"{company} recent news funding acquisition product launch",
            topic="news", time_range="year", exclude_domains=EXCLUDE_DOMAINS,
        )
    )
    # The company's own careers/program pages: top-priority sources for the process and role sections.
    careers_results = [r for r in search(f"{company} careers {terms} {role} program how to apply", exclude_domains=EXCLUDE_DOMAINS)
                       if source_kind(r["url"], company) == "company"]
    careers_results += search_company_careers(company, role, level, company_sites([r["url"] for r in company_results], company))
    forum_results = [r for q in forum_queries(company, role, level) for r in search(q)]
    other_results = search(f"{company} {role} {terms} {family_terms} interview process common questions", exclude_domains=EXCLUDE_DOMAINS)
    interview_kept, interview_filtered, forum = interview_search(forum_results, other_results, company, role, level)
    research = {
        "careers": _dedupe(careers_results),  # first, so a page found twice is filed here, not under company news
        # pages whose title names the company first (stable): only the top FULL_TEXT_COMPANY are fetched, and a story that
        # mentions the company in passing (a DeepMind alum's startup, Mandia's new firm) has nothing to say about it
        "company": sorted(rank(drop_stale(_dedupe(company_results), today), company), key=lambda r: not title_names_company(r, company)),
        "interview": rank(interview_kept, company),
        "posting": [],
        "filtered": interview_filtered,  # not a source group: reported in the run log and debug file
        "forum_counts": forum,  # not a source group either: {found, listing, kept} for the run log
    }
    if not jd:
        postings = search(f"{company} {role} {terms} job posting {location}", max_results=POSTING_RESULTS)
        research["posting"] = resolve_posting(rank_postings(postings, company, role, level, location), location,
                                              focus_keywords(company, role, level), level)
    return research


def search_more(company: str, role: str, level: str, section: str, known_urls: set[str],
                filtered_out: list[dict] | None = None, location: str = DEFAULT_LOCATION,
                forum_out: dict | None = None) -> list[dict]:
    """Targeted second-round search for a section that came up short. Returns only unseen results.

    Interview results that are irrelevant are appended to `filtered_out` (if given) instead of returned; the forum counts
    ({found, listing, kept}) are written into `forum_out` (if given).
    """
    terms = LEVEL_QUERY_TERMS[level]
    if section == "interview":
        forum_results = [r for q in forum_queries(company, role, level, retry=True) for r in search(q)]
        other_results = search(f"{company} {role} {terms} {interview_terms(role)} interview questions what to expect",
                               exclude_domains=EXCLUDE_DOMAINS)
        results, filtered, forum = interview_search(forum_results, other_results, company, role, level)
        if forum_out is not None:
            forum_out.update(forum)
        if filtered_out is not None:
            filtered_out += [f for f in filtered if f["url"] not in known_urls]
    elif section == "role":
        postings = search(f"{company} careers {role} {terms} responsibilities qualifications {location}", max_results=POSTING_RESULTS)
        results = [{**r, "url": normalize_posting_url(r["url"])} for r in rank_postings(postings, company, role, level, location)][:2]
        own = search_company_careers(company, role, level, company_sites(sorted(known_urls), company), "responsibilities qualifications")
        results += [r for r in own if r["url"] not in known_urls][:2]
    else:
        raise ValueError(f"no follow-up search for section {section!r}")
    known = {canonical_url(u) for u in known_urls}
    return [r for r in _dedupe(results) if canonical_url(r["url"]) not in known]
