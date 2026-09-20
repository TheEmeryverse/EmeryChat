#!/usr/bin/env python3
"""Hardened host-side command broker for Emery.

This process runs as the ``hudson`` systemd user service.  It accepts one
bounded, non-interactive command per Unix-socket connection and applies the
host-side validation independently of the container application.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import resource
import signal
import socket
import struct
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPOSITORY_ROOT))

from emery.terminal_runtime import (  # noqa: E402
    JobStartRequest,
    SessionStartRequest,
    TerminalBroker,
    TerminalRuntimeConfig,
)
from emery.execution_store import sanitize  # noqa: E402


PROTOCOL_VERSION = 1
DEFAULT_CWD = "/home/hudson"
MAX_COMMAND_CHARS = 4_000
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_OUTPUT_CHARS = 12_000
MAX_OUTPUT_CHARS = 100_000
MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_CONCURRENT_COMMANDS = 2
MAX_REQUEST_ID_CHARS = 128

# Explicit allowlist: never forward the full user/systemd environment because
# it can contain application credentials.
ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TERM", "TMPDIR",
        "USER", "LOGNAME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME", "VIRTUAL_ENV", "CONDA_PREFIX",
        "UV_PROJECT_ENVIRONMENT",
    }
)

# Hard host denials.  Less severe operations such as git push continue to use
# Emery's Telegram approval flow; these protect the host if that flow is
# bypassed inside the container.
_HOST_DENY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r":\(\)\s*\{.*:\|:.*\};\s*:", re.IGNORECASE), "fork bomb"),
    (re.compile(r"\b(?:mkfs(?:\.[\w.-]+)?|fdisk|parted)\b", re.IGNORECASE), "disk formatting or partitioning"),
    (re.compile(r"\bdd\b[^\n]*\bof\s*=\s*/dev(?:/|\s|$)", re.IGNORECASE), "raw device write"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b", re.IGNORECASE), "host power control"),
    (re.compile(r"\brm\s+(?:-[^\s]*r[^\s]*|--recursive)[^\n]*\s+/(?:\s|$)", re.IGNORECASE), "root filesystem deletion"),
    (re.compile(r"(?:>|>>)\s*/(?:etc|boot|proc|sys)(?:/|\s|$)", re.IGNORECASE), "system path overwrite"),
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

ALLOWED_CWD_ROOTS: tuple[Path, ...] = (Path(DEFAULT_CWD).resolve(),)
MAX_CONCURRENT_COMMANDS = DEFAULT_MAX_CONCURRENT_COMMANDS
ALLOWED_PEER_UID = os.getuid()
_active_commands = 0
_active_commands_lock: asyncio.Lock | None = None
_terminal_broker: TerminalBroker | None = None


def _new_request_id() -> str:
    return f"host-{uuid.uuid4().hex}"


def _request_id(request: Any) -> str:
    candidate = request.get("request_id") if isinstance(request, dict) else None
    if isinstance(candidate, str) and _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return _new_request_id()


def _response(request_id: str, **fields: Any) -> dict[str, Any]:
    return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, **fields}


def _error(request_id: str, message: str) -> dict[str, Any]:
    return _response(request_id, status="error", exit_code=None, output="", error=message)


def _safe_environment(cwd: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in ALLOWED_ENVIRONMENT_NAMES}
    environment.setdefault("HOME", DEFAULT_CWD)
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    environment.setdefault("SHELL", "/bin/sh")
    environment.setdefault("USER", "hudson")
    environment.setdefault("LOGNAME", environment["USER"])
    environment["PWD"] = str(cwd)
    return environment


def _bounded(value: Any, default: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1.0, min(parsed, maximum))


def _resolve_cwd(raw_cwd: Any) -> tuple[Path | None, str | None]:
    cwd = str(raw_cwd or DEFAULT_CWD).strip() or DEFAULT_CWD
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        path = Path(DEFAULT_CWD) / path
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"invalid working directory: {exc}"
    if not resolved.is_dir():
        return None, f"working directory is not a directory: {resolved}"
    if not any(root == resolved or root in resolved.parents for root in ALLOWED_CWD_ROOTS):
        allowed = ", ".join(str(root) for root in ALLOWED_CWD_ROOTS)
        return None, f"working directory is outside the allowed roots: {allowed}"
    return resolved, None


def _host_policy_reason(command: str) -> str | None:
    for pattern, reason in _HOST_DENY_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def _set_child_resource_limits(timeout: float) -> None:
    """Apply conservative per-command POSIX limits where supported."""
    limits = (
        (resource.RLIMIT_CPU, (max(1, math.ceil(timeout) + 1), max(1, math.ceil(timeout) + 1))),
        (resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024)),
        (resource.RLIMIT_NOFILE, (256, 256)),
    )
    for limit, value in limits:
        try:
            resource.setrlimit(limit, value)
        except (OSError, ValueError):
            pass


def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _try_acquire_command_slot() -> bool:
    global _active_commands, _active_commands_lock
    if _active_commands_lock is None:
        _active_commands_lock = asyncio.Lock()
    async with _active_commands_lock:
        if _active_commands >= MAX_CONCURRENT_COMMANDS:
            return False
        _active_commands += 1
        return True


async def _release_command_slot() -> None:
    global _active_commands, _active_commands_lock
    if _active_commands_lock is None:
        return
    async with _active_commands_lock:
        _active_commands = max(0, _active_commands - 1)


async def _execute(request: dict[str, Any]) -> dict[str, Any]:
    request_id = _request_id(request)
    protocol_version = request.get("protocol_version", PROTOCOL_VERSION)
    if protocol_version != PROTOCOL_VERSION:
        return _error(request_id, f"unsupported protocol_version: {protocol_version!r}")
    supplied_id = request.get("request_id")
    if supplied_id is not None and (not isinstance(supplied_id, str) or not _REQUEST_ID_RE.fullmatch(supplied_id)):
        return _error(request_id, "request_id must contain only letters, numbers, '.', '_', ':', or '-'")

    operation = str(request.get("operation") or "exec").strip().lower()
    if operation != "exec":
        return await _execute_terminal_operation(request, request_id, operation)

    command = request.get("command")
    if not isinstance(command, str) or not command.strip():
        return _error(request_id, "command must be a non-empty string")
    command = command.strip()
    if len(command) > MAX_COMMAND_CHARS:
        return _error(request_id, f"command is limited to {MAX_COMMAND_CHARS} characters")

    policy_reason = _host_policy_reason(command)
    if policy_reason:
        return _error(request_id, f"command denied by host policy: {policy_reason}")

    cwd, cwd_error = _resolve_cwd(request.get("cwd"))
    if cwd_error:
        return _error(request_id, cwd_error)
    assert cwd is not None

    timeout = _bounded(request.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    try:
        max_output = max(1_000, min(int(request.get("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)), MAX_OUTPUT_CHARS))
    except (TypeError, ValueError):
        max_output = DEFAULT_MAX_OUTPUT_CHARS

    if not await _try_acquire_command_slot():
        return _response(
            request_id,
            status="busy",
            exit_code=None,
            output="",
            error=f"host runner is at its concurrency limit ({MAX_CONCURRENT_COMMANDS})",
        )

    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            executable="/bin/sh",
            cwd=str(cwd),
            env=_safe_environment(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=lambda: _set_child_resource_limits(timeout),
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            output = (stdout or b"").decode("utf-8", errors="replace")
            output = sanitize(output, max_output)
            truncated = len(output) > max_output
            if truncated:
                output = output[:max_output] + "\n[output truncated]"
            return _response(
                request_id,
                status="completed" if process.returncode == 0 else "failed",
                exit_code=process.returncode,
                output=output,
                working_directory=str(cwd),
                output_truncated=truncated,
            )
        except asyncio.TimeoutError:
            _terminate(process)
            stdout, _ = await process.communicate()
            output = sanitize((stdout or b"").decode("utf-8", errors="replace"), max_output)
            return _response(
                request_id,
                status="timeout",
                exit_code=None,
                output=output[:max_output],
                working_directory=str(cwd),
                error=f"Command exceeded the {timeout:g}-second timeout and was terminated.",
            )
    except (OSError, ValueError) as exc:
        return _response(request_id, status="error", exit_code=None, output="", error=f"Unable to execute command: {exc}")
    finally:
        await _release_command_slot()


def _serialize_runtime(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_serialize_runtime(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_runtime(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_runtime(item) for key, item in value.items()}
    if isinstance(value, str):
        return sanitize(value, 100_000)
    return value


def _terminal_cwd(request: dict[str, Any]) -> tuple[str | None, str | None]:
    cwd, error = _resolve_cwd(request.get("cwd"))
    return (str(cwd), None) if cwd is not None else (None, error)


async def _execute_terminal_operation(request: dict[str, Any], request_id: str, operation: str) -> dict[str, Any]:
    """Dispatch persistent terminal operations through the host-owned broker."""
    broker = _terminal_broker
    if broker is None:
        return _error(request_id, "terminal broker is not ready")
    command = request.get("command") if operation == "job_start" else request.get("input", "")
    if operation in {"job_start", "session_write"}:
        if not isinstance(command, str) or not command.strip():
            return _error(request_id, "command/input must be a non-empty string")
        if len(command) > MAX_COMMAND_CHARS * 4:
            return _error(request_id, "terminal input is too long")
        policy_reason = _host_policy_reason(command)
        if policy_reason:
            return _error(request_id, f"command denied by host policy: {policy_reason}")
    try:
        if operation == "session_start":
            cwd, error = _terminal_cwd(request)
            if error:
                return _error(request_id, error)
            result = await broker.start_session(SessionStartRequest(
                request_id=request_id, cwd=cwd,
                idle_timeout_seconds=request.get("idle_timeout_seconds"),
                max_lifetime_seconds=request.get("max_lifetime_seconds"),
            ))
        elif operation == "session_write":
            result = {"session_id": request.get("session_id"), "written": await broker.write_session(str(request.get("session_id") or ""), str(command)), "status": "completed"}
        elif operation == "session_read":
            result = await broker.read_session(str(request.get("session_id") or ""), request.get("max_output_chars"))
        elif operation == "session_close":
            result = await broker.close_session(str(request.get("session_id") or ""), str(request.get("reason") or "closed"))
        elif operation == "job_start":
            cwd, error = _terminal_cwd(request)
            if error:
                return _error(request_id, error)
            result = await broker.start_job(JobStartRequest(
                request_id=request_id, command=str(command), cwd=cwd,
                max_lifetime_seconds=request.get("max_lifetime_seconds"),
                max_output_chars=request.get("max_output_chars"),
            ))
        elif operation == "job_status":
            result = await broker.get_job(str(request.get("job_id") or ""))
        elif operation == "job_read":
            result = await broker.read_job(str(request.get("job_id") or ""), request.get("max_output_chars"))
        elif operation == "job_wait":
            result = await broker.wait_job(str(request.get("job_id") or ""), _bounded(request.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS))
        elif operation == "job_cancel":
            result = await broker.cancel_job(str(request.get("job_id") or ""), str(request.get("reason") or "cancelled"))
        elif operation == "list_sessions":
            result = await broker.list_sessions()
        elif operation == "list_jobs":
            result = await broker.list_jobs()
        else:
            return _error(request_id, f"unknown terminal operation: {operation}")
        payload = _serialize_runtime(result)
        if isinstance(payload, list):
            return _response(request_id, status="completed", operation=operation, items=payload)
        if isinstance(payload, dict):
            payload.pop("protocol_version", None)
            payload.pop("request_id", None)
        response = _response(request_id, operation=operation, **payload)
        response.setdefault("status", "completed")
        return response
    except Exception as exc:
        return _error(request_id, f"terminal operation failed: {exc}")


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    connection = writer.get_extra_info("socket")
    if connection is None or not hasattr(connection, "getsockopt"):
        return None
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except (OSError, AttributeError, struct.error):
        return None


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request_id = _new_request_id()
    try:
        peer_uid = _peer_uid(writer)
        if peer_uid is not None and peer_uid != ALLOWED_PEER_UID:
            response = _error(request_id, "socket peer is not the configured Emery service user")
        else:
            line = await reader.readline()
            if len(line) > MAX_REQUEST_BYTES:
                response = _error(request_id, f"request is limited to {MAX_REQUEST_BYTES} bytes")
            else:
                try:
                    request = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    request = None
                response = await _execute(request) if isinstance(request, dict) else _error(request_id, "invalid JSON request")
        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def _configure(*, allowed_cwd_roots: Iterable[str] | None, max_concurrent_commands: int, allowed_peer_uid: int | None) -> None:
    global ALLOWED_CWD_ROOTS, MAX_CONCURRENT_COMMANDS, ALLOWED_PEER_UID
    raw_roots = tuple(allowed_cwd_roots or (DEFAULT_CWD,))
    roots: list[Path] = []
    for raw_root in raw_roots:
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if root.is_dir():
            roots.append(root)
    if not roots:
        raise ValueError("at least one allowed cwd root must exist and be a directory")
    ALLOWED_CWD_ROOTS = tuple(dict.fromkeys(roots))
    MAX_CONCURRENT_COMMANDS = max(1, min(int(max_concurrent_commands), 16))
    ALLOWED_PEER_UID = os.getuid() if allowed_peer_uid is None else int(allowed_peer_uid)


async def main(socket_path: str, *, allowed_cwd_roots: Iterable[str] | None = None, max_concurrent_commands: int = DEFAULT_MAX_CONCURRENT_COMMANDS, allowed_peer_uid: int | None = None) -> None:
    global _terminal_broker
    _configure(
        allowed_cwd_roots=allowed_cwd_roots,
        max_concurrent_commands=max_concurrent_commands,
        allowed_peer_uid=allowed_peer_uid,
    )
    _terminal_broker = TerminalBroker(TerminalRuntimeConfig(
        default_cwd=DEFAULT_CWD,
        metadata_path=REPOSITORY_ROOT / "data" / "runtime" / "terminal-runtime.json",
        max_sessions=8,
        max_jobs=16,
    ))
    await _terminal_broker.start()
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(_handle, path=str(path), limit=MAX_REQUEST_BYTES)
    os.chmod(path, 0o600)
    async with server:
        try:
            await server.serve_forever()
        finally:
            await _terminal_broker.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/user/1000/emery-command/command.sock")
    parser.add_argument("--allowed-cwd", action="append", dest="allowed_cwd_roots", default=[DEFAULT_CWD])
    parser.add_argument("--max-concurrent-commands", type=int, default=DEFAULT_MAX_CONCURRENT_COMMANDS)
    parser.add_argument("--allowed-peer-uid", type=int, default=os.getuid())
    args = parser.parse_args()
    asyncio.run(main(args.socket, allowed_cwd_roots=args.allowed_cwd_roots, max_concurrent_commands=args.max_concurrent_commands, allowed_peer_uid=args.allowed_peer_uid))
