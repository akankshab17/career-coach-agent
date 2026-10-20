"""Extract facts from one source at a time, then ground them in code.

A fact survives grounding only if its exact_quote really appears in the source text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from urllib.parse import urlparse

import llm

PROMPT_VERSION = "extract-v4"  # bump when EXTRACT_SYSTEM_PROMPT changes, so old cache entries are ignored
CACHE_DIR = os.path.join(".cache", "extract")
SECTIONS = ("company", "interview", "role", "timeline")
STAGES = ("oa", "phone-screen", "team-screen", "onsite", "unknown")
LEVELS = ("intern", "new-grad", "experienced", "unknown")
EXPERIENCE_KINDS = ("forum", "blog")  # first-person accounts live here: Reddit, LeetCode discuss, Medium and other blogs
MAX_FACTS_PER_SOURCE = 12
MIN_QUOTE_WORDS = 3

# Facts that go stale fast. Facts like these need a dated source.
_BIG = r"(?:\d{1,3}(?:,\d{3})+|\d{3,})(?:\.\d+)?"  # 250, 8,000, 67,400 (so "3 people on the panel" is not a headcount)
TIME_SENSITIVE_RE = re.compile(
    r"valuation|valued at|\bworth\b|headcount|employees\b|employs|workforce|staff of|"
    r"funding|raised|\bseries [a-h]\b|tender offer|revenue|\bipo\b|layoffs?|laid off|market cap|"
    r"(?:us)?\$\s?\d[\d,.]*\s?(?:billion|million|trillion|bn|mn)\b|"  # bare "$33.7 billion" / "$7 million"
    rf"~?{_BIG}\+?[ -](?:employees?|people|staff|workers|associates|team members)\b",  # "67,400-employee", "~8,000 people"
    re.I,
)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

# Words that mark a claim as unconfirmed. A source whose *title* says "may acquire" must not become "acquired".
HEDGED_TITLE_RE = re.compile(
    r"\b(?:might|could|reportedly|considering|exploring|exploration|in talks|rumou?rs?|rumou?red|plans? to|"
    r"weighs?|weighing|mulls?|eyes?|seeks?|set to)\b|(?-i:\bmay\b)|\ballegedly\b", re.I)  # lowercase "may" only
HEDGE_WORD_RE = re.compile(
    r"\b(?:reportedly|may|might|could|considering|exploring|in talks|rumou?r(?:s|ed)?|allegedly|"
    r"plans? to|expected to|would|according to reports?|is said to|weighing)\b", re.I)
LOWERABLE_STARTERS = {"The", "A", "An", "This", "There", "It", "They", "Each", "Most", "Some", "Many", "All",
                      "Interviewers", "Interviews", "Rounds", "Questions"}

REJECT_REASONS = {
    "malformed": "extractor returned an entry without fact/quote/section",
    "quote-too-short": f"quote has fewer than {MIN_QUOTE_WORDS} words",
    "quote-not-found": "exact_quote does not appear in the source text",
    "number-not-in-quote": "the fact contains a number its quote doesn't",
    "wrong-subject": "the fact is about the publishing site, another company, or isn't about the target company's own process/role",
    "undated-time-sensitive": "time-sensitive fact from an undated source",
    "duplicate": "same fact already grounded",
}

EXTRACT_SYSTEM_PROMPT = f"""You extract facts for a student's interview-prep brief from ONE web page.
Return JSON only, no prose:
{{"facts": [{{"fact": "...", "exact_quote": "...", "section": "company|interview|role|timeline", "stage": "oa|phone-screen|team-screen|onsite|unknown", "level": "intern|new-grad|experienced|unknown", "about_company": true|false}}]}}
Rules:
- exact_quote: copied VERBATIM from the page text, one contiguous span of about 5-40 words that states the fact.
  Do not fix typos, reorder, or join separate passages.
- fact: one short sentence restating ONLY what the quote says. No added numbers, names, durations or details.
- section: "company" = what the company does, products, news, acquisitions, funding;
  "interview" = interview stages, formats, durations, question types, tips;
  "role" = responsibilities, qualifications, skills, program description, team, location of the role;
  "timeline" = the APPLICATION calendar: when applications open or close, deadlines, decision dates, start dates,
  how long the process takes. Dates and deadlines are "timeline", never "interview".
- Company career and program pages (the company's own site) are the best source for "role" and "interview" facts:
  extract what the page says about the program, eligibility, qualifications, how to apply and the process.
- stage (interview facts only; use "unknown" otherwise): which interview stage the fact is about.
  "oa" = online assessment / OA / take-home test; "phone-screen" = recruiter or technical phone/video screen;
  "team-screen" = team/hiring-manager conversation; "onsite" = onsite / virtual onsite / final loop.
  Use "unknown" when the text does not say which stage, e.g. general tips. One fact = one stage: if the
  quote covers several stages, make separate facts with separate quotes.
- level (interview facts only; use "unknown" otherwise): the seniority of the candidate whose process the page describes:
  "intern" = internship, "new-grad" = new graduate / university hire / entry level, "experienced" = full-time
  industry hire, senior, staff, "unknown" = the page doesn't say.
- about_company: true ONLY if the fact is about the target company's own interview process, role, products or
  news. false if it is about the website or service that published the page (e.g. a prep site's own practice
  tool, course, mentors, pricing or feedback), about another company, or is generic with nothing specific to this company.
- If the page title or quote hedges ("may", "reportedly", "considering"), keep that hedge in the fact.
- Skip facts about other companies, and about other seniority levels than the candidate's.
- At most {MAX_FACTS_PER_SOURCE} facts, the most useful first. If nothing is relevant return {{"facts": []}}."""


# ---------- source text ----------

def source_text(source: dict) -> tuple[str, str]:
    """The text we extract from and ground against: (text, "full" | "snippet").

    Pages Tavily can't extract (Reddit, mostly) still have a search snippet. A snippet is real text from
    the source, so a quote found in it is still verified evidence.
    """
    if source.get("full_text"):
        return source["full_text"], "full"
    return source.get("content") or "", "snippet"


# ---------- extraction (LLM) + cache ----------

def _cache_path(url: str, company: str, role: str, level: str, text_kind: str, model: str, text: str) -> str:
    # Key: URL + prompt version + the model that extracted + a hash of the exact text it read. Company/role/level
    # are in the prompt too, and text_kind keeps snippet and full-page extractions apart. A different model = a
    # different entry; the prompt version changes only when the prompt does. The text hash means that when the
    # text we feed the extractor changes (better page cleaning, a different part of the page), old results
    # (even an empty list from reading a menu) are not reused. A pasted JD is keyed on its text for the same reason.
    key = "|".join([PROMPT_VERSION, model, url, company.lower(), role.lower(), level, text_kind,
                    hashlib.sha256(text.encode()).hexdigest()])
    return os.path.join(CACHE_DIR, hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def parse_facts(output: str) -> list[dict]:
    """Pull the facts list out of the model's reply. Raises ValueError if there is no usable JSON."""
    m = re.search(r"\{.*\}", output, re.S)
    data = json.loads(m.group(0)) if m else None
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise ValueError("extractor did not return {\"facts\": [...]}")
    return [f for f in data["facts"] if isinstance(f, dict)][:MAX_FACTS_PER_SOURCE]


def extract_facts(source: dict, company: str, role: str, level: str, use_cache: bool = True) -> tuple[list[dict], bool]:
    """LLM-extract candidate facts from one source's text (full page or snippet). Returns (facts, came_from_cache)."""
    text, text_kind = source_text(source)
    provider, model = llm.get_settings("extract")
    model_id = f"{provider}/{model}"
    path = _cache_path(source["url"], company, role, level, text_kind, model_id, text)
    if use_cache and os.path.exists(path):
        with open(path) as f:
            return json.load(f)["facts"], True

    user = (
        f"Company: {company}\nRole: {role}\nCandidate level: {level}\n"
        f"URL: {source['url']}\nPublished: {source.get('published_date') or 'unknown'}\n\n"
        f"Page text{' (search snippet only)' if text_kind == 'snippet' else ''}:\n{text}"
    )
    facts = parse_facts(llm.generate(EXTRACT_SYSTEM_PROMPT, user, task="extract"))
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:  # cached even when caching is off for reading, so the next normal run benefits
        json.dump({"url": source["url"], "prompt_version": PROMPT_VERSION, "model": model_id, "facts": facts}, f, indent=2)
    return facts, False


# ---------- grounding (code only) ----------

_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "‑": "-"})


def normalize(text: str) -> str:
    """Lowercase, unify quotes/dashes/ellipses, drop link URLs, punctuation and markdown, collapse whitespace."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(text.split())


def add_hedge(sentence: str) -> str:
    """Prefix 'Reportedly,' so an unconfirmed claim stays unconfirmed. Lowercases only common starter words."""
    head, _, rest = sentence.partition(" ")
    if head in LOWERABLE_STARTERS and rest:
        return f"Reportedly, {head.lower()} {rest}"
    return f"Reportedly, {sentence}"


def publisher_label(url: str) -> str:
    """The site's own name from its host: 'www.extern.com' -> 'extern', 'prep.co.uk' -> 'prep'."""
    parts = (urlparse(url).hostname or "").lower().removeprefix("www.").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""


def about_publisher_not_company(fact: str, src: dict, company: str | None) -> str | None:
    """The publisher's brand name (e.g. 'Extern') appears in the fact but the company's doesn't: the fact
    describes the site's own offering. Returns that brand name, or None."""
    if not company or src.get("kind") == "company":
        return None
    label = publisher_label(src["url"])
    slug = re.sub(r"[^a-z0-9]", "", company.lower())
    if len(label) < 4 or slug in label or slug in re.sub(r"[^a-z0-9]", "", fact.lower()):
        return None
    return label if re.search(rf"\b{re.escape(label)}\b", fact, re.I) else None


def ground(candidates: list[dict], sources: dict, already: list[dict] | None = None, company: str | None = None) -> tuple[list[dict], list[dict]]:
    """Keep candidates whose quote is really in their source. Returns (grounded, rejected).

    Each candidate must carry `source_id`. Grounded facts get `published_date` from the source, `grounded_in`
    ("full" or "snippet"), `stage` and `level` (interview facts), `hedged` (the source title is hedged, so the
    fact text is made to be too), and `is_experience`: an interview fact from a first-person source (Reddit,
    LeetCode discuss, a blog). Role or company facts from those sites (a job repost) are not experiences.
    Facts about the publishing site or another company are rejected as wrong-subject.
    """
    grounded: list[dict] = []
    rejected: list[dict] = []
    seen = {normalize(f["fact"]) for f in (already or [])}

    def reject(c: dict, code: str) -> None:
        rejected.append({**c, "reason": code})

    for c in candidates:
        fact, quote, section = c.get("fact"), c.get("exact_quote"), c.get("section")
        if not isinstance(fact, str) or not isinstance(quote, str) or section not in SECTIONS:
            reject(c, "malformed")
            continue
        src = sources[c["source_id"]]
        text, text_kind = source_text(src)
        nquote = normalize(quote)
        if len(nquote.split()) < MIN_QUOTE_WORDS:
            reject(c, "quote-too-short")
        elif nquote not in normalize(text):
            reject(c, "quote-not-found")
        elif c.get("about_company") is False:
            reject(c, "wrong-subject")
        elif (brand := about_publisher_not_company(fact, src, company)):
            reject({**c, "detail": f"describes {brand}'s own offering"}, "wrong-subject")
        elif any(n not in NUMBER_RE.findall(quote) for n in NUMBER_RE.findall(fact)):
            reject(c, "number-not-in-quote")
        elif TIME_SENSITIVE_RE.search(fact) and not src.get("published_date"):
            reject(c, "undated-time-sensitive")
        elif normalize(fact) in seen:
            reject(c, "duplicate")
        else:
            seen.add(normalize(fact))
            fact = fact.strip()
            hedged = bool(HEDGED_TITLE_RE.search(src.get("title", "")))
            if hedged and not HEDGE_WORD_RE.search(fact):
                fact = add_hedge(fact)
            stage = c.get("stage") if c.get("stage") in STAGES else "unknown"
            level = c.get("level") if c.get("level") in LEVELS else "unknown"
            grounded.append({
                "fact": fact,
                "exact_quote": quote.strip(),
                "section": section,
                "is_experience": section == "interview" and src["kind"] in EXPERIENCE_KINDS,
                "source_id": c["source_id"],
                "published_date": src.get("published_date"),
                "grounded_in": text_kind,
                "stage": stage if section == "interview" else "",
                "level": level if section == "interview" else "",
                "hedged": hedged,
                "source_type": src.get("type", ""),
                "official": bool(src.get("official")),
            })
    return grounded, rejected


# ---------- the posting's location (code only) ----------

def location_fact(sid: str, source: dict, location: str | None) -> dict | None:
    """A grounded role fact saying where the posting's job is, taken verbatim from the page (or None).

    Built directly from the text, so it is grounded by construction. `pinned` keeps it out of the writer's fact cap.
    """
    from search import posting_location

    found = posting_location(source, location)
    if not found or normalize(found["quote"]) not in normalize(source["full_text"]):
        return None
    place = found["place"]
    where = " (outside " + location + ")" if location and found["class"] == "other" else ""
    return {
        "fact": f"Posting location: {place}{where}.", "exact_quote": found["quote"], "section": "role",
        "is_experience": False, "source_id": sid, "published_date": source.get("published_date"),
        "grounded_in": "full", "stage": "", "level": "", "hedged": False, "pinned": True,
        "source_type": source.get("type", ""), "official": bool(source.get("official")),
    }
