"""Model-facing terminal session and background-job tools.

The low-level broker lives in :mod:`emery.terminal_runtime`.  Production
calls use the host runner's private Unix socket so PTYs and jobs are created
as ``hudson`` rather than as the container process.  The same API can use a
local broker for development and tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from emery.config import COMMAND_EXECUTION_BACKEND, COMMAND_EXECUTION_HOST_SOCKET
from emery import globals
from emery.command_execution import COMMAND_EXECUTION_CWD, _command_root, _dangerous_reason
from emery.execution_store import get_execution_store, sanitize
from emery.session_persistence import ensure_runtime_recovered, get_session_integration
from emery.terminal_runtime import (
    ExecRequest,
    JobStartRequest,
    SessionStartRequest,
    TerminalBroker,
    TerminalRuntimeConfig,
)


_local_broker: TerminalBroker | None = None
_local_broker_lock = asyncio.Lock()


def _request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def _context() -> dict[str, Any]:
    return {
        "chat_id": globals.TARGET_CHAT_ID.get(),
        "thread_id": globals.CURRENT_THREAD_ID.get(),
        "actor_user_id": globals.current_user_id.get(),
    }


async def _session_context() -> tuple[Any, dict[str, Any]]:
    await ensure_runtime_recovered()
    integration = get_session_integration()
    return integration, integration.ensure_chat_session()


async def _resource_guard(resource_type: str, resource_id: str) -> tuple[Any, dict[str, Any] | None]:
    integration, _session = await _session_context()
    _binding, error = integration.authorize_resource(resource_type, resource_id)
    return integration, error


def _sync_resource_result(integration: Any, resource_type: str, resource_id: str, result: dict[str, Any], *, terminal_status: bool = True) -> None:
    if not terminal_status or not isinstance(result, dict):
        return
    status = str(result.get("status") or "")
    if status == "error":
        return
    if resource_type == "terminal_session" and status in {"closed", "stale", "orphaned", "expired", "stale_on_restart"}:
        integration.update_resource(resource_type, resource_id, status=status)
    elif resource_type == "terminal_job" and status in {"completed", "succeeded", "failed", "cancelled", "expired", "orphaned", "stale_on_restart"}:
        integration.update_resource(resource_type, resource_id, status=status)


def _visible_resource(integration: Any, chat_session_id: str, resource_type: str, resource_id: str) -> bool:
    binding = integration.store.get_resource_binding(resource_type, resource_id)
    if binding is None:
        return True
    if str(binding.get("chat_session_id")) != str(chat_session_id):
        return False
    return binding.get("status") not in {"stale", "recovery_pending", "closed", "orphaned", "stale_on_restart"}
async def _broker() -> TerminalBroker:
    global _local_broker
    async with _local_broker_lock:
        if _local_broker is None:
            _local_broker = TerminalBroker(TerminalRuntimeConfig(
                default_cwd=COMMAND_EXECUTION_CWD,
                metadata_path=str(Path(COMMAND_EXECUTION_CWD) / "data" / "runtime" / "terminal-runtime.json"),
            ))
            await _local_broker.start()
        return _local_broker


async def _host_call(operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one versioned request to the host runner or use the local broker."""
    payload = dict(payload or {})
    request_id = str(payload.pop("request_id", "") or _request_id())
    if COMMAND_EXECUTION_BACKEND == "host":
        request = {"protocol_version": 1, "request_id": request_id, "operation": operation, **payload}
        reader = writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(COMMAND_EXECUTION_HOST_SOCKET), timeout=5.0
            )
            writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
            request_timeout = payload.get("timeout_seconds") or 30
            line = await asyncio.wait_for(reader.readline(), timeout=float(request_timeout) + 10.0)
            if not line:
                raise RuntimeError("host runner closed the connection without a result")
            response = json.loads(line.decode("utf-8"))
            if not isinstance(response, dict):
                raise RuntimeError("host runner returned an invalid response")
            response.setdefault("request_id", request_id)
            return response
        except Exception as exc:
            return {"status": "error", "request_id": request_id, "error": f"Host terminal unavailable: {exc}"}
        finally:
            if writer is not None:
                writer.close()
                with_context = getattr(writer, "wait_closed", None)
                if with_context is not None:
                    try:
                        await with_context()
                    except OSError:
                        pass

    broker = await _broker()
    try:
        if operation == "exec":
            result = await broker.exec(ExecRequest(request_id=request_id, command=payload["command"], cwd=payload.get("cwd"), timeout_seconds=payload.get("timeout_seconds"), max_output_chars=payload.get("max_output_chars")))
        elif operation == "session_start":
            result = await broker.start_session(SessionStartRequest(request_id=request_id, cwd=payload.get("cwd"), idle_timeout_seconds=payload.get("idle_timeout_seconds"), max_lifetime_seconds=payload.get("max_lifetime_seconds")))
        elif operation == "session_write":
            result = {"written": await broker.write_session(payload["session_id"], payload.get("input", "")), "session_id": payload["session_id"], "status": "completed"}
        elif operation == "session_read":
            result = await broker.read_session(payload["session_id"], payload.get("max_output_chars"))
        elif operation == "session_close":
            result = await broker.close_session(payload["session_id"], payload.get("reason", "closed"))
        elif operation == "job_start":
            result = await broker.start_job(JobStartRequest(request_id=request_id, command=payload["command"], cwd=payload.get("cwd"), max_lifetime_seconds=payload.get("max_lifetime_seconds"), max_output_chars=payload.get("max_output_chars")))
        elif operation == "job_status":
            result = await broker.get_job(payload["job_id"])
        elif operation == "job_read":
            result = await broker.read_job(payload["job_id"], payload.get("max_output_chars"))
        elif operation == "job_wait":
            result = await broker.wait_job(payload["job_id"], payload.get("timeout_seconds"))
        elif operation == "job_cancel":
            result = await broker.cancel_job(payload["job_id"], payload.get("reason", "cancelled"))
        elif operation == "list_sessions":
            result = await broker.list_sessions()
        elif operation == "list_jobs":
            result = await broker.list_jobs()
        else:
            return {"status": "error", "request_id": request_id, "error": f"unknown terminal operation: {operation}"}
        if isinstance(result, list):
            return {"status": "completed", "request_id": request_id, "items": [asdict(item) if hasattr(item, "__dataclass_fields__") else item for item in result]}
        return {"status": "completed", "request_id": request_id, **(asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result))}
    except Exception as exc:
        return {"status": "error", "request_id": request_id, "error": sanitize(str(exc))}


async def _approve(command: str, justification: str | None) -> dict[str, Any] | None:
    reason = _dangerous_reason(command)
    if not reason:
        return None
    from emery.command_approval import request_command_approval
    decision = await request_command_approval(
        command, reason,
        chat_id=globals.TARGET_CHAT_ID.get(), user_id=globals.current_user_id.get(),
        thread_id=globals.CURRENT_THREAD_ID.get(), timeout_seconds=300,
        justification=justification or f"The command matched the safety policy: {reason}.",
        approval_key=f"terminal:{_command_root(command)}",
    )
    if decision.get("approved"):
        return None
    return {"status": "blocked", "error": f"Command was not executed: {decision.get('message', 'approval denied')}"}


async def terminal_exec(command: str, working_directory: str | None = None, timeout_seconds: int | float | None = None, justification: str | None = None) -> dict[str, Any]:
    integration, chat_session = await _session_context()
    approval = await _approve(command, justification)
    if approval:
        return approval
    result = await _host_call("exec", {"command": command, "cwd": working_directory, "timeout_seconds": timeout_seconds})
    get_execution_store().audit(kind="command", action="exec", status=result.get("status", "error"), request_id=result.get("request_id", ""), session_id=chat_session["id"], command=command, result=result.get("output", ""), **_context())
    return result


async def terminal_session_start(working_directory: str | None = None, idle_timeout_seconds: int | float | None = None, max_lifetime_seconds: int | float | None = None) -> dict[str, Any]:
    integration, chat_session = await _session_context()
    result = await _host_call("session_start", {"cwd": working_directory, "idle_timeout_seconds": idle_timeout_seconds, "max_lifetime_seconds": max_lifetime_seconds})
    if result.get("session_id") and result.get("status") not in {"error", "blocked"}:
        integration.bind_resource("terminal_session", result["session_id"], chat_session_id=chat_session["id"], metadata=result)
    get_execution_store().audit(kind="terminal", action="session_start", status=result.get("status", "error"), request_id=result.get("request_id", ""), session_id=chat_session["id"], result=result.get("session_id", ""), **_context())
    return result


async def terminal_session_write(session_id: str, input: str, justification: str | None = None) -> dict[str, Any]:
    _integration, error = await _resource_guard("terminal_session", session_id)
    if error:
        return error
    approval = await _approve(input, justification)
    if approval:
        return approval
    result = await _host_call("session_write", {"session_id": session_id, "input": input})
    get_execution_store().audit(kind="terminal", action="session_write", status=result.get("status", "error"), request_id=result.get("request_id", ""), session_id=session_id, command=input, **_context())
    return result


async def terminal_session_read(session_id: str, max_output_chars: int | None = None) -> dict[str, Any]:
    integration, error = await _resource_guard("terminal_session", session_id)
    if error:
        return error
    result = await _host_call("session_read", {"session_id": session_id, "max_output_chars": max_output_chars})
    _sync_resource_result(integration, "terminal_session", session_id, result)
    return result


async def terminal_session_close(session_id: str, reason: str = "closed") -> dict[str, Any]:
    integration, error = await _resource_guard("terminal_session", session_id)
    if error:
        return error
    result = await _host_call("session_close", {"session_id": session_id, "reason": reason})
    _sync_resource_result(integration, "terminal_session", session_id, result)
    return result


async def terminal_job_start(command: str, working_directory: str | None = None, max_lifetime_seconds: int | float | None = None, justification: str | None = None) -> dict[str, Any]:
    integration, chat_session = await _session_context()
    approval = await _approve(command, justification)
    if approval:
        return approval
    result = await _host_call("job_start", {"command": command, "cwd": working_directory, "max_lifetime_seconds": max_lifetime_seconds})
    if result.get("job_id") and result.get("status") not in {"error", "blocked"}:
        integration.bind_resource("terminal_job", result["job_id"], chat_session_id=chat_session["id"], metadata=result)
    get_execution_store().audit(kind="command", action="job_start", status=result.get("status", "error"), request_id=result.get("request_id", ""), session_id=chat_session["id"], command=command, result=result.get("job_id", ""), **_context())
    return result


async def terminal_job_status(job_id: str) -> dict[str, Any]:
    integration, error = await _resource_guard("terminal_job", job_id)
    if error:
        return error
    result = await _host_call("job_status", {"job_id": job_id})
    _sync_resource_result(integration, "terminal_job", job_id, result)
    return result


async def terminal_job_read(job_id: str, max_output_chars: int | None = None) -> dict[str, Any]:
    integration, error = await _resource_guard("terminal_job", job_id)
    if error:
        return error
    result = await _host_call("job_read", {"job_id": job_id, "max_output_chars": max_output_chars})
    _sync_resource_result(integration, "terminal_job", job_id, result)
    return result


async def terminal_job_wait(job_id: str, timeout_seconds: int | float | None = None) -> dict[str, Any]:
    integration, error = await _resource_guard("terminal_job", job_id)
    if error:
        return error
    result = await _host_call("job_wait", {"job_id": job_id, "timeout_seconds": timeout_seconds})
    _sync_resource_result(integration, "terminal_job", job_id, result)
    return result


async def terminal_job_cancel(job_id: str, justification: str | None = None) -> dict[str, Any]:
    integration, error = await _resource_guard("terminal_job", job_id)
    if error:
        return error
    approval = await _approve(f"cancel terminal job {job_id}", justification)
    if approval:
        return approval
    result = await _host_call("job_cancel", {"job_id": job_id})
    _sync_resource_result(integration, "terminal_job", job_id, result)
    return result


async def terminal_list_sessions() -> dict[str, Any]:
    integration, chat_session = await _session_context()
    result = await _host_call("list_sessions")
    if isinstance(result.get("items"), list):
        result["items"] = [
            item for item in result["items"]
            if _visible_resource(integration, chat_session["id"], "terminal_session", str(item.get("session_id", "")))
        ]
    return result


async def terminal_list_jobs() -> dict[str, Any]:
    integration, chat_session = await _session_context()
    result = await _host_call("list_jobs")
    if isinstance(result.get("items"), list):
        result["items"] = [
            item for item in result["items"]
            if _visible_resource(integration, chat_session["id"], "terminal_job", str(item.get("job_id", "")))
        ]
    return result


__all__ = [
    "terminal_exec", "terminal_session_start", "terminal_session_write",
    "terminal_session_read", "terminal_session_close", "terminal_job_start",
    "terminal_job_status", "terminal_job_read", "terminal_job_wait",
    "terminal_job_cancel", "terminal_list_sessions", "terminal_list_jobs",
]
