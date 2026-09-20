"""Logical browser sessions layered over Emery's CDP browser adapter.

``browser_control`` deliberately remains a low-level CDP adapter: it knows
about Chromium targets and websocket transports, but not about who owns a tab
or how long a browser task should live.  This module supplies that missing
coordination layer without changing the raw adapter's public functions.

The default :class:`CdpBrowserAdapter` creates a session-scoped adapter backed
by one isolated Chromium profile/process.  An application can inject another
adapter in tests or when Chromium is hosted by another process.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from emery.config import BASE_DIR, BROWSER_USER_DATA_DIR


logger = logging.getLogger(__name__)

_UNSET = object()
_DEFAULT_LEASE_SECONDS = 60 * 60
_DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
_MAX_LEASE_SECONDS = 24 * 60 * 60
_MAX_IDLE_TIMEOUT_SECONDS = 24 * 60 * 60
_MAX_SESSION_HISTORY = 128


class BrowserSessionError(RuntimeError):
    """Base error for browser-session lifecycle and ownership failures."""


class BrowserSessionNotFound(BrowserSessionError):
    """Raised when a logical browser session is not active."""


class BrowserSessionOwnershipError(BrowserSessionError):
    """Raised when a caller does not match the session owner."""


class BrowserSessionExpired(BrowserSessionError):
    """Raised when a session lease or idle timeout has elapsed."""


@dataclass(frozen=True)
class BrowserSessionOwner:
    """Identity scope attached to a logical browser session.

    Any non-``None`` field is part of the ownership boundary.  For example,
    passing a chat and thread but no user creates a shared thread session;
    passing all three creates a user-scoped session within that thread.
    """

    chat_id: int | str | None = None
    thread_id: int | str | None = None
    user_id: int | str | None = None

    def as_dict(self) -> dict[str, int | str | None]:
        return asdict(self)


@dataclass(frozen=True)
class BrowserProfile:
    """Profile isolation request passed to adapter/profile hooks.

    ``directory`` is the unique Chromium ``--user-data-dir`` for this session.
    The default CDP adapter launches one supervised Chromium process per
    profile.  Custom adapters may use the metadata as their own isolation
    contract.
    """

    key: str
    directory: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    isolation: str = "process"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "directory": self.directory,
            "metadata": dict(self.metadata),
            "isolation": self.isolation,
        }


@dataclass
class BrowserTab:
    """A target attached to one logical browser session."""

    target_id: str
    type: str | None = None
    title: str | None = None
    url: str | None = None
    websocket_url: str | None = None
    status: str = "active"
    opened_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BrowserTab":
        target_id = str(payload.get("target_id") or payload.get("id") or "").strip()
        if not target_id:
            raise ValueError("browser tab payload must include target_id or id")
        return cls(
            target_id=target_id,
            type=payload.get("type"),
            title=payload.get("title"),
            url=payload.get("url"),
            websocket_url=payload.get("websocket_url") or payload.get("webSocketDebuggerUrl"),
            status=str(payload.get("status") or "active"),
        )

    def update(self, payload: Mapping[str, Any], *, now: float | None = None) -> None:
        self.type = payload.get("type", self.type)
        self.title = payload.get("title", self.title)
        self.url = payload.get("url", self.url)
        self.websocket_url = payload.get(
            "websocket_url",
            payload.get("webSocketDebuggerUrl", self.websocket_url),
        )
        self.status = str(payload.get("status") or "active")
        self.last_seen_at = time.time() if now is None else now

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrowserSession:
    """Persisted-in-memory state for one logical browser task."""

    session_id: str
    owner: BrowserSessionOwner
    profile: BrowserProfile
    created_at: float
    last_used_at: float
    lease_expires_at: float
    idle_timeout_seconds: float
    status: str = "active"
    tabs: dict[str, BrowserTab] = field(default_factory=dict)
    closed_at: float | None = None
    close_reason: str | None = None

    def touch(self, now: float) -> None:
        self.last_used_at = now

    def is_expired(self, now: float) -> tuple[bool, str | None]:
        if now >= self.lease_expires_at:
            return True, "lease_expired"
        if now - self.last_used_at >= self.idle_timeout_seconds:
            return True, "idle_timeout"
        return False, None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "session_id": self.session_id,
            "owner": self.owner.as_dict(),
            "profile": self.profile.as_dict(),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "lease_expires_at": self.lease_expires_at,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "status": self.status,
            "tabs": [tab.as_dict() for tab in self.tabs.values()],
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
        }
        return payload


class BrowserAdapter(Protocol):
    async def open_tab(self, url: str, *, profile: BrowserProfile | None = None) -> Mapping[str, Any]: ...

    async def list_tabs(self) -> Mapping[str, Any]: ...

    async def close_tab(self, target_id: str) -> Mapping[str, Any]: ...

    async def close(self) -> Mapping[str, Any]: ...


class CdpBrowserAdapter:
    """Factory-compatible adapter for session-scoped Chromium processes."""

    def __init__(self) -> None:
        self._isolated_adapters: dict[str, BrowserAdapter] = {}

    def for_profile(self, profile: BrowserProfile):
        from emery.browser_control import IsolatedCdpBrowserAdapter

        key = str(profile.directory or profile.key)
        adapter = self._isolated_adapters.get(key)
        if adapter is None:
            adapter = IsolatedCdpBrowserAdapter(profile)
            self._isolated_adapters[key] = adapter
        return adapter

    async def open_tab(self, url: str, *, profile: BrowserProfile | None = None) -> Mapping[str, Any]:
        # Preserve direct use of this adapter outside BrowserSessionManager.
        if profile is not None:
            return await self.for_profile(profile).open_tab(url, profile=profile)
        from emery.browser_control import open_browser_tab
        return await open_browser_tab(url)

    async def list_tabs(self) -> Mapping[str, Any]:
        from emery.browser_control import list_browser_tabs

        return await list_browser_tabs()

    async def close_tab(self, target_id: str) -> Mapping[str, Any]:
        from emery.browser_control import close_browser_tab

        return await close_browser_tab(target_id)

    async def close(self) -> Mapping[str, Any]:
        from emery.browser_control import close_browser

        isolated_results = []
        for adapter in list(self._isolated_adapters.values()):
            try:
                isolated_results.append(await adapter.close())
            except Exception as exc:
                isolated_results.append({"status": "error", "error": str(exc)})
        self._isolated_adapters.clear()
        raw_result = await close_browser()
        return {"status": "completed", "isolated": isolated_results, "raw": raw_result}


ProfileFactory = Callable[[str, BrowserSessionOwner], BrowserProfile | Mapping[str, Any] | Awaitable[BrowserProfile | Mapping[str, Any] | None] | None]
SessionHook = Callable[[BrowserSession], Any]


def _coerce_timeout(value: float | int | None, default: float, maximum: float, label: str) -> float:
    if value is None:
        return default
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if value <= 0 or value > maximum:
        raise ValueError(f"{label} must be greater than 0 and at most {maximum:g} seconds")
    return value


class BrowserSessionManager:
    """Own logical browser sessions and their attached CDP targets."""

    def __init__(
        self,
        adapter: BrowserAdapter | None = None,
        *,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        default_lease_seconds: float = _DEFAULT_LEASE_SECONDS,
        default_idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
        max_sessions: int = 32,
        profile_factory: ProfileFactory | None = None,
        on_session_created: SessionHook | None = None,
        on_session_closed: SessionHook | None = None,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be greater than 0")
        self.adapter = adapter or CdpBrowserAdapter()
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.default_lease_seconds = _coerce_timeout(
            default_lease_seconds, _DEFAULT_LEASE_SECONDS, _MAX_LEASE_SECONDS, "default_lease_seconds"
        )
        self.default_idle_timeout_seconds = _coerce_timeout(
            default_idle_timeout_seconds,
            _DEFAULT_IDLE_TIMEOUT_SECONDS,
            _MAX_IDLE_TIMEOUT_SECONDS,
            "default_idle_timeout_seconds",
        )
        self.max_sessions = int(max_sessions)
        self.profile_factory = profile_factory
        self.on_session_created = on_session_created
        self.on_session_closed = on_session_closed
        self._sessions: dict[str, BrowserSession] = {}
        self._history: dict[str, BrowserSession] = {}
        self._session_adapters: dict[str, BrowserAdapter] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._cleanup_interval_seconds = 60.0

    async def create_session(
        self,
        *,
        chat_id: int | str | None = None,
        thread_id: int | str | None = None,
        user_id: int | str | None = None,
        session_id: str | None = None,
        lease_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
        profile_key: str | None = None,
        profile_directory: str | None = None,
        profile_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a logical session and return its integration-safe status."""
        lease = _coerce_timeout(lease_seconds, self.default_lease_seconds, _MAX_LEASE_SECONDS, "lease_seconds")
        idle = _coerce_timeout(
            idle_timeout_seconds,
            self.default_idle_timeout_seconds,
            _MAX_IDLE_TIMEOUT_SECONDS,
            "idle_timeout_seconds",
        )
        clean_id = str(session_id or f"bws_{uuid.uuid4().hex}").strip()
        if not clean_id or len(clean_id) > 128:
            raise ValueError("session_id must be 1-128 characters")
        owner = BrowserSessionOwner(chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        profile = await self._make_profile(
            clean_id,
            owner,
            profile_key=profile_key,
            profile_directory=profile_directory,
            profile_metadata=profile_metadata,
        )
        now = self.clock()
        session = BrowserSession(
            session_id=clean_id,
            owner=owner,
            profile=profile,
            created_at=self.wall_clock(),
            last_used_at=now,
            lease_expires_at=now + lease,
            idle_timeout_seconds=idle,
        )
        async with self._lock:
            if clean_id in self._sessions:
                raise ValueError(f"browser session already exists: {clean_id}")
            if len(self._sessions) >= self.max_sessions:
                raise BrowserSessionError("maximum active browser session limit reached")
            active_directories = {
                str(item.profile.directory)
                for item in self._sessions.values()
                if item.profile.directory
            }
            if profile.directory and str(profile.directory) in active_directories:
                raise BrowserSessionError("browser profile directory is already assigned to an active session")
            self._sessions[clean_id] = session
            self._session_adapters[clean_id] = self._adapter_for_profile(profile)
        # Start the lease/idle reaper lazily when browser sessions are first used.
        self.start_cleanup()
        await self._run_hook(self.on_session_created, session)
        return session.as_dict()

    async def _make_profile(
        self,
        session_id: str,
        owner: BrowserSessionOwner,
        *,
        profile_key: str | None,
        profile_directory: str | None,
        profile_metadata: Mapping[str, Any] | None,
    ) -> BrowserProfile:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:64] or "session"
        configured_root = Path(BROWSER_USER_DATA_DIR).expanduser()
        if not configured_root.is_absolute():
            configured_root = BASE_DIR / configured_root
        unique_directory = configured_root / "sessions" / f"{safe_id}-{uuid.uuid4().hex}"
        default_metadata = dict(profile_metadata or {})
        # Only directories generated by Emery are deleted automatically. A
        # caller-supplied profile path may be shared with another lifecycle
        # owner and must never be removed implicitly.
        default_metadata.setdefault("managed", profile_directory is None)
        default = BrowserProfile(
            key=str(profile_key or session_id),
            directory=profile_directory or str(unique_directory),
            metadata=default_metadata,
            isolation="process",
        )
        if self.profile_factory is None:
            return default
        result = self.profile_factory(session_id, owner)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return default
        if isinstance(result, BrowserProfile):
            profile = result
        elif isinstance(result, Mapping):
            profile = BrowserProfile(
                key=str(result.get("key") or default.key),
                directory=result.get("directory", default.directory),
                metadata=dict(result.get("metadata") or default.metadata),
                isolation=str(result.get("isolation") or default.isolation),
            )
        else:
            raise TypeError("profile_factory must return BrowserProfile, a mapping, or None")
        if not profile.directory:
            profile = BrowserProfile(
                key=profile.key,
                directory=default.directory,
                metadata=dict(profile.metadata),
                isolation=profile.isolation,
            )
        metadata = dict(profile.metadata)
        if profile.directory != default.directory and "managed" not in metadata:
            metadata["managed"] = False
        if metadata != dict(profile.metadata):
            profile = BrowserProfile(
                key=profile.key,
                directory=profile.directory,
                metadata=metadata,
                isolation=profile.isolation,
            )
        return profile

    def _adapter_for_profile(self, profile: BrowserProfile) -> BrowserAdapter:
        factory = getattr(self.adapter, "for_profile", None)
        if callable(factory):
            return factory(profile)
        return self.adapter

    def _session_adapter(self, session_id: str) -> BrowserAdapter:
        try:
            return self._session_adapters[str(session_id)]
        except KeyError as exc:
            raise BrowserSessionNotFound(f"No browser adapter for session: {session_id}") from exc

    async def _run_hook(self, hook: SessionHook | None, session: BrowserSession) -> None:
        if hook is None:
            return
        result = hook(session)
        if inspect.isawaitable(result):
            await result

    def _active_session(self, session_id: str, *, allow_expired: bool = False) -> BrowserSession:
        session = self._sessions.get(str(session_id))
        if session is None:
            raise BrowserSessionNotFound(f"No active browser session: {session_id}")
        expired, reason = session.is_expired(self.clock())
        if expired and not allow_expired:
            raise BrowserSessionExpired(f"Browser session {session_id} expired: {reason}")
        return session

    @staticmethod
    def _owner_matches(session: BrowserSession, provided: Mapping[str, Any]) -> bool:
        for field_name in ("chat_id", "thread_id", "user_id"):
            expected = getattr(session.owner, field_name)
            if expected is None:
                continue
            if field_name not in provided or provided[field_name] is _UNSET:
                return False
            actual = provided[field_name]
            if actual is None or str(actual) != str(expected):
                return False
        return True

    def _require_owner(
        self,
        session: BrowserSession,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> None:
        provided = {"chat_id": chat_id, "thread_id": thread_id, "user_id": user_id}
        if session.owner.chat_id is None and session.owner.thread_id is None and session.owner.user_id is None:
            return
        if not self._owner_matches(session, provided):
            raise BrowserSessionOwnershipError(f"Caller does not own browser session {session.session_id}")

    def _touch(self, session: BrowserSession) -> None:
        session.touch(self.clock())

    async def get_session(
        self,
        session_id: str,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
        touch: bool = True,
        include_tabs: bool = True,
        include_closed: bool = False,
    ) -> dict[str, Any]:
        try:
            session = self._active_session(session_id)
        except BrowserSessionNotFound:
            if not include_closed:
                raise
            session = self._history.get(str(session_id))
            if session is None:
                raise
        self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        if touch and session.status == "active":
            self._touch(session)
        if include_tabs and session.status == "active":
            await self._sync_tabs(session)
        return session.as_dict()

    async def renew_session(
        self,
        session_id: str,
        *,
        lease_seconds: float | None = None,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = self._active_session(session_id)
        self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        lease = _coerce_timeout(lease_seconds, self.default_lease_seconds, _MAX_LEASE_SECONDS, "lease_seconds")
        session.lease_expires_at = self.clock() + lease
        self._touch(session)
        return session.as_dict()

    async def attach_tab(
        self,
        session_id: str,
        tab: Mapping[str, Any] | str,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = self._active_session(session_id)
        self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        tab_payload = {"target_id": tab} if isinstance(tab, str) else tab
        browser_tab = BrowserTab.from_payload(tab_payload)
        session.tabs[browser_tab.target_id] = browser_tab
        self._touch(session)
        return browser_tab.as_dict()

    async def detach_tab(
        self,
        session_id: str,
        target_id: str,
        *,
        close: bool = False,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = self._active_session(session_id)
        self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        target_key = str(target_id)
        tab = session.tabs.pop(target_key, None)
        if tab is None:
            raise BrowserSessionNotFound(f"No tab {target_key} attached to browser session {session_id}")
        close_result = None
        if close:
            close_result = await self.adapter.close_tab(target_key)
        self._touch(session)
        return {"status": "completed", "tab": tab.as_dict(), "closed": bool(close), "close_result": close_result}

    async def open_tab(
        self,
        session_id: str,
        url: str,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = self._active_session(session_id)
        self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        result = await self._session_adapter(session.session_id).open_tab(url, profile=session.profile)
        if not isinstance(result, Mapping):
            raise BrowserSessionError("browser adapter returned an invalid open-tab response")
        if result.get("status") not in {None, "completed"} or not result.get("target_id"):
            return dict(result)
        await self.attach_tab(session_id, result, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        try:
            from emery.browser_control import register_isolated_target_owner
            register_isolated_target_owner(
                str(result["target_id"]),
                {"chat_id": session.owner.chat_id, "thread_id": session.owner.thread_id, "user_id": session.owner.user_id},
            )
        except Exception:
            logger.debug("Unable to register browser target ownership", exc_info=True)
        self._touch(session)
        return {**dict(result), "browser_session_id": session.session_id}

    async def _sync_tabs(self, session: BrowserSession) -> None:
        """Refresh owned target metadata without claiming unrelated tabs."""
        try:
            result = await self._session_adapter(session.session_id).list_tabs()
        except Exception as exc:
            logger.debug("Unable to refresh browser session %s tabs: %s", session.session_id, exc)
            return
        if not isinstance(result, Mapping):
            return
        observed = {
            str(item.get("target_id") or item.get("id")): item
            for item in result.get("tabs", []) or []
            if isinstance(item, Mapping) and (item.get("target_id") or item.get("id"))
        }
        now = self.wall_clock()
        for target_id, tab in session.tabs.items():
            payload = observed.get(target_id)
            if payload is None:
                tab.status = "missing"
                tab.last_seen_at = now
            else:
                tab.update(payload, now=now)

    async def list_tabs(
        self,
        session_id: str,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = self._active_session(session_id)
        self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        await self._sync_tabs(session)
        self._touch(session)
        return {"status": "completed", "browser_session_id": session.session_id, "tabs": [tab.as_dict() for tab in session.tabs.values()]}

    async def status(
        self,
        session_id: str,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
        include_tabs: bool = True,
        include_closed: bool = False,
    ) -> dict[str, Any]:
        return await self.get_session(
            session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            include_tabs=include_tabs,
            include_closed=include_closed,
        )

    async def list_sessions(
        self,
        *,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> list[dict[str, Any]]:
        sessions = list(self._sessions.values())
        if chat_id is _UNSET and thread_id is _UNSET and user_id is _UNSET:
            return [session.as_dict() for session in sessions]
        return [
            session.as_dict()
            for session in sessions
            if self._owner_matches(session, {"chat_id": chat_id, "thread_id": thread_id, "user_id": user_id})
        ]

    async def close_session(
        self,
        session_id: str,
        *,
        reason: str = "closed",
        close_tabs: bool = True,
        _internal: bool = False,
        chat_id: int | str | None | object = _UNSET,
        thread_id: int | str | None | object = _UNSET,
        user_id: int | str | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = self._active_session(session_id, allow_expired=_internal)
        if not _internal:
            self._require_owner(session, chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        self._sessions.pop(session.session_id, None)
        session_adapter = self._session_adapters.pop(session.session_id, self.adapter)
        session.status = "closed" if reason == "closed" else reason
        session.close_reason = reason
        session.closed_at = self.wall_clock()
        close_results = []
        if close_tabs:
            for target_id in list(session.tabs):
                try:
                    close_results.append(await session_adapter.close_tab(target_id))
                except Exception as exc:
                    close_results.append({"status": "error", "target_id": target_id, "error": str(exc)})
            for tab in session.tabs.values():
                tab.status = "closed"
        try:
            browser_close_result = await session_adapter.close()
        except Exception as exc:
            browser_close_result = {"status": "error", "error": str(exc)}
        self._history[session.session_id] = session
        while len(self._history) > _MAX_SESSION_HISTORY:
            self._history.pop(next(iter(self._history)))
        await self._run_hook(self.on_session_closed, session)
        return {
            "status": session.status,
            "browser_session_id": session.session_id,
            "closed_tabs": close_results,
            "browser_close": browser_close_result,
            "session": session.as_dict(),
        }

    async def cleanup_expired(self) -> list[dict[str, Any]]:
        """Close sessions whose absolute lease or idle timeout has elapsed."""
        now = self.clock()
        expired = [
            (session.session_id, reason)
            for session in list(self._sessions.values())
            for is_expired, reason in [session.is_expired(now)]
            if is_expired
        ]
        results = []
        for session_id, reason in expired:
            try:
                results.append(await self.close_session(session_id, reason=reason or "expired", _internal=True))
            except BrowserSessionNotFound:
                continue
        return results

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval_seconds)
                await self.cleanup_expired()
        except asyncio.CancelledError:
            raise

    def start_cleanup(self, *, interval_seconds: float = 60.0) -> asyncio.Task:
        """Start the lease/idle reaper; returns the task for observability."""
        interval = float(interval_seconds)
        if interval <= 0:
            raise ValueError("interval_seconds must be greater than 0")
        if self._cleanup_task and not self._cleanup_task.done():
            return self._cleanup_task
        self._cleanup_interval_seconds = interval
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="emery-browser-session-cleanup")
        return self._cleanup_task

    async def stop_cleanup(self) -> None:
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close_all(self, *, reason: str = "manager_shutdown") -> list[dict[str, Any]]:
        """Close every logical session and stop the background reaper."""
        await self.stop_cleanup()
        results = []
        for session_id in list(self._sessions):
            try:
                results.append(await self.close_session(session_id, reason=reason, _internal=True))
            except BrowserSessionNotFound:
                continue
        return results


_default_manager: BrowserSessionManager | None = None


def get_browser_session_manager() -> BrowserSessionManager:
    """Return Emery's process-wide manager, created lazily."""
    global _default_manager
    if _default_manager is None:
        _default_manager = BrowserSessionManager()
    return _default_manager


async def close_browser_session_manager() -> list[dict[str, Any]]:
    """Lifecycle helper for application shutdown hooks and tests."""
    manager = get_browser_session_manager()
    return await manager.close_all()


__all__ = [
    "BrowserAdapter",
    "BrowserProfile",
    "BrowserSession",
    "BrowserSessionError",
    "BrowserSessionExpired",
    "BrowserSessionManager",
    "BrowserSessionNotFound",
    "BrowserSessionOwner",
    "BrowserSessionOwnershipError",
    "BrowserTab",
    "CdpBrowserAdapter",
    "close_browser_session_manager",
    "get_browser_session_manager",
]
