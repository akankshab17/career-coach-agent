"""How useful a grounded fact is to the writer: specificity, near-duplicate merging, product/tech news first.

All code heuristics (no LLM). Used by brief.select_for_writing. The Amazon brief showed why: page order + round-robin
gave the writer "Amazon seeks self-motivated individuals" and "Engineering roles use powerful technologies" while
"LRU Cache variation and a Top K problem", "Course Schedule II" and the official posting's degree requirement stayed out.
"""
from __future__ import annotations

import re

from facts import normalize

# Named algorithm / design / interview topics. Each hit is evidence of a concrete, preparable fact.
TOPIC_RE = re.compile(
    r"\b(?:lru|lfu|cache|heap|priority queue|hash ?map|hash ?table|linked list|binary search|bfs|dfs|breadth[- ]first|"
    r"depth[- ]first|graph|tree|trie|dynamic programming|\bdp\b|two pointers?|sliding window|top[- ]?k|sorting|recursion|"
    r"backtracking|topological|union[- ]find|intervals?|matrix|substring|subarray|stack|queue|system design|"
    r"object[- ]oriented design|\bood\b|low[- ]level design|high[- ]level design|api design|concurrency|multithreading|"
    r"\bsql\b|leetcode|big[- ]o|time and space complexity|complexity|bar raiser|hirevue|codesignal|hackerrank|"
    r"java\b|python|c\+\+|javascript|typescript|golang|rust\b|kotlin|swift\b|react|microservices?|distributed systems?|"
    r"product sense|estimation|metrics|case study|a/b test)", re.I)
# Interview questions spelled out: "asked about a missed deadline", "design a pizza", "tell me about a time".
QUESTION_RE = re.compile(
    r"\b(?:questions? (?:about|on|like|such as)|asked (?:me |us |the candidate )?(?:to|about|how|why|what|whether)|"
    r"tell me about a time|design(?:ing)? an? \w+|implement(?:ing)? an? \w+|variation of)\b", re.I)
# Concrete requirements in a posting.
REQUIREMENT_RE = re.compile(
    r"\b(?:must|required?|requires|bachelor|master'?s|ph\.?d|degree|gpa|\d\+? years?|years? of|experience (?:with|in)|"
    r"proficien\w*|qualifications?|eligib\w*|authori[sz]ed to work|sponsorship|programming language|stem field)\b", re.I)
PROCESS_RE = re.compile(r"\b(?:online assessment|\boa\b|phone screen|onsite|on-site|virtual interview|loop|rounds?|"
                        r"behavio[u]?ral|leadership principles?|recruiter|offer)\b", re.I)
# Marketing and filler: true, cited, and useless for preparing.
GENERIC_RE = re.compile(
    r"\b(?:self-motivated|passionate|passion|powerful technologies|challenging and complex|dynamic career|impactful|"
    r"diverse perspectives|innovative ideas|world-class|cutting-edge|exciting|rewarding|thrive|make history|"
    r"best fits? (?:their|your)|review available roles|browse|talent community|personalized job recommendations|"
    r"collaborative teams?|millions of customers|transform(?:ing)? the future|dream up|resilient individuals|"
    r"flexibility to pursue)\b", re.I)
META_RE = re.compile(r"\b(?:the|this) (?:page|article|guide|post|site|blog|video) (?:includes|covers|lists|describes|offers|has)\b", re.I)
NUMBER_RE = re.compile(r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", re.I)
# Title-Case names inside a sentence ("Course Schedule II", "Count Inversions", "LRU Cache"); these words don't count.
NAME_RE = re.compile(r"(?<=[a-z,;:(] )(?:[A-Z][A-Za-z0-9+#]*|[0-9]+)(?:\s+(?:[A-Z][A-Za-z0-9+#]*|II|III|IV|K|[0-9]+))+")
NAME_STOPWORDS = {"online", "assessment", "leadership", "principles", "principle", "bar", "raiser", "software", "development",
                  "engineer", "engineers", "engineering", "sde", "new", "grad", "united", "states", "the", "a", "an",
                  "machine", "learning", "data", "science", "product", "manager", "university", "talent", "community"}


def specificity(f: dict, company: str = "") -> int:
    """Higher = more concrete. Named problems/topics/questions, numbers and requirements score; marketing and
    statements about the page itself cost."""
    text = f["fact"]
    company_words = {w.lower() for w in company.split()}
    score = min(len(TOPIC_RE.findall(text)), 3) * 2
    names = [n for n in NAME_RE.findall(text)
             if {w.lower() for w in n.split()} - NAME_STOPWORDS - company_words]
    score += min(len(names), 2) * 2
    score += 2 if QUESTION_RE.search(text) else 0
    score += 1 if NUMBER_RE.search(text) else 0
    if f["section"] == "role":
        score += min(len(REQUIREMENT_RE.findall(text)), 2) * 2
    if f["section"] == "interview":
        score += 1 if PROCESS_RE.search(text) else 0
    score -= 3 * len(GENERIC_RE.findall(text))
    score -= 3 if META_RE.search(text) else 0
    return score


# ---- company news: product/tech first for engineering roles ----

TECH_NEWS_RE = re.compile(
    r"\b(?:launch\w*|releas\w*|unveil\w*|introduc\w*|product|platform|model|\bai\b|artificial intelligence|agent\w*|cloud|"
    r"chips?|data cent(?:er|re)s?|software|api|developer|engineering|feature|service|device|robot\w*|open[- ]source|"
    r"infrastructure|research|technology|acquir\w*|acquisition)\b", re.I)
FINANCE_NEWS_RE = re.compile(
    r"\b(?:stock|shares?|earnings|revenue|profit|quarter(?:ly)?|valuation|valued|investors?|analysts?|market cap|"
    r"dividend|price target|wall street|guidance|fiscal|ipo|funding round|raised)\b", re.I)
TECH_FAMILIES = ("software", "data", "hardware", "security")


def news_rank(f: dict, family: str | None) -> int:
    """0 = product/tech news, 1 = other, 2 = market/finance news; only for engineering-type roles (else all 1)."""
    if family not in TECH_FAMILIES:
        return 1
    tech, money = bool(TECH_NEWS_RE.search(f["fact"])), bool(FINANCE_NEWS_RE.search(f["fact"]))
    return 0 if tech and not money else 2 if money and not tech else 1


# ---- near-duplicates ----

STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "is", "are", "be", "by", "its", "their",
             "that", "this", "as", "at", "can", "may", "will", "who", "which", "from", "it", "they", "people", "candidates"}
NEAR_DUPLICATE = 0.6  # word overlap (Jaccard) at which two facts in the same section make the same claim
MIN_WORDS = 4


def _words(fact: str, company: str) -> set[str]:
    drop = STOPWORDS | {w.lower() for w in company.split()}
    return {w.rstrip("s") for w in normalize(fact).split() if w not in drop}


def near_duplicate(a: dict, b: dict, company: str = "") -> bool:
    if a["section"] != b["section"] or a.get("pinned") or b.get("pinned"):
        return False
    if re.findall(r"\d+", a["fact"]) != re.findall(r"\d+", b["fact"]):
        return False  # "4 rounds" and "5 rounds" are different claims, however alike the words
    wa, wb = _words(a["fact"], company), _words(b["fact"], company)
    if len(wa) < MIN_WORDS or len(wb) < MIN_WORDS:
        return False
    return len(wa & wb) / len(wa | wb) >= NEAR_DUPLICATE


def merge_near_duplicates(facts: list[dict], better, company: str = "") -> list[dict]:
    """One fact per claim. Facts that say the same thing (same section, >= 60% word overlap) collapse into the one
    `better` ranks first (a sort key: lower is better), which gets `also_sources` (the others' source ids, so the brief cites
    them all). Order of first appearance is kept; untouched facts are returned as they are (same objects)."""
    groups: list[list[dict]] = []
    for f in facts:
        for g in groups:
            if any(near_duplicate(f, other, company) for other in g):
                g.append(f)
                break
        else:
            groups.append([f])
    out = []
    for g in groups:
        if len(g) == 1:
            out.append(g[0])
            continue
        best = min(g, key=better)
        extra = [o["source_id"] for o in g if o is not best and o["source_id"] != best["source_id"]]
        out.append({**best, "also_sources": list(dict.fromkeys(extra)), "merged": len(g)})
    return out
