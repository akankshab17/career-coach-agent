"""SQLite job store for the web app: one row per brief request, so jobs survive a restart.

The table is also the queue: the worker takes the oldest 'queued' row. Every call opens its own connection, so the API
threads and the worker thread never share one.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(".cache", "jobs.db")
REUSE_DAYS = 7  # a finished brief with the same inputs, younger than this, is returned instead of running again
STATUSES = ("queued", "running", "done", "failed")
JSON_COLUMNS = ("steps", "summary")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    step TEXT,
    steps TEXT NOT NULL DEFAULT '[]',
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    level TEXT NOT NULL,
    location TEXT NOT NULL,
    jd TEXT,
    input_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    brief_md TEXT,
    debug_md TEXT,
    summary TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs (input_key, status, finished_at);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def input_key(company: str, role: str, level: str, location: str, jd: str | None) -> str:
    """Same inputs (ignoring case and surrounding spaces; the JD exactly) -> same key."""
    parts = [company.strip().lower(), role.strip().lower(), level, location.strip().lower(), (jd or "").strip()]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class JobStore:
    def __init__(self, path: str = DB_PATH, now=utcnow):
        self.path, self.now = path, now
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")  # the worker writes progress while the API reads
        return db

    def _stamp(self) -> str:
        return self.now().isoformat()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        job = dict(row)
        for col in JSON_COLUMNS:
            job[col] = json.loads(job[col]) if job[col] else ([] if col == "steps" else None)
        return job

    # ---- creating / finding ----

    def create(self, company: str, role: str, level: str, location: str, jd: str | None) -> dict:
        job_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO jobs (id, status, company, role, level, location, jd, input_key, created_at) "
                       "VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                       (job_id, company.strip(), role.strip(), level, location.strip(), jd, input_key(company, role, level, location, jd),
                        self._stamp()))
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with closing(self._connect()) as db:
            return self._row(db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def find_reusable(self, key: str) -> dict | None:
        """A job with these inputs that is still queued/running, or finished successfully less than REUSE_DAYS ago."""
        cutoff = (self.now() - timedelta(days=REUSE_DAYS)).isoformat()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE input_key = ? AND (status IN ('queued', 'running') OR (status = 'done' AND finished_at >= ?)) "
                "ORDER BY created_at DESC LIMIT 1", (key, cutoff)).fetchone()
        return self._row(row)

    def recent(self, limit: int = 20) -> list[dict]:
        """Newest first, without the large text columns."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id, status, step, company, role, level, location, created_at, finished_at, error "
                              "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count(self, status: str) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)).fetchone()[0]

    # ---- the worker's side ----

    def claim_next(self) -> dict | None:
        """Mark the oldest queued job running and return it (None if nothing is queued)."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status = 'running', started_at = ?, step = NULL, steps = '[]' WHERE id = ?",
                       (self._stamp(), row["id"]))
        return self.get(row["id"])

    def set_step(self, job_id: str, step: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE jobs SET step = ? WHERE id = ?", (step, job_id))

    def add_line(self, job_id: str, line: str) -> None:
        with closing(self._connect()) as db, db:
            steps = json.loads(db.execute("SELECT steps FROM jobs WHERE id = ?", (job_id,)).fetchone()["steps"])
            db.execute("UPDATE jobs SET steps = ? WHERE id = ?", (json.dumps(steps + [line]), job_id))

    def finish(self, job_id: str, brief_md: str, debug_md: str, summary: dict) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE jobs SET status = 'done', step = 'done', finished_at = ?, brief_md = ?, debug_md = ?, summary = ?, "
                       "error = NULL WHERE id = ?", (self._stamp(), brief_md, debug_md, json.dumps(summary), job_id))

    def fail(self, job_id: str, error: str, debug_md: str | None = None, summary: dict | None = None) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, debug_md = ?, summary = ? WHERE id = ?",
                       (self._stamp(), error, debug_md, json.dumps(summary) if summary else None, job_id))

    def requeue_interrupted(self) -> int:
        """After a restart: jobs that were running when the server stopped run again from the start (the extraction
        cache makes the repeated work cheap). Returns how many."""
        with closing(self._connect()) as db, db:
            return db.execute("UPDATE jobs SET status = 'queued', step = NULL, steps = '[]', started_at = NULL "
                              "WHERE status = 'running'").rowcount
