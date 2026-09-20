"""Small Chromium DevTools Protocol adapter.

The browser tools use a narrow CDP surface directly.  Connections are kept per
page target so CDP events (console messages and native JavaScript dialogs) are
not lost between tool calls.  Sessions are discarded when a target's websocket
changes or its transport fails.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import socket
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from emery import globals
from emery.config import (
    BASE_DIR,
    BROWSER_AUTO_LAUNCH,
    BROWSER_CDP_PORT,
    BROWSER_CDP_URL,
    BROWSER_CHROMIUM_PATH,
    BROWSER_HEADLESS,
    BROWSER_REQUIRE_ACTION_APPROVAL,
    BROWSER_ACTION_APPROVAL_TIMEOUT_SECONDS,
    BROWSER_TIMEOUT_SECONDS,
    BROWSER_USER_DATA_DIR,
    ENABLE_BROWSER,
)


_chromium_process: subprocess.Popen | None = None
_target_sessions: dict[tuple[str, str], "_TargetSession"] = {}
_isolated_target_endpoints: dict[str, str] = {}
_isolated_target_owners: dict[str, tuple[str | None, str | None, str | None]] = {}
_isolated_endpoints: set[str] = set()
_MAX_TARGET_SESSIONS = 8
_MAX_CONSOLE_ENTRIES = 200

_BROWSER_CREDENTIAL_TERMS = re.compile(
    r"password|passcode|token|api[ _-]?key|secret|private[ _-]?key|credit[ _-]?card|cvv|ssn|one[ -]?time[ -]?code",
    re.IGNORECASE,
)
_BROWSER_LOGIN_TERMS = re.compile(
    r"login|log[ -]?in|sign[ -]?in|authenticate|account|security|billing|payment|profile|admin|settings",
    re.IGNORECASE,
)
_BROWSER_EDIT_TERMS = re.compile(
    r"sign[ -]?in|log[ -]?in|save|update|change|delete|remove|submit|confirm|connect|authorize",
    re.IGNORECASE,
)


async def _browser_sensitive_context(target: dict[str, Any], ref: str | None = None) -> dict[str, Any]:
    """Classify whether a browser mutation touches credentials or sensitive data."""
    ref_json = json.dumps(str(ref or ""))
    expression = f"""(() => {{
        const ref = {ref_json};
        const byRef = ref ? Array.from(document.querySelectorAll('[data-emery-ref]'))
            .find(candidate => candidate.getAttribute('data-emery-ref') === ref) : null;
        const active = document.activeElement;
        const el = byRef || active;
        const attrs = el ? [
            el.tagName || '', el.type || '', el.name || '', el.id || '',
            el.getAttribute('aria-label') || '', el.getAttribute('autocomplete') || '',
            el.getAttribute('placeholder') || '', el.innerText || '', el.value || ''
        ].join(' ') : '';
        const page = [location.href || '', document.title || ''].join(' ');
        const loginPage = /login|log-in|signin|sign-in|auth|account|security|billing|payment|profile|admin|settings/i.test(page);
        const credentialField = /password|passcode|token|api[ _-]?key|secret|private[ _-]?key|credit[ _-]?card|cvv|ssn|one[ -]?time[ -]?code/i.test(attrs)
            || (loginPage && /username|user name|email|account|login|password/i.test(attrs));
        const editControl = loginPage && /sign[ -]?in|log[ -]?in|authenticate|save|update|change|delete|remove|submit|confirm|connect|authorize/i.test(attrs);
        return {{
            page,
            element: attrs.slice(0, 500),
            credential_field: credentialField,
            edit_control: editControl,
            tag: el ? el.tagName.toLowerCase() : '',
            type: el ? (el.type || '') : ''
        }};
    }})()"""
    try:
        result = await _cdp_call(target, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        value = ((result.get("result") or {}).get("value") or {})
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {"credential_field": True, "edit_control": False, "element": "unable to classify browser mutation"}


def _browser_sensitive_reason(context: Mapping[str, Any], action: str) -> tuple[bool, str, str]:
    element = str(context.get("element") or "")
    page = str(context.get("page") or "")
    if bool(context.get("credential_field")) or _BROWSER_CREDENTIAL_TERMS.search(element):
        return True, "credentials", "This browser action may enter or submit credentials or other secrets."
    if bool(context.get("edit_control")) and _BROWSER_LOGIN_TERMS.search(page):
        return True, "sensitive_edit", "This browser action changes or submits sensitive account information."
    if action == "dialog" and _BROWSER_CREDENTIAL_TERMS.search(element):
        return True, "credentials", "This browser dialog may handle credentials or other secrets."
    return False, "", ""


async def _approve_browser_action(
    action: str,
    target_id: str | None,
    detail: str = "",
    *,
    sensitive: bool = False,
    approval_key: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Require approval only for browser mutations classified as sensitive."""
    if not BROWSER_REQUIRE_ACTION_APPROVAL or not sensitive:
        return None
    from emery import globals
    from emery.command_execution import _redact_output
    from emery.command_approval import request_command_approval
    safe_detail = _redact_output(str(detail or ""))[:500]
    approval = await request_command_approval(
        f"browser_{action} target_id={target_id or '(default)'} {safe_detail}".strip(),
        reason or f"browser action: {action}",
        chat_id=globals.TARGET_CHAT_ID.get(),
        user_id=globals.current_user_id.get(),
        thread_id=globals.CURRENT_THREAD_ID.get(),
        timeout_seconds=BROWSER_ACTION_APPROVAL_TIMEOUT_SECONDS,
        approval_key=approval_key or f"browser:{action}",
    )
    if approval.get("approved"):
        return None
    return {
        "status": "blocked",
        "target_id": target_id,
        "error": f"Browser action was not executed: {approval.get('message', 'approval denied')}",
    }


class _TargetSession:
    """One serialized, event-aware websocket session for a page target."""

    def __init__(self, target: dict[str, Any], websocket: Any):
        self.target_id = str(target.get("id"))
        self.websocket_url = str(target["webSocketDebuggerUrl"])
        self.websocket = websocket
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.send_lock = asyncio.Lock()
        self.reader_task: asyncio.Task | None = None
        self.console: deque[dict[str, Any]] = deque(maxlen=_MAX_CONSOLE_ENTRIES)
        self.pending_dialog: dict[str, Any] | None = None
        self.dialog_generation = 0
        self.dialog_event = asyncio.Event()
        self.closed = False

    async def start(self) -> None:
        self.reader_task = asyncio.create_task(self._read_events())
        # These domains are deliberately enabled only on the target session
        # that is actually used, keeping the browser's event surface bounded.
        await self.call("Page.enable")
        await self.call("Runtime.enable")

    async def _read_events(self) -> None:
        try:
            async for raw_message in self.websocket:
                payload = json.loads(raw_message)
                request_id = payload.get("id")
                if request_id is not None:
                    future = self.pending.pop(int(request_id), None)
                    if future is None or future.done():
                        continue
                    if payload.get("error"):
                        future.set_exception(RuntimeError(payload["error"].get("message") or str(payload["error"])))
                    else:
                        future.set_result(payload.get("result") or {})
                    continue
                self._record_event(payload.get("method"), payload.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.closed = True
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(f"CDP session closed: {exc}"))
            self.pending.clear()

    def _record_event(self, method: str | None, params: dict[str, Any]) -> None:
        if method == "Runtime.consoleAPICalled":
            args = []
            for item in params.get("args") or []:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                if value is None:
                    value = item.get("description") or item.get("unserializableValue")
                args.append(value)
            self.console.append({
                "type": params.get("type") or "log",
                "args": args,
                "timestamp": params.get("timestamp"),
                "execution_context_id": params.get("executionContextId"),
            })
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            self.console.append({
                "type": "exception",
                "args": [details.get("text") or (details.get("exception") or {}).get("description") or "JavaScript exception"],
                "timestamp": params.get("timestamp"),
                "execution_context_id": details.get("executionContextId"),
            })
        elif method == "Page.javascriptDialogOpening":
            self.dialog_generation += 1
            self.dialog_event.set()
            self.pending_dialog = {
                "type": params.get("type") or "alert",
                "message": params.get("message") or "",
                "default_prompt": params.get("defaultPrompt") or "",
                "opened_at": time.time(),
                "target_id": self.target_id,
            }
        elif method == "Page.javascriptDialogClosed":
            self.pending_dialog = None
            self.dialog_event.clear()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("CDP session is closed")
        if self.pending_dialog and method != "Page.handleJavaScriptDialog":
            return {"__emery_dialog__": dict(self.pending_dialog)}
        request_id = self.next_id
        self.next_id += 1
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[request_id] = future
        dialog_generation = self.dialog_generation
        try:
            async with self.send_lock:
                await self.websocket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            dialog_wait = asyncio.create_task(self.dialog_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {future, dialog_wait},
                    timeout=BROWSER_TIMEOUT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError(f"Timed out waiting for CDP method {method}")
                if future in done:
                    result = future.result()
                    return result if isinstance(result, dict) else {}
                if self.dialog_generation > dialog_generation and self.pending_dialog:
                    # Keep the command future in the session's pending map. CDP
                    # will complete it after the caller responds to the dialog.
                    return {"__emery_dialog__": dict(self.pending_dialog)}
                result = await asyncio.wait_for(future, timeout=BROWSER_TIMEOUT_SECONDS)
                return result if isinstance(result, dict) else {}
            finally:
                if not dialog_wait.done():
                    dialog_wait.cancel()
                await asyncio.gather(dialog_wait, return_exceptions=True)
        except Exception:
            self.pending.pop(request_id, None)
            raise

    async def close(self) -> None:
        self.closed = True
        if self.reader_task and self.reader_task is not asyncio.current_task():
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        try:
            await self.websocket.close()
        except Exception:
            pass
        for future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        self.pending.clear()


async def close_browser_sessions(endpoint: str | None = None) -> None:
    """Close retained target sessions, optionally limited to one CDP endpoint."""
    normalized_endpoint = _normalize_endpoint(endpoint) if endpoint else None
    selected = {
        key: session
        for key, session in _target_sessions.items()
        if normalized_endpoint is None or key[0] == normalized_endpoint
    }
    sessions = list(selected.values())
    for key in selected:
        _target_sessions.pop(key, None)
    await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
    if normalized_endpoint is None:
        _isolated_target_endpoints.clear()
        _isolated_target_owners.clear()
    else:
        for target_id, target_endpoint in list(_isolated_target_endpoints.items()):
            if target_endpoint == normalized_endpoint:
                _isolated_target_endpoints.pop(target_id, None)
                _isolated_target_owners.pop(target_id, None)


def _endpoint_url() -> str:
    return BROWSER_CDP_URL.rstrip("/")


def _normalize_endpoint(endpoint: str | None) -> str:
    return str(endpoint or _endpoint_url()).rstrip("/")


def register_isolated_target_owner(target_id: str, owner: Mapping[str, Any]) -> None:
    """Attach the logical session owner to an isolated target."""
    _isolated_target_owners[str(target_id)] = (
        None if owner.get("chat_id") is None else str(owner.get("chat_id")),
        None if owner.get("thread_id") is None else str(owner.get("thread_id")),
        None if owner.get("user_id") is None else str(owner.get("user_id")),
    )


def _isolated_target_owner_error(target_id: str) -> str | None:
    owner = _isolated_target_owners.get(str(target_id))
    if owner is None:
        return "The isolated browser target has no active ownership record."
    current = tuple(
        None if value is None else str(value)
        for value in (
            globals.TARGET_CHAT_ID.get(),
            globals.CURRENT_THREAD_ID.get(),
            globals.current_user_id.get(),
        )
    )
    for expected, actual in zip(owner, current):
        if expected is not None and expected != actual:
            return "The isolated browser target belongs to another Emery chat session."
    return None


def _valid_url(url: str) -> tuple[bool, str]:
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "url must be an absolute http(s) URL"
    if parsed.username or parsed.password:
        return False, "URLs containing embedded credentials are not allowed"
    if len(clean) > 2_048:
        return False, "url is limited to 2048 characters"
    return True, clean


async def _browser_version(endpoint: str | None = None) -> dict[str, Any] | None:
    try:
        response = await globals.http_client.get(
            f"{_normalize_endpoint(endpoint)}/json/version",
            timeout=BROWSER_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _chromium_binary() -> str | None:
    candidates = [
        BROWSER_CHROMIUM_PATH,
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        "/usr/bin/chromium",
        "/usr/lib/chromium/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    playwright_cache = Path.home() / ".cache" / "ms-playwright"
    if playwright_cache.is_dir():
        candidates.extend(
            str(path)
            for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-linux/chrome")
            for path in sorted(playwright_cache.glob(pattern), reverse=True)
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class IsolatedChromium:
    """Own one Chromium process, profile directory, and CDP endpoint.

    This is deliberately separate from the legacy raw-browser singleton below.
    A browser-session adapter gets one instance of this class, so its tabs can
    never be discovered through another session's CDP endpoint.
    """

    def __init__(self, profile_directory: str, *, cleanup_profile: bool = False) -> None:
        self.profile_directory = Path(profile_directory).expanduser()
        self.cleanup_profile = bool(cleanup_profile)
        self.process: subprocess.Popen | None = None
        self.endpoint: str | None = None
        self.port: int | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> tuple[bool, str | None, bool]:
        async with self._lock:
            if self.process is not None and self.process.poll() is None and self.endpoint:
                if await _browser_version(self.endpoint):
                    return True, None, False

            if self.process is not None:
                await self.close(remove_profile=False)

            if not BROWSER_AUTO_LAUNCH:
                return False, "Browser auto-launch is disabled; no isolated Chromium process is available", False

            binary = _chromium_binary()
            if not binary:
                return False, "Chromium was not found; set BROWSER_CHROMIUM_PATH or install Chromium", False

            self.profile_directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.profile_directory, 0o700)
            except OSError:
                pass
            self.port = _allocate_cdp_port()
            self.endpoint = f"http://127.0.0.1:{self.port}"
            _isolated_endpoints.add(self.endpoint)
            args = [
                binary,
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile_directory}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]
            if BROWSER_HEADLESS:
                args.append("--headless=new")
            try:
                self.process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                self.process = None
                if self.cleanup_profile:
                    shutil.rmtree(self.profile_directory, ignore_errors=True)
                return False, f"Unable to launch Chromium: {exc}", False

        endpoint = self.endpoint or ""
        if await _wait_for_endpoint(endpoint):
            return True, None, True
        await self.close()
        return False, f"Chromium launched but CDP did not become ready at {endpoint}", True

    async def close(self, *, remove_profile: bool | None = None) -> dict[str, Any]:
        endpoint = self.endpoint
        await close_browser_sessions(endpoint=endpoint)
        process = self.process
        terminated = False
        if process is not None:
            if process.poll() is None:
                terminated = True
                pid = getattr(process, "pid", None)
                try:
                    # Chromium starts renderer children, so terminate the
                    # supervised process group rather than only the browser
                    # parent.  Fake processes in tests need only terminate().
                    if pid and hasattr(os, "killpg"):
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    await asyncio.to_thread(process.wait, timeout=BROWSER_TIMEOUT_SECONDS)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        if pid and hasattr(os, "killpg"):
                            os.killpg(pid, signal.SIGKILL)
                        else:
                            process.kill()
                        await asyncio.to_thread(process.wait, timeout=2)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    try:
                        process.terminate()
                        await asyncio.to_thread(process.wait, timeout=BROWSER_TIMEOUT_SECONDS)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        self.process = None
        if endpoint:
            _isolated_endpoints.discard(_normalize_endpoint(endpoint))
            for target_id, target_endpoint in list(_isolated_target_endpoints.items()):
                if target_endpoint == _normalize_endpoint(endpoint):
                    _isolated_target_endpoints.pop(target_id, None)
                    _isolated_target_owners.pop(target_id, None)
        self.endpoint = None
        self.port = None

        should_remove = self.cleanup_profile if remove_profile is None else bool(remove_profile)
        if should_remove:
            try:
                shutil.rmtree(self.profile_directory)
            except FileNotFoundError:
                pass
            except OSError:
                # Cleanup is best effort after the process has stopped. Never
                # turn a successful browser close into an unsafe retry loop.
                pass
        return {
            "status": "completed",
            "browser_process_terminated": terminated,
            "profile_removed": should_remove,
        }


class IsolatedCdpBrowserAdapter:
    """Session-scoped browser adapter backed by one :class:`IsolatedChromium`."""

    def __init__(self, profile: Any) -> None:
        profile_directory = str(getattr(profile, "directory", "") or "").strip()
        if not profile_directory:
            raise ValueError("isolated browser sessions require a unique profile directory")
        metadata = getattr(profile, "metadata", {}) or {}
        self.profile = profile
        self.browser = IsolatedChromium(
            profile_directory,
            cleanup_profile=bool(metadata.get("managed", False)),
        )

    async def _ready(self) -> tuple[bool, str | None, bool]:
        return await self.browser.ensure()

    async def open_tab(self, url: str, *, profile: Any | None = None) -> Mapping[str, Any]:
        ready, error, launched = await self._ready()
        if not ready:
            return {"status": "error", "launched": launched, "error": error}
        return await _open_browser_tab_at_endpoint(self.browser.endpoint or "", url, launched=launched)

    async def list_tabs(self) -> Mapping[str, Any]:
        ready, error, launched = await self._ready()
        if not ready:
            return {"status": "error", "tabs": [], "launched": launched, "error": error}
        return await _list_browser_tabs_at_endpoint(self.browser.endpoint or "", launched=launched)

    async def close_tab(self, target_id: str) -> Mapping[str, Any]:
        if not self.browser.endpoint:
            return {"status": "completed", "target_id": str(target_id), "message": "Browser tab already closed."}
        return await _close_browser_tab_at_endpoint(self.browser.endpoint, target_id)

    async def close(self) -> Mapping[str, Any]:
        return await self.browser.close()


async def _wait_for_endpoint(endpoint: str, *, timeout_seconds: float | None = None) -> bool:
    deadline = asyncio.get_running_loop().time() + min(
        timeout_seconds or 15.0,
        BROWSER_TIMEOUT_SECONDS * 3,
    )
    while asyncio.get_running_loop().time() < deadline:
        if await _browser_version(endpoint):
            return True
        await asyncio.sleep(0.2)
    return False


def _allocate_cdp_port() -> int:
    """Reserve a currently-free loopback port for a supervised Chromium."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _ensure_browser() -> tuple[bool, str | None, bool]:
    if await _browser_version():
        return True, None, False
    if not BROWSER_AUTO_LAUNCH:
        return False, f"No Chromium CDP endpoint is reachable at {_endpoint_url()}", False

    binary = _chromium_binary()
    if not binary:
        return False, "Chromium was not found; set BROWSER_CHROMIUM_PATH or install Chromium", False

    user_data_dir = Path(BROWSER_USER_DATA_DIR)
    if not user_data_dir.is_absolute():
        user_data_dir = BASE_DIR / user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)

    global _chromium_process
    if _chromium_process is None or _chromium_process.poll() is not None:
        args = [
            binary,
            f"--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={BROWSER_CDP_PORT}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
        if BROWSER_HEADLESS:
            args.append("--headless=new")
        try:
            _chromium_process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"Unable to launch Chromium: {exc}", False

    deadline = asyncio.get_running_loop().time() + min(15.0, BROWSER_TIMEOUT_SECONDS * 3)
    while asyncio.get_running_loop().time() < deadline:
        if await _browser_version():
            return True, None, True
        await asyncio.sleep(0.2)
    return False, f"Chromium launched but CDP did not become ready at {_endpoint_url()}", True


async def _list_browser_tabs_at_endpoint(
    endpoint: str,
    *,
    launched: bool = False,
) -> dict[str, Any]:
    try:
        response = await globals.http_client.get(
            f"{_normalize_endpoint(endpoint)}/json/list",
            timeout=BROWSER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        tabs = response.json()
        if not isinstance(tabs, list):
            tabs = []
        normalized = [
            {
                "target_id": item.get("id"),
                "type": item.get("type"),
                "title": item.get("title"),
                "url": item.get("url"),
                "websocket_url": item.get("webSocketDebuggerUrl"),
            }
            for item in tabs
            if isinstance(item, dict)
        ]
        normalized_endpoint = _normalize_endpoint(endpoint)
        if normalized_endpoint in _isolated_endpoints:
            for item in normalized:
                target_id = item.get("target_id")
                if target_id:
                    _isolated_target_endpoints[str(target_id)] = normalized_endpoint
        return {
            "status": "completed",
            "tabs": normalized,
            "cdp_url": _normalize_endpoint(endpoint),
            "launched": launched,
        }
    except Exception as exc:
        return {"status": "error", "tabs": [], "launched": launched, "error": f"Unable to list browser tabs: {exc}"}


async def list_browser_tabs() -> dict[str, Any]:
    """List tabs exposed by the configured raw-browser Chromium endpoint."""
    if not ENABLE_BROWSER:
        return {"status": "disabled", "tabs": [], "error": "Browser control is disabled."}
    ready, error, launched = await _ensure_browser()
    if not ready:
        return {"status": "error", "tabs": [], "launched": launched, "error": error}
    return await _list_browser_tabs_at_endpoint(_endpoint_url(), launched=launched)


async def _close_browser_tab_at_endpoint(endpoint: str, target_id: str) -> dict[str, Any]:
    target_key = str(target_id)
    approval_error = await _approve_browser_action("close_tab", target_id)
    if approval_error:
        return approval_error

    session = _target_sessions.pop((_normalize_endpoint(endpoint), target_key), None)
    _isolated_target_endpoints.pop(target_key, None)
    _isolated_target_owners.pop(target_key, None)
    if session is not None:
        await session.close()
    try:
        response = await globals.http_client.get(
            f"{_normalize_endpoint(endpoint)}/json/close/{quote(target_key, safe='')}",
            timeout=BROWSER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return {"status": "completed", "target_id": target_key, "message": "Browser tab closed."}
    except Exception as exc:
        return {"status": "error", "target_id": target_key, "error": f"Unable to close browser tab: {exc}"}


async def close_browser_tab(target_id: str) -> dict[str, Any]:
    """Close one raw Chromium page target through the CDP HTTP endpoint."""
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    return await _close_browser_tab_at_endpoint(str(target.get("cdp_url") or _endpoint_url()), str(target.get("id") or target_id))


async def close_browser() -> dict[str, Any]:
    """Close Emery's supervised browser sessions and any Chromium it launched."""
    global _chromium_process
    if not ENABLE_BROWSER:
        return {"status": "disabled", "error": "Browser control is disabled."}

    await close_browser_sessions()
    logical_session_results = []
    try:
        # Avoid creating the logical-session manager merely because the raw
        # close tool was called. If it is already active, close its isolated
        # Chromium processes as part of the same shutdown operation.
        from emery import browser_sessions

        if browser_sessions._default_manager is not None:
            logical_session_results = await browser_sessions._default_manager.close_all(
                reason="raw_browser_shutdown"
            )
    except Exception as exc:
        logical_session_results = [{"status": "error", "error": str(exc)}]
    process = _chromium_process
    terminated = False
    if process is not None:
        if process.poll() is None:
            try:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=BROWSER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=2)
            terminated = True
        _chromium_process = None
    return {
        "status": "completed",
        "browser_process_terminated": terminated,
        "browser_session_results": logical_session_results,
        "message": "Emery browser sessions closed." if terminated else "Emery browser sessions closed; no Emery-launched Chromium process was running.",
    }


async def _raw_browser_tabs(endpoint: str | None = None) -> list[dict[str, Any]]:
    response = await globals.http_client.get(
        f"{_normalize_endpoint(endpoint)}/json/list",
        timeout=BROWSER_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


async def _resolve_target(
    target_id: str | None,
    *,
    endpoint: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        tabs = await _raw_browser_tabs(endpoint)
    except Exception as exc:
        return None, f"Unable to inspect Chromium tabs: {exc}"

    pages = [tab for tab in tabs if tab.get("type") == "page"]
    if target_id:
        target = next((tab for tab in pages if str(tab.get("id")) == str(target_id)), None)
        if target is None:
            return None, f"No page tab found for target_id={target_id}"
    elif len(pages) == 1:
        target = pages[0]
    else:
        return None, "target_id is required when Chromium has zero or multiple page tabs"

    websocket_url = target.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str) or not websocket_url.startswith(("ws://", "wss://")):
        return None, "The selected tab does not expose a valid CDP websocket URL"
    return target, None


async def _get_target_session(target: dict[str, Any], endpoint: str | None = None) -> _TargetSession:
    target_id = str(target.get("id"))
    endpoint = _normalize_endpoint(endpoint or target.get("cdp_url"))
    websocket_url = str(target.get("webSocketDebuggerUrl") or "")
    if not target_id or not websocket_url.startswith(("ws://", "wss://")):
        raise RuntimeError("The selected tab does not expose a valid CDP websocket URL")

    session_key = (endpoint, target_id)
    existing = _target_sessions.get(session_key)
    if existing and existing.websocket_url == websocket_url and not existing.closed:
        return existing
    if existing:
        await existing.close()
        _target_sessions.pop(session_key, None)

    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("Browser interactions require the 'websockets' package") from exc

    if len(_target_sessions) >= _MAX_TARGET_SESSIONS:
        oldest_id = next(iter(_target_sessions))
        oldest = _target_sessions.pop(oldest_id)
        await oldest.close()
    websocket = await websockets.connect(
        target["webSocketDebuggerUrl"],
        open_timeout=BROWSER_TIMEOUT_SECONDS,
        close_timeout=1,
        max_size=16 * 1024 * 1024,
    )
    session = _TargetSession(target, websocket)
    _target_sessions[session_key] = session
    try:
        await session.start()
        return session
    except Exception:
        _target_sessions.pop(session_key, None)
        await session.close()
        raise


async def _cdp_call(target: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    endpoint = _normalize_endpoint(target.get("cdp_url"))
    session = await _get_target_session(target, endpoint)
    try:
        result = await session.call(method, params)
        if method != "Page.handleJavaScriptDialog" and session.pending_dialog:
            return {"__emery_dialog__": dict(session.pending_dialog)}
        return result
    except Exception:
        # A broken transport must not be reused for a later tool call.
        session_key = (endpoint, session.target_id)
        if _target_sessions.get(session_key) is session:
            _target_sessions.pop(session_key, None)
        await session.close()
        raise


def _session_for_target(target: dict[str, Any]) -> _TargetSession | None:
    endpoint = _normalize_endpoint(target.get("cdp_url"))
    session = _target_sessions.get((endpoint, str(target.get("id"))))
    return session if session and not session.closed else None


def _dialog_result(target: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    dialog = result.get("__emery_dialog__")
    if not isinstance(dialog, dict):
        return None
    return {
        "status": "awaiting_dialog",
        "target_id": target.get("id"),
        "dialog": dialog,
        "message": "A native JavaScript dialog is open; call browser_handle_dialog to accept or dismiss it.",
    }


def _ref_expression(ref: str) -> str:
    clean = str(ref or "").strip()
    if not re.fullmatch(r"@e\d+", clean):
        raise ValueError("ref must be an element reference from browser_snapshot, such as @e1")
    encoded = json.dumps(clean)
    return f"""(() => {{
        const ref = {encoded};
        const el = Array.from(document.querySelectorAll('[data-emery-ref]'))
            .find(candidate => candidate.getAttribute('data-emery-ref') === ref);
        if (!el) return {{ok: false, error: 'Element reference is stale; call browser_snapshot again.'}};
        return {{ok: true, tag: el.tagName.toLowerCase(), value: el.value || ''}};
    }})()"""


async def _prepare_target(
    target_id: str | None,
    *,
    endpoint: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    if not ENABLE_BROWSER:
        return None, None, "Browser control is disabled."
    selected_endpoint = _normalize_endpoint(endpoint) if endpoint else _isolated_target_endpoints.get(str(target_id or ""))
    if selected_endpoint and selected_endpoint in _isolated_endpoints:
        owner_error = _isolated_target_owner_error(str(target_id or ""))
        if owner_error:
            return None, None, owner_error
        ready = await _browser_version(selected_endpoint) is not None
        error = None if ready else f"No isolated Chromium CDP endpoint is reachable at {selected_endpoint}"
        launched = False
    else:
        selected_endpoint = selected_endpoint or _endpoint_url()
        ready, error, launched = await _ensure_browser()
    if not ready:
        return None, None, error
    target, error = await _resolve_target(target_id, endpoint=selected_endpoint)
    if target is not None:
        target["cdp_url"] = selected_endpoint
    return target, None, error


async def browser_snapshot(target_id: str | None = None, full: bool = False, max_chars: int = 12_000) -> dict[str, Any]:
    """Return a compact text snapshot with stable refs for interactive elements."""
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    max_chars = max(1_000, min(int(max_chars), 30_000))
    expression = """(() => {
        const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
        };
        const nodes = Array.from(document.querySelectorAll(
            'a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]'
        )).filter(visible);
        const elements = nodes.map((el, index) => {
            const ref = `@e${index + 1}`;
            el.setAttribute('data-emery-ref', ref);
            return {
                ref,
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                name: (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') || '').trim().slice(0, 300),
                href: el.href || ''
            };
        });
        return {
            title: document.title,
            url: location.href,
            text: (document.body && document.body.innerText || '').trim(),
            elements
        };
    })()"""
    try:
        result = await _cdp_call(target, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        dialog_response = _dialog_result(target, result)
        if dialog_response:
            return dialog_response
        value = ((result.get("result") or {}).get("value") or {})
        if not isinstance(value, dict):
            return {"status": "error", "error": "Chromium returned an invalid page snapshot."}
        lines = [f"Target: {target.get('id')}", f"Title: {value.get('title') or '(untitled)'}", f"URL: {value.get('url') or target.get('url')}"]
        lines.append("Interactive elements:")
        for element in value.get("elements") or []:
            label = element.get("name") or "(unnamed)"
            role = element.get("role") or element.get("tag") or "element"
            href = f" -> {element['href']}" if element.get("href") else ""
            lines.append(f"[{element.get('ref')}] {role}: {label}{href}")
        if full:
            lines.extend(["", "Page text:", str(value.get("text") or "")])
        snapshot = "\n".join(lines)
        truncated = len(snapshot) > max_chars
        if truncated:
            snapshot = snapshot[:max_chars] + "\n[page snapshot truncated]"
        return {"status": "completed", "target_id": target.get("id"), "url": value.get("url"), "title": value.get("title"), "snapshot": snapshot, "truncated": truncated}
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to snapshot browser page: {exc}"}


async def browser_navigate(target_id: str, url: str) -> dict[str, Any]:
    valid, normalized_url = _valid_url(url)
    if not valid:
        return {"status": "error", "error": normalized_url}
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    approval_error = await _approve_browser_action("navigate", target_id, normalized_url)
    if approval_error:
        return approval_error
    try:
        result = await _cdp_call(target, "Page.navigate", {"url": normalized_url})
        dialog_response = _dialog_result(target, result)
        if dialog_response:
            return dialog_response
        await asyncio.sleep(0.2)
        return {"status": "completed", "target_id": target.get("id"), "url": normalized_url, "frame_id": result.get("frameId")}
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to navigate browser tab: {exc}"}


async def browser_click(target_id: str, ref: str) -> dict[str, Any]:
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    sensitive_context = await _browser_sensitive_context(target, ref)
    sensitive, sensitive_kind, sensitive_reason = _browser_sensitive_reason(sensitive_context, "click")
    approval_error = await _approve_browser_action(
        "click",
        target_id,
        ref,
        sensitive=sensitive,
        approval_key=f"browser:{sensitive_kind}" if sensitive_kind else None,
        reason=sensitive_reason or "browser action: click",
    )
    if approval_error:
        return approval_error
    try:
        result = await _cdp_call(target, "Runtime.evaluate", {
            "expression": _ref_expression(ref).replace(
                "return {ok: true, tag: el.tagName.toLowerCase(), value: el.value || ''};",
                "el.scrollIntoView({block: 'center'}); el.click(); return {ok: true, tag: el.tagName.toLowerCase()};",
            ),
            "returnByValue": True,
            "awaitPromise": True,
        })
        dialog_response = _dialog_result(target, result)
        if dialog_response:
            return dialog_response
        value = ((result.get("result") or {}).get("value") or {})
        if not value.get("ok"):
            return {"status": "error", "target_id": target.get("id"), "error": value.get("error", "Click failed")}
        await asyncio.sleep(0.2)
        return {"status": "completed", "target_id": target.get("id"), "ref": ref, "message": "Element clicked."}
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to click browser element: {exc}"}


async def browser_type(target_id: str, ref: str, text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text) > 4_000:
        return {"status": "error", "error": "text must be a string of at most 4000 characters"}
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    sensitive_context = await _browser_sensitive_context(target, ref)
    sensitive, sensitive_kind, sensitive_reason = _browser_sensitive_reason(sensitive_context, "type")
    approval_error = await _approve_browser_action(
        "type",
        target_id,
        f"ref={ref} text_length={len(text)}",
        sensitive=sensitive,
        approval_key=f"browser:{sensitive_kind}" if sensitive_kind else None,
        reason=sensitive_reason or "browser action: type",
    )
    if approval_error:
        return approval_error
    try:
        focus_result = await _cdp_call(target, "Runtime.evaluate", {
            "expression": _ref_expression(ref).replace(
                "return {ok: true, tag: el.tagName.toLowerCase(), value: el.value || ''};",
                "el.focus(); if (el.select) el.select(); else document.execCommand('selectAll'); return {ok: true};",
            ),
            "returnByValue": True,
        })
        dialog_response = _dialog_result(target, focus_result)
        if dialog_response:
            return dialog_response
        value = ((focus_result.get("result") or {}).get("value") or {})
        if not value.get("ok"):
            return {"status": "error", "target_id": target.get("id"), "error": value.get("error", "Element focus failed")}
        key_down = await _cdp_call(target, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        dialog_response = _dialog_result(target, key_down)
        if dialog_response:
            return dialog_response
        key_up = await _cdp_call(target, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        dialog_response = _dialog_result(target, key_up)
        if dialog_response:
            return dialog_response
        insert_result = await _cdp_call(target, "Input.insertText", {"text": text})
        dialog_response = _dialog_result(target, insert_result)
        if dialog_response:
            return dialog_response
        return {"status": "completed", "target_id": target.get("id"), "ref": ref, "message": "Text entered."}
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to type into browser element: {exc}"}


async def browser_press(target_id: str, key: str) -> dict[str, Any]:
    key_map = {
        "enter": ("Enter", "Enter", 13), "tab": ("Tab", "Tab", 9),
        "escape": ("Escape", "Escape", 27), "backspace": ("Backspace", "Backspace", 8),
        "delete": ("Delete", "Delete", 46), "arrowup": ("ArrowUp", "ArrowUp", 38),
        "arrowdown": ("ArrowDown", "ArrowDown", 40), "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
        "arrowright": ("ArrowRight", "ArrowRight", 39),
    }
    clean_key = str(key or "").strip()
    mapped = key_map.get(clean_key.casefold())
    if mapped is None and len(clean_key) == 1:
        mapped = (clean_key, clean_key, ord(clean_key))
    if mapped is None:
        return {"status": "error", "error": "Unsupported key; use Enter, Tab, Escape, Backspace, Delete, an arrow key, or one character."}
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    sensitive_context = await _browser_sensitive_context(target)
    sensitive, sensitive_kind, sensitive_reason = _browser_sensitive_reason(sensitive_context, "press")
    approval_error = await _approve_browser_action(
        "press",
        target_id,
        clean_key,
        sensitive=sensitive,
        approval_key=f"browser:{sensitive_kind}" if sensitive_kind else None,
        reason=sensitive_reason or "browser action: press",
    )
    if approval_error:
        return approval_error
    try:
        event = {"key": mapped[0], "code": mapped[1], "windowsVirtualKeyCode": mapped[2]}
        key_down = await _cdp_call(target, "Input.dispatchKeyEvent", {"type": "keyDown", **event})
        dialog_response = _dialog_result(target, key_down)
        if dialog_response:
            return dialog_response
        key_up = await _cdp_call(target, "Input.dispatchKeyEvent", {"type": "keyUp", **event})
        dialog_response = _dialog_result(target, key_up)
        if dialog_response:
            return dialog_response
        return {"status": "completed", "target_id": target.get("id"), "key": clean_key}
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to press browser key: {exc}"}


async def browser_back(target_id: str) -> dict[str, Any]:
    """Go back one entry in the selected tab's navigation history."""
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    approval_error = await _approve_browser_action("back", target_id)
    if approval_error:
        return approval_error
    try:
        result = await _cdp_call(target, "Page.goBack")
        dialog_response = _dialog_result(target, result)
        if dialog_response:
            return dialog_response
        await asyncio.sleep(0.2)
        if result.get("entryId") is None and not result.get("url"):
            return {"status": "error", "target_id": target.get("id"), "error": "The tab has no previous history entry."}
        return {
            "status": "completed",
            "target_id": target.get("id"),
            "url": result.get("url") or target.get("url"),
            "entry_id": result.get("entryId"),
        }
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to go back in browser tab: {exc}"}


async def browser_scroll(target_id: str, direction: str = "down", amount: int = 800) -> dict[str, Any]:
    """Scroll the selected page by a bounded amount in one cardinal direction."""
    clean_direction = str(direction or "down").strip().casefold()
    if clean_direction not in {"up", "down", "left", "right"}:
        return {"status": "error", "error": "direction must be up, down, left, or right"}
    try:
        clean_amount = max(1, min(int(amount), 5_000))
    except (TypeError, ValueError):
        return {"status": "error", "error": "amount must be an integer between 1 and 5000"}
    deltas = {"up": (0, -clean_amount), "down": (0, clean_amount), "left": (-clean_amount, 0), "right": (clean_amount, 0)}
    delta_x, delta_y = deltas[clean_direction]
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    approval_error = await _approve_browser_action("scroll", target_id, f"direction={clean_direction} amount={clean_amount}")
    if approval_error:
        return approval_error
    expression = f"""(() => {{
        window.scrollBy({{left: {delta_x}, top: {delta_y}, behavior: 'instant'}});
        return {{x: window.scrollX, y: window.scrollY,
            max_x: Math.max(0, document.documentElement.scrollWidth - window.innerWidth),
            max_y: Math.max(0, document.documentElement.scrollHeight - window.innerHeight)}};
    }})()"""
    try:
        result = await _cdp_call(target, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        dialog_response = _dialog_result(target, result)
        if dialog_response:
            return dialog_response
        value = ((result.get("result") or {}).get("value") or {})
        return {
            "status": "completed",
            "target_id": target.get("id"),
            "direction": clean_direction,
            "amount": clean_amount,
            "scroll": value,
        }
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to scroll browser page: {exc}"}


async def browser_console(target_id: str, max_entries: int = 100, clear: bool = False) -> dict[str, Any]:
    """Return console events observed since this target session was opened."""
    try:
        limit = max(1, min(int(max_entries), _MAX_CONSOLE_ENTRIES))
    except (TypeError, ValueError):
        return {"status": "error", "error": "max_entries must be an integer between 1 and 200"}
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    try:
        session = await _get_target_session(target)
        entries = list(session.console)[-limit:]
        if clear:
            session.console.clear()
        return {
            "status": "completed",
            "target_id": target.get("id"),
            "entries": entries,
            "count": len(entries),
            "cleared": bool(clear),
        }
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to read browser console: {exc}"}


async def browser_handle_dialog(target_id: str, action: str, prompt_text: str | None = None) -> dict[str, Any]:
    """Accept or dismiss the currently open native JavaScript dialog."""
    clean_action = str(action or "").strip().casefold()
    if clean_action not in {"accept", "dismiss"}:
        return {"status": "error", "error": "action must be accept or dismiss"}
    if prompt_text is not None and (not isinstance(prompt_text, str) or len(prompt_text) > 4_000):
        return {"status": "error", "error": "prompt_text must be a string of at most 4000 characters"}
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    session = _session_for_target(target)
    dialog_context = {
        "element": str((session.pending_dialog or {}).get("message") if session else ""),
        "page": str(target.get("url") or ""),
        "credential_field": bool(session and (session.pending_dialog or {}).get("type") == "prompt"),
    }
    sensitive, sensitive_kind, sensitive_reason = _browser_sensitive_reason(dialog_context, "dialog")
    approval_error = await _approve_browser_action(
        "dialog",
        target_id,
        f"action={clean_action}",
        sensitive=sensitive,
        approval_key=f"browser:{sensitive_kind}" if sensitive_kind else None,
        reason=sensitive_reason or "browser action: dialog",
    )
    if approval_error:
        return approval_error
    try:
        session = _session_for_target(target)
        if session is None:
            return {"status": "error", "target_id": target.get("id"), "error": "No active CDP session is supervising this tab."}
        dialog = dict(session.pending_dialog or {})
        if not dialog:
            return {"status": "error", "target_id": target.get("id"), "error": "No native JavaScript dialog is currently open."}
        params: dict[str, Any] = {"accept": clean_action == "accept"}
        if prompt_text is not None and dialog.get("type") == "prompt" and clean_action == "accept":
            params["promptText"] = prompt_text
        await session.call("Page.handleJavaScriptDialog", params)
        return {
            "status": "completed",
            "target_id": target.get("id"),
            "action": clean_action,
            "dialog": dialog,
        }
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to respond to browser dialog: {exc}"}


async def browser_screenshot(target_id: str) -> dict[str, Any]:
    target, _, error = await _prepare_target(target_id)
    if error:
        return {"status": "error", "error": error}
    try:
        result = await _cdp_call(target, "Page.captureScreenshot", {"format": "png", "fromSurface": True})
        dialog_response = _dialog_result(target, result)
        if dialog_response:
            return dialog_response
        image_bytes = base64.b64decode(result.get("data") or "", validate=True)
        from emery.media import can_attach_model_image, queue_model_attachment, store_artifact

        artifact_id = store_artifact(image_bytes, mime_type="image/png", label="browser screenshot")
        attachable = can_attach_model_image()
        response = {
            "status": "completed",
            "target_id": target.get("id"),
            "image_artifact_id": artifact_id,
            "attached_to_model": attachable,
        }
        if attachable:
            queue_model_attachment()
            response["_model_attachment"] = {"artifact_id": artifact_id, "label": "Browser screenshot"}
        return response
    except Exception as exc:
        return {"status": "error", "target_id": target.get("id"), "error": f"Unable to capture browser screenshot: {exc}"}


async def _open_browser_tab_at_endpoint(
    endpoint: str,
    url: str,
    *,
    launched: bool = False,
) -> dict[str, Any]:
    valid, normalized_url = _valid_url(url)
    if not valid:
        return {"status": "error", "error": normalized_url}

    try:
        # Chromium's /json/new endpoint takes the destination URL directly
        # after the question mark; passing it as `?url=...` creates about:blank
        # on current Chromium builds.
        encoded_url = quote(normalized_url, safe=":/?=&%")
        response = await globals.http_client.put(
            f"{_normalize_endpoint(endpoint)}/json/new?{encoded_url}",
            timeout=BROWSER_TIMEOUT_SECONDS,
        )
        # Older Chromium builds accept GET for this endpoint instead of PUT.
        if response.status_code == 405:
            response = await globals.http_client.get(
                f"{_normalize_endpoint(endpoint)}/json/new?{encoded_url}",
                timeout=BROWSER_TIMEOUT_SECONDS,
            )
        response.raise_for_status()
        target = response.json()
        if not isinstance(target, dict):
            return {"status": "error", "error": "Chromium returned an invalid tab response."}
        target_id = target.get("id")
        normalized_endpoint = _normalize_endpoint(endpoint)
        if target_id and normalized_endpoint in _isolated_endpoints:
            _isolated_target_endpoints[str(target_id)] = normalized_endpoint
        return {
            "status": "completed",
            "launched": launched,
            "cdp_url": normalized_endpoint,
            "target_id": target_id,
            "type": target.get("type"),
            "title": target.get("title"),
            "url": target.get("url") or normalized_url,
            "websocket_url": target.get("webSocketDebuggerUrl"),
        }
    except Exception as exc:
        return {"status": "error", "launched": launched, "error": f"Unable to open browser tab: {exc}"}


async def open_browser_tab(url: str) -> dict[str, Any]:
    """Open one http(s) URL through the legacy raw-browser endpoint."""
    if not ENABLE_BROWSER:
        return {"status": "disabled", "error": "Browser control is disabled. Set ENABLE_BROWSER=true to enable it."}
    valid, normalized_url = _valid_url(url)
    if not valid:
        return {"status": "error", "error": normalized_url}
    ready, error, launched = await _ensure_browser()
    if not ready:
        return {"status": "error", "launched": launched, "error": error}
    return await _open_browser_tab_at_endpoint(_endpoint_url(), normalized_url, launched=launched)
