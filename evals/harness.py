"""Eval harness: run fixed cases through the pipeline, record/replay the web, compare metrics between runs.

Record / replay happens at the Tavily client (the only network seam), so every piece of our own logic (query
building, filters, page cleaning and focusing, extraction, grounding, writing, finalize) runs in replay exactly as live.

    python evals/run_evals.py --record        run live, save search results + page texts (+ facts) per case
    python evals/run_evals.py                 replay: no Tavily calls; LLM calls still happen (the extraction cache helps)
    python evals/run_evals.py --from-facts    replay only the writer + finalize from each case's saved facts.json
    python evals/run_evals.py --repeat 3      run the writer 3 times per case and average the writer metrics

Fixtures contain third-party page text, so evals/fixtures/ and evals/results/ are gitignored; only evals/cases.yaml
and evals/baseline.json (metrics only) are committed.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
for _p in (str(ROOT / "src"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import brief  # noqa: E402
import graph  # noqa: E402
import llm  # noqa: E402
import search  # noqa: E402
from brief import RunStats  # noqa: E402

PAGE_CAP = 200_000  # chars of one page kept in a fixture
SECRET_ENV_VARS = ("TAVILY_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY")
SECRET_PATTERNS = (r"tvly-[A-Za-z0-9_-]{8,}", r"gsk_[A-Za-z0-9]{16,}", r"sk-[A-Za-z0-9_-]{16,}")

WRITER_TOLERANCE = 2  # "sentences dropped" / "rewrites" must be worse by MORE than this to be flagged
PERF_TOLERANCE = 0.25  # latency / tokens: relative
CACHE_RATE_TOLERANCE = 0.05  # two runs have "the same cache hit rate" if they differ by less than this

# metric -> (direction, kind). kind: extraction (strict if same cache rate), writer (tolerance), output (strict),
# perf (relative tolerance), info (never flagged). direction: +1 higher is better, -1 lower is better.
METRICS: dict[str, tuple[int, str]] = {
    "sources": (0, "info"),
    "full_text": (+1, "extraction"),
    "facts_company": (+1, "extraction"),
    "facts_interview": (+1, "extraction"),
    "facts_role": (+1, "extraction"),
    "facts_timeline": (+1, "extraction"),
    "grounded": (+1, "extraction"),
    "rejected": (-1, "extraction"),
    "dropped": (-1, "writer"),
    "rewrites": (-1, "writer"),
    "empty_sections": (-1, "output"),
    "fixture_misses": (-1, "output"),
    "latency_s": (-1, "perf"),
    "tokens_in": (-1, "perf"),
    "tokens_out": (-1, "perf"),
    "tokens_reasoning": (0, "info"),
}
LABELS = {
    "sources": "sources", "full_text": "full-text pages", "facts_company": "facts: company", "facts_interview": "facts: interview",
    "facts_role": "facts: role", "facts_timeline": "facts: timeline", "grounded": "grounded", "rejected": "rejected",
    "dropped": "sentences dropped", "rewrites": "rewrites", "empty_sections": "empty sections", "fixture_misses": "fixture misses",
    "latency_s": "latency (s)", "tokens_in": "tokens in", "tokens_out": "tokens out", "tokens_reasoning": "of which reasoning",
}
WRITER_ONLY_KINDS = {"writer", "output"}  # what can be compared between a facts-only run and a full run
WRITER_METRICS = tuple(m for m, (_, kind) in METRICS.items() if kind == "writer")  # averaged over --repeat runs


class Aborted(Exception):
    """The run cannot continue (a daily LLM limit): stop all cases."""


# ====================================================================== cases

@dataclass
class Case:
    name: str
    company: str
    role: str
    level: str
    location: str = "United States"
    jd: str | None = None


def load_cases(path: Path | None = None) -> list[Case]:
    data = yaml.safe_load(Path(path or EVALS / "cases.yaml").read_text())
    cases = [Case(**c) for c in data["cases"]]
    if len({c.name for c in cases}) != len(cases):
        raise ValueError("duplicate case names in cases.yaml")
    for c in cases:
        if c.level not in search.LEVELS:
            raise ValueError(f"case {c.name}: level must be one of {search.LEVELS}")
    return cases


# ====================================================================== record / replay

def call_key(query: str, max_results, options: dict) -> str:
    return hashlib.sha1(json.dumps({"q": query, "n": max_results, "o": options}, sort_keys=True, default=str).encode()).hexdigest()[:16]


class RecordingClient:
    """Wraps the real Tavily client and remembers every raw response."""

    def __init__(self, real):
        self.real = real
        self.searches: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}

    def search(self, query, max_results=None, **options):
        response = self.real.search(query=query, max_results=max_results, **options)
        self.searches[call_key(query, max_results, options)] = {"query": query, "max_results": max_results, "options": options, "response": response}
        return response

    def extract(self, urls):
        response = self.real.extract(urls=urls)
        for r in response.get("results", []):
            self.pages[r["url"]] = {"raw_content": (r.get("raw_content") or "")[:PAGE_CAP], "failed": False}
        for f in response.get("failed_results", []):
            self.pages.setdefault(f.get("url"), {"raw_content": "", "failed": True})
        return response


class ReplayClient:
    """Serves recorded responses. Anything that was not recorded is reported (never silently invented)."""

    def __init__(self, searches: dict, pages: dict):
        self.searches, self.pages = searches, pages
        self.misses: list[dict] = []
        self.calls = 0

    def search(self, query, max_results=None, **options):
        self.calls += 1
        hit = self.searches.get(call_key(query, max_results, options))
        if hit:
            return hit["response"]
        self.misses.append({"kind": "search", "what": query})
        print(f"[replay] MISSING search not in fixtures: {query!r} (re-record this case)", file=sys.stderr)
        return {"results": []}

    def extract(self, urls):
        self.calls += 1
        results, failed = [], []
        for url in urls:
            page = self.pages.get(url)
            if page and not page["failed"]:
                results.append({"url": url, "raw_content": page["raw_content"]})
            elif page:
                failed.append({"url": url, "error": "failed when recorded"})
            else:
                self.misses.append({"kind": "page", "what": url})
                print(f"[replay] MISSING page not in fixtures: {url} (re-record this case)", file=sys.stderr)
                failed.append({"url": url, "error": "not in fixtures"})
        return {"results": results, "failed_results": failed}


@contextlib.contextmanager
def use_tavily_client(client):
    """Make search.py use `client` instead of building a TavilyClient (no key needed, no network)."""
    old_cls, old_key = search.TavilyClient, os.environ.get("TAVILY_API_KEY")
    search.TavilyClient = lambda api_key=None, **kw: client
    os.environ.setdefault("TAVILY_API_KEY", "replay-no-network")
    try:
        yield client
    finally:
        search.TavilyClient = old_cls
        if old_key is None:
            os.environ.pop("TAVILY_API_KEY", None)


def assert_no_secrets(text: str) -> None:
    """Refuse to write a fixture that contains an API key (from .env or one that merely looks like one)."""
    for var in SECRET_ENV_VARS:
        value = os.environ.get(var, "")
        if len(value) >= 8 and value in text:
            raise RuntimeError(f"refusing to save: the value of {var} appears in the data")
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            raise RuntimeError(f"refusing to save: text matching /{pattern}/ (an API key?) appears in the data")


def write_json(path: Path, data) -> None:
    text = json.dumps(data, indent=1, ensure_ascii=False, default=str)
    assert_no_secrets(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fixture_dir(case: Case, fixtures: Path | None = None) -> Path:
    return (fixtures or EVALS / "fixtures") / case.name


def save_fixtures(case: Case, client: RecordingClient, recorded_on: date, fixtures: Path | None = None) -> None:
    d = fixture_dir(case, fixtures)
    write_json(d / "search.json", client.searches)
    write_json(d / "pages.json", client.pages)
    write_json(d / "meta.json", {"case": case.__dict__, "recorded_on": recorded_on.isoformat(), "git": git_info(),
                                 "models": model_info(), "searches": len(client.searches), "pages": len(client.pages)})


def load_fixtures(case: Case, fixtures: Path | None = None) -> tuple[ReplayClient, dict]:
    d = fixture_dir(case, fixtures)
    if not (d / "meta.json").exists():
        raise FileNotFoundError(f"no fixtures for '{case.name}' in {d}. Record them first: python evals/run_evals.py --record")
    meta = json.loads((d / "meta.json").read_text())
    return ReplayClient(json.loads((d / "search.json").read_text()), json.loads((d / "pages.json").read_text())), meta


def facts_payload(case: Case, stats: RunStats, result: brief.BriefResult, recorded_on: str) -> dict:
    """Everything the writer + finalize stages need, so they can be re-run without extraction."""
    sources = {}
    for sid, s in result.sources.items():
        sources[sid] = {k: v for k, v in s.items() if k not in ("content", "full_text")}
        sources[sid]["content"] = (s.get("content") or "")[:300]
        sources[sid]["full_text"] = (s.get("full_text") or "")[:200]  # only its presence matters to the writer stage
    return {"case": case.__dict__, "recorded_on": recorded_on, "facts": stats.grounded, "rejected": stats.rejected,
            "sources": sources, "retry_sections": stats.retry_sections, "facts_extracted": stats.facts_extracted,
            "steps": stats.steps}


# ====================================================================== metrics

BOILERPLATE = re.compile(
    r"^(?:-\s*)?(?:" + re.escape(brief.PLACEHOLDER) + r"|" + re.escape(brief.NO_INTERVIEW) + r"|Limited interview data\b.*|"
    r"No official posting text found\b.*|\*\*[^*]+\*\*)$")


def empty_sections(text: str) -> list[str]:
    """Names of sections that say nothing: only a placeholder / data warning / note / orphan label."""
    body = text.split("\n## Sources")[0]
    parts = re.split(r"(?m)^## (.*)$", body)  # [header, name, content, name, content, ...]
    empty = []
    for name, content in zip(parts[1::2], parts[2::2]):
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if all(BOILERPLATE.match(l) for l in lines):
            empty.append(name.strip())
    return empty


def cache_counts(steps: list[str]) -> tuple[int, int] | None:
    """(from cache, via LLM) summed over the [extract] log lines, or None if there are none."""
    found = [re.search(r"(\d+) from cache, (\d+) via LLM", s) for s in steps if s.startswith("[extract]")]
    found = [m for m in found if m]
    return (sum(int(m.group(1)) for m in found), sum(int(m.group(2)) for m in found)) if found else None


def case_metrics(stats: RunStats, text: str, latency: float, usage: dict, misses: int, sources: dict | None,
                 from_facts: bool = False) -> dict:
    sections = {sec: sum(f["section"] == sec for f in stats.grounded) for sec in ("company", "interview", "role", "timeline")}
    cache = None if from_facts else cache_counts(stats.steps)
    empty = empty_sections(text) if text else []
    return {
        "sources": stats.sources_total,
        "full_text": stats.sources_with_text,
        **{f"facts_{k}": v for k, v in sections.items()},
        "grounded": len(stats.grounded),
        "rejected": len(stats.rejected),
        "dropped": len(stats.drops),
        "rewrites": len(stats.rewrites),
        "empty_sections": len(empty),
        "fixture_misses": misses,
        "latency_s": round(latency, 1),
        "tokens_in": usage["input"],
        "tokens_out": usage["output"],
        "tokens_reasoning": usage["reasoning"],
        # context for the comparison (not metrics):
        "empty_section_names": empty,
        "cache_hits": cache[0] if cache else None,
        "cache_misses": cache[1] if cache else None,
        "retry": stats.retry_sections,
    }


def cache_rate(m: dict) -> float | None:
    hits, misses = m.get("cache_hits"), m.get("cache_misses")
    if hits is None or misses is None or hits + misses == 0:
        return None
    return hits / (hits + misses)


# ====================================================================== comparison

def family(mode: str) -> str:
    return "facts" if mode == "from-facts" else "full"  # record and replay produce the same kind of numbers


def judge(metric: str, ref, cur, ref_case: dict, cur_case: dict) -> tuple[str, str]:
    """('worse' | 'better' | 'same' | 'info', note) for one metric of one case, current vs a reference run."""
    direction, kind = METRICS[metric]
    if ref is None or cur is None:
        return "info", ""
    if cur == ref:
        return "same", ""
    if kind == "info":
        return "info", ""
    delta = (cur - ref) * direction  # > 0 means better
    if delta > 0:
        return "better", ""
    worse_by = -delta
    if kind == "writer":
        return ("worse", f"beyond ±{WRITER_TOLERANCE}") if worse_by > WRITER_TOLERANCE else ("same", f"within ±{WRITER_TOLERANCE}")
    if kind == "perf":
        if ref == 0:
            return "info", ""
        rate_ref, rate_cur = cache_rate(ref_case), cache_rate(cur_case)
        if metric == "latency_s" and (rate_ref is None or rate_cur is None or abs(rate_ref - rate_cur) >= CACHE_RATE_TOLERANCE):
            return "info", "cache hit rate differs"
        return ("worse", f">{int(PERF_TOLERANCE * 100)}%") if worse_by / abs(ref) > PERF_TOLERANCE else ("same", "within tolerance")
    if kind == "extraction":
        r1, r2 = cache_rate(ref_case), cache_rate(cur_case)
        same_rate = r1 is not None and r2 is not None and abs(r1 - r2) < CACHE_RATE_TOLERANCE
        if same_rate or (r1 is None and r2 is None):
            return "worse", "strict (same cache hit rate)" if same_rate else "strict"
        tol = max(2, int(0.1 * abs(ref)))  # a fresh extraction is a new LLM sample: some noise is expected
        return ("worse", f"beyond ±{tol} (cache hit rate differs)") if worse_by > tol else ("same", f"within ±{tol}: cache hit rate differs")
    return "worse", ""  # output: strict


def compare_case(ref: dict | None, cur: dict, ref_mode: str, cur_mode: str) -> list[dict]:
    """Rows {metric, ref, cur, verdict, note} for every comparable metric."""
    rows = []
    if ref is None:
        return rows
    same_family = family(ref_mode) == family(cur_mode)
    for metric, (_, kind) in METRICS.items():
        if not same_family and kind not in WRITER_ONLY_KINDS:
            continue  # a facts-only run does not recompute extraction, tokens or latency of a full run
        verdict, note = judge(metric, ref.get(metric), cur.get(metric), ref, cur)
        rows.append({"metric": metric, "ref": ref.get(metric), "cur": cur.get(metric), "verdict": verdict, "note": note})
    if ref.get("status", "ok") == "ok" and cur.get("status", "ok") != "ok":
        rows.append({"metric": "status", "ref": "ok", "cur": cur.get("status"), "verdict": "worse", "note": cur.get("error", "")})
    return rows


def regressions(comparison: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    return [(case, row) for case, rows in comparison.items() for row in rows if row["verdict"] == "worse"]


# ====================================================================== results + baseline

def git_info() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    commit = run("rev-parse", "--short", "HEAD")
    return {"commit": commit or None, "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


def model_info() -> dict:
    try:
        provider, extract_model = llm.get_settings("extract")
        _, write_model = llm.get_settings("write")
        return {"provider": provider, "extract": extract_model, "write": write_model,
                "extract_effort": llm.get_reasoning_effort("extract", provider, extract_model),
                "write_effort": llm.get_reasoning_effort("write", provider, write_model)}
    except llm.LLMError:
        return {}


def result_files(results: Path) -> list[Path]:
    return sorted(p for p in results.glob("*.json") if re.fullmatch(r"\d{8}-\d{6}", p.stem))


def unique_timestamp(results: Path) -> str:
    """UTC 'YYYYmmdd-HHMMSS', moved forward a second at a time if a run already used it (never overwrite a run)."""
    now = datetime.now(timezone.utc)
    while (results / f"{now.strftime('%Y%m%d-%H%M%S')}.json").exists():
        now += timedelta(seconds=1)
    return now.strftime("%Y%m%d-%H%M%S")


def previous_run(results: Path, mode: str, exclude: str | None = None) -> dict | None:
    """The newest saved run of the same family that finished (not aborted), other than `exclude`."""
    for path in reversed(result_files(results)):
        if path.stem == exclude:
            continue
        data = json.loads(path.read_text())
        if not data.get("aborted") and family(data["mode"]) == family(mode):
            return data
    return None


METRIC_KEYS = tuple(METRICS)


def baseline_entry(case_metrics_: dict) -> dict:
    return {k: case_metrics_[k] for k in METRIC_KEYS if k in case_metrics_} | {
        k: case_metrics_[k] for k in ("cache_hits", "cache_misses") if case_metrics_.get(k) is not None}


def set_baseline(path: Path, run: dict) -> list[str]:
    """Pin this run's metrics (metrics only, no text) as the baseline. Returns the cases written."""
    if run["mode"] == "from-facts":
        raise ValueError("the baseline must come from a full run (--record or replay): a facts-only run does not recompute extraction")
    if run.get("aborted"):
        raise ValueError("this run was aborted; it cannot become the baseline")
    good = {n: c for n, c in run["cases"].items() if c.get("status") == "ok"}
    if not good:
        raise ValueError("no case finished successfully; nothing to pin")
    current = json.loads(path.read_text()) if path.exists() else {"cases": {}}
    current["cases"].update({n: baseline_entry(c) for n, c in good.items()})
    current.update({"pinned": run["timestamp"], "mode": "replay", "git": run["git"], "models": run["models"]})
    write_json(path, current)
    return sorted(good)


# ====================================================================== running

def run_case(case: Case, mode: str, use_cache: bool, fixtures: Path | None, out_dir: Path | None, repeat: int = 1) -> dict:
    """Run one case in `mode` ('record' | 'replay' | 'from-facts'). Returns its metrics dict (status ok/failed).

    With repeat > 1 the writer + finalize run (repeat - 1) more times from the facts this run saved, and the writer
    metrics become the mean over all runs (per-run values in `writer_runs`). Everything else comes from the first run."""
    llm.reset_usage()
    stats = RunStats()
    log = io.StringIO()
    text, sources, misses, error = "", None, 0, None
    recorded_on = date.today().isoformat()
    start = time.perf_counter()
    result = None
    try:
        with contextlib.redirect_stderr(log):
            if mode == "from-facts":
                result = run_from_facts(case, fixtures, stats)
            elif mode == "record":
                import tavily
                real = tavily.TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
                with use_tavily_client(RecordingClient(real)) as recorder:
                    result = graph.run_pipeline(case.company, case.role, case.level, case.jd, use_cache=use_cache,
                                                today=date.fromisoformat(recorded_on), stats=stats, location=case.location)
                    save_fixtures(case, recorder, date.fromisoformat(recorded_on), fixtures)
            else:
                replay, meta = load_fixtures(case, fixtures)
                recorded_on = meta["recorded_on"]
                with use_tavily_client(replay):
                    try:
                        result = graph.run_pipeline(case.company, case.role, case.level, case.jd, use_cache=use_cache,
                                                    today=date.fromisoformat(recorded_on), stats=stats, location=case.location)
                    finally:
                        misses = len(replay.misses)
        text, sources = result.text, result.sources
        if mode in ("record", "replay"):  # keep facts.json current so --from-facts has something to replay
            write_json(fixture_dir(case, fixtures) / "facts.json", facts_payload(case, stats, result, recorded_on))
    except llm.QuotaExceededError:
        raise Aborted(f"daily LLM limit reached during '{case.name}'")
    except FileNotFoundError:
        raise
    except llm.LLMError as exc:
        error = str(exc).splitlines()[0][:300]
    latency = time.perf_counter() - start
    metrics = case_metrics(stats, text, latency, llm.usage_totals(), misses, sources, from_facts=(mode == "from-facts"))
    metrics["status"] = "failed" if error else "ok"
    if error:
        metrics["error"] = error
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{case.name}.log").write_text(log.getvalue())
        if text:
            (out_dir / f"{case.name}.md").write_text(text)
        (out_dir / f"{case.name}.debug.md").write_text(stats.debug_report(sources or {}))
    if repeat > 1 and not error:
        repeat_writer(case, metrics, repeat, fixtures, out_dir)
    return metrics


def writer_metrics(stats: RunStats) -> dict:
    return {"dropped": len(stats.drops), "rewrites": len(stats.rewrites)}


def repeat_writer(case: Case, metrics: dict, repeat: int, fixtures: Path | None, out_dir: Path | None) -> None:
    """Run writer + finalize (repeat - 1) more times on the same facts; replace the writer metrics by their mean.
    The writer is sampled at temperature 0 with a seed, but providers only promise best-effort determinism."""
    runs = [{k: metrics[k] for k in WRITER_METRICS}]
    for i in range(2, repeat + 1):
        stats, log = RunStats(), io.StringIO()
        try:
            with contextlib.redirect_stderr(log):
                result = run_from_facts(case, fixtures, stats)
        except llm.QuotaExceededError:
            raise Aborted(f"daily LLM limit reached during '{case.name}' (writer repeat {i}/{repeat})")
        runs.append(writer_metrics(stats))
        if out_dir is not None:
            (out_dir / f"{case.name}.repeat{i}.md").write_text(result.text)
            (out_dir / f"{case.name}.repeat{i}.debug.md").write_text(stats.debug_report(result.sources))
    for k in WRITER_METRICS:
        metrics[k] = round(sum(r[k] for r in runs) / len(runs), 1)
    metrics["writer_runs"] = runs


def run_from_facts(case: Case, fixtures: Path | None, stats: RunStats) -> brief.BriefResult:
    """Writer + finalize only, from the facts saved by the last full run of this case."""
    path = fixture_dir(case, fixtures) / "facts.json"
    if not path.exists():
        raise FileNotFoundError(f"no facts.json for '{case.name}'. Run it once (record or replay) first.")
    saved = json.loads(path.read_text())
    sources = saved["sources"]
    stats.steps = list(saved.get("steps", []))
    stats.grounded, stats.rejected = saved["facts"], saved["rejected"]
    stats.facts_grounded, stats.facts_extracted = len(saved["facts"]), saved.get("facts_extracted", 0)
    stats.sources_total = len(sources)
    stats.sources_with_text = sum(1 for s in sources.values() if s.get("full_text"))
    stats.retry_sections = saved.get("retry_sections", [])
    state = {"company": case.company, "role": case.role, "level": case.level, "facts": saved["facts"], "sources": sources,
             "today": date.fromisoformat(saved["recorded_on"]), "stats": stats}
    state.update(graph.write_node(state))
    state.update(graph.finalize_node(state))
    return state["result"]


@dataclass
class Run:
    data: dict
    comparisons: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # {"previous": {...}, "baseline": {...}}

    @property
    def regressions(self) -> list[tuple[str, str, dict]]:
        return [(ref, case, row) for ref, comp in self.comparisons.items() for case, row in regressions(comp)]


def run_all(cases: list[Case], mode: str, use_cache: bool = True, fixtures: Path | None = None, results: Path | None = None,
            baseline_path: Path | None = None, repeat: int = 1) -> Run:
    results = results or EVALS / "results"
    baseline_path = baseline_path or EVALS / "baseline.json"
    timestamp = unique_timestamp(results)
    out_dir = results / timestamp
    data = {"timestamp": timestamp, "mode": mode, "use_cache": use_cache, "repeat": repeat, "git": git_info(),
            "models": model_info(), "aborted": None, "cases": {}}
    for case in cases:
        print(f"[eval] {case.name}: running ({mode})...", flush=True)  # the pipeline's own logs go to <case>.log
        try:
            data["cases"][case.name] = run_case(case, mode, use_cache, fixtures, out_dir, repeat)
            m = data["cases"][case.name]
            print(f"[eval] {case.name}: {m['status']} in {m['latency_s']}s", flush=True)
        except Aborted as exc:
            data["aborted"] = str(exc)
            break
    write_json(results / f"{timestamp}.json", data)

    run = Run(data)
    if not data["aborted"]:
        prev = previous_run(results, mode, exclude=timestamp)
        if prev:
            run.comparisons["previous run " + prev["timestamp"]] = {
                n: compare_case(prev["cases"].get(n), c, prev["mode"], mode) for n, c in data["cases"].items()}
        if baseline_path.exists():
            base = json.loads(baseline_path.read_text())
            run.comparisons["baseline"] = {
                n: compare_case(base["cases"].get(n), c, base.get("mode", "replay"), mode) for n, c in data["cases"].items()}
    return run


# ====================================================================== printing

MARK = {"worse": "▼ WORSE", "better": "▲", "same": "", "info": ""}


def fmt(v) -> str:
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def render(run: Run) -> str:
    data = run.data
    out = [f"Eval run {data['timestamp']} | mode: {data['mode']} | git {data['git'].get('commit')}"
           f"{' (dirty)' if data['git'].get('dirty') else ''} | extract: {data['models'].get('extract')} "
           f"({data['models'].get('extract_effort')}), write: {data['models'].get('write')} ({data['models'].get('write_effort')})"]
    if data["mode"] == "from-facts":
        out.append("Facts-only run: extraction/grounding numbers come from facts.json, not recomputed; only writer metrics are compared.")
    if data.get("repeat", 1) > 1:
        out.append(f"Writer metrics ({', '.join(LABELS[k] for k in WRITER_METRICS)}) are the mean of {data['repeat']} writer runs per case.")
    refs = list(run.comparisons)
    for name, m in data["cases"].items():
        out += ["", f"=== {name} " + ("(FAILED: " + m.get("error", "") + ")" if m["status"] != "ok" else "") + "=" * 20]
        header = ["metric", "now"] + [f"vs {r.split(' ')[0]} {r.split(' ')[-1] if 'previous' in r else ''}".strip() for r in refs]
        rows = []
        lookup = {r: {row["metric"]: row for row in run.comparisons[r].get(name, [])} for r in refs}
        for metric in METRICS:
            cells = [LABELS[metric], fmt(m.get(metric))]
            for r in refs:
                row = lookup[r].get(metric)
                if row is None:
                    cells.append("")
                else:
                    cells.append(f"{fmt(row['ref'])} {MARK[row['verdict']]}".strip() + (f" ({row['note']})" if row["verdict"] == "worse" and row["note"] else ""))
            rows.append(cells)
        for k in WRITER_METRICS if m.get("writer_runs") else ():
            rows.append([f"  {LABELS[k]} per run:", ", ".join(str(r[k]) for r in m["writer_runs"])] + [""] * len(refs))
        if m.get("empty_section_names"):
            rows.append(["  empty:", ", ".join(m["empty_section_names"])] + [""] * len(refs))
        rate = cache_rate(m)
        if rate is not None:
            rows.append(["cache hit rate", f"{rate:.0%}"] + [""] * len(refs))
        widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
        for r in [header] + rows:
            out.append("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    if data["aborted"]:
        out += ["", f"ABORTED: {data['aborted']}. Remaining cases were not run; this run is not used for comparisons."]
    bad = run.regressions
    out += ["", f"{len(bad)} regression(s)" + (":" if bad else ".")]
    out += [f"  - [{ref.split(' ')[0]}] {case}: {LABELS.get(row['metric'], row['metric'])} {fmt(row['ref'])} -> {fmt(row['cur'])}"
            + (f" ({row['note']})" if row["note"] else "") for ref, case, row in bad]
    return "\n".join(out)


# ====================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Run the eval cases (record / replay / from-facts) and compare with earlier runs")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="run live and save search results + page texts + facts to evals/fixtures/<case>/")
    mode.add_argument("--from-facts", action="store_true", help="replay only writer + finalize from each case's facts.json")
    p.add_argument("--cases", help="comma-separated case names (default: all)")
    p.add_argument("--no-cache", action="store_true", help="redo fact extraction instead of reading .cache/extract")
    p.add_argument("--no-fail", action="store_true", help="exit 0 even if a metric got worse")
    p.add_argument("--set-baseline", action="store_true", help="after the run, pin its metrics as evals/baseline.json")
    p.add_argument("--list", action="store_true", help="list the cases and exit")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="run the writer N times per case (from the same facts) and average the writer metrics")
    args = p.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    cases = load_cases()
    if args.list:
        print("\n".join(f"{c.name}: {c.company} / {c.role} / {c.level} / {c.location}" for c in cases))
        return 0
    if args.cases:
        wanted = [n.strip() for n in args.cases.split(",")]
        unknown = [n for n in wanted if n not in {c.name for c in cases}]
        if unknown:
            print(f"unknown case(s): {', '.join(unknown)}. Known: {', '.join(c.name for c in cases)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c.name in wanted]
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    mode_name = "record" if args.record else "from-facts" if args.from_facts else "replay"
    try:
        run = run_all(cases, mode_name, use_cache=not args.no_cache, repeat=args.repeat)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(render(run))
    print(f"\nSaved {EVALS / 'results' / (run.data['timestamp'] + '.json')} (briefs, debug files and logs in the folder next to it)")
    if args.set_baseline:
        try:
            written = set_baseline(EVALS / "baseline.json", run.data)
            print(f"Baseline updated for: {', '.join(written)} -> evals/baseline.json (commit it)")
        except ValueError as exc:
            print(f"Baseline NOT updated: {exc}", file=sys.stderr)
            return 2
    if run.data["aborted"]:
        return 2
    return 1 if run.regressions and not args.no_fail else 0
