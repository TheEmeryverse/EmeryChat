"""Deterministic prompt-prefix state and request-shape helpers.

This module deliberately knows nothing about a particular model server.  It
freezes the reusable part of an Emery request and gives the engine fresh
mutable copies when it builds a request.  Endpoint-specific cache metadata is
opt-in; local OpenAI-compatible endpoints do not receive it by default.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping


_CACHE_LOCK = threading.RLock()
_PROMPT_EPOCH = 0
_STATE_CACHE: dict[tuple[Any, ...], "StablePromptState"] = {}
_TOOL_ASSEMBLER: Callable[..., Any] | None = None
_TOOL_DISPATCHER: Callable[..., Any] | None = None


def _canonical(value: Any) -> Any:
    """Return a JSON-compatible, deterministic representation of ``value``."""
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=lambda item: str(item))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=lambda item: stable_json(item))
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return value.isoformat()
        except Exception:
            pass
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def stable_json(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _deep_tuple(value: Any) -> Any:
    """Make cached state immutable enough that callers cannot mutate it."""
    if isinstance(value, Mapping):
        return tuple((str(key), _deep_tuple(inner)) for key, inner in sorted(value.items(), key=lambda pair: str(pair[0])))
    if isinstance(value, (list, tuple)):
        return tuple(_deep_tuple(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_deep_tuple(item) for item in value), key=repr))
    return value


def _deep_dict(value: Any) -> Any:
    """Rehydrate the immutable representation used by ``StablePromptState``."""
    if isinstance(value, tuple):
        if all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value):
            return {key: _deep_dict(inner) for key, inner in value}
        return [_deep_dict(item) for item in value]
    return copy.deepcopy(value)


def _context_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return stable_json(value)


@dataclass(frozen=True)
class StablePromptState:
    """Frozen reusable prefix and tool schema for one prompt epoch."""

    epoch: int
    model: str
    stable_prefix: tuple[Any, ...]
    tool_schema: tuple[Any, ...]
    stable_prefix_hash: str
    tool_schema_hash: str

    def prefix_messages(self) -> list[dict]:
        return _deep_dict(self.stable_prefix)

    def tools(self) -> list[dict]:
        return _deep_dict(self.tool_schema)


def current_prompt_epoch() -> int:
    with _CACHE_LOCK:
        return _PROMPT_EPOCH


def invalidate_prompt_cache(reason: str = "") -> int:
    """Invalidate all frozen prompt state and advance the prompt epoch."""
    global _PROMPT_EPOCH
    with _CACHE_LOCK:
        _PROMPT_EPOCH += 1
        _STATE_CACHE.clear()
        epoch = _PROMPT_EPOCH
    logging.info("ENGINE: Prompt cache invalidated%s; epoch=%s", f" ({reason})" if reason else "", epoch)
    return epoch


def invalidate_prompt_epoch(reason: str = "") -> int:
    """Compatibility alias for callers that use the epoch terminology."""
    return invalidate_prompt_cache(reason)


def clear_prompt_cache(reason: str = "") -> int:
    return invalidate_prompt_cache(reason)


def get_stable_prompt_state(
    *,
    stable_system_prompt: str,
    model: str,
    tool_schema: list[dict] | tuple[dict, ...] | None = None,
    session_context: Any = None,
    prompt_epoch: int | None = None,
) -> StablePromptState:
    """Get a frozen prefix snapshot, keyed by prompt inputs and epoch."""
    epoch = current_prompt_epoch() if prompt_epoch is None else int(prompt_epoch)
    session_text = _context_text(session_context)
    system_text = str(stable_system_prompt or "")
    if session_text:
        session_section = session_text if session_text.startswith("# Session Context") else f"# Session Context\n{session_text}"
        system_text = f"{system_text}\n\n{session_section}"

    normalized_tools = list(tool_schema or [])
    prefix = [{"role": "system", "content": system_text}]
    key = (epoch, str(model), stable_hash(prefix), stable_hash(normalized_tools))
    with _CACHE_LOCK:
        cached = _STATE_CACHE.get(key)
        if cached is not None:
            return cached
        state = StablePromptState(
            epoch=epoch,
            model=str(model),
            stable_prefix=_deep_tuple(prefix),
            tool_schema=_deep_tuple(normalized_tools),
            stable_prefix_hash=stable_hash(prefix),
            tool_schema_hash=stable_hash(normalized_tools),
        )
        _STATE_CACHE[key] = state
        return state


def request_shape_hash(payload: Mapping[str, Any]) -> str:
    """Hash request structure while excluding message text and tool arguments.

    This is diagnostic metadata only.  It is intentionally not inserted into
    the outbound model payload.
    """
    def shape(value: Any, key: str = "") -> Any:
        if isinstance(value, Mapping):
            return {str(k): shape(v, str(k)) for k, v in sorted(value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (list, tuple)):
            return [shape(item, key) for item in value]
        if key in {"content", "arguments", "reasoning_content", "thinking", "reasoning"}:
            return "<text>"
        if isinstance(value, (str, int, float, bool)) or value is None:
            return type(value).__name__ if isinstance(value, str) else value
        return type(value).__name__

    return stable_hash(shape(payload))


def cache_diagnostics(payload: Mapping[str, Any], state: StablePromptState) -> dict[str, Any]:
    """Return non-sensitive diagnostics suitable for logging."""
    messages = payload.get("messages") or []
    return {
        "epoch": state.epoch,
        "model": state.model,
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "stable_prefix_hash": state.stable_prefix_hash,
        "tool_schema_hash": state.tool_schema_hash,
        "request_shape_hash": request_shape_hash(payload),
        "endpoint_cache_reported": False,
    }


def endpoint_cache_options() -> dict[str, Any]:
    """Read explicit opt-in endpoint metadata settings.

    The current custom local endpoint is not assumed to understand either
    field.  Both options remain absent unless an operator opts in via env.
    """
    send_control = str(os.getenv("PROMPT_CACHE_SEND_CACHE_CONTROL", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    send_key = str(os.getenv("PROMPT_CACHE_SEND_KEY", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    configured_key = (
        os.getenv("PROMPT_CACHE_KEY")
        or os.getenv("MAIN_MODEL_PROMPT_CACHE_KEY")
        or ""
    ).strip()
    result: dict[str, Any] = {}
    if send_control:
        result["cache_control"] = {"type": os.getenv("PROMPT_CACHE_CONTROL_TYPE", "ephemeral")}
    if send_key and configured_key:
        result["prompt_cache_key"] = configured_key
    return result


def register_tool_search_hooks(*, assembler=None, dispatcher=None) -> None:
    """Register Worker 2's optional capability assembly/dispatch hooks.

    The engine does not own or recreate a Tool Search registry.  Worker 2 can
    register hooks here, or expose one of the discovery names below from an
    optional ``emery.tool_search``/``emery.tool_search_hooks`` module.
    """
    global _TOOL_ASSEMBLER, _TOOL_DISPATCHER
    with _CACHE_LOCK:
        _TOOL_ASSEMBLER = assembler
        _TOOL_DISPATCHER = dispatcher
    invalidate_prompt_cache("tool-search hooks changed")


def _discover_hook(kind: str):
    with _CACHE_LOCK:
        registered = _TOOL_ASSEMBLER if kind == "assemble" else _TOOL_DISPATCHER
    if registered is not None:
        return registered

    try:
        from emery import tool_registry
    except Exception:
        tool_registry = None
    modules = [tool_registry]
    for module_name in ("emery.tool_search", "emery.tool_search_hooks"):
        try:
            modules.append(__import__(module_name, fromlist=["*"]))
        except Exception:
            continue
    names = (
        (
            "assemble_tool_search", "assemble_capabilities", "get_tool_capabilities",
            "get_capability_snapshot", "assemble_visible_tool_schemas",
            "get_visible_schemas", "get_visible_tool_schemas",
        )
        if kind == "assemble"
        else ("dispatch_tool_search", "dispatch_capability", "dispatch_tool_call")
    )
    for module in modules:
        for name in names:
            candidate = getattr(module, name, None) if module is not None else None
            if callable(candidate):
                return candidate
    return None


def resolve_tooling(default_available: Mapping[str, Callable], default_schema: list[dict], *, request_context=None):
    """Resolve optional Worker 2 tooling without imposing a registry shape."""
    assembler = _discover_hook("assemble")
    if assembler is None:
        return dict(default_available), copy.deepcopy(list(default_schema)), "legacy-registry"

    try:
        result = assembler(request_context=request_context)
    except TypeError:
        try:
            result = assembler(request_context)
        except TypeError:
            result = assembler()
    except Exception as exc:
        logging.warning("ENGINE: Optional Tool Search assembly unavailable: %s", exc)
        return dict(default_available), copy.deepcopy(list(default_schema)), "legacy-registry"

    # Worker 2's schema-only assembly is intentionally accepted as a stable
    # boundary. The legacy handlers remain the source of truth for eager calls;
    # the three bridge names are dispatched by the optional bridge hook.
    if isinstance(result, (list, tuple)):
        schema = copy.deepcopy(list(result))
        available = {
            name: handler
            for name, handler in default_available.items()
            if any(
                isinstance(item, Mapping)
                and isinstance(item.get("function"), Mapping)
                and item["function"].get("name") == name
                for item in schema
            )
        }
        bridge_dispatch_names = {"tool_search", "tool_describe", "tool_call"}
        for item in schema:
            function = item.get("function") if isinstance(item, Mapping) else None
            name = function.get("name") if isinstance(function, Mapping) else None
            if name in bridge_dispatch_names:
                available[name] = _bridge_placeholder
        return available, schema, "tool-search-hook"
    if not isinstance(result, Mapping):
        logging.warning("ENGINE: Optional Tool Search assembly returned unsupported data; using legacy registry.")
        return dict(default_available), copy.deepcopy(list(default_schema)), "legacy-registry"
    available = result.get("available_tools", result.get("tools_by_name", default_available))
    schema = result.get("tools_schema", result.get("tools", default_schema))
    if not isinstance(available, Mapping) or not isinstance(schema, (list, tuple)):
        logging.warning("ENGINE: Optional Tool Search assembly missing compatible hooks; using legacy registry.")
        return dict(default_available), copy.deepcopy(list(default_schema)), "legacy-registry"
    return dict(available), copy.deepcopy(list(schema)), "tool-search-hook"


def _bridge_placeholder(**_kwargs):
    """Marker handler for bridge schemas dispatched by Worker 2's hook."""
    return None


async def dispatch_tool_call(name: str, args: Mapping[str, Any], fallback: Callable[..., Any]):
    dispatcher = _discover_hook("dispatch")
    if dispatcher is None:
        return await fallback(**args) if args else await fallback()
    # Worker 2's dispatcher is a bridge dispatcher, not a replacement for
    # every eager handler in the legacy registry. Registered custom hooks may
    # opt into all names; discovered module hooks are limited to bridge names.
    with _CACHE_LOCK:
        is_registered = _TOOL_DISPATCHER is not None
    if not is_registered and name not in {"tool_search", "tool_describe", "tool_call"}:
        return await fallback(**args) if args else await fallback()
    try:
        result = dispatcher(name, dict(args or {}))
    except TypeError:
        result = dispatcher(name=name, args=dict(args or {}))
    if inspect.isawaitable(result):
        return await result
    return result
