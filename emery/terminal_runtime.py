"""Process-backed terminal sessions and jobs for Emery.

This module is intentionally independent from Emery's Telegram/tool code.  It
provides a small runtime that an agent adapter can use for three execution
shapes:

* :meth:`TerminalBroker.exec` for a bounded one-shot command;
* :meth:`TerminalBroker.start_session` for a persistent interactive PTY; and
* :meth:`TerminalBroker.start_job` for a background process.

The broker owns lifecycle and resource limits.  It does not make approval or
authorization decisions; callers should perform those checks before invoking
an operation.  Metadata is optionally persisted as an atomic JSON document so
that a process restart can report previously active resources as orphaned
instead of pretending they are still usable.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import pty
import signal
import tempfile
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol


PROTOCOL_VERSION = 1
DEFAULT_CWD = "/home/hudson"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_CHARS = 12_000
MAX_COMMAND_CHARS = 4_000
MAX_OUTPUT_CHARS = 100_000
MAX_SESSION_INPUT_CHARS = 16_000


def _safe_environment() -> dict[str, str]:
    """Pass only routine process settings into agent-owned processes."""

    safe_names = {
        "HOME", "LANG", "LC_ALL", "PATH", "PWD", "SHELL", "TERM", "TMPDIR",
        "USER", "VIRTUAL_ENV", "CONDA_PREFIX", "UV_PROJECT_ENVIRONMENT",
    }
    blocked_fragments = (
        "API_KEY", "AUTH", "COOKIE", "CREDENTIAL", "PASSWORD", "PRIVATE_KEY",
        "SECRET", "TOKEN",
    )
    return {
        key: value
        for key, value in os.environ.items()
        if key in safe_names or not any(fragment in key.upper() for fragment in blocked_fragments)
    }


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


class TerminalRuntimeError(RuntimeError):
    """Base exception for invalid runtime operations."""


class TerminalNotFound(TerminalRuntimeError):
    """Raised when a session or job ID is unknown."""


class TerminalExpired(TerminalRuntimeError):
    """Raised when an operation targets an expired resource."""


class TerminalClosed(TerminalRuntimeError):
    """Raised when the broker has been closed."""


@dataclass(frozen=True)
class TerminalRuntimeConfig:
    """Resource and lifecycle limits for one broker instance."""

    default_cwd: str = DEFAULT_CWD
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: float = 120.0
    default_max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    max_output_chars: int = MAX_OUTPUT_CHARS
    session_idle_timeout_seconds: float = 30 * 60
    session_max_lifetime_seconds: float = 24 * 60 * 60
    job_max_lifetime_seconds: float = 60 * 60
    max_sessions: int = 8
    max_jobs: int = 16
    metadata_path: str | Path | None = None
    reaper_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.max_timeout_seconds < 1:
            raise ValueError("max_timeout_seconds must be at least one second")
        if self.max_output_chars < 1_000:
            raise ValueError("max_output_chars must be at least 1000")
        if self.max_sessions < 1 or self.max_jobs < 1:
            raise ValueError("resource limits must be positive")


@dataclass(frozen=True)
class ExecRequest:
    """A bounded, one-shot shell execution request."""

    command: str
    request_id: str = field(default_factory=lambda: _id("req"))
    cwd: str | None = None
    timeout_seconds: float | None = None
    max_output_chars: int | None = None


@dataclass(frozen=True)
class SessionStartRequest:
    """Request to create a persistent interactive shell session."""

    request_id: str = field(default_factory=lambda: _id("req"))
    cwd: str | None = None
    idle_timeout_seconds: float | None = None
    max_lifetime_seconds: float | None = None


@dataclass(frozen=True)
class JobStartRequest:
    """Request to launch a bounded background shell process."""

    command: str
    request_id: str = field(default_factory=lambda: _id("req"))
    cwd: str | None = None
    max_lifetime_seconds: float | None = None
    max_output_chars: int | None = None


@dataclass(frozen=True)
class ExecResult:
    protocol_version: int
    request_id: str
    status: str
    command: str
    cwd: str
    exit_code: int | None
    output: str
    output_truncated: bool
    started_at: float
    finished_at: float
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


@dataclass(frozen=True)
class SessionRecord:
    protocol_version: int
    session_id: str
    request_id: str
    status: str
    cwd: str
    pid: int | None
    created_at: float
    last_activity_at: float
    expires_at: float
    output_truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class JobRecord:
    protocol_version: int
    job_id: str
    request_id: str
    status: str
    command: str
    cwd: str
    pid: int | None
    created_at: float
    started_at: float
    finished_at: float | None
    expires_at: float
    exit_code: int | None = None
    output: str = ""
    output_truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class OutputChunk:
    """Bounded output returned from a session or job read."""

    resource_id: str
    output: str
    output_truncated: bool
    status: str


class TerminalRuntime(Protocol):
    async def exec(self, request: ExecRequest) -> ExecResult: ...

    async def start_session(self, request: SessionStartRequest | None = None) -> SessionRecord: ...

    async def write_session(self, session_id: str, input_text: str) -> int: ...

    async def read_session(self, session_id: str, max_output_chars: int | None = None) -> OutputChunk: ...

    async def close_session(self, session_id: str, reason: str = "closed") -> SessionRecord: ...

    async def start_job(self, request: JobStartRequest) -> JobRecord: ...

    async def get_job(self, job_id: str) -> JobRecord: ...

    async def read_job(self, job_id: str, max_output_chars: int | None = None) -> OutputChunk: ...

    async def cancel_job(self, job_id: str, reason: str = "cancelled") -> JobRecord: ...


class _BoundedBuffer:
    def __init__(self, limit: int):
        self.limit = limit
        self._chunks: deque[str] = deque()
        self._size = 0
        self.truncated = False

    def append(self, value: str) -> None:
        if not value:
            return
        if len(value) > self.limit:
            value = value[-self.limit :]
            self._chunks.clear()
            self._size = 0
            self.truncated = True
        while self._size + len(value) > self.limit and self._chunks:
            removed = self._chunks.popleft()
            self._size -= len(removed)
            self.truncated = True
        self._chunks.append(value)
        self._size += len(value)

    def take(self, limit: int) -> tuple[str, bool]:
        limit = max(1, min(limit, self.limit))
        output = ""
        while self._chunks and len(output) < limit:
            chunk = self._chunks.popleft()
            remaining = limit - len(output)
            if len(chunk) > remaining:
                output += chunk[:remaining]
                self._chunks.appendleft(chunk[remaining:])
                self._size -= remaining
            else:
                output += chunk
                self._size -= len(chunk)
        was_truncated = self.truncated
        if not self._chunks:
            self.truncated = False
        return output, was_truncated

    def snapshot(self) -> tuple[str, bool]:
        return "".join(self._chunks), self.truncated


class _MetadataStore:
    """Small atomic JSON metadata store; no schema migration is required."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path).expanduser() if path else None

    def load(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {"version": 1, "sessions": {}, "jobs": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "sessions": {}, "jobs": {}}
        if not isinstance(value, dict):
            return {"version": 1, "sessions": {}, "jobs": {}}
        value.setdefault("version", 1)
        value.setdefault("sessions", {})
        value.setdefault("jobs", {})
        return value

    def save(self, value: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)


@dataclass
class _SessionHandle:
    record: SessionRecord
    master_fd: int
    child_pid: int
    output: _BoundedBuffer
    pump_task: asyncio.Task[None]


@dataclass
class _JobHandle:
    record: JobRecord
    process: asyncio.subprocess.Process
    output: _BoundedBuffer
    wait_task: asyncio.Task[None] | None


class TerminalBroker:
    """Async terminal runtime with one-shot, PTY, and background-job APIs."""

    def __init__(self, config: TerminalRuntimeConfig | None = None):
        self.config = config or TerminalRuntimeConfig()
        self._metadata = _MetadataStore(self.config.metadata_path)
        self._sessions: dict[str, _SessionHandle] = {}
        self._jobs: dict[str, _JobHandle] = {}
        self._historical_sessions: dict[str, dict[str, Any]] = {}
        self._historical_jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Load metadata and start lifecycle cleanup.

        Resources from a previous broker process cannot be safely reattached;
        they are marked ``orphaned`` and retained for auditability.
        """
        async with self._lock:
            if self._closed:
                raise TerminalClosed("terminal broker is closed")
            metadata = self._metadata.load()
            self._historical_sessions = dict(metadata.get("sessions", {}))
            self._historical_jobs = dict(metadata.get("jobs", {}))
            changed = False
            for record in metadata.get("sessions", {}).values():
                if record.get("status") in {"running", "starting"}:
                    record["status"] = "orphaned"
                    record["error"] = "broker restarted; PTY is no longer attached"
                    changed = True
            for record in metadata.get("jobs", {}).values():
                if record.get("status") in {"running", "starting"}:
                    record["status"] = "orphaned"
                    record["error"] = "broker restarted; process is no longer supervised"
                    changed = True
            if changed:
                self._metadata.save(metadata)
            if self._reaper_task is None:
                self._reaper_task = asyncio.create_task(self._reaper(), name="emery-terminal-reaper")

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            reaper = self._reaper_task
            self._reaper_task = None
            sessions = list(self._sessions.values())
            jobs = list(self._jobs.values())
        if reaper:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        for handle in sessions:
            self._kill_pid(handle.child_pid)
            with contextlib.suppress(OSError):
                os.close(handle.master_fd)
            handle.pump_task.cancel()
        for handle in jobs:
            self._kill_process(handle.process)
            if handle.wait_task is not None:
                handle.wait_task.cancel()
        for handle in sessions:
            with contextlib.suppress(asyncio.CancelledError):
                await handle.pump_task
        for handle in jobs:
            if handle.wait_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await handle.wait_task

    async def exec(self, request: ExecRequest) -> ExecResult:
        """Run one bounded command and return its complete bounded result."""
        self._ensure_open()
        command = self._validate_command(request.command)
        cwd = self._resolve_cwd(request.cwd)
        timeout = _bounded_float(
            request.timeout_seconds,
            self.config.default_timeout_seconds,
            1.0,
            self.config.max_timeout_seconds,
        )
        output_limit = self._output_limit(request.max_output_chars)
        started = _now()
        process: asyncio.subprocess.Process | None = None
        buffer = _BoundedBuffer(output_limit)
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-lc", command,
                cwd=cwd,
                env=_safe_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            await asyncio.wait_for(self._collect_reader(process.stdout, buffer), timeout=timeout)
            await process.wait()
            status = "completed" if process.returncode == 0 else "failed"
            error = None if status == "completed" else f"Command exited with status {process.returncode}."
        except asyncio.TimeoutError:
            if process is not None:
                self._kill_process(process)
                await process.wait()
            status = "timeout"
            error = f"Command exceeded the {timeout:g}-second timeout and was terminated."
        except asyncio.CancelledError:
            if process is not None:
                self._kill_process(process)
                await process.wait()
            raise
        except OSError as exc:
            status = "error"
            error = f"Unable to execute command: {exc}"
        finished = _now()
        output, truncated = buffer.snapshot()
        return ExecResult(
            protocol_version=PROTOCOL_VERSION,
            request_id=request.request_id,
            status=status,
            command=command,
            cwd=cwd,
            exit_code=process.returncode if process and status in {"completed", "failed"} else None,
            output=output,
            output_truncated=truncated,
            started_at=started,
            finished_at=finished,
            error=error,
        )

    async def start_session(self, request: SessionStartRequest | None = None) -> SessionRecord:
        self._ensure_open()
        request = request or SessionStartRequest()
        cwd = self._resolve_cwd(request.cwd)
        async with self._lock:
            running_sessions = sum(record.status == "running" for record in (handle.record for handle in self._sessions.values()))
            if running_sessions >= self.config.max_sessions:
                raise TerminalRuntimeError("maximum terminal session limit reached")
        now = _now()
        idle = _bounded_float(request.idle_timeout_seconds, self.config.session_idle_timeout_seconds, 1.0, 7 * 24 * 60 * 60)
        lifetime = _bounded_float(request.max_lifetime_seconds, self.config.session_max_lifetime_seconds, 1.0, 7 * 24 * 60 * 60)
        expires_at = min(now + idle, now + lifetime)
        env = _safe_environment()
        env.update({"TERM": env.get("TERM", "xterm-256color"), "PS1": "emery$ "})
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                os.execvpe("/bin/bash", ["bash", "--noprofile", "--norc", "-i"], env)
            except BaseException:
                os._exit(127)
        os.set_blocking(master_fd, True)
        session_id = _id("sess")
        record = SessionRecord(PROTOCOL_VERSION, session_id, request.request_id, "running", cwd, pid, now, now, expires_at)
        output = _BoundedBuffer(self.config.max_output_chars)
        pump = asyncio.create_task(self._pump_pty(session_id, master_fd, output), name=f"emery-pty-{session_id}")
        async with self._lock:
            self._sessions[session_id] = _SessionHandle(record, master_fd, pid, output, pump)
            self._persist_locked()
        return record

    async def write_session(self, session_id: str, input_text: str) -> int:
        if not isinstance(input_text, str) or len(input_text) > MAX_SESSION_INPUT_CHARS:
            raise ValueError(f"session input is limited to {MAX_SESSION_INPUT_CHARS} characters")
        handle = await self._session_handle(session_id)
        if handle.record.status != "running":
            raise TerminalExpired(f"terminal session {session_id} is {handle.record.status}")
        written = await asyncio.to_thread(os.write, handle.master_fd, input_text.encode("utf-8"))
        async with self._lock:
            handle.record = replace(handle.record, last_activity_at=_now(), expires_at=self._session_expiry(handle.record))
            self._persist_locked()
        return written

    async def read_session(self, session_id: str, max_output_chars: int | None = None) -> OutputChunk:
        handle = await self._session_handle(session_id)
        output, truncated = handle.output.take(self._output_limit(max_output_chars))
        return OutputChunk(session_id, output, truncated, handle.record.status)

    async def close_session(self, session_id: str, reason: str = "closed") -> SessionRecord:
        handle = await self._session_handle(session_id)
        if handle.record.status == "running":
            self._kill_pid(handle.child_pid)
            with contextlib.suppress(OSError):
                os.close(handle.master_fd)
            handle.pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.pump_task
        async with self._lock:
            handle.record = replace(handle.record, status=reason, last_activity_at=_now())
            self._persist_locked()
        return handle.record

    async def start_job(self, request: JobStartRequest) -> JobRecord:
        self._ensure_open()
        command = self._validate_command(request.command)
        cwd = self._resolve_cwd(request.cwd)
        output_limit = self._output_limit(request.max_output_chars)
        lifetime = _bounded_float(request.max_lifetime_seconds, self.config.job_max_lifetime_seconds, 1.0, 7 * 24 * 60 * 60)
        async with self._lock:
            running_jobs = sum(record.status == "running" for record in (handle.record for handle in self._jobs.values()))
            if running_jobs >= self.config.max_jobs:
                raise TerminalRuntimeError("maximum terminal job limit reached")
        now = _now()
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh", "-lc", command,
                cwd=cwd,
                env=_safe_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise TerminalRuntimeError(f"unable to start job: {exc}") from exc
        job_id = _id("job")
        record = JobRecord(PROTOCOL_VERSION, job_id, request.request_id, "running", command, cwd, process.pid, now, now, None, now + lifetime)
        handle = _JobHandle(record, process, _BoundedBuffer(output_limit), None)
        handle.wait_task = asyncio.create_task(self._wait_job(job_id, handle), name=f"emery-job-{job_id}")
        async with self._lock:
            self._jobs[job_id] = handle
            self._persist_locked()
        return record

    async def get_job(self, job_id: str) -> JobRecord:
        handle = await self._job_handle(job_id)
        return handle.record

    async def read_job(self, job_id: str, max_output_chars: int | None = None) -> OutputChunk:
        handle = await self._job_handle(job_id)
        output, truncated = handle.output.take(self._output_limit(max_output_chars))
        async with self._lock:
            handle.record = replace(handle.record, output=output, output_truncated=truncated)
            self._persist_locked()
        return OutputChunk(job_id, output, truncated, handle.record.status)

    async def wait_job(self, job_id: str, timeout_seconds: float | None = None) -> JobRecord:
        handle = await self._job_handle(job_id)
        if handle.record.status == "running":
            if timeout_seconds is None:
                if handle.wait_task is not None:
                    await handle.wait_task
            else:
                if handle.wait_task is not None:
                    await asyncio.wait_for(asyncio.shield(handle.wait_task), timeout=max(0.0, timeout_seconds))
        return handle.record

    async def cancel_job(self, job_id: str, reason: str = "cancelled") -> JobRecord:
        handle = await self._job_handle(job_id)
        if handle.record.status == "running":
            async with self._lock:
                handle.record = replace(handle.record, status=reason, finished_at=_now(), exit_code=None)
                self._persist_locked()
            self._kill_process(handle.process)
            if handle.wait_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await handle.wait_task
        return handle.record

    async def list_sessions(self) -> list[SessionRecord]:
        return [handle.record for handle in self._sessions.values()]

    async def list_jobs(self) -> list[JobRecord]:
        return [handle.record for handle in self._jobs.values()]

    async def _collect_reader(self, reader: asyncio.StreamReader | None, buffer: _BoundedBuffer) -> None:
        if reader is None:
            return
        while True:
            chunk = await reader.read(8192)
            if not chunk:
                return
            buffer.append(chunk.decode("utf-8", errors="replace"))

    async def _pump_pty(self, session_id: str, master_fd: int, buffer: _BoundedBuffer) -> None:
        del session_id
        try:
            while True:
                chunk = await asyncio.to_thread(os.read, master_fd, 8192)
                if not chunk:
                    return
                buffer.append(chunk.decode("utf-8", errors="replace"))
        except (asyncio.CancelledError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in {errno.EIO, errno.EBADF}:
                raise

    async def _wait_job(self, job_id: str, handle: _JobHandle) -> None:
        try:
            await self._collect_reader(handle.process.stdout, handle.output)
            await handle.process.wait()
            async with self._lock:
                output, truncated = handle.output.snapshot()
                if handle.record.status == "running":
                    status = "completed" if handle.process.returncode == 0 else "failed"
                    error = None if status == "completed" else f"Command exited with status {handle.process.returncode}."
                    handle.record = replace(handle.record, status=status, finished_at=_now(), exit_code=handle.process.returncode, output=output, output_truncated=truncated, error=error)
                else:
                    handle.record = replace(handle.record, output=output, output_truncated=truncated)
                self._persist_locked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                handle.record = replace(handle.record, status="error", finished_at=_now(), error=str(exc))
                self._persist_locked()
        del job_id

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(max(0.5, self.config.reaper_interval_seconds))
            now = _now()
            for session_id, handle in list(self._sessions.items()):
                if handle.record.status == "running" and now >= handle.record.expires_at:
                    with contextlib.suppress(TerminalRuntimeError):
                        await self.close_session(session_id, "expired")
            for job_id, handle in list(self._jobs.items()):
                if handle.record.status == "running" and now >= handle.record.expires_at:
                    with contextlib.suppress(TerminalRuntimeError):
                        await self.cancel_job(job_id, "expired")

    async def _session_handle(self, session_id: str) -> _SessionHandle:
        async with self._lock:
            handle = self._sessions.get(session_id)
        if handle is None:
            raise TerminalNotFound(f"terminal session not found: {session_id}")
        return handle

    async def _job_handle(self, job_id: str) -> _JobHandle:
        async with self._lock:
            handle = self._jobs.get(job_id)
        if handle is None:
            raise TerminalNotFound(f"terminal job not found: {job_id}")
        return handle

    def _ensure_open(self) -> None:
        if self._closed:
            raise TerminalClosed("terminal broker is closed")

    def _validate_command(self, command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        command = command.strip()
        if len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"command is limited to {MAX_COMMAND_CHARS} characters")
        return command

    def _resolve_cwd(self, cwd: str | None) -> str:
        value = str(cwd or self.config.default_cwd).strip() or self.config.default_cwd
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(self.config.default_cwd) / path
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"working directory is not a directory: {path}")
        return str(path)

    def _output_limit(self, value: int | None) -> int:
        return _bounded_int(value, self.config.default_max_output_chars, 1_000, self.config.max_output_chars)

    def _session_expiry(self, record: SessionRecord) -> float:
        return min(_now() + self.config.session_idle_timeout_seconds, record.created_at + self.config.session_max_lifetime_seconds)

    def _persist_locked(self) -> None:
        metadata = self._metadata.load()
        metadata["version"] = 1
        metadata["protocol_version"] = PROTOCOL_VERSION
        sessions = dict(self._historical_sessions)
        sessions.update({key: asdict(handle.record) for key, handle in self._sessions.items()})
        jobs = dict(self._historical_jobs)
        jobs.update({key: asdict(handle.record) for key, handle in self._jobs.items()})
        metadata["sessions"] = sessions
        metadata["jobs"] = jobs
        self._metadata.save(metadata)

    @staticmethod
    def _kill_pid(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)

    @staticmethod
    def _kill_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)


__all__ = [
    "ExecRequest", "ExecResult", "JobRecord", "JobStartRequest", "OutputChunk",
    "PROTOCOL_VERSION", "SessionRecord", "SessionStartRequest", "TerminalBroker",
    "TerminalClosed", "TerminalExpired", "TerminalNotFound", "TerminalRuntime",
    "TerminalRuntimeConfig", "TerminalRuntimeError",
]
