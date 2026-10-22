"""Write the brief from grounded facts only, then check and render it in code."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import guidance
from facts import HEDGE_WORD_RE, LOWERABLE_STARTERS, NUMBER_RE, REJECT_REASONS, add_hedge
from ranking import merge_near_duplicates, news_rank, specificity
from search import TYPE_LABEL, canonical_url, is_blocked, is_careers_page, prefers, role_family, source_kind, source_type

JD_URL = "jd://user-provided"
LEVEL_LABEL = {"intern": "intern", "new-grad": "new grad", "experienced": "experienced"}
KEY_SECTIONS = ("interview", "role")  # TL;DR is built from these; its coverage = the interview count
DERIVED_SECTIONS = ("how to prepare", "questions to ask")  # may draw conclusions, but must cite facts
PLACEHOLDER = "No grounded information found for this section."
TIMELINE_MAX = 4  # application-calendar facts sent to the writer
NO_INTERVIEW = "No interview reports found."
MIN_INTERVIEW_FACTS = 3  # fewer than this and the TL;DR opens with a "Limited interview data" warning
LEVEL_PROCESS = {"intern": "internship process", "new-grad": "new-grad process", "experienced": "full-time process"}
LEVEL_ROLE = {"intern": "internship role", "new-grad": "new-grad role", "experienced": "experienced role"}  # role-fact labels
FORUM_RESERVE = 3  # interview slots kept for candidates' own reports (forum/blog) when there are any
STAGE_ORDER = ("oa", "phone-screen", "team-screen", "onsite", "unknown")
STAGE_LABEL = {"oa": "Online assessment", "phone-screen": "Phone screen", "team-screen": "Team screen",
               "onsite": "Onsite", "unknown": "Stage not specified"}
COMPANY_MAX_SENTENCES = 5
MAX_WRITER_FACTS = 30  # fewer facts = faster, cheaper, and a tighter brief
COMPANY_FACTS_FOR_WRITER = 10  # at most this many company facts reach the writer...
COMPANY_FACTS_PER_SOURCE = 3  # ...and at most this many from one article (one news event)
COMPANY_RESERVE = 5  # company facts are kept even when interview + role facts would fill the budget

SECTION3 = "## 3. What the role really needs"
SECTION3_INFERRED = "## 3. What the role really needs (Inferred, not from a JD)"


# ---------- sources ----------

def add_sources(sources: dict[str, dict], results: list[dict], group: str, company: str) -> list[str]:
    """Add results under new ids S<n>, skipping pages already known (same URL, or the same page in another language:
    an English variant replaces a known non-English one in place, keeping its id and group). Returns the new ids."""
    known = {canonical_url(s["url"]): sid for sid, s in sources.items()}
    new_ids = []
    for r in results:
        if is_blocked(r["url"]):
            continue
        key = canonical_url(r["url"])
        if key in known:
            old = sources[known[key]]
            if prefers(r["url"], old["url"]) and "full_text" not in old:  # not yet fetched: switch to the English page
                old.update({k: r[k] for k in ("url", "title", "content", "published_date") if k in r})
            continue
        known[key] = f"S{len(sources) + 1}"
        sid = f"S{len(sources) + 1}"
        if r["url"] == JD_URL:  # the JD the user pasted is as official as it gets
            kind, stype, official = "jd", "official-posting", True
        else:
            kind = source_kind(r["url"], company)
            stype = source_type(r["url"], company, group, r.get("published_date"))
            official = stype == "official-posting" or is_careers_page(r["url"], company, group)
        sources[sid] = {**r, "kind": kind, "group": group, "type": stype, "official": official}
        new_ids.append(sid)
    return new_ids


def collect_sources(research: dict[str, list[dict]], company: str, jd: str | None = None) -> dict[str, dict]:
    """Number every search result S1, S2, ... (deduped by URL). The JD, if given, is a source too."""
    sources: dict[str, dict] = {}
    for group, results in research.items():
        if group not in ("filtered", "forum_counts"):  # notes for the run log, not sources
            add_sources(sources, results, group, company)
    if jd:
        add_sources(sources, [{"title": "Job description provided by you", "url": JD_URL, "content": jd,
                               "published_date": None, "full_text": jd}], "jd", company)
    return sources


# ---------- run stats + debug report ----------

@dataclass
class Drop:
    section: str
    sentence: str
    code: str
    detail: str = ""


REWRITE_KINDS = {
    "candidates-report": "forum fact rewritten to start with 'Candidates report'",
    "hedge-added": "fact from a hedged source ('may', 'reportedly'...) given its hedge back",
    "stage-split": "sentence mixed facts from different interview stages; split into one sentence per fact",
    "stage-trim": "TL;DR bullet mixed interview stages; trimmed to its first fact",
    "level-label": "fact from a different seniority level's process; labelled as such",
    "source-label": "section 3 has no official posting text, so each fact is labelled with its source type",
}


@dataclass
class Rewrite:
    section: str
    before: str
    after: str
    kind: str = "candidates-report"


DROP_REASONS = {
    "uncited": "sentence cites no grounded fact",
    "unknown-fact": "cites only fact ids that don't exist",
    "number-not-in-facts": "contains a number that isn't in the facts it cites",
    "tldr-interview-without-interview-fact": "TL;DR interview claim not backed by an interview fact",
    "company-section-cap": f"company section already has {COMPANY_MAX_SENTENCES} sentences",
    "question-logistics-or-rumor": "question about interview logistics, or based on a rumor/unconfirmed fact",
    "derived-cites-company-news": "'How to prepare' and 'Questions to ask' may cite role or interview facts only, not company news",
    "derived-cites-timeline": "'How to prepare' and 'Questions to ask' may cite role or interview facts only, not application dates",
    "tldr-timeline": "application timelines and deadlines are not interview content and don't belong in the TL;DR",
}


@dataclass
class RunStats:
    steps: list[str] = field(default_factory=list)  # one line per graph step, as printed
    sources_total: int = 0
    sources_with_text: int = 0
    facts_extracted: int = 0
    facts_grounded: int = 0
    rejected: list[dict] = field(default_factory=list)
    extract_errors: list[tuple[str, str]] = field(default_factory=list)
    retry_sections: list[str] = field(default_factory=list)
    per_source: dict[str, dict] = field(default_factory=dict)  # sid -> url, text kind, extracted/grounded/rejected
    grounded: list[dict] = field(default_factory=list)
    filtered: list[dict] = field(default_factory=list)  # irrelevant interview results: {url, title, reason}
    excluded: list[dict] = field(default_factory=list)  # fetched postings not used (e.g. require another country): {url, title, reason}
    draft: str = ""  # the writer's raw output, before any checking
    draft_truncated: bool = False
    selected: int = 0
    checked: int = 0
    drops: list[Drop] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)
    general_sections: list[str] = field(default_factory=list)  # sections filled with code-written general guidance
    # progress callback (the web app's worker): on_progress(step, None) when a step starts, (None, line) for each log line
    on_progress: Callable[[str | None, str | None], None] | None = field(default=None, repr=False, compare=False)

    def pipeline_line(self) -> str:
        retry = f"yes ({', '.join(self.retry_sections)})" if self.retry_sections else "no"
        return (
            f"[pipeline] sources={self.sources_total} with_text={self.sources_with_text} "
            f"extracted={self.facts_extracted} grounded={self.facts_grounded} rejected={len(self.rejected)} retry={retry}"
        )

    @property
    def contributing_sources(self) -> int:
        """Sources that gave at least one grounded fact (full pages and snippets alike)."""
        return len({f["source_id"] for f in self.grounded})

    def writing_breakdown(self) -> str:
        counts = Counter(d.code for d in self.drops)
        parts = ", ".join(f"{counts[c]} {c}" for c in DROP_REASONS if counts[c])
        text = f"{self.checked} sentences checked, {len(self.drops)} dropped" + (f" ({parts})" if parts else "")
        if self.rewrites:
            kinds = Counter(r.kind for r in self.rewrites)
            text += f", {len(self.rewrites)} rewritten (" + ", ".join(f"{n} {k}" for k, n in kinds.items()) + ")"
        return text

    def log_line(self) -> str:
        return f"[finalize] {self.writing_breakdown()}"

    def debug_report(self, sources: dict | None = None) -> str:
        """Markdown: step counts, every rejected fact, every dropped/rewritten sentence."""
        def url(sid):
            return (sources or {}).get(sid, {}).get("url", sid)

        lines = ["# Run report", "", *[f"- `{s}`" for s in self.steps], ""]  # steps already end with finalize + pipeline

        if self.per_source:
            lines += ["# Facts per source", "", "| source | text | chars | extracted | grounded (by section) | rejected | error |",
                      "|---|---|---|---|---|---|---|"]
            for sid, row in self.per_source.items():
                by_section = ", ".join(f"{sec} {n}" for sec, n in row["grounded"].items()) or "0"
                lines.append(f"| {sid} {row['url']} | {row['text']} | {row['chars']} | {row['extracted']} | "
                             f"{by_section} | {row['rejected']} | {row['error']} |")
            lines.append("")

        if self.filtered:
            lines += [f"# Filtered interview sources ({len(self.filtered)})", ""]
            lines += [f"- [{f['reason']}] {f['title']} — {f['url']}" for f in self.filtered] + [""]
        if self.general_sections:
            lines += [f"# General guidance used ({len(self.general_sections)})", "",
                      "No interview or role facts reached the writer, so these sections hold code-written general guidance "
                      "for the role family (labelled in the brief):", ""]
            lines += [f"- {s}" for s in self.general_sections] + [""]
        if self.excluded:
            lines += [f"# Excluded postings ({len(self.excluded)})", ""]
            lines += [f"- {e['title']} — {e['url']}: {e['reason']}" for e in self.excluded] + [""]

        if self.grounded:
            lines += ["# Grounded facts", ""]
            for f in self.grounded:
                tag = " [snippet-grounded]" if f.get("grounded_in") == "snippet" else ""
                tag += " [hedged source]" if f.get("hedged") else ""
                where = f"{f['section']}/{f['stage']}" if f.get("stage") else f["section"]
                kind = f" [{f['source_type']}]" if f.get("source_type") else ""
                lines.append(f"- ({where}, {f['source_id']}{kind}){tag} {f['fact']}")
            lines.append("")

        lines += ["# Rejected facts (grounding)", ""]
        if not self.rejected:
            lines.append("None.")
        for n, r in enumerate(self.rejected, 1):
            lines += [
                f"## {n}. [{r['reason']}] {REJECT_REASONS.get(r['reason'], '')}",
                f"- Fact: {r.get('fact')}",
                f"- Quote: \"{r.get('exact_quote')}\"",
                f"- Section: {r.get('section')} | Source: {url(r.get('source_id'))}" + (f" | {r['detail']}" if r.get("detail") else ""),
                "",
            ]
        if self.extract_errors:
            lines += ["# Extraction errors", ""] + [f"- {u}: {e}" for u, e in self.extract_errors] + [""]

        lines += ["# Dropped sentences (writing)", ""]
        if not self.drops:
            lines.append("None.")
        for n, d in enumerate(self.drops, 1):
            lines += [f"## {n}. [{d.code}] in \"{d.section}\"", f"> {d.sentence}", "",
                      f"- Reason: {DROP_REASONS[d.code]}" + (f": {d.detail}" if d.detail else ""), ""]
        if self.rewrites:
            lines += ["", "# Rewritten sentences (kept)", ""]
            for r in self.rewrites:
                lines += [f"- [{r.kind}] in \"{r.section}\"", f"  - Before: {r.before}", f"  - After: {r.after}"]
        if self.draft:
            lines += ["", "# Writer draft (raw, before checking)", "",
                      f"Draft cut off by the token limit: {'YES' if self.draft_truncated else 'no'}", "", "```", self.draft.strip(), "```"]
        return "\n".join(lines) + "\n"


@dataclass
class BriefResult:
    text: str
    stats: RunStats
    sources: dict = field(default_factory=dict)


# ---------- writing ----------

SYSTEM_PROMPT = """You write interview-prep briefs for students from a list of verified facts.
Rules:
- Use ONLY the numbered facts given. No outside knowledge. Do not add numbers, names, durations or
  details that the fact doesn't contain.
- One fact per sentence, except: combine closely related facts about the same event into one sentence
  citing all of them. Every sentence ends with the fact id(s) it states, like [F3] or [F3][F7].
- Never write "the acquisition", "the deal", "the company" etc. without naming what it is in that sentence.
- Facts marked CANDIDATE-REPORT are first-person interview experiences (Reddit, LeetCode, blogs): start those
  sentences with "Candidates report". Other facts are stated plainly.
- Facts marked LEVEL-MISMATCH describe a different seniority level than the candidate's (another level's interview
  process, or a role/posting for another level). Prefer other facts; if you use one, end the sentence with the label shown
  on the fact, e.g. "(full-time process)" or "(internship role)".
- Facts marked HEDGED come from a source that is unconfirmed ("may", "reportedly"). Keep the hedge in your
  sentence (e.g. "Reportedly, ..."); never state them as completed or certain.
- Timeline facts (application dates, deadlines, decision timing) are NOT interview content: they go only under the
  "Timeline" sub-heading of section 2 and never in the TL;DR.
- Facts show their source type (official-posting, company-site, third-party-guide, job-aggregator, forum/blog,
  news). Company career pages and postings are the most reliable for the process and the role.
- Interview facts carry a stage (oa, phone-screen, team-screen, onsite, unknown). NEVER combine facts from
  different stages in one sentence, anywhere in the brief.
- For company news with a published date, add "(reported Mon YYYY)" from that date.
- If two facts conflict, use the newer one and say so, citing both.
- "How to prepare" and "Questions to ask" may draw conclusions from the facts, but every item must cite
  the fact(s) it is based on, and those must be ROLE or INTERVIEW facts. Never base them on company news.
- Never omit a section. If a section has no facts, write exactly: "No grounded information found for this section."
- Output markdown with exactly the headings requested, in order."""


def _round_robin_by_source(facts: list[dict], per_source: int | None = None) -> list[dict]:
    """Interleave facts across sources (each source's own order kept) so one long page can't eat the budget."""
    queues: dict[str, list[dict]] = {}
    for f in facts:
        queues.setdefault(f["source_id"], []).append(f)
    out, rnd = [], 0
    while True:
        batch = [q[rnd] for q in queues.values() if rnd < len(q) and (per_source is None or rnd < per_source)]
        if not batch:
            return out
        out += batch
        rnd += 1


def _ranked(facts: list[dict], per_source: int | None = None) -> list[dict]:
    """Facts from official sources (the company's postings and career pages) first, each half interleaved across sources."""
    official = [f for f in facts if f.get("official")]
    others = [f for f in facts if not f.get("official")]
    return _round_robin_by_source(official, per_source) + _round_robin_by_source(others, per_source)


SPECIFIC = 3  # specificity at or above this: a named problem, topic, question or requirement
ROLE_TIER = {"official-posting": 0, "company-site": 1, "third-party-guide": 2, "job-aggregator": 3, "forum/blog": 4, "news": 5}


def role_tier(f: dict) -> int:
    """0 = the official posting / JD, 1 = the company's own site, 2+ = everything else (no type known: by `official`)."""
    t = f.get("source_type")
    return ROLE_TIER[t] if t in ROLE_TIER else (1 if f.get("official") else 2)


def _by_specificity(facts: list[dict], score) -> list[dict]:
    """Bands (specific, neutral, generic), each interleaved across sources with every source's facts best-first:
    a named LeetCode problem beats "the interview assesses problem solving", wherever it sits on its page."""
    bands = ([f for f in facts if score(f) >= SPECIFIC], [f for f in facts if 0 <= score(f) < SPECIFIC],
             [f for f in facts if score(f) < 0])
    return [f for band in bands for f in _ranked(sorted(band, key=lambda f: -score(f)))]  # sorted() is stable


def _role_order(facts: list[dict], score) -> list[dict]:
    """The official posting (or the JD) first, then the company's site, guides, aggregators...; within a tier the most
    specific first. Marketing lines (negative score) go last whatever their source."""
    useful = [f for f in facts if score(f) >= 0]
    out = []
    for tier in sorted({role_tier(f) for f in useful}):
        in_tier = [f for f in useful if role_tier(f) == tier]
        out += _round_robin_by_source(sorted(in_tier, key=lambda f: -score(f)))
    return out + sorted((f for f in facts if score(f) < 0), key=lambda f: -score(f))


def select_for_writing(facts: list[dict], limit: int = MAX_WRITER_FACTS, level: str | None = None, role: str | None = None,
                       company: str = "") -> list[dict]:
    """At most `limit` facts for the writer, the most useful ones:
    - near-duplicates (the same claim from several sources) become one fact citing all of them;
    - interview facts: the candidate's level first, then by specificity (named problems, topics, questions, numbers beat
      generic statements), at least FORUM_RESERVE candidate reports when there are any;
    - role facts: the candidate's level first, then the official posting / JD, then the company's site, ..., most specific
      first, marketing last;
    - company facts: never fewer than COMPANY_RESERVE; for engineering roles product/tech news before market news.
    Pinned facts (the posting's location) are always kept. Returned in first-appearance order, so fact ids are stable."""
    family = role_family(role) if role else None
    scores: dict[int, int] = {}

    def score(f: dict) -> int:
        if id(f) not in scores:
            scores[id(f)] = specificity(f, company)
        return scores[id(f)]

    merged = merge_near_duplicates(facts, better=lambda f: (role_tier(f), -score(f)), company=company)
    pinned = [f for f in merged if f.get("pinned")]
    pool = [f for f in merged if not f.get("pinned")]
    timeline = _ranked([f for f in pool if f["section"] == "timeline"])[:TIMELINE_MAX]
    limit -= len(pinned) + len(timeline)
    company_facts = sorted((f for f in pool if f["section"] == "company"), key=lambda f: news_rank(f, family))
    company_facts = _ranked(company_facts, COMPANY_FACTS_PER_SOURCE)
    company_facts = sorted(company_facts, key=lambda f: news_rank(f, family))  # interleaved, but product/tech news first

    def level_first(section: str, order) -> list[dict]:  # the candidate's level (or unknown) first; other levels only to fill
        facts_ = [f for f in pool if f["section"] == section]
        fits = [f for f in facts_ if not level_mismatch(f, level)]
        return order(fits, score) + order([f for f in facts_ if f not in fits], score)

    interview, role_facts = level_first("interview", _by_specificity), level_first("role", _role_order)

    reserve = min(COMPANY_RESERVE, len(company_facts))
    room = limit - reserve
    interview_quota = min(len(interview), -(-room * 3 // 5))  # ~60% of the non-company room
    role_quota = min(len(role_facts), room - interview_quota)
    interview_quota = min(len(interview), room - role_quota)  # unused role room flows to interview
    picked = interview[:interview_quota]
    reports = [f for f in interview if f.get("is_experience")][:min(FORUM_RESERVE, interview_quota)]
    if reports and sum(f.get("is_experience", False) for f in picked) < len(reports):
        # official pages rank first and can fill every interview slot; candidates' own reports keep FORUM_RESERVE of them
        rest = [f for f in interview if f not in reports]
        picked = reports + rest[:interview_quota - len(reports)]
    chosen = picked + role_facts[:role_quota]
    company_room = min(COMPANY_FACTS_FOR_WRITER, limit - len(chosen))
    chosen += company_facts[:max(company_room, reserve)]
    keep = {id(f) for f in chosen} | {id(f) for f in pinned} | {id(f) for f in timeline}
    return [f for f in merged if id(f) in keep]


def fact_ids(facts: list[dict]) -> dict[str, dict]:
    return {f"F{i}": f for i, f in enumerate(facts, 1)}


def level_mismatch(f: dict, level: str | None) -> bool:
    """An interview or role fact about a different seniority level than the candidate's (another level's process, an
    intern program's duties in a new-grad brief, a manager posting's requirements)."""
    return bool(level) and f["section"] in ("interview", "role") and f.get("level") in LEVEL_PROCESS and f["level"] != level


def level_label(f: dict) -> str:
    """'full-time process' for an interview fact, 'experienced role' for a role fact."""
    return (LEVEL_ROLE if f["section"] == "role" else LEVEL_PROCESS)[f["level"]]


def _fact_line(fid: str, f: dict, level: str | None = None) -> str:
    tags = [f"source {'+'.join([f['source_id'], *f.get('also_sources', [])])}" + (f" {f['source_type']}" if f.get("source_type") else "")]
    if f.get("stage"):
        tags.append(f"stage {f['stage']}")
    if f.get("level") in LEVEL_PROCESS:
        tags.append(f"level {f['level']}")
    if level_mismatch(f, level):
        tags.append(f"LEVEL-MISMATCH, label \"({level_label(f)})\"")
    if f.get("is_experience"):
        tags.append("CANDIDATE-REPORT")
    if f.get("hedged"):
        tags.append("HEDGED")
    if f.get("published_date"):
        tags.append(f"published {f['published_date']}")
    return f"[{fid}] ({', '.join(tags)}) {f['fact']}  | quote: \"{f['exact_quote']}\""


def has_role_document(sources: dict) -> bool:
    """True if section 3 can rest on official text: the user's JD, the company's own posting, or its careers/program page.
    A job aggregator or a third-party guide doesn't count."""
    return any(s.get("official") and s.get("full_text") for s in sources.values())


def build_writer_prompt(company, role, level, facts: list[dict], sources: dict, today: date) -> tuple[str, str]:
    by_id = fact_ids(facts)
    blocks = []
    for section in ("company", "interview", "timeline", "role"):
        if section == "interview":  # grouped by stage, in interview order
            lines = [_fact_line(fid, f, level) for stage in STAGE_ORDER for fid, f in by_id.items()
                     if f["section"] == "interview" and f["stage"] == stage]
        else:
            lines = [_fact_line(fid, f, level) for fid, f in by_id.items() if f["section"] == section]
        blocks.append(f"### {section} facts\n" + ("\n".join(lines) if lines else "(none)"))
    stage_labels = ", ".join(f'"{STAGE_LABEL[s]}"' for s in STAGE_ORDER)
    s3 = SECTION3 if has_role_document(sources) else SECTION3_INFERRED
    label = LEVEL_LABEL[level]
    n_interview = sum(f["section"] == "interview" for f in facts)
    if n_interview >= MIN_INTERVIEW_FACTS:
        tldr_rule = ("exactly 3 bullets, ONLY from interview facts, no company news, no timeline/deadline facts: 1) FORMAT: how the interview is "
                     "structured, from facts of ONE stage (the most informative one); 2) KEY TOPICS: what is asked, "
                     f"from facts of ONE stage; 3) STANDOUT INSIGHT: one surprising or useful fact a {label} candidate "
                     "should know. A bullet never mixes stages.")
    elif n_interview:
        tldr_rule = (f"there are only {n_interview} interview fact(s). The system adds a first bullet saying interview data is "
                     "limited; do NOT write one. Write at most 2 bullets, ONLY from the interview facts, one stage per bullet, never timeline facts.")
    else:
        tldr_rule = ("there are NO interview facts. The system adds the first bullets itself; do NOT write them. Write at most "
                     "2 bullets from role facts about what the role needs. Never describe interview stages or format, and never use timeline facts.")
    facts_text = "\n\n".join(blocks)
    user = f"""Today's date: {today.isoformat()}
Company: {company}
Role: {role}
Candidate level: {label}

Facts:
{facts_text}

Write the brief with these headings exactly, in this order:
## TL;DR
({tldr_rule})
## 1. Company overview + recent news
(at most {COMPANY_MAX_SENTENCES} sentences; at most 1-2 sentences per news event; merge related facts, e.g.
"Stripe agreed to acquire AI gateway OpenRouter for over $7 billion (reported Aug 2026) [F9][F10].")
## 2. Interview process + common questions
(group by stage: for each stage that has facts, in this order, write a bold label line, then one bullet per
fact or per small group of facts from THAT stage only. Labels in order: {stage_labels}. Skip stages with no facts.
Then, if there are timeline facts, a final bold label line "**Timeline**" followed by one bullet per timeline fact.)
{s3}
(role facts; start with the "Posting location" fact if there is one. If none of the role facts come from an official
source, say which source type each statement rests on, e.g. "(job aggregator)")
## 4. How to prepare
(4-6 numbered practice items, each derived from ROLE or INTERVIEW facts and citing them; no company news)
## 5. Questions to ask the interviewer
(3-5 numbered questions tailored to a {label} candidate about the team, the work, learning and growth, and the
role, each citing a ROLE or INTERVIEW fact; no company news. NEVER ask about interview logistics (rounds,
scheduling, timeline, next steps), rumors, or unconfirmed/HEDGED news.)"""
    return SYSTEM_PROMPT, user


# ---------- finalize (code only) ----------

# The writer's citation style varies by model: [F1], [F1, F2], [F1-F3], (F1), 【F1】, or a bare "F1" at the end of a sentence.
_ID = r"F\s?\d+"
_ITEM = rf"{_ID}(?:\s*[-–—]\s*F?\s?\d+)?"  # F3 or F1-F3
_SEP = r"(?:\s*[,;&、，]\s*|\s+and\s+|\s+)"
CITE_GROUP_RE = re.compile(rf"[\[(（【〔]\s*({_ITEM}(?:{_SEP}{_ITEM})*)\s*[\])）】〕]", re.I)
BARE_CITE_RE = re.compile(rf"(?<![\w\[])F\d+(?:(?:\s*[,;]\s*|\s+and\s+|\s+)F\d+)*(?=[ \t]*(?:[.!?]|$))", re.M)
MAX_RANGE = 60


def _expand(items: str) -> list[int]:
    nums: list[int] = []
    for item in re.findall(_ITEM, items, re.I):
        found = [int(n) for n in re.findall(r"\d+", item)]
        if len(found) == 2 and found[0] <= found[1] <= found[0] + MAX_RANGE:
            nums += range(found[0], found[1] + 1)
        else:
            nums += found[:1]
    return nums


def normalize_citations(body: str, n_facts: int) -> str:
    """Rewrite every citation variant to canonical [F1][F2]. Bare ids ("... rounds F3.") are only
    converted when they are real fact ids, at the end of a sentence."""
    body = CITE_GROUP_RE.sub(lambda m: "".join(f"[F{n}]" for n in _expand(m.group(1))), body)
    # "... ecosystem. (reported Dec 2025) [F12]" -> "... ecosystem (reported Dec 2025) [F12]." A label after the full stop
    # left the sentence without its citation, and it was dropped as uncited.
    body = LABEL_AFTER_STOP_RE.sub(lambda m: f" {m.group(2)} {m.group(3).strip()}{m.group(1)}", body)
    body = LABEL_AFTER_CITES_RE.sub(lambda m: f" {m.group(3)} {m.group(1).strip()}{m.group(2)}", body)

    def bare(m: re.Match) -> str:
        nums = [int(n) for n in re.findall(r"\d+", m.group(0))]
        return "".join(f"[F{n}]" for n in nums) if all(1 <= n <= n_facts for n in nums) else m.group(0)

    return BARE_CITE_RE.sub(bare, body)


CITE_RE = re.compile(r"\[(F\d+)\]")
# A short parenthetical the writer put after the sentence's end mark: "... ecosystem. (reported Dec 2025) [F12]",
# "... day-to-day work? (based on team-screen AI questions) [F27]", "... engines [F3]. (reported Nov 2025)".
LABEL_AFTER_STOP_RE = re.compile(r"([.!?])[ \t]*(\([^()\n]{3,80}\))[ \t]*((?:\[F\d+\][ \t]*)+)")
LABEL_AFTER_CITES_RE = re.compile(r"((?:[ \t]*\[F\d+\])+)[ \t]*([.!?])[ \t]*(\([^()\n]{3,80}\))[ \t]*(?=\n|$)")
BOUNDARY_RE = re.compile(r"([.!?])((?:\s*\[F\d+\])*)(?=\s|$)")  # a sentence end, plus its trailing citations
# Words whose trailing "." does not end a sentence. Dotted forms (B.Tech, M.S., e.g., U.S., Ph.D.) are matched by pattern.
ABBREVIATIONS = {"inc", "corp", "co", "ltd", "llc", "plc", "vs", "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st",
                 "approx", "dept", "est", "fig", "no", "e.g", "i.e", "u.s", "u.k", "a.m", "p.m", "cf", "resp"}
DOTTED_RE = re.compile(r"(?:[A-Za-z]{1,3}\.)+[A-Za-z]{1,3}")  # M.S  e.g  U.S  Ph.D  B.Tech


def _is_abbreviation_dot(text: str, dot: int, end: int) -> bool:
    """Is the '.' at text[dot] part of an abbreviation rather than a sentence end?"""
    m = re.search(r"(\S+)$", text[:dot])
    token = (m.group(1) if m else "").lstrip("(\"'*[")
    if token.lower() in ABBREVIATIONS or DOTTED_RE.fullmatch(token) or re.fullmatch(r"[A-Z]", token):
        return True  # "Inc.", "M.S.", "e.g.", or an initial like "J."
    nxt = text[end:].lstrip()
    return bool(nxt) and nxt[0].islower()  # a new sentence doesn't start in lowercase


def split_sentences(text: str) -> list[str]:
    """Split on . ! ? (with any trailing [F1] citations kept), but not inside abbreviations like B.Tech, M.S., e.g., U.S., Inc."""
    out, start = [], 0
    for m in BOUNDARY_RE.finditer(text):
        if m.group(1) == "." and not m.group(2) and _is_abbreviation_dot(text, m.start(), m.end()):
            continue
        out.append(text[start:m.end()])
        start = m.end()
    out.append(text[start:])
    return [x.strip() for x in out if x.strip()]
LINE_PREFIX_RE = re.compile(r"^(\s*(?:[*\-]|\d+\.)\s+)?(.*)$", re.S)
NUMBERED_RE = re.compile(r"^(\s*)\d+\.(\s+)")
REPORTED_RE = re.compile(r"\(reported [^)]*\)", re.I)
ATTRIBUTED_RE = re.compile(r"\bcandidates?\b[^.]{0,30}\b(report|said|say|mention|describe|share|recall)|reported by candidates", re.I)
STAGE_LABEL_LINE_RE = re.compile(r"^\*\*[^*]+\*\*:?$")  # a bold stage label like "**Phone screen**"
LOGISTICS_RE = re.compile(r"\b(interviews?|rounds?|schedul\w*|timeline|next steps?|hear back|screens?|assessments?|onsite|on-site|how long)\b", re.I)


def fact_sentence(fid: str, f: dict) -> str:
    """A fact as its own cited sentence."""
    return f"{f['fact'].rstrip('. ')} [{fid}]."


LEVEL_LABEL_RE = re.compile(r"\((?:internship|intern|new[- ]grad|full-time|experienced|other[- ]level)[^)]*\)", re.I)


def add_level_label(sentence: str, label: str) -> str:
    """'... 4 rounds [F3].' -> '... 4 rounds (full-time process) [F3].' (label goes before the citations)."""
    m = re.match(r"^(.*?)([.!?]*)((?:\s*\[F\d+\])*)([.!?]*)\s*$", sentence, re.S)
    body, p1, cites, p2 = m.groups()
    return f"{body} ({label}){p1}{cites}{p2}"


def attribute_to_candidates(sentence: str) -> str:
    """Rewrite an unattributed forum sentence to start with 'Candidates report'. Keeps proper nouns intact."""
    head, _, rest = sentence.partition(" ")
    if head in LOWERABLE_STARTERS and rest:
        return f"Candidates report that {head.lower()} {rest}"
    return f"Candidates report: {sentence}"


def _is_exempt(sentence: str) -> bool:
    """Lead-ins ending in ':' and the empty-section placeholder need no citation."""
    t = sentence.strip()
    return t.endswith(":") or t == PLACEHOLDER or bool(STAGE_LABEL_LINE_RE.match(t))


def _parse(body: str) -> list[dict]:
    """Lines; prose lines become {prefix, sents, section} so sentences can be dropped one by one."""
    lines, section = [], ""
    for raw in body.splitlines():
        if raw.lstrip().startswith("#"):
            section = raw.lstrip("# ").strip()
        if not raw.strip() or raw.lstrip().startswith("#") or set(raw.strip()) <= set("-*_ "):
            lines.append({"raw": raw})
            continue
        m = LINE_PREFIX_RE.match(raw)
        sents = split_sentences(m.group(2))
        lines.append({"prefix": m.group(1) or "", "sents": sents, "section": section})
    return lines


INTERVIEW_WORDS_RE = re.compile(r"interview|\brounds?\b|\bstages?\b|screen|onsite|on-site|assessment|\bOA\b|\bloop\b", re.I)


SOURCE_LABEL_RE = re.compile(r"\((?:" + "|".join(re.escape(v) for v in TYPE_LABEL.values()) + r")\)", re.I)


def source_labels(cited: list[dict]) -> str:
    """'third-party guide' / 'third-party guide and job aggregator' for the facts a sentence rests on."""
    names = list(dict.fromkeys(TYPE_LABEL[f["source_type"]] for f in cited if f.get("source_type") in TYPE_LABEL))
    return " and ".join(names)


def check_body(body: str, facts: list[dict], stats: RunStats, sources: dict | None = None, level: str | None = None) -> str:
    """Drop sentences that cite no real fact, add numbers, or overclaim; rewrite unattributed forum facts;
    cap the company section; make sure all six sections exist, in order; renumber lists."""
    by_id = fact_ids(facts)
    body = normalize_citations(body, len(facts))
    lines = _parse(body)
    company_sentences = 0
    unofficial_role = sources is not None and not has_role_document(sources)  # section 3 rests on unofficial text

    for line in lines:
        if "sents" not in line:
            continue
        section = line["section"]
        derived = any(d in section.lower() for d in DERIVED_SECTIONS)
        is_tldr = section.lower().startswith("tl;dr")
        is_company = section.startswith("1.")
        is_questions = "questions to ask" in section.lower()
        kept = []

        def process(sent: str) -> None:
            nonlocal company_sentences
            cited = CITE_RE.findall(sent)
            valid = [i for i in cited if i in by_id]
            if is_questions and (LOGISTICS_RE.search(CITE_RE.sub("", sent)) or any(by_id[i].get("hedged") for i in valid)):
                stats.drops.append(Drop(section, sent, "question-logistics-or-rumor"))
                return
            if not cited:
                if _is_exempt(sent):
                    kept.append(sent)
                else:
                    stats.drops.append(Drop(section, sent, "uncited"))
                return
            if not valid:
                stats.drops.append(Drop(section, sent, "unknown-fact", ", ".join(cited)))
                return
            sent = CITE_RE.sub(lambda m: m.group(0) if m.group(1) in by_id else "", sent)
            fact_text = " ".join(by_id[i]["fact"] + " " + by_id[i]["exact_quote"] for i in valid)
            claim = REPORTED_RE.sub("", CITE_RE.sub("", sent))
            # A year is fine if a cited source is dated that year ("In early 2026 ..." from a Feb 2026 article); the
            # "(reported Mon YYYY)" label was already removed from `claim`.
            years = {by_id[i]["published_date"][:4] for i in valid if by_id[i].get("published_date")}
            missing = [n for n in NUMBER_RE.findall(claim) if n not in NUMBER_RE.findall(fact_text) and n not in years]
            if missing:
                stats.drops.append(Drop(section, sent, "number-not-in-facts", ", ".join(missing)))
                return
            if derived and any(by_id[i]["section"] == "company" for i in valid):
                stats.drops.append(Drop(section, sent, "derived-cites-company-news"))
                return
            if derived and any(by_id[i]["section"] == "timeline" for i in valid):
                stats.drops.append(Drop(section, sent, "derived-cites-timeline"))
                return
            if is_tldr and any(by_id[i]["section"] == "timeline" for i in valid):
                stats.drops.append(Drop(section, sent, "tldr-timeline"))
                return
            if is_tldr and INTERVIEW_WORDS_RE.search(CITE_RE.sub("", sent)) and not any(
                    by_id[i]["section"] == "interview" for i in valid):
                stats.drops.append(Drop(section, sent, "tldr-interview-without-interview-fact"))
                return
            if is_company:
                if company_sentences >= COMPANY_MAX_SENTENCES:
                    stats.drops.append(Drop(section, sent, "company-section-cap"))
                    return
                company_sentences += 1
            if any(by_id[i].get("hedged") for i in valid) and not HEDGE_WORD_RE.search(claim):
                new = add_hedge(sent)
                stats.rewrites.append(Rewrite(section, sent, new, "hedge-added"))
                sent = new
            if not derived and all(by_id[i].get("is_experience") for i in valid) and not ATTRIBUTED_RE.search(sent):
                new = attribute_to_candidates(sent)
                stats.rewrites.append(Rewrite(section, sent, new, "candidates-report"))
                sent = new
            mismatched = [by_id[i] for i in valid if level_mismatch(by_id[i], level)]
            wrong_level = sorted({level_label(f) for f in mismatched})
            if wrong_level and not LEVEL_LABEL_RE.search(sent):
                kinds = {f["section"] for f in mismatched}
                label = wrong_level[0] if len(wrong_level) == 1 else (
                    "other-level processes" if kinds == {"interview"} else "other-level roles" if kinds == {"role"} else "other levels")
                new = add_level_label(sent, label)
                stats.rewrites.append(Rewrite(section, sent, new, "level-label"))
                sent = new
            if unofficial_role and section.startswith("3.") and not SOURCE_LABEL_RE.search(sent):
                label = source_labels([by_id[i] for i in valid])
                if label:
                    new = add_level_label(sent, label)
                    stats.rewrites.append(Rewrite(section, sent, new, "source-label"))
                    sent = new
            kept.append(sent)

        for sent in line["sents"]:
            stats.checked += 1
            valid = [i for i in CITE_RE.findall(sent) if i in by_id]
            stages = {by_id[i]["stage"] for i in valid if by_id[i]["section"] == "interview" and by_id[i]["stage"] != "unknown"}
            if len(stages) > 1 and not derived:  # facts from different interview stages must not share a sentence
                if is_tldr:  # one bullet = one fact's worth; keep the first
                    new = fact_sentence(valid[0], by_id[valid[0]])
                    stats.rewrites.append(Rewrite(section, sent, new, "stage-trim"))
                    process(new)
                else:
                    pieces = [fact_sentence(i, by_id[i]) for i in dict.fromkeys(valid)]
                    stats.rewrites.append(Rewrite(section, sent, " ".join(pieces), "stage-split"))
                    for piece in pieces:
                        process(piece)
            else:
                process(sent)
        line["sents"] = kept

    out, counter = [], 0
    for line in lines:
        if "raw" in line:
            if line["raw"].lstrip().startswith("#"):
                counter = 0
            out.append(line["raw"])
        elif line["sents"]:
            prefix = line["prefix"]
            if NUMBERED_RE.match(prefix):  # renumber lists so drops don't leave gaps
                counter += 1
                prefix = NUMBERED_RE.sub(lambda m: f"{m.group(1)}{counter}.{m.group(2)}", prefix)
            out.append(prefix + " ".join(line["sents"]))
    s3 = SECTION3 if sources is None or has_role_document(sources) else SECTION3_INFERRED
    interview = [f for f in facts if f["section"] == "interview"]
    location_line = None
    for fid, f in by_id.items():  # the posting's location always shows in section 3
        cited_in_s3 = any(f"[{fid}]" in sent for line in lines if line.get("section", "").startswith("3.") for sent in line.get("sents", []))
        if f.get("pinned") and not cited_in_s3:
            location_line = fact_sentence(fid, f)
            break
    s3_note = None
    if unofficial_role:  # no official posting text: say so, and what section 3 rests on
        role_facts = [f for f in facts if f["section"] == "role" and not f.get("pinned")]
        types = source_labels(role_facts)
        s3_note = f"No official posting text found; based on {types}." if types else "No official posting text found."
    return ensure_sections("\n".join(out), s3, len(interview), len({f["source_id"] for f in interview}), location_line, s3_note)


def _section_index(heading: str) -> int | None:
    """Which of the six sections a writer heading is: TL;DR -> 0, '3. ...' -> 3. None if unrecognised."""
    h = heading.strip().lower()
    if h.startswith("tl;dr") or h.startswith("tldr"):
        return 0
    m = re.match(r"(\d)\.", h)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 5 else None


def _bare(line: str) -> str:
    return re.sub(r"^\s*(?:[*\-]|\d+\.)\s+", "", line).strip()


def ensure_sections(body: str, s3_heading: str, n_interview_facts: int, n_interview_sources: int,
                    location_line: str | None = None, s3_note: str | None = None) -> str:
    """Re-emit the six sections in order under their canonical headings. A missing or empty section
    (e.g. the writer's output was cut off) gets a placeholder instead of disappearing. With fewer than
    MIN_INTERVIEW_FACTS interview facts the TL;DR opens with a data warning; the posting's location opens section 3."""
    headings = ["## TL;DR", "## 1. Company overview + recent news", "## 2. Interview process + common questions",
                s3_heading, "## 4. How to prepare", "## 5. Questions to ask the interviewer"]
    content = [""] * 6
    parts = re.split(r"(?m)^##\s+(.*)$", body)  # [pre, heading, text, heading, text, ...]
    for i in range(1, len(parts), 2):
        idx = _section_index(parts[i])
        if idx is not None:
            content[idx] += parts[i + 1]
    if n_interview_facts < MIN_INTERVIEW_FACTS:
        lead = [f"- Limited interview data ({n_interview_sources} source{'' if n_interview_sources == 1 else 's'})."]
        if n_interview_facts == 0:
            lead.append(f"- {NO_INTERVIEW}")
        rest = [l for l in content[0].splitlines()  # the warning replaces the writer's placeholder, if any
                if l.strip() and not _bare(l).startswith("Limited interview data") and _bare(l) not in (NO_INTERVIEW, PLACEHOLDER)]
        content[0] = "\n".join(lead + rest)
    if n_interview_facts == 0 and content[2].replace(PLACEHOLDER, "").strip(" \n-*_"):
        content[2] = NO_INTERVIEW + "\n\n" + content[2].replace(PLACEHOLDER, "").strip("\n")  # e.g. only a Timeline came through
    section3 = content[3].replace(PLACEHOLDER, "").strip("\n")
    if location_line:
        section3 = location_line + ("\n" + section3 if section3.strip(" -*_") else "")
    if not section3.strip(" -*_"):
        section3 = PLACEHOLDER
    content[3] = f"{s3_note}\n{section3}" if s3_note else section3
    out = []
    for heading, text in zip(headings, content):
        text = text.strip("\n")
        out.append(f"{heading}\n{text if text.strip(' -*_') else PLACEHOLDER}")
    return "\n\n".join(out) + "\n"


GENERAL_SECTIONS = {"prep": "## 4. How to prepare", "questions": "## 5. Questions to ask the interviewer"}


def general_note(role: str) -> str:
    return f"Only company information was found; sections 4 and 5 give general guidance for {guidance.family_name(role)}."


def add_general_guidance(body: str, role: str, facts: list[dict], stats: RunStats) -> str:
    """When the writer had no interview or role facts (only company facts), sections 4 and 5 end up as placeholders and the
    brief has nothing to act on. Fill those placeholders with general guidance for the role family, labelled as such
    (code-written, uncited, never presented as company data), and say so in the TL;DR. Runs after check_body, so the
    guidance is not checked for citations."""
    if any(f["section"] in KEY_SECTIONS and not f.get("pinned") for f in facts):
        return body
    for kind, heading in GENERAL_SECTIONS.items():
        empty = re.compile(rf"(?m)^{re.escape(heading)}\n{re.escape(PLACEHOLDER)}$")
        if empty.search(body):
            body = empty.sub(lambda _: f"{heading}\n{guidance.section_text(kind, role)}", body)
            stats.general_sections.append(heading.removeprefix("## "))
    if stats.general_sections:
        body = re.sub(r"(?ms)^(## TL;DR\n.*?)(\n\n## 1\.)", lambda m: f"{m.group(1)}\n- {general_note(role)}{m.group(2)}", body, count=1)
    return body


def render(body: str, facts: list[dict], sources: dict) -> tuple[str, list[dict]]:
    """Turn [F3][F7] into numbered source links (one per source), numbered by first use."""
    by_id = fact_ids(facts)
    order: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        sids: list[str] = []
        for fid in CITE_RE.findall(m.group(0)):
            for sid in [by_id[fid]["source_id"], *by_id[fid].get("also_sources", [])]:  # a merged fact cites every source
                if sid not in sids:
                    sids.append(sid)
        links = []
        for sid in sids:
            order.setdefault(sid, len(order) + 1)
            url = sources[sid]["url"]
            links.append(f"[{order[sid]}]" if url == JD_URL else f"[{order[sid]}]({url})")
        return "".join(links)

    text = re.sub(r"\[F\d+\](?:\s*\[F\d+\])*", repl, body)
    return text, [sources[sid] for sid in order]


def finalize_brief(company, role, level, body: str, facts: list[dict], sources: dict, stats: RunStats) -> BriefResult:
    body = check_body(body, facts, stats, sources, level)
    body = add_general_guidance(body, role, facts, stats)
    body, cited = render(body, facts, sources)
    lines = []
    for n, s in enumerate(cited, 1):
        if s["url"] == JD_URL:
            lines.append(f"{n}. Job description provided by you")
        else:
            date_part = f" ({s['published_date']})" if s.get("published_date") else ""
            title = re.sub(r"([\[\]])", r"\\\1", s["title"] or s["url"])  # a "]" in a title must not end the link text
            lines.append(f"{n}. [{title}]({s['url']}){date_part}")
    footer = "\n\n## Sources\n" + ("\n".join(lines) if lines else "None cited.")
    note = (
        f"\n\n_Grounding: {stats.facts_grounded} facts grounded from {stats.contributing_sources} sources "
        f"({len(stats.rejected)} rejected). Writing: {stats.writing_breakdown()}._"
    )
    header = f"# Interview brief: {company} — {role} ({LEVEL_LABEL[level]})\n\n"
    return BriefResult(f"{header}{body.strip()}{footer}{note}\n", stats, sources)
