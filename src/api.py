"""Web API for the brief pipeline: uvicorn --app-dir src --factory api:serve  (see README "Web app").

POST /briefs queues a job and returns its id at once; one background worker runs the pipeline, one job at a time
(llm.py keeps per-run usage and fallback state in module globals, and Groq's limits are shared anyway).
GET /briefs/{id} reports status, the current step and, when done, the brief. Jobs live in SQLite (jobs.py).

If APP_ACCESS_CODE is set, every endpoint but /health needs it in the X-Access-Code header. In production (the Docker image)
REQUIRE_ACCESS_CODE=1 makes the server refuse to start without one, PRIVATE_BRIEFS=1 turns off listing everyone's briefs,
and MAX_BRIEFS_PER_DAY caps how many new briefs start per UTC day (shared Groq/Tavily quota).
"""
from __future__ import annotations

import hmac
import os
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Callable, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import llm
from brief import BriefResult, RunStats
from jobs import DB_PATH, JobStore, input_key
from location import DEFAULT_LOCATION

ACCESS_ENV = "APP_ACCESS_CODE"
ACCESS_HEADER = "X-Access-Code"
MAX_QUEUED = 20  # more waiting jobs than this and new requests are refused (429)
DAILY_ENV, DEFAULT_DAILY = "MAX_BRIEFS_PER_DAY", 30  # new briefs per UTC day (0 = no limit)
REQUIRE_ENV, PRIVATE_ENV = "REQUIRE_ACCESS_CODE", "PRIVATE_BRIEFS"
MIN_ACCESS_CODE = 16  # characters, when a code is required: there is no lockout after wrong guesses


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def daily_limit() -> int:
    raw = (os.getenv(DAILY_ENV) or "").strip()
    if not raw:
        return DEFAULT_DAILY
    if not raw.isdigit():
        raise ValueError(f"{DAILY_ENV} must be a whole number (0 = no limit), got '{raw}'")
    return int(raw)
STEPS = ("search", "fetch", "extract", "ground", "coverage", "search_more", "write", "finalize")
Runner = Callable[..., BriefResult]  # graph.run_pipeline's signature


def default_runner(company, role, level, jd, location, stats) -> BriefResult:
    from graph import run_pipeline  # imported lazily: tests swap the runner and never build the graph

    return run_pipeline(company, role, level, jd, stats=stats, location=location)


# ---------- request / response models ----------

class BriefRequest(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    level: Literal["intern", "new-grad", "experienced"] = "new-grad"
    location: str = Field(DEFAULT_LOCATION, min_length=1, max_length=100)
    jd: str | None = Field(None, max_length=20000)
    force: bool = False  # run again even if a recent brief with the same inputs exists

    @field_validator("company", "role", "location")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("jd")
    @classmethod
    def blank_jd_is_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v is not None else None


def debug_summary(stats: RunStats) -> dict:
    """The numbers worth showing next to a brief (the full report is at /briefs/{id}/debug)."""
    usage = llm.usage_totals()
    return {
        "sources": stats.sources_total,
        "sources_with_text": stats.sources_with_text,
        "facts_grounded": stats.facts_grounded,
        "facts_rejected": len(stats.rejected),
        "facts_by_section": {sec: sum(f["section"] == sec for f in stats.grounded) for sec in ("company", "interview", "role", "timeline")},
        "sentences_dropped": len(stats.drops),
        "sentences_rewritten": len(stats.rewrites),
        "retry_sections": stats.retry_sections,
        "general_guidance_sections": stats.general_sections,
        "llm": usage["models"],
        "fallback_calls": usage["fallback_calls"],
        "cost_usd": usage["cost"],
        "tokens": {"input": usage["input"], "output": usage["output"], "reasoning": usage["reasoning"]},
        "steps": stats.steps,
    }


def job_view(job: dict, reused: bool = False) -> dict:
    view = {k: job[k] for k in ("status", "step", "steps", "company", "role", "level", "location", "created_at", "started_at",
                                "finished_at", "error")}
    view.update(job_id=job["id"], has_jd=bool(job["jd"]), reused=reused)
    if job["status"] == "done":
        view.update(brief_markdown=job["brief_md"], debug_summary=job["summary"])
    elif job["status"] == "failed" and job["summary"]:
        view["debug_summary"] = job["summary"]
    return view


# ---------- the worker ----------

class Worker:
    """Runs queued jobs one at a time in a background thread. process_next() is also what tests call directly."""

    def __init__(self, store: JobStore, runner: Runner = default_runner):
        self.store, self.runner = store, runner
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="brief-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        while not self.stopping.is_set():
            if not self.process_next():
                self.wake.wait(timeout=2)
                self.wake.clear()

    def process_next(self) -> bool:
        """Run the oldest queued job to completion. False if nothing was queued."""
        job = self.store.claim_next()
        if job is None:
            return False
        jid = job["id"]
        llm.reset_usage()
        llm.reset_fallback()  # each brief is its own run: it starts on the primary provider

        def progress(step: str | None, line: str | None) -> None:
            if step:
                self.store.set_step(jid, step)
            if line:
                self.store.add_line(jid, line)

        stats = RunStats(on_progress=progress)
        try:
            result = self.runner(job["company"], job["role"], job["level"], job["jd"], job["location"], stats)
        except llm.LLMError as exc:  # daily limit, budget, bad key, every extraction failed: a clear message
            self.store.fail(jid, str(exc), stats.debug_report(), debug_summary(stats))
        except Exception as exc:  # a bug: keep the worker alive and the traceback in the job's debug report
            print(f"[api] job {jid} crashed:\n{traceback.format_exc()}", file=sys.stderr)
            self.store.fail(jid, f"Internal error: {type(exc).__name__}: {exc}",
                            stats.debug_report() + "\n# Traceback\n\n```\n" + traceback.format_exc() + "```\n", debug_summary(stats))
        else:
            self.store.finish(jid, result.text, result.stats.debug_report(result.sources), debug_summary(result.stats))
        return True


# ---------- access code ----------

def require_access_code(x_access_code: str | None = Header(None)) -> None:
    """When APP_ACCESS_CODE is set, the X-Access-Code header must match it (constant-time compare). Unset = open (local use)."""
    expected = os.getenv(ACCESS_ENV, "")
    if expected and not hmac.compare_digest((x_access_code or "").encode(), expected.encode()):
        raise HTTPException(status_code=401, detail=f"Missing or wrong access code (send it in the {ACCESS_HEADER} header).")


# ---------- the app ----------

def create_app(db_path: str = DB_PATH, runner: Runner = default_runner, start_worker: bool = True, store: JobStore | None = None) -> FastAPI:
    daily_limit()  # a bad MAX_BRIEFS_PER_DAY fails at startup, not on the first request
    store = store or JobStore(db_path)
    worker = Worker(store, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        requeued = store.requeue_interrupted()
        if requeued:
            print(f"[api] {requeued} job(s) interrupted by the last shutdown will run again", file=sys.stderr)
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="Career-coach brief API", lifespan=lifespan)
    app.state.store, app.state.worker = store, worker
    guarded = [Depends(require_access_code)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "access_code_required": bool(os.getenv(ACCESS_ENV)), "private_briefs": _flag(PRIVATE_ENV)}

    @app.get("/auth", dependencies=guarded)
    def auth() -> dict:
        """200 if the access code is right (or none is needed): lets the web app check a code before using it."""
        return {"ok": True}

    @app.post("/briefs", dependencies=guarded, status_code=202)
    def create_brief(req: BriefRequest) -> dict:
        if not req.force and (existing := store.find_reusable(input_key(req.company, req.role, req.level, req.location, req.jd))):
            return job_view(existing, reused=True)  # same inputs: a brief from the last REUSE_DAYS days, or one in progress
        limit = daily_limit()
        if limit:
            now = store.now()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            started = store.count_created_since(day_start)
            if started >= limit:
                left = day_start + timedelta(days=1) - now
                hours, minutes = divmod(int(left.total_seconds()) // 60, 60)
                raise HTTPException(status_code=429, detail=(
                    f"Daily limit reached: {started} new briefs have been started today (limit {limit}). New briefs can start "
                    f"again at 00:00 UTC, in {hours}h {minutes}m. Briefs built in the last 7 days with the same inputs still open."))
        if store.count("queued") >= MAX_QUEUED:
            raise HTTPException(status_code=429, detail=f"{MAX_QUEUED} briefs are already waiting; try again later.")
        job = store.create(req.company, req.role, req.level, req.location, req.jd)
        worker.wake.set()
        return job_view(job)

    @app.get("/briefs", dependencies=guarded)
    def list_briefs(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
        if _flag(PRIVATE_ENV):  # everyone shares one access code; JDs can hold student details
            raise HTTPException(status_code=403, detail="Listing briefs is turned off on this server (PRIVATE_BRIEFS).")
        return [{"job_id": j["id"], **{k: v for k, v in j.items() if k != "id"}} for j in store.recent(limit)]

    @app.get("/briefs/{job_id}", dependencies=guarded)
    def get_brief(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such brief.")
        return job_view(job)

    @app.get("/briefs/{job_id}/debug", dependencies=guarded)
    def get_debug(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such brief.")
        return {"job_id": job_id, "status": job["status"], "debug_markdown": job["debug_md"]}

    return app


def serve() -> FastAPI:
    """The uvicorn factory: reads .env (API keys, APP_ACCESS_CODE), opens .cache/jobs.db, starts the worker. A factory rather
    than a module-level app, so importing this module (tests) creates no database and starts no thread."""
    load_dotenv()
    code = os.getenv(ACCESS_ENV, "")
    if _flag(REQUIRE_ENV) and len(code) < MIN_ACCESS_CODE:
        raise RuntimeError(f"{REQUIRE_ENV} is on but {ACCESS_ENV} is {'not set' if not code else 'too short'}: set a random code of "
                           f"at least {MIN_ACCESS_CODE} characters as a secret (refusing to start without one).")
    return create_app()
