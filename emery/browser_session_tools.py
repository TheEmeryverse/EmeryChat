"""Model-facing logical browser-session lifecycle tools."""

from __future__ import annotations

from typing import Any

from emery import globals
from emery.browser_sessions import BrowserSessionNotFound, get_browser_session_manager
from emery.execution_store import get_execution_store
from emery.session_persistence import ensure_runtime_recovered, get_session_integration


def _owner() -> dict[str, Any]:
    return {
        "chat_id": globals.TARGET_CHAT_ID.get(),
        "thread_id": globals.CURRENT_THREAD_ID.get(),
        "user_id": globals.current_user_id.get(),
    }


def _audit_owner() -> dict[str, Any]:
    owner = _owner()
    return {"chat_id": owner["chat_id"], "thread_id": owner["thread_id"], "actor_user_id": owner["user_id"]}


async def _session_context() -> tuple[Any, dict[str, Any]]:
    await ensure_runtime_recovered()
    integration = get_session_integration()
    return integration, integration.ensure_chat_session()


def _persisted_status(browser_session_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
    session = metadata.get("session") if isinstance(metadata.get("session"), dict) else dict(metadata)
    result = dict(session)
    result.update({
        "status": binding.get("status", "stale"),
        "browser_session_id": browser_session_id,
        "session_id": browser_session_id,
        "live": False,
        "stale": True,
        "resource_metadata_restored": True,
    })
    return result


async def _guard(browser_session_id: str) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    integration, _chat_session = await _session_context()
    binding, error = integration.authorize_resource("browser_session", browser_session_id)
    return integration, binding, error


async def browser_session_start(lease_seconds: int | float | None = None, idle_timeout_seconds: int | float | None = None) -> dict[str, Any]:
    integration, chat_session = await _session_context()
    result = await get_browser_session_manager().create_session(**_owner(), lease_seconds=lease_seconds, idle_timeout_seconds=idle_timeout_seconds)
    session_id = result.get("session_id")
    if session_id:
        integration.bind_resource("browser_session", session_id, chat_session_id=chat_session["id"], metadata={"session": result})
    get_execution_store().audit(kind="browser", action="session_start", status="completed", session_id=chat_session["id"], result=result.get("session_id", ""), **_audit_owner())
    return result


async def browser_session_status(browser_session_id: str) -> dict[str, Any]:
    integration, binding, error = await _guard(browser_session_id)
    if error:
        if binding and error.get("status") == "stale":
            return _persisted_status(browser_session_id, binding)
        return error
    try:
        result = await get_browser_session_manager().status(browser_session_id, **_owner())
    except BrowserSessionNotFound:
        if binding:
            return _persisted_status(browser_session_id, binding)
        raise
    integration.update_resource("browser_session", browser_session_id, metadata={"session": result})
    return result


async def browser_session_list() -> dict[str, Any]:
    integration, chat_session = await _session_context()
    sessions = await get_browser_session_manager().list_sessions(**_owner())
    live_ids = {str(item.get("session_id")) for item in sessions}
    # A restart intentionally does not reattach an old Chromium process. Keep
    # its durable metadata visible as stale so the model can explain what
    # happened instead of silently presenting an empty browser state.
    for binding in integration.list_resources(chat_session_id=chat_session["id"]):
        if binding.get("resource_type") != "browser_session":
            continue
        resource_id = str(binding.get("resource_id") or "")
        if resource_id and resource_id not in live_ids:
            sessions.append(_persisted_status(resource_id, binding))
    return {"status": "completed", "sessions": sessions}


async def browser_session_open_tab(browser_session_id: str, url: str) -> dict[str, Any]:
    integration, binding, error = await _guard(browser_session_id)
    if error:
        return error
    try:
        result = await get_browser_session_manager().open_tab(browser_session_id, url, **_owner())
    except BrowserSessionNotFound:
        if binding:
            return _persisted_status(browser_session_id, binding)
        raise
    if result.get("status") not in {"error", "blocked"}:
        try:
            status = await get_browser_session_manager().status(browser_session_id, **_owner())
        except Exception:
            status = result
        integration.update_resource("browser_session", browser_session_id, metadata={"session": status, "last_url": url})
    get_execution_store().audit(kind="browser", action="open_tab", status=result.get("status", "error"), session_id=browser_session_id, result=url, **_audit_owner())
    return result


async def browser_session_close(browser_session_id: str, close_tabs: bool = True) -> dict[str, Any]:
    integration, binding, error = await _guard(browser_session_id)
    if error:
        return error
    try:
        result = await get_browser_session_manager().close_session(browser_session_id, close_tabs=close_tabs, **_owner())
    except BrowserSessionNotFound:
        if binding:
            return _persisted_status(browser_session_id, binding)
        raise
    integration.update_resource("browser_session", browser_session_id, status="closed", metadata={"session": result.get("session", result)})
    get_execution_store().audit(kind="browser", action="session_close", status=result.get("status", "error"), session_id=browser_session_id, **_audit_owner())
    return result


async def browser_session_cleanup() -> dict[str, Any]:
    integration, _chat_session = await _session_context()
    results = await get_browser_session_manager().cleanup_expired()
    for result in results:
        session_id = result.get("browser_session_id")
        if session_id:
            integration.update_resource("browser_session", session_id, status="closed", metadata={"session": result.get("session", result)})
    return {"status": "completed", "closed": results}


__all__ = [
    "browser_session_cleanup",
    "browser_session_close",
    "browser_session_list",
    "browser_session_open_tab",
    "browser_session_start",
    "browser_session_status",
]
