"""Where is a job posting? Pure text heuristics (no LLM), so postings in the wrong country can be avoided.

A small place lexicon (countries + their well-known cities) is enough to tell "Wuhan, China" from "Seattle, WA".
When a posting names places in several countries, the country mentioned most wins.
"""
from __future__ import annotations

import re

DEFAULT_LOCATION = "United States"

# country -> aliases (the country's names and its well-known cities / regions). Lowercase.
PLACES: dict[str, tuple[str, ...]] = {
    "united states": (
        "united states", "usa", "u.s.a", "u.s.", "us-based", "remote - us", "remote, us", "new york", "san francisco",
        "south san francisco", "seattle", "mountain view", "sunnyvale", "palo alto", "menlo park", "los angeles", "san jose",
        "san diego", "austin", "chicago", "boston", "cambridge, ma", "atlanta", "denver", "dallas", "houston", "columbus, indiana",
        "indianapolis", "washington, dc", "portland", "pittsburgh", "raleigh", "durham", "phoenix", "minneapolis", "detroit",
        "nashville", "salt lake city", "kirkland", "bellevue", "redmond", "irvine", "california", "texas", "new jersey",
        "florida", "massachusetts", "virginia", "indiana", "ohio", "michigan", "illinois", "colorado", "georgia",
    ),
    "china": ("china", "prc", "wuhan", "beijing", "shanghai", "shenzhen", "hangzhou", "guangzhou", "chengdu", "xi'an", "nanjing", "suzhou"),
    "india": ("india", "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata"),
    "united kingdom": ("united kingdom", "u.k.", "england", "scotland", "london", "manchester", "birmingham", "edinburgh", "cambridge, uk"),
    "canada": ("canada", "toronto", "vancouver", "ontario", "montreal", "ottawa", "calgary", "waterloo", "british columbia"),
    "germany": ("germany", "berlin", "munich", "hamburg", "frankfurt", "stuttgart", "cologne"),
    "france": ("france", "paris", "lyon", "toulouse"),
    "ireland": ("ireland", "dublin", "cork"),
    "netherlands": ("netherlands", "amsterdam", "rotterdam", "eindhoven"),
    "spain": ("spain", "madrid", "barcelona"),
    "switzerland": ("switzerland", "zurich", "geneva"),
    "sweden": ("sweden", "stockholm"),
    "poland": ("poland", "warsaw", "krakow", "wroclaw"),
    "israel": ("israel", "tel aviv", "haifa"),
    "singapore": ("singapore",),
    "japan": ("japan", "tokyo", "osaka"),
    "south korea": ("south korea", "seoul"),
    "australia": ("australia", "sydney", "melbourne", "brisbane"),
    "brazil": ("brazil", "sao paulo", "são paulo"),
    "mexico": ("mexico", "mexico city", "guadalajara", "monterrey"),
    "uae": ("united arab emirates", "uae", "dubai", "abu dhabi"),
}
# What a user might type for a country, when it isn't the country's own name.
COUNTRY_SYNONYMS = {"us": "united states", "u.s.": "united states", "usa": "united states", "america": "united states",
                    "uk": "united kingdom", "great britain": "united kingdom", "britain": "united kingdom"}

_US_STATE_ABBREVS = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).replace(" ", "|")
US_CITY_STATE_RE = re.compile(rf"\b[A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+)*, (?:{_US_STATE_ABBREVS})\b")  # "Seattle, WA"
LOCATION_LINE_RE = re.compile(r"(?im)^[\s*#>_-]*(?:job |work |office )?locations?\b\**\s*[:|\-–]\s*\**\s*(.{3,100}?)\s*$")
SCAN_CHARS = 3000  # a posting states its location near the top; the footer boilerplate mentions other places

MATCH, UNKNOWN, OTHER = "match", "unknown", "other"
RANK = {MATCH: 0, UNKNOWN: 1, OTHER: 2}


def _alias_re(alias: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", re.I)


_COUNTRY_RES = {c: [_alias_re(a) for a in aliases] for c, aliases in PLACES.items()}


def canonical_country(location: str) -> str | None:
    """'USA' / 'United States' -> 'united states'. None if it isn't a known country (e.g. a city)."""
    loc = location.strip().lower()
    loc = COUNTRY_SYNONYMS.get(loc, loc)
    return loc if loc in PLACES else None


def country_hits(text: str) -> dict[str, int]:
    """How many place aliases of each country appear in `text`."""
    hits = {}
    for country, patterns in _COUNTRY_RES.items():
        n = sum(len(p.findall(text)) for p in patterns)
        if country == "united states":
            n += len(US_CITY_STATE_RE.findall(text))
        if n:
            hits[country] = n
    return hits


def classify_location(text: str, wanted: str | None) -> str:
    """'match' if the text places the job in `wanted`, 'other' if mainly somewhere else, else 'unknown'.

    A "Location: ..." line, when present, is trusted over the rest of the text.
    """
    if not wanted:
        return UNKNOWN
    line = LOCATION_LINE_RE.search(text[:SCAN_CHARS * 3])
    scope = line.group(1) if line else text[:SCAN_CHARS]
    hits = country_hits(scope)
    if not hits and line:
        hits = country_hits(text[:SCAN_CHARS])  # a location line we don't recognise: fall back to the top of the page
    want = canonical_country(wanted)
    if want is None:  # a city or free text, e.g. "Austin, TX"
        if _alias_re(wanted.strip().lower()).search(scope.lower()):
            return MATCH
        return OTHER if hits and "united states" not in hits else UNKNOWN
    if not hits:
        return UNKNOWN
    top = max(hits.values())
    return MATCH if hits.get(want, 0) == top else OTHER


def find_location(text: str, wanted: str | None) -> dict | None:
    """Where the posting says the job is: {place, quote, class} or None. `quote` is verbatim from `text`."""
    line = LOCATION_LINE_RE.search(text[:SCAN_CHARS * 3])
    if line:
        quote = line.group(1).strip(" *_|")
        place = quote
    else:
        head = text[:SCAN_CHARS]
        hits = country_hits(head)
        if not hits:
            return None
        country = max(hits, key=hits.get)
        found = [m for pat in _COUNTRY_RES[country] for m in [pat.search(head)] if m]
        if country == "united states" and (city_state := US_CITY_STATE_RE.search(head)):
            found.append(city_state)
        m = min(found, key=lambda x: x.start())  # the earliest place the page names in that country
        tail = re.match(r"\s*,\s*[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?", head[m.end():])  # "Wuhan" -> "Wuhan, China"
        quote = head[m.start(): m.end() + (tail.end() if tail else 0)]
        place = quote if country in quote.lower() or quote.lower() == country else f"{quote}, {country.title()}"
    return {"place": place, "quote": quote, "class": classify_location(text, wanted)}
