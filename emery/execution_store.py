"""Durable, sanitized metadata and audit storage for agent execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 2
DEFAULT_PATH = ROOT / "data" / "runtime" / "execution.db"
_SECRETS = re.compile(r"(?i)(authorization\s*:\s*bearer\s+|(?:api[_-]?key|token|password|secret|cookie)\s*[:=])[^\s,;&]+")


class RecordNotFoundError(LookupError):
    pass


class InvalidStatusTransition(ValueError):
    pass


def sanitize(value: Any, limit: int = 2_000) -> str:
    text = str(value or "")
    text = _SECRETS.sub("[REDACTED]", text)
    text = re.sub(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", "[REDACTED_TELEGRAM_TOKEN]", text)
    return text[:limit]


def digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()


def _clean_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = "[REDACTED]" if re.search(r"(?i)(token|secret|password|cookie|api[_-]?key|private[_-]?key)", key_text) else _clean_metadata(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_clean_metadata(item) for item in value]
    return sanitize(value)


class ExecutionStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        raw = str(path or os.getenv("EXECUTION_STORE_PATH") or DEFAULT_PATH)
        self.path = Path(raw).expanduser()
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._inspection_connection: sqlite3.Connection | None = None
        self._initialize()

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._inspection_connection is None:
            self._inspection_connection = self._connect()
        return self._inspection_connection

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, chat_id INTEGER, thread_id INTEGER, user_id INTEGER,
                    workspace TEXT NOT NULL, status TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, last_activity REAL NOT NULL, closed_at REAL
                );
                CREATE TABLE IF NOT EXISTS execution_jobs (
                    id TEXT PRIMARY KEY, session_id TEXT, kind TEXT NOT NULL, command_preview TEXT,
                    cwd TEXT, backend TEXT, status TEXT NOT NULL, pid INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                    started_at REAL, finished_at REAL,
                    FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, session_id TEXT, job_id TEXT,
                    actor_user_id INTEGER, chat_id INTEGER, thread_id INTEGER, kind TEXT NOT NULL,
                    action TEXT NOT NULL, status TEXT NOT NULL, command_preview TEXT,
                    arguments_preview TEXT, result_preview TEXT, created_at REAL NOT NULL,
                    duration_seconds REAL
                );
                CREATE INDEX IF NOT EXISTS jobs_session_idx ON execution_jobs(session_id, created_at);
                CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_events(created_at);
                CREATE TABLE IF NOT EXISTS chat_agent_sessions (
                    id TEXT PRIMARY KEY,
                    stable_key TEXT NOT NULL UNIQUE,
                    agent_name TEXT NOT NULL,
                    chat_id TEXT,
                    thread_id TEXT,
                    user_id TEXT,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    last_activity REAL NOT NULL,
                    last_reconciled_at REAL,
                    closed_at REAL
                );
                CREATE INDEX IF NOT EXISTS chat_sessions_scope_idx
                    ON chat_agent_sessions(chat_id, thread_id, user_id, agent_name);
                CREATE TABLE IF NOT EXISTS session_resource_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_session_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    closed_at REAL,
                    UNIQUE(resource_type, resource_id),
                    FOREIGN KEY(chat_session_id) REFERENCES chat_agent_sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS resource_bindings_chat_idx
                    ON session_resource_bindings(chat_session_id, status, created_at);
            """)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_audit'").fetchone():
                columns = {row[1] for row in db.execute("PRAGMA table_info(execution_audit)")}
                if "request_id" not in columns:
                    db.execute("ALTER TABLE execution_audit ADD COLUMN request_id TEXT")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if "id" in result:
            result.setdefault("session_id", result["id"])
            result.setdefault("job_id", result["id"])
        if "metadata_json" in result:
            try:
                result["metadata"] = json.loads(result["metadata_json"])
            except (TypeError, ValueError):
                result["metadata"] = {}
            if isinstance(result["metadata"], dict):
                result.setdefault("session_type", result["metadata"].get("_session_type"))
                result.setdefault("owner_id", result["metadata"].get("_owner_id"))
                if "result" in result["metadata"]:
                    result["result"] = result["metadata"]["result"]
                if "error" in result["metadata"]:
                    result["error"] = result["metadata"]["error"]
        return result

    def close(self) -> None:
        if self._inspection_connection is not None:
            self._inspection_connection.close()
            self._inspection_connection = None

    def __enter__(self) -> "ExecutionStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def create_session(self, *, chat_id: int | None = None, thread_id: int | None = None,
                       user_id: int | None = None, workspace: str = "/home/hudson",
                       metadata: Any = None, session_id: str | None = None,
                       session_type: str = "agent", owner_id: str | int | None = None) -> dict[str, Any]:
        session_id = session_id or f"sess_{uuid.uuid4().hex[:16]}"
        now = time.time()
        meta = dict(metadata or {}) if isinstance(metadata, dict) else {}
        meta.setdefault("_session_type", session_type)
        if owner_id is not None:
            meta.setdefault("_owner_id", owner_id)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO agent_sessions(id,chat_id,thread_id,user_id,workspace,status,metadata_json,created_at,last_activity) VALUES (?,?,?,?,?,'active',?,?,?)", (session_id, chat_id, thread_id, user_id if user_id is not None else owner_id, sanitize(workspace, 500), json.dumps(_clean_metadata(meta), sort_keys=True), now, now))
        return self.get_session(session_id) or {"session_id": session_id, "status": "active"}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            return self._row(db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone())

    def touch_session(self, session_id: str, *, status: str | None = None) -> None:
        with self._lock, self._connect() as db:
            if status is None:
                db.execute("UPDATE agent_sessions SET last_activity=? WHERE id=?", (time.time(), session_id))
            else:
                db.execute("UPDATE agent_sessions SET status=?,last_activity=? WHERE id=?", (sanitize(status, 64), time.time(), session_id))

    def close_session(self, session_id: str, *, status: str = "closed") -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE agent_sessions SET status=?,closed_at=?,last_activity=? WHERE id=?", (sanitize(status, 64), time.time(), time.time(), session_id))

    def _require(self, table: str, record_id: str) -> dict[str, Any]:
        value = self.get_session(record_id) if table == "session" else self.get_job(record_id)
        if value is None:
            raise RecordNotFoundError(f"{table} not found: {record_id}")
        return value

    def transition_session(self, session_id: str, status: str, *, expected_status: str | None = None) -> dict[str, Any]:
        current = self._require("session", session_id)
        if expected_status and current["status"] != expected_status:
            raise InvalidStatusTransition("unexpected session status")
        if current["status"] == "closed" and status != "closed":
            raise InvalidStatusTransition("closed sessions cannot be reopened")
        if status == "closed":
            self.close_session(session_id)
        else:
            self.touch_session(session_id, status=status)
        return self._require("session", session_id)

    def create_job(self, *, kind: str = "terminal", session_id: str | None = None,
                   command: str = "", cwd: str = "", backend: str = "",
                   metadata: Any = None, job_id: str | None = None,
                   status: str = "pending") -> dict[str, Any]:
        job_id = job_id or f"job_{uuid.uuid4().hex[:16]}"
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO execution_jobs(id,session_id,kind,command_preview,cwd,backend,status,metadata_json,created_at) VALUES (?,?,?,?,?,?,?, ?,?)", (job_id, session_id, sanitize(kind, 64), sanitize(command), sanitize(cwd, 500), sanitize(backend, 64), sanitize(status, 64), json.dumps(_clean_metadata(metadata or {}), sort_keys=True), time.time()))
        return self.get_job(job_id) or {"job_id": job_id, "status": status}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            return self._row(db.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone())

    def transition_job(self, job_id: str, status: str, *, expected_status: str | None = None,
                       exit_code: int | None = None, result: Any = None, error: str | None = None) -> dict[str, Any]:
        current = self._require("job", job_id)
        if expected_status and current["status"] != expected_status:
            raise InvalidStatusTransition("unexpected job status")
        if current["status"] in {"succeeded", "completed", "failed", "cancelled", "expired", "orphaned"}:
            raise InvalidStatusTransition("terminal job cannot transition")
        fields = ["status=?"]; values: list[Any] = [status]
        if status == "running": fields.append("started_at=?"); values.append(time.time())
        if status in {"succeeded", "completed", "failed", "cancelled", "expired", "orphaned"}: fields.append("finished_at=?"); values.append(time.time())
        if result is not None or error is not None: fields.append("metadata_json=?"); values.append(json.dumps(_clean_metadata({"result": result, "error": error}), sort_keys=True))
        values.append(job_id)
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE execution_jobs SET {','.join(fields)} WHERE id=?", values)
        return self._require("job", job_id)

    def update_job(self, job_id: str, *, status: str | None = None, pid: int | None = None, metadata: Any = None, started: bool = False, finished: bool = False) -> None:
        fields: list[str] = []; values: list[Any] = []
        if status is not None: fields.append("status=?"); values.append(sanitize(status, 64))
        if pid is not None: fields.append("pid=?"); values.append(pid)
        if metadata is not None: fields.append("metadata_json=?"); values.append(json.dumps(_clean_metadata(metadata), sort_keys=True))
        if started: fields.append("started_at=?"); values.append(time.time())
        if finished: fields.append("finished_at=?"); values.append(time.time())
        if fields:
            values.append(job_id)
            with self._lock, self._connect() as db:
                db.execute(f"UPDATE execution_jobs SET {','.join(fields)} WHERE id=?", values)

    def list_jobs(self, *, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM execution_jobs " + ("WHERE session_id=? " if session_id else "") + "ORDER BY created_at DESC LIMIT ?", ((session_id, limit) if session_id else (limit,))).fetchall()
            return [self._row(row) for row in rows]

    # Durable chat/agent session state is deliberately separate from the
    # process-backed ``agent_sessions`` table above.  The latter records
    # individual execution attempts; these records identify the stable chat
    # scope that owns resources across application restarts.
    @staticmethod
    def _scope_value(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:256] if text else None

    def get_or_create_chat_session(
        self,
        *,
        chat_id: Any,
        thread_id: Any = None,
        user_id: Any = None,
        agent_name: str = "emery",
        metadata: Any = None,
    ) -> dict[str, Any]:
        """Return the deterministic chat/agent record for one ownership scope."""
        agent = sanitize(agent_name, 64) or "emery"
        chat = self._scope_value(chat_id)
        thread = self._scope_value(thread_id)
        user = self._scope_value(user_id)
        stable_key = json.dumps(
            {"agent": agent, "chat_id": chat, "thread_id": thread, "user_id": user},
            sort_keys=True,
            separators=(",", ":"),
        )
        session_id = f"chat_{hashlib.sha256(stable_key.encode('utf-8')).hexdigest()[:24]}"
        now = time.time()
        clean_metadata = _clean_metadata(metadata or {})
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO chat_agent_sessions(
                    id, stable_key, agent_name, chat_id, thread_id, user_id,
                    status, metadata_json, created_at, last_activity
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(stable_key) DO UPDATE SET
                    status='active', last_activity=excluded.last_activity,
                    metadata_json=CASE WHEN excluded.metadata_json='{}'
                        THEN chat_agent_sessions.metadata_json
                        ELSE excluded.metadata_json END
                """,
                (
                    session_id, stable_key, agent, chat, thread, user,
                    json.dumps(clean_metadata, sort_keys=True), now, now,
                ),
            )
        return self.get_chat_session(session_id) or {
            "id": session_id,
            "session_id": session_id,
            "stable_key": stable_key,
            "status": "active",
        }

    def get_chat_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM chat_agent_sessions WHERE id=?", (str(session_id),)
            ).fetchone()
        return self._row(row)

    def get_chat_session_for_scope(
        self, *, chat_id: Any, thread_id: Any = None, user_id: Any = None,
        agent_name: str = "emery",
    ) -> dict[str, Any] | None:
        agent = sanitize(agent_name, 64) or "emery"
        values = (self._scope_value(chat_id), self._scope_value(thread_id), self._scope_value(user_id), agent)
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM chat_agent_sessions
                WHERE chat_id IS ? AND thread_id IS ? AND user_id IS ? AND agent_name=?
                """,
                values,
            ).fetchone()
        return self._row(row)

    def touch_chat_session(self, session_id: str, *, metadata: Any = None) -> None:
        now = time.time()
        fields = ["last_activity=?"]
        values: list[Any] = [now]
        if metadata is not None:
            fields.append("metadata_json=?")
            values.append(json.dumps(_clean_metadata(metadata), sort_keys=True))
        values.append(str(session_id))
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE chat_agent_sessions SET {','.join(fields)} WHERE id=?", values)

    def close_chat_session(self, session_id: str, *, status: str = "closed") -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE chat_agent_sessions SET status=?, closed_at=?, last_activity=? WHERE id=?",
                (sanitize(status, 64), now, now, str(session_id)),
            )

    def mark_chat_session_reconciled(self, session_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE chat_agent_sessions SET last_reconciled_at=?, last_activity=? WHERE id=?",
                (time.time(), time.time(), str(session_id)),
            )

    def bind_resource(
        self,
        *,
        chat_session_id: str,
        resource_type: str,
        resource_id: str,
        metadata: Any = None,
        status: str = "active",
    ) -> dict[str, Any]:
        """Bind a runtime resource exactly once to a durable chat session.

        The unique resource key prevents a resource created by one chat from
        silently being reassigned to another chat after a restart.
        """
        resource_kind = sanitize(resource_type, 64)
        resource_key = sanitize(resource_id, 256)
        if not resource_kind or not resource_key:
            raise ValueError("resource_type and resource_id are required")
        now = time.time()
        clean_metadata = json.dumps(_clean_metadata(metadata or {}), sort_keys=True)
        with self._lock, self._connect() as db:
            if not db.execute("SELECT 1 FROM chat_agent_sessions WHERE id=?", (str(chat_session_id),)).fetchone():
                raise RecordNotFoundError(f"chat session not found: {chat_session_id}")
            existing = db.execute(
                "SELECT * FROM session_resource_bindings WHERE resource_type=? AND resource_id=?",
                (resource_kind, resource_key),
            ).fetchone()
            if existing is not None and str(existing["chat_session_id"]) != str(chat_session_id):
                raise InvalidStatusTransition("resource is already owned by another chat session")
            db.execute(
                """
                INSERT INTO session_resource_bindings(
                    chat_session_id, resource_type, resource_id, status,
                    metadata_json, created_at, last_seen_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(resource_type, resource_id) DO UPDATE SET
                    status=excluded.status, metadata_json=excluded.metadata_json,
                    last_seen_at=excluded.last_seen_at,
                    closed_at=CASE WHEN excluded.status IN ('closed','stale','orphaned')
                        THEN COALESCE(session_resource_bindings.closed_at, excluded.last_seen_at)
                        ELSE NULL END
                """,
                (str(chat_session_id), resource_kind, resource_key, sanitize(status, 64), clean_metadata, now, now),
            )
        return self.get_resource_binding(resource_kind, resource_key) or {}

    def get_resource_binding(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM session_resource_bindings WHERE resource_type=? AND resource_id=?",
                (sanitize(resource_type, 64), sanitize(resource_id, 256)),
            ).fetchone()
        return self._row(row)

    def update_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        status: str | None = None,
        metadata: Any = None,
    ) -> dict[str, Any] | None:
        current = self.get_resource_binding(resource_type, resource_id)
        if current is None:
            return None
        fields = ["last_seen_at=?"]
        values: list[Any] = [time.time()]
        if status is not None:
            fields.append("status=?")
            values.append(sanitize(status, 64))
            if status in {"closed", "stale", "orphaned", "recovery_pending"}:
                fields.append("closed_at=COALESCE(closed_at, ?)")
                values.append(time.time())
        if metadata is not None:
            fields.append("metadata_json=?")
            values.append(json.dumps(_clean_metadata(metadata), sort_keys=True))
        values.extend([sanitize(resource_type, 64), sanitize(resource_id, 256)])
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE session_resource_bindings SET {','.join(fields)} WHERE resource_type=? AND resource_id=?",
                values,
            )
        return self.get_resource_binding(resource_type, resource_id)

    def list_resource_bindings(
        self, *, chat_session_id: str | None = None, statuses: tuple[str, ...] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2_000))
        clauses: list[str] = []
        values: list[Any] = []
        if chat_session_id is not None:
            clauses.append("chat_session_id=?")
            values.append(str(chat_session_id))
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(sanitize(status, 64) for status in statuses)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM session_resource_bindings{where} ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def audit(self, *, action: str, kind: str, status: str, request_id: str = "", session_id: str = "", job_id: str = "", actor_user_id: int | None = None, chat_id: int | None = None, thread_id: int | None = None, command: str = "", arguments: str = "", result: str = "", duration_seconds: float | None = None) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT INTO audit_events(request_id,session_id,job_id,actor_user_id,chat_id,thread_id,kind,action,status,command_preview,arguments_preview,result_preview,created_at,duration_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (sanitize(request_id, 128), sanitize(session_id, 128), sanitize(job_id, 128), actor_user_id, chat_id, thread_id, sanitize(kind, 64), sanitize(action, 128), sanitize(status, 64), sanitize(command), sanitize(arguments), sanitize(result), time.time(), duration_seconds))
            return int(cursor.lastrowid)

    def recent_audit(self, *, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM audit_events " + ("WHERE session_id=? " if session_id else "") + "ORDER BY id DESC LIMIT ?", ((session_id, limit) if session_id else (limit,))).fetchall()
            return [dict(row) for row in rows]

    def list_audit(self, *, resource_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.recent_audit(limit=limit)
        return [row for row in rows if not resource_type or row.get("kind") == resource_type]

    def record_command_audit(self, *, action: str, command: str, cwd: str = "", session_id: str = "", job_id: str = "", request_id: str = "", details: Any = None, status: str = "completed") -> dict[str, Any]:
        event_id = self.audit(kind="command", action=action, status=status, request_id=request_id, session_id=session_id, job_id=job_id, command=command, arguments=cwd, result=json.dumps(details or {}, default=str))
        return {"event_id": str(event_id), "command": sanitize(command), "details": _clean_metadata(details or {})}

    def record_browser_audit(self, *, action: str, url: str = "", summary: str = "", session_id: str = "", request_id: str = "", details: Any = None, status: str = "completed") -> dict[str, Any]:
        safe_url = re.sub(r"(?i)([?&](?:token|api[_-]?key|password|secret)=)[^&#\s]+", r"\1%5BREDACTED%5D", url)
        event_id = self.audit(kind="browser", action=action, status=status, request_id=request_id, session_id=session_id, arguments=safe_url, result=summary)
        return {"event_id": str(event_id), "url": safe_url, "summary": sanitize(summary), "details": details or {}}


_store: ExecutionStore | None = None
_store_lock = threading.Lock()


def get_execution_store() -> ExecutionStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ExecutionStore()
        return _store


__all__ = ["ExecutionStore", "InvalidStatusTransition", "RecordNotFoundError", "SCHEMA_VERSION", "digest", "get_execution_store", "sanitize"]
