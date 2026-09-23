"""Canonical metadata for Emery's tools.

The legacy registry is intentionally still the source of truth for handlers and
the model-facing tool schemas used by the existing engine.  This module builds
a read-only, normalized view over that registry so tool discovery can be added
without changing how any handler is called.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping


# These are deliberately small and stable.  They are used as hints for search,
# not as permissions: an enabled handler is still callable through the normal
# engine path.
_DOMAIN_OVERRIDES = {
    "jot_down_note": "memory",
    "read_scratchpad": "memory",
    "clear_scratchpad": "memory",
    "save_user_memory": "memory",
    "get_camera_security_log": "security",
    "get_reolink_snapshot": "security",
    "get_available_cameras": "security",
    "get_noaa_weather": "weather",
    "set_weather_location_alias": "weather",
    "remove_weather_location_alias": "weather",
    "list_weather_location_aliases": "weather",
    "overseer_search_movie": "media",
    "overseer_request_movie": "media",
    "overseer_search_tv": "media",
    "overseer_request_tv_season": "media",
    "use_research_image": "media",
    "generate_image": "media",
    "edit_image": "media",
    "extract_document_with_docling": "web",
    "search_fred_series": "finance",
    "get_fred_series_observations": "finance",
    "search_imf_indicators": "finance",
    "get_imf_datamapper_series": "finance",
    "get_stock_snapshot": "finance",
    "get_stock_price_history": "finance",
    "get_bond_market_dashboard": "finance",
    "get_inflation_dashboard": "finance",
    "get_us_macro_dashboard": "finance",
    "get_equity_market_dashboard": "finance",
    "get_global_macro_dashboard": "finance",
    "get_housing_consumer_dashboard": "finance",
    "get_labor_market_dashboard": "finance",
    "list_portainer_environments": "infrastructure",
    "list_portainer_containers": "infrastructure",
    "update_portainer_container": "infrastructure",
    "run_command": "system",
    "terminal_exec": "system",
    "terminal_session_start": "system",
    "terminal_session_write": "system",
    "terminal_session_read": "system",
    "terminal_session_close": "system",
    "terminal_job_start": "system",
    "terminal_job_status": "system",
    "terminal_job_read": "system",
    "terminal_job_wait": "system",
    "terminal_job_cancel": "system",
    "terminal_list_sessions": "system",
    "terminal_list_jobs": "system",
    "list_browser_tabs": "browser",
    "open_browser_tab": "browser",
    "browser_snapshot": "browser",
    "browser_screenshot": "browser",
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_type": "browser",
    "browser_press": "browser",
    "browser_back": "browser",
    "browser_scroll": "browser",
    "browser_console": "browser",
    "browser_handle_dialog": "browser",
    "close_browser_tab": "browser",
    "close_browser": "browser",
    "browser_session_start": "browser",
    "browser_session_status": "browser",
    "browser_session_list": "browser",
    "browser_session_open_tab": "browser",
    "browser_session_close": "browser",
    "browser_session_cleanup": "browser",
    "add_scheduled_job": "automation",
    "list_scheduled_jobs": "automation",
    "remove_scheduled_job": "automation",
}

_ALIASES = {
    "get_noaa_weather": ("weather", "forecast", "nws"),
    "web_search": ("search", "internet", "browse", "research"),
    "fetch_web_content": ("read webpage", "open url", "scrape"),
    "extract_document_with_docling": ("docling", "extract pdf", "read pdf", "document extraction", "pdf", "docx", "pptx"),
    "use_research_image": ("web image", "research photo", "inspect image", "send image"),
    "get_news_headlines": ("news", "headlines"),
    "get_today_in_history": ("history", "this day in history"),
    "get_calendar_events": ("calendar", "appointments", "schedule today"),
    "get_nest_thermostats": ("thermostat", "temperature", "nest"),
    "get_stock_snapshot": ("stock", "quote", "ticker"),
    "get_stock_price_history": ("stock history", "price chart", "ohlcv"),
    "get_bond_market_dashboard": ("bonds", "treasury", "yield curve"),
    "get_inflation_dashboard": ("inflation", "cpi"),
    "get_system_stats": ("cpu", "ram", "host load"),
    "generate_image": ("image", "draw", "illustrate", "create image", "image generation", "visual asset"),
    "edit_image": ("edit image", "image edit", "retouch", "modify photo", "change attached photo"),
    "speak_message": ("voice", "audio", "read aloud"),
    "send_inter_agent_message": ("hermes", "delegate", "coprocessor"),
    "jot_down_note": ("scratchpad", "working note", "temporary note"),
    "save_user_memory": ("remember", "durable memory", "preference"),
    "run_command": ("terminal", "shell", "command", "execute command", "programming", "local files", "project files", "system command"),
    "list_browser_tabs": ("browser", "chromium", "tabs", "cdp"),
    "open_browser_tab": ("browser", "chromium", "open tab", "navigate", "cdp"),
    "browser_snapshot": ("browser", "page text", "accessibility", "interactive elements"),
    "browser_screenshot": ("browser", "screenshot", "visual", "screen"),
    "browser_navigate": ("browser", "navigate", "go to", "url"),
    "browser_click": ("browser", "click", "press button", "activate"),
    "browser_type": ("browser", "type", "fill form", "input"),
    "browser_press": ("browser", "key", "enter", "tab", "keyboard"),
    "browser_back": ("browser", "back", "history", "previous page"),
    "browser_scroll": ("browser", "scroll", "page", "viewport"),
    "browser_console": ("browser", "console", "javascript", "debug"),
    "browser_handle_dialog": ("browser", "alert", "confirm", "prompt", "dialog"),
    "close_browser_tab": ("browser", "close", "tab", "browser window"),
    "close_browser": ("browser", "close", "shutdown", "browser session"),
    "terminal_exec": ("terminal", "shell", "run command", "one-shot command", "programming", "local files"),
    "terminal_session_start": ("terminal", "interactive shell", "persistent session", "shell state", "programming", "local files"),
    "terminal_session_write": ("terminal", "interactive shell", "send command", "input"),
    "terminal_session_read": ("terminal", "interactive shell", "read output", "session output"),
    "terminal_session_close": ("terminal", "interactive shell", "close session", "stop shell"),
    "terminal_job_start": ("terminal", "background command", "long-running command", "job"),
    "terminal_job_status": ("terminal", "background job", "job status", "is it finished"),
    "terminal_job_read": ("terminal", "background job", "job output", "read output"),
    "terminal_job_wait": ("terminal", "background job", "wait for command", "completion"),
    "terminal_job_cancel": ("terminal", "background job", "stop command", "cancel job"),
    "terminal_list_sessions": ("terminal", "list sessions", "recover session", "interactive shell"),
    "terminal_list_jobs": ("terminal", "list jobs", "recover job", "background command"),
    "browser_session_start": ("browser", "isolated browser", "browser session", "session id"),
    "browser_session_status": ("browser", "browser session", "session tabs", "session status"),
    "browser_session_list": ("browser", "list browser sessions", "isolated browser"),
    "browser_session_open_tab": ("browser", "browser session", "open tab", "isolated tab"),
    "browser_session_close": ("browser", "browser session", "close session", "close tabs"),
    "browser_session_cleanup": ("browser", "browser cleanup", "expired session", "idle session"),
}

# Eager tools are the compact, commonly used set.  Specialized integrations
# remain discoverable but are not included in a default small model context.
_EAGER_DEFAULTS = {
    "jot_down_note", "read_scratchpad", "clear_scratchpad",
    "save_user_memory", "get_calendar_events",
    "get_noaa_weather", "set_weather_location_alias",
    "remove_weather_location_alias", "list_weather_location_aliases",
    "get_news_headlines", "get_today_in_history", "web_search",
    "fetch_web_content", "extract_document_with_docling", "use_research_image", "get_youtube_transcript", "get_system_stats",
    "edit_image",
    "send_inter_agent_message", "delegate_to_coprocessor", "react_to_message",
    "reply_to_message", "send_sticker", "send_gif",
    "run_command",
    "list_browser_tabs", "open_browser_tab", "browser_snapshot", "browser_screenshot",
    "browser_navigate", "browser_click", "browser_type", "browser_press", "browser_back", "browser_scroll",
    "browser_console", "browser_handle_dialog",
    "close_browser_tab", "close_browser",
}


def _domain_for(name: str) -> str:
    if name in _DOMAIN_OVERRIDES:
        return _DOMAIN_OVERRIDES[name]
    if name.startswith(("get_nest_", "set_nest_")):
        return "home"
    if name.startswith(("get_", "set_", "remove_", "list_")):
        return name.split("_", 2)[1] if "_" in name else "general"
    if name.startswith(("search_", "fetch_", "web_")):
        return "web"
    if name.startswith("send_") or name.startswith("reply_") or name.startswith("react_"):
        return "messaging"
    if name.startswith("overseer_"):
        return "media"
    return "general"


def _normalize_schema(raw_schema: Mapping[str, Any] | None, name: str, description: str) -> dict:
    """Return a safe OpenAI-compatible function schema copy."""
    raw = deepcopy(dict(raw_schema or {}))
    function = raw.get("function") if raw.get("type") == "function" else raw
    function = deepcopy(function if isinstance(function, Mapping) else {})
    parameters = function.get("parameters")
    if not isinstance(parameters, Mapping) or not parameters:
        parameters = {"type": "object", "properties": {}}
    else:
        parameters = deepcopy(dict(parameters))
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": str(function.get("name") or name),
            "description": str(function.get("description") or description),
            "parameters": parameters,
        },
    }


@dataclass(frozen=True)
class ToolMetadata:
    """Canonical, serializable metadata for one registered Emery tool."""

    name: str
    description: str
    domain: str
    aliases: tuple[str, ...]
    enabled: bool
    eager: bool
    schema: dict
    handler: Callable[..., Any] | None = None

    @property
    def deferred(self) -> bool:
        return self.enabled and not self.eager

    @property
    def policy(self) -> str:
        if not self.enabled:
            return "disabled"
        return "eager" if self.eager else "deferred"

    def to_dict(self, *, include_schema: bool = True, include_handler: bool = False) -> dict:
        result = {
            "name": self.name,
            "description": self.description,
            "domain": self.domain,
            "aliases": list(self.aliases),
            "enabled": self.enabled,
            "eager": self.eager,
            "deferred": self.deferred,
            "policy": self.policy,
        }
        if include_schema:
            result["schema"] = deepcopy(self.schema)
        if include_handler:
            result["handler"] = self.handler
        return result


def build_tool_catalog(
    available_tools: Mapping[str, Callable[..., Any]],
    tools_schema: list[Mapping[str, Any]],
) -> dict[str, ToolMetadata]:
    """Build a deterministic catalog without mutating either registry input.

    A schema without a handler is retained as disabled metadata, while a
    handler without a schema receives a minimal schema.  This makes registry
    drift visible to callers without dropping an internal tool.
    """
    schemas_by_name: dict[str, Mapping[str, Any]] = {}
    for raw_schema in tools_schema or []:
        if not isinstance(raw_schema, Mapping):
            continue
        function = raw_schema.get("function") if raw_schema.get("type") == "function" else raw_schema
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if name and name not in schemas_by_name:
            schemas_by_name[name] = raw_schema

    # Preserve legacy schema registration order on the wire. A stable order
    # matters for prefix reuse and avoids subtly changing model tool choice.
    # Handler-only entries are appended deterministically afterward.
    names = []
    seen_names = set()
    for raw_schema in tools_schema or []:
        function = raw_schema.get("function") if isinstance(raw_schema, Mapping) and raw_schema.get("type") == "function" else raw_schema
        name = function.get("name") if isinstance(function, Mapping) else None
        if name and str(name) not in seen_names:
            names.append(str(name))
            seen_names.add(str(name))
    for name in sorted(str(name) for name in available_tools):
        if name not in seen_names:
            names.append(name)
            seen_names.add(name)
    catalog: dict[str, ToolMetadata] = {}
    for name in names:
        raw_schema = schemas_by_name.get(name)
        function = raw_schema.get("function") if isinstance(raw_schema, Mapping) and raw_schema.get("type") == "function" else raw_schema
        function = function if isinstance(function, Mapping) else {}
        description = str(function.get("description") or "No description provided.").strip()
        aliases = tuple(dict.fromkeys(str(alias).strip().lower() for alias in _ALIASES.get(name, ()) if str(alias).strip()))
        handler = available_tools.get(name)
        enabled = callable(handler)
        eager = name in _EAGER_DEFAULTS
        schema = _normalize_schema(raw_schema, name, description)
        catalog[name] = ToolMetadata(
            name=name,
            description=description,
            domain=_domain_for(name),
            aliases=aliases,
            enabled=enabled,
            eager=eager,
            schema=schema,
            handler=handler if enabled else None,
        )
    return catalog


def catalog_to_dict(catalog: Mapping[str, ToolMetadata], *, include_schema: bool = False) -> dict[str, dict]:
    return {name: metadata.to_dict(include_schema=include_schema) for name, metadata in sorted(catalog.items())}


def get_canonical_tool_catalog() -> dict[str, ToolMetadata]:
    """Lazily return metadata for the live legacy registry.

    The lazy import keeps this module usable in isolation and avoids making the
    registry's import order part of the public API.
    """
    from emery import tool_registry

    return build_tool_catalog(tool_registry.AVAILABLE_TOOLS, tool_registry.tools_schema)
