from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class JobStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get("ROUTER_DB_PATH", "/data/emeryrouter.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, request_type TEXT NOT NULL,
                priority INTEGER NOT NULL, sequence INTEGER NOT NULL, idempotency_key TEXT,
                body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL,
                finished_at REAL, result BLOB, content_type TEXT, error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source, idempotency_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_pending_order ON jobs(status, priority, sequence)")
            # A process can die after any backend call. Requeue accepted work
            # with its original idempotency key and stable FIFO sequence.
            db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (time.time(),))

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def enqueue(self, source: str, request_type: str, priority: int, body: dict[str, Any], key: str | None):
        now = time.time()
        job_id = uuid.uuid4().hex
        encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                existing = db.execute("SELECT id FROM jobs WHERE source=? AND idempotency_key=?", (source, key)).fetchone()
                if existing:
                    db.execute("COMMIT")
                    return existing["id"], False
            sequence = db.execute("SELECT COALESCE(MAX(sequence), 0)+1 FROM jobs").fetchone()[0]
            db.execute("""INSERT INTO jobs(id,source,request_type,priority,sequence,idempotency_key,body,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'queued',?,?)""", (job_id, source, request_type, priority, sequence, key, encoded, now, now))
            db.execute("COMMIT")
        return job_id, True

    def next_job(self, max_attempts: int):
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='failed',error='retry limit reached',finished_at=?,updated_at=? WHERE status='queued' AND attempts>=?", (now, now, max_attempts))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 AND updated_at<=? ORDER BY priority ASC,sequence ASC LIMIT 1", (now,)).fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,started_at=?,updated_at=? WHERE id=? AND status='queued'", (now, now, row["id"]))
            db.execute("COMMIT")
            return dict(row) | {"attempts": row["attempts"] + 1}

    def complete(self, job_id: str, result: bytes, content_type: str):
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'complete' END,result=?,content_type=?,finished_at=?,updated_at=? WHERE id=? AND status='running'", (result, content_type, now, now, job_id))

    def retry_or_fail(self, job: dict[str, Any], error: str, max_attempts: int, delay: float):
        now = time.time()
        status = "queued" if job["attempts"] < max_attempts else "failed"
        safe_error = error[:800]
        with self._lock, self._connect() as db:
            if status == "queued":
                db.execute("UPDATE jobs SET status='queued',error=?,updated_at=? WHERE id=? AND status='running' AND cancel_requested=0", (safe_error, now + delay, job["id"]))
                # next_job respects retry_after using updated_at.
                db.execute("UPDATE jobs SET status='failed',error='cancelled',finished_at=?,updated_at=? WHERE id=? AND cancel_requested=1", (now, now, job["id"]))
            else:
                db.execute("UPDATE jobs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=? AND status='running'", (safe_error, now, now, job["id"]))

    def status(self, job_id: str, source: str):
        with self._connect() as db:
            row = db.execute("SELECT id,source,request_type,priority,status,attempts,created_at,updated_at,started_at,finished_at,error,cancel_requested FROM jobs WHERE id=? AND source=?", (job_id, source)).fetchone()
            return dict(row) if row else None

    def result(self, job_id: str, source: str):
        with self._connect() as db:
            row = db.execute("SELECT status,result,content_type,error FROM jobs WHERE id=? AND source=?", (job_id, source)).fetchone()
            return dict(row) if row else None

    def cancel(self, job_id: str, source: str):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=? AND source=?", (job_id, source)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,finished_at=?,updated_at=? WHERE id=?", (time.time(), time.time(), job_id))
            elif row["status"] == "running":
                # Keep an active model response intact; discard only its final
                # result if the authenticated owner cancels while it runs.
                db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (time.time(), job_id))
            return row["status"]

    def fail_pending(self, job_id: str, error: str):
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=?,finished_at=?,updated_at=? WHERE id=? AND status='queued'", (error[:800], now, now, job_id))

    def queue_position(self, job_id: str):
        with self._connect() as db:
            row = db.execute("SELECT priority,sequence,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "queued":
                return 0
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND (priority<? OR (priority=? AND sequence<=?))", (row["priority"], row["priority"], row["sequence"])).fetchone()[0]
            return count

    def pending(self, max_attempts: int):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running') AND attempts<?", (max_attempts,)).fetchone()[0]

    def pending_interactive(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE request_type='interactive' AND source IN ('portal','jellyfin') AND status IN ('queued','running')").fetchone()[0]
