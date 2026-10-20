"""Turn search results into a cited, verified markdown brief."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

import llm
from search import source_kind

SNIPPET_CHARS = 800  # per source in the writing prompt
VERIFY_CHARS = 2500  # per source in the fact-check prompt
JD_URL = "jd://user-provided"

LEVEL_LABEL = {"intern": "intern", "new-grad": "new grad", "experienced": "experienced"}

SECTION3 = "## 3. What the role really needs"
SECTION3_INFERRED = "## 3. What the role really needs (Inferred, not from a JD)"

SYSTEM_PROMPT = """You write interview-prep briefs. Rules:
- Use ONLY the numbered sources provided. Never use outside knowledge.
- Every factual sentence must end with a citation like [S2] (or [S1][S4]). Cite EACH sentence and
  EACH bullet on its own; do not cite once for a whole list.
- If a claim is not supported by a source, leave it out. Do not guess. Questions to ask the
  interviewer do not need citations.
- If a section has no supporting sources, write "No supported information found."
- Sources are tagged with a kind and a published date. Prefer company and reputable sources.
- Sources tagged FORUM (Reddit, Blind, LeetCode discuss...) are anecdotes: phrase every claim from them
  as "Candidates report ..." and never state it as established fact.
- Recent news: put the date from the source's Published field next to each item, like (Feb 2026).
  Use "(date unknown)" if the source has none. Prefer sources from the last 12 months.
- If sources conflict (e.g. different figures), use the NEWEST source and say so explicitly,
  e.g. "Earlier reports said X [S1], but a newer source says Y [S2]."
- Tailor interview, preparation and questions to the candidate's level; skip details that clearly
  refer to a different level.
- Output markdown with exactly the headings requested, in order."""

VERIFY_SYSTEM_PROMPT = """You are a strict fact-checker. You get source texts and numbered claims, each citing sources.
A claim is SUPPORTED only if the cited source text directly states every factual part of it
(figures, dates, names, who said what). Inference, extrapolation or partial support = unsupported.
Reply with JSON only, no prose: {"unsupported": [<claim numbers>]}. Use [] if all are supported."""


# ---------- sources ----------

def collect_sources(research: dict[str, list[dict]], company: str, jd: str | None = None) -> dict[str, dict]:
    """Dedupe results by URL and give each an id: S1, S2, ... The JD (if any) is a source too."""
    groups = dict(research)
    if jd:
        groups["jd"] = [{"title": "Job description provided by you", "url": JD_URL, "content": jd, "published_date": None}]
    sources: dict[str, dict] = {}
    seen: set[str] = set()
    for results in groups.values():
        for r in results:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            kind = "jd" if r["url"] == JD_URL else source_kind(r["url"], company)
            sources[f"S{len(seen)}"] = {**r, "kind": kind}
    return sources


def _source_header(sid: str, s: dict) -> str:
    return f"[{sid}] {s['title']}\nKind: {s['kind'].upper()} | Published: {s['published_date'] or 'unknown'} | URL: {s['url']}"


def build_prompts(company, role, level, jd, research, sources, today: date) -> tuple[str, str]:
    posting = research.get("posting") or []
    posting_url = posting[0]["url"] if posting else None
    has_jd = bool(jd) or bool(posting)
    s3 = SECTION3 if has_jd else SECTION3_INFERRED
    if jd:
        s3_rule = "Base section 3 ONLY on the job description source (kind JD)."
    elif posting:
        s3_rule = f"Base section 3 ONLY on the single job posting {posting_url}. Ignore all other sources there."
    else:
        s3_rule = "No job posting was found, so section 3 is inferred from the other sources; keep the heading exactly as given."
    source_text = "\n\n".join(f"{_source_header(sid, s)}\n{s['content'][:SNIPPET_CHARS]}" for sid, s in sources.items())
    jd_note = "" if not jd else "\n(The user's job description is source JD below.)"
    user = f"""Today's date: {today.isoformat()}
Company: {company}
Role: {role}
Candidate level: {LEVEL_LABEL[level]}{jd_note}

Sources:
{source_text}

{s3_rule}

Write the brief with these headings exactly, in this order:
## TL;DR
(exactly 3 bullets: the most important things to know, each cited)
## 1. Company overview + recent news
## 2. Interview process + common questions
{s3}
## 4. How to prepare
(specific practice items grounded in the sources, e.g. a named problem type or topic, each cited)
## 5. Questions to ask the interviewer
(tailored to a {LEVEL_LABEL[level]} candidate)"""
    return SYSTEM_PROMPT, user


# ---------- verification ----------

CITE_RE = re.compile(r"\[(S\d+)\]")
SENT_RE = re.compile(r".+?(?:[.!?](?:\s*\[S\d+\])*(?=\s|$)|$)")  # sentence + its trailing citations
LINE_PREFIX_RE = re.compile(r"^(\s*(?:[*\-]|\d+\.)\s+)?(.*)$", re.S)
ATTRIBUTED_RE = re.compile(r"\bcandidates?\b[^.]{0,30}\b(report|said|say|mention|describe|share|recall)|reported by candidates", re.I)


@dataclass
class VerifyStats:
    checked: int = 0
    dropped_uncited: int = 0
    dropped_unsupported: int = 0
    dropped_forum: int = 0
    verified: bool = True

    @property
    def dropped(self) -> int:
        return self.dropped_uncited + self.dropped_unsupported + self.dropped_forum

    def log_line(self) -> str:
        if not self.verified:
            return "[verify] FAILED: brief is UNVERIFIED"
        return (
            f"[verify] {self.checked} sentences checked, {self.dropped} dropped "
            f"({self.dropped_unsupported} unsupported, {self.dropped_uncited} uncited, "
            f"{self.dropped_forum} forum claims not phrased as 'candidates report')"
        )


@dataclass
class BriefResult:
    text: str
    stats: VerifyStats = field(default_factory=VerifyStats)


def _is_exempt(sentence: str) -> bool:
    """Questions and lead-ins ending in ':' make no factual claim, so they need no citation."""
    t = sentence.strip()
    return t.endswith("?") or t.endswith(":")


def _parse(body: str) -> list[dict]:
    """Split body into lines; prose lines become {prefix, sents} so sentences can be dropped one by one."""
    lines = []
    for raw in body.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or set(raw.strip()) <= set("-*_ "):
            lines.append({"raw": raw})
            continue
        m = LINE_PREFIX_RE.match(raw)
        sents = [x.strip() for x in SENT_RE.findall(m.group(2)) if x.strip()]
        lines.append({"prefix": m.group(1) or "", "sents": sents})
    return lines


def _verify_call(claims: dict[int, str], sources: dict, ids: set[str]) -> set[int]:
    source_text = "\n\n".join(
        f"{_source_header(sid, sources[sid])}\n{sources[sid]['content'][:VERIFY_CHARS]}" for sid in sorted(ids, key=lambda x: int(x[1:]))
    )
    claim_text = "\n".join(f"[{n}] {text}" for n, text in claims.items())
    out = llm.generate(VERIFY_SYSTEM_PROMPT, f"Sources:\n{source_text}\n\nClaims:\n{claim_text}")
    m = re.search(r"\{.*\}", out, re.S)
    data = json.loads(m.group(0)) if m else None
    if not isinstance(data, dict) or not isinstance(data.get("unsupported"), list):
        raise ValueError("verifier did not return the expected JSON")
    return {int(n) for n in data["unsupported"]}


def verify_body(body: str, sources: dict) -> tuple[str, VerifyStats]:
    """Drop every sentence that is uncited, mis-attributed (forum) or not supported by its cited source."""
    stats = VerifyStats()
    lines = _parse(body)
    to_check: dict[int, str] = {}
    where: dict[int, tuple[int, int]] = {}
    drop: set[tuple[int, int]] = set()

    for li, line in enumerate(lines):
        for si, sent in enumerate(line.get("sents", [])):
            stats.checked += 1
            ids = [i for i in CITE_RE.findall(sent) if i in sources]
            if not CITE_RE.findall(sent):
                if not _is_exempt(sent):
                    drop.add((li, si)); stats.dropped_uncited += 1
                continue
            if not ids:
                drop.add((li, si)); stats.dropped_unsupported += 1
                continue
            if all(sources[i]["kind"] == "forum" for i in ids) and not ATTRIBUTED_RE.search(sent):
                drop.add((li, si)); stats.dropped_forum += 1
                continue
            n = len(to_check)
            to_check[n] = sent
            where[n] = (li, si)

    if to_check:
        cited = {i for sent in to_check.values() for i in CITE_RE.findall(sent) if i in sources}
        try:
            for n in _verify_call(to_check, sources, cited):
                if n in where:
                    drop.add(where[n]); stats.dropped_unsupported += 1
        except (ValueError, json.JSONDecodeError):
            stats.verified = False  # keep the brief, but say loudly that it was not checked

    out: list[str] = []
    for li, line in enumerate(lines):
        if "raw" in line:
            out.append(line["raw"])
            continue
        kept = [s for si, s in enumerate(line["sents"]) if (li, si) not in drop]
        if kept:
            out.append(line["prefix"] + " ".join(kept))
    return _fill_empty_sections("\n".join(out)), stats


def _fill_empty_sections(body: str) -> str:
    parts = re.split(r"(?m)^(##? .*)$", body)  # [pre, heading, text, heading, text, ...]
    for i in range(2, len(parts), 2):
        if not parts[i].strip(" \n-*_"):
            parts[i] = "\nNo supported information found.\n"
    return re.sub(r"\n{3,}", "\n\n", "".join(parts))


# ---------- rendering ----------

def render(body: str, sources: dict) -> tuple[str, list[dict]]:
    """Number citations by first use, link them to real URLs, and keep only the sources actually cited."""
    order: dict[str, int] = {}
    for sid in CITE_RE.findall(body):
        if sid in sources and sid not in order:
            order[sid] = len(order) + 1

    def repl(m: re.Match) -> str:
        sid = m.group(1)
        if sid not in order:
            return ""  # citation to a source that doesn't exist
        url = sources[sid]["url"]
        return f"[{order[sid]}]" if url == JD_URL else f"[{order[sid]}]({url})"

    cited = [sources[sid] for sid in order]
    return CITE_RE.sub(repl, body), cited


def generate_brief(company: str, role: str, level: str, jd: str | None, research: dict, today: date | None = None) -> BriefResult:
    today = today or date.today()
    sources = collect_sources(research, company, jd)
    system_prompt, user_prompt = build_prompts(company, role, level, jd, research, sources, today)
    body = llm.generate(system_prompt, user_prompt)

    body, stats = verify_body(body, sources)
    body, cited = render(body, sources)

    lines = []
    for n, s in enumerate(cited, 1):
        if s["url"] == JD_URL:
            lines.append(f"{n}. Job description provided by you")
        else:
            date_part = f" ({s['published_date']})" if s["published_date"] else ""
            lines.append(f"{n}. {s['title']} — {s['url']}{date_part}")
    footer = "\n\n## Sources\n" + "\n".join(lines)
    note = (
        "\n\n> ⚠ Verification step failed: claims in this brief were NOT checked against their sources."
        if not stats.verified
        else f"\n\n_Verification: {stats.checked} sentences checked, {stats.dropped} dropped as unsupported._"
    )
    header = f"# Interview brief: {company} — {role} ({LEVEL_LABEL[level]})\n\n"
    return BriefResult(f"{header}{body.strip()}{footer}{note}\n", stats)
