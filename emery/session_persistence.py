"""Durable chat ownership for terminal and browser resources.

The low-level terminal broker and browser session manager intentionally keep
their live process state in memory.  This module supplies the durable layer
above them: one stable Emery session per chat scope, immutable resource
ownership, safe metadata snapshots, and restart reconciliation.

Resource IDs are never reassigned.  On an application restart, resources that
were live in the previous process are treated as stale and are closed when
possible.  Their sanitized metadata remains available for audit/status views.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Mapping

from emery import globals
from emery.execution_store import ExecutionStore, get_execution_store
from emery.telegram_utils import normalize_message_thread_id


logger = logging.getLogger(__name__)

AGENT_NAME = "emery"
_RESOURCE_ACTIVE_STATUSES = ("active", "running", "pending", "recovery_pending")
_RESOURCE_USABLE_STATUSES = {
    "active", "running", "pending", "completed", "failed", "cancelled", "expired",
}
_recovery_lock = asyncio.Lock()
_recovered_store_paths: set[str] = set()


def _group_scope_user(chat_id: Any, user_id: Any) -> Any:
    """Group chats share one resource scope; private chats remain user scoped."""
    try:
        return None if int(chat_id) < 0 else user_id
    except (TypeError, ValueError):
        return user_id


class DurableSessionIntegration:
    """Synchronous ownership and metadata facade over :class:`ExecutionStore`."""

    def __init__(self, store: ExecutionStore | None = None, *, agent_name: str = AGENT_NAME):
        self.store = store or get_execution_store()
        self.agent_name = str(agent_name or AGENT_NAME)[:64]

    def current_scope(self) -> dict[str, Any]:
        chat_id = globals.TARGET_CHAT_ID.get()
        thread_id = globals.CURRENT_THREAD_ID.get()
        user_id = globals.current_user_id.get()
        if chat_id is not None:
            thread_id = normalize_message_thread_id(chat_id, thread_id)
        return {
            "chat_id": chat_id,
            "thread_id": thread_id,
            "user_id": _group_scope_user(chat_id, user_id),
        }

    def ensure_chat_session(self, **scope: Any) -> dict[str, Any]:
        values = self.current_scope()
        values.update({key: value for key, value in scope.items() if key in values})
        return self.store.get_or_create_chat_session(
            chat_id=values["chat_id"],
            thread_id=values["thread_id"],
            user_id=_group_scope_user(values["chat_id"], values["user_id"]),
            agent_name=self.agent_name,
        )

    def bind_resource(
        self,
        resource_type: str,
        resource_id: str,
        *,
        chat_session_id: str | None = None,
        metadata: Any = None,
        status: str = "active",
    ) -> dict[str, Any]:
        session = self.ensure_chat_session() if chat_session_id is None else self.store.get_chat_session(chat_session_id)
        if session is None:
            raise ValueError(f"unknown chat session: {chat_session_id}")
        self.store.touch_chat_session(session["id"])
        return self.store.bind_resource(
            chat_session_id=session["id"],
            resource_type=resource_type,
            resource_id=str(resource_id),
            metadata=metadata,
            status=status,
        )

    def update_resource(self, resource_type: str, resource_id: str, *, status: str | None = None, metadata: Any = None) -> dict[str, Any] | None:
        binding = self.store.update_resource(resource_type, str(resource_id), status=status, metadata=metadata)
        if binding:
            self.store.touch_chat_session(binding["chat_session_id"])
        return binding

    def authorize_resource(self, resource_type: str, resource_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return ``(binding, error)`` without breaking legacy unbound IDs."""
        binding = self.store.get_resource_binding(resource_type, str(resource_id))
        if binding is None:
            # Resource IDs created before durable ownership was introduced are
            # allowed to follow their existing API path.  New resources are
            # always bound before they are returned to the model.
            return None, None
        current = self.ensure_chat_session()
        if str(binding["chat_session_id"]) != str(current["id"]):
            return binding, {"status": "blocked", "error": "resource belongs to another Emery chat session"}
        if binding.get("status") not in _RESOURCE_USABLE_STATUSES:
            return binding, {"status": "stale", "error": f"resource is not live ({binding.get('status')})"}
        self.store.touch_chat_session(current["id"])
        return binding, None

    def resource_metadata(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        binding = self.store.get_resource_binding(resource_type, str(resource_id))
        if not binding:
            return None
        metadata = binding.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    def list_resources(self, *, chat_session_id: str | None = None, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if chat_session_id is None:
            chat_session_id = self.ensure_chat_session()["id"]
        return self.store.list_resource_bindings(chat_session_id=chat_session_id, statuses=statuses)


_default_integration: DurableSessionIntegration | None = None


def get_session_integration() -> DurableSessionIntegration:
    global _default_integration
    if _default_integration is None:
        _default_integration = DurableSessionIntegration()
    return _default_integration


async def _default_terminal_call(operation: str, resource_id: str) -> Mapping[str, Any]:
    from emery.terminal_tools import _host_call

    if operation == "session_close":
        return await _host_call(operation, {"session_id": resource_id, "reason": "stale_on_restart"})
    return await _host_call(operation, {"job_id": resource_id, "reason": "stale_on_restart"})


async def _default_browser_close(target_id: str) -> Mapping[str, Any]:
    from emery.browser_control import close_browser_tab

    return await close_browser_tab(target_id)


def _resource_tabs(binding: Mapping[str, Any]) -> list[str]:
    metadata = binding.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    session = metadata.get("session")
    if isinstance(session, Mapping):
        tabs = session.get("tabs")
    else:
        tabs = metadata.get("tabs")
    if not isinstance(tabs, list):
        return []
    target_ids: list[str] = []
    for tab in tabs:
        if isinstance(tab, Mapping):
            target = tab.get("target_id") or tab.get("id")
            if target:
                target_ids.append(str(target))
    return target_ids


async def recover_stale_resources(
    *,
    integration: DurableSessionIntegration | None = None,
    terminal_call: Callable[[str, str], Awaitable[Mapping[str, Any]]] | None = None,
    browser_close: Callable[[str], Awaitable[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Reconcile resources left by the previous Emery process.

    Recovery is idempotent.  A successful close, an already-missing resource,
    or a browser tab with no remaining target is recorded as ``stale``.  If a
    broker is temporarily unavailable, the binding becomes
    ``recovery_pending`` and can be retried on the next startup/tool call.
    """
    integration = integration or get_session_integration()
    terminal_call = terminal_call or _default_terminal_call
    browser_close = browser_close or _default_browser_close
    store_key = str(integration.store.path)
    async with _recovery_lock:
        if store_key in _recovered_store_paths:
            return []
        bindings = integration.store.list_resource_bindings(statuses=_RESOURCE_ACTIVE_STATUSES)
        results: list[dict[str, Any]] = []
        pending = False
        for binding in bindings:
            resource_type = str(binding.get("resource_type") or "")
            resource_id = str(binding.get("resource_id") or "")
            success = True
            cleanup: list[Any] = []
            try:
                if resource_type == "terminal_session":
                    response = await terminal_call("session_close", resource_id)
                    cleanup.append(response)
                    success = str(response.get("status")) != "error" or "not found" in str(response.get("error", "")).lower()
                elif resource_type == "terminal_job":
                    response = await terminal_call("job_cancel", resource_id)
                    cleanup.append(response)
                    success = str(response.get("status")) != "error" or "not found" in str(response.get("error", "")).lower()
                elif resource_type == "browser_session":
                    for target_id in _resource_tabs(binding):
                        try:
                            cleanup.append(await browser_close(target_id))
                        except Exception as exc:  # pragma: no cover - adapter-specific
                            success = False
                            cleanup.append({"status": "error", "error": str(exc), "target_id": target_id})
                else:
                    success = False
                    cleanup.append({"status": "error", "error": "unknown resource type"})
            except Exception as exc:  # broker/socket may be unavailable during startup
                success = False
                cleanup.append({"status": "error", "error": str(exc)})
            new_status = "stale" if success else "recovery_pending"
            metadata = dict(binding.get("metadata") or {})
            metadata["recovery"] = {"status": new_status, "cleanup": cleanup, "recovered_at": time.time()}
            integration.store.update_resource(resource_type, resource_id, status=new_status, metadata=metadata)
            integration.store.mark_chat_session_reconciled(binding["chat_session_id"])
            pending = pending or not success
            results.append({"resource_type": resource_type, "resource_id": resource_id, "status": new_status})
        if not pending:
            _recovered_store_paths.add(store_key)
        return results


async def ensure_runtime_recovered() -> list[dict[str, Any]]:
    return await recover_stale_resources()


__all__ = [
    "AGENT_NAME",
    "DurableSessionIntegration",
    "ensure_runtime_recovered",
    "get_session_integration",
    "recover_stale_resources",
]
