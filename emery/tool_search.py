"""Small model-facing bridge for Emery tool discovery and deferred calls."""

from __future__ import annotations

import inspect
import json
import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

from emery import tool_registry
from emery.tool_search_catalog import ToolMetadata, build_tool_catalog


MAX_QUERIES = 8
MAX_QUERY_CHARS = 256
MAX_RESULTS = 16
MAX_ARGUMENT_CHARS = 32_768


class ToolSearchError(ValueError):
    """An expected, structured failure from the tool-search bridge."""

    def __init__(self, code: str, message: str, *, details: Any = None, tool: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.tool = tool

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.tool:
            error["tool"] = self.tool
        if self.details is not None:
            error["details"] = self.details
        return {"ok": False, "error": error}


def _catalog() -> dict[str, ToolMetadata]:
    # Rebuild from the legacy objects so a test or an embedding worker can
    # replace a handler/schema before dispatch without changing tool behavior.
    return build_tool_catalog(tool_registry.AVAILABLE_TOOLS, tool_registry.tools_schema)


def get_tool_catalog() -> dict[str, ToolMetadata]:
    """Return the current canonical catalog keyed by stable tool name."""
    return _catalog()


def _as_queries(queries: str | Sequence[str]) -> list[str]:
    if isinstance(queries, str):
        queries = [queries]
    if not isinstance(queries, Sequence) or isinstance(queries, (bytes, bytearray)):
        raise ToolSearchError("invalid_queries", "queries must be a string or a sequence of strings")
    if not queries or len(queries) > MAX_QUERIES:
        raise ToolSearchError("query_count_out_of_range", f"provide between 1 and {MAX_QUERIES} queries")
    normalized = []
    for index, query in enumerate(queries):
        if not isinstance(query, str):
            raise ToolSearchError("invalid_query", "each query must be a string", details={"index": index})
        query = query.strip()
        if not query:
            raise ToolSearchError("invalid_query", "queries cannot be empty", details={"index": index})
        if len(query) > MAX_QUERY_CHARS:
            raise ToolSearchError("query_too_long", f"each query is limited to {MAX_QUERY_CHARS} characters", details={"index": index})
        normalized.append(query)
    return normalized


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _tool_search_score(metadata: ToolMetadata, query: str) -> tuple[int, int]:
    query_lower = query.lower().strip()
    query_tokens = _tokens(query_lower)
    name_lower = metadata.name.lower()
    alias_text = " ".join(metadata.aliases).lower()
    searchable = f"{name_lower} {metadata.description.lower()} {metadata.domain.lower()} {alias_text}"
    searchable_tokens = _tokens(searchable)
    score = 0
    if query_lower == name_lower:
        score += 10000
    if query_lower in metadata.aliases:
        score += 9000
    if query_lower in name_lower:
        score += 4000
    if query_lower in alias_text:
        score += 2500
    score += 100 * len(query_tokens & _tokens(name_lower))
    score += 60 * len(query_tokens & _tokens(alias_text))
    score += 25 * len(query_tokens & _tokens(metadata.domain))
    score += 10 * len(query_tokens & _tokens(metadata.description))
    if query_tokens and query_tokens.issubset(searchable_tokens):
        score += 100
    return score, len(query_tokens & searchable_tokens)


def search_tools(
    queries: str | Sequence[str],
    limit: int = 8,
    *,
    include_disabled: bool = False,
    include_deferred: bool = True,
) -> list[dict]:
    """Search enabled tool metadata deterministically, merging duplicate hits."""
    normalized_queries = _as_queries(queries)
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ToolSearchError("result_limit_out_of_range", f"limit must be an integer from 1 to {MAX_RESULTS}")
    if type(include_disabled) is not bool:
        raise ToolSearchError("invalid_include_disabled", "include_disabled must be a boolean")
    if type(include_deferred) is not bool:
        raise ToolSearchError("invalid_include_deferred", "include_deferred must be a boolean")

    matches = []
    for catalog_index, metadata in enumerate(_catalog().values()):
        if not include_disabled and not metadata.enabled:
            continue
        if not include_deferred and metadata.deferred:
            continue
        per_query = [_tool_search_score(metadata, query) for query in normalized_queries]
        best_score, best_overlap = max(per_query)
        if best_score <= 0:
            continue
        first_query = next(index for index, score in enumerate(per_query) if score[0] == best_score)
        matches.append((best_score, best_overlap, first_query, catalog_index, metadata))

    matches.sort(key=lambda item: (-item[0], -item[1], item[2], item[3], item[4].name))
    return [metadata.to_dict(include_schema=False) for *_, metadata in matches[:limit]]


def describe_tool(tool_name: str, include_schema: bool = True) -> dict:
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ToolSearchError("invalid_tool_name", "tool_name must be a non-empty string")
    if len(tool_name.strip()) > MAX_QUERY_CHARS:
        raise ToolSearchError("tool_name_too_long", f"tool_name is limited to {MAX_QUERY_CHARS} characters")
    metadata = _catalog().get(tool_name.strip())
    if metadata is None:
        raise ToolSearchError("unknown_tool", f"unknown Emery tool: {tool_name.strip()}", tool=tool_name.strip())
    return metadata.to_dict(include_schema=include_schema)


def classify_tool(tool_name: str) -> str:
    """Return ``eager``, ``deferred``, or ``unavailable`` for a tool name."""
    metadata = _catalog().get(str(tool_name).strip())
    if metadata is None or not metadata.enabled:
        return "unavailable"
    return metadata.policy


def get_tool_activation(tool_name: str) -> dict:
    metadata = _catalog().get(str(tool_name).strip())
    if metadata is None:
        return {"name": str(tool_name).strip(), "enabled": False, "policy": "unavailable", "eager": False, "deferred": False}
    return metadata.to_dict(include_schema=False)


def is_tool_enabled(tool_name: str) -> bool:
    metadata = _catalog().get(str(tool_name).strip())
    return bool(metadata and metadata.enabled)


def is_tool_eager(tool_name: str) -> bool:
    metadata = _catalog().get(str(tool_name).strip())
    return bool(metadata and metadata.enabled and metadata.eager)


def is_tool_deferred(tool_name: str) -> bool:
    metadata = _catalog().get(str(tool_name).strip())
    return bool(metadata and metadata.enabled and metadata.deferred)


def get_eager_tool_schemas() -> list[dict]:
    return [deepcopy(metadata.schema) for metadata in _catalog().values() if metadata.enabled and metadata.eager]


def get_deferred_tool_schemas() -> list[dict]:
    return [deepcopy(metadata.schema) for metadata in _catalog().values() if metadata.enabled and metadata.deferred]


def get_visible_tool_schemas(*, include_deferred: bool = False, include_bridge: bool = True) -> list[dict]:
    """Assemble compact schemas for Worker 3's model payload."""
    schemas = get_eager_tool_schemas()
    if include_deferred:
        schemas.extend(get_deferred_tool_schemas())
    if include_bridge:
        schemas.extend(deepcopy(BRIDGE_TOOLS_SCHEMA))
    return schemas


def _validation_error(message: str, path: str, **details: Any) -> ToolSearchError:
    payload = {"path": path}
    payload.update(details)
    return ToolSearchError("invalid_arguments", message, details=payload)


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        expected_types = expected
    else:
        expected_types = [expected] if expected else []
    type_ok = True
    if expected_types:
        type_ok = any(
            (kind == "object" and isinstance(value, dict))
            or (kind == "array" and isinstance(value, list))
            or (kind == "string" and isinstance(value, str))
            or (kind == "boolean" and type(value) is bool)
            or (kind == "integer" and type(value) is int)
            or (kind == "number" and type(value) in (int, float))
            or (kind == "null" and value is None)
            for kind in expected_types
        )
    if not type_ok:
        raise _validation_error(f"expected {expected}", path, expected=expected)
    if "enum" in schema and value not in schema["enum"]:
        raise _validation_error("value is not one of the allowed options", path, allowed=schema["enum"])
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise _validation_error("string is shorter than minLength", path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise _validation_error("string is longer than maxLength", path)
    if isinstance(value, (int, float)) and type(value) is not bool:
        if "minimum" in schema and value < schema["minimum"]:
            raise _validation_error("number is below minimum", path)
        if "maximum" in schema and value > schema["maximum"]:
            raise _validation_error("number is above maximum", path)
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise _validation_error("array has too many items", path)
        if isinstance(schema.get("items"), Mapping):
            for index, item in enumerate(value):
                _validate_value(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = [name for name in required if name not in value]
        if missing:
            raise _validation_error("missing required argument(s)", path, missing=missing)
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise _validation_error("unknown argument(s)", path, unknown=unknown)
        for name, item in value.items():
            if name in properties and isinstance(properties[name], Mapping):
                _validate_value(item, properties[name], f"{path}.{name}")


def validate_tool_arguments(tool_name: str, arguments: Mapping[str, Any] | None) -> dict:
    metadata = _catalog().get(str(tool_name).strip())
    if metadata is None or not metadata.enabled:
        raise ToolSearchError("unknown_tool", f"unknown or disabled Emery tool: {tool_name}", tool=str(tool_name).strip())
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ToolSearchError("invalid_arguments", "arguments must be a JSON object", tool=metadata.name)
    arguments = dict(arguments)
    if any(not isinstance(name, str) for name in arguments):
        raise ToolSearchError("invalid_arguments", "argument names must be strings", tool=metadata.name)
    try:
        encoded_size = len(json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ToolSearchError("invalid_arguments", "arguments must be JSON-serializable", tool=metadata.name) from exc
    if encoded_size > MAX_ARGUMENT_CHARS:
        raise ToolSearchError("arguments_too_large", f"arguments are limited to {MAX_ARGUMENT_CHARS} characters", tool=metadata.name)
    _validate_value(arguments, metadata.schema["function"]["parameters"], "$")
    return arguments


async def invoke_tool(tool_name: str, arguments: Mapping[str, Any] | None = None, *, deferred_only: bool = False) -> dict:
    """Validate and invoke a registered handler, returning a stable envelope."""
    clean_name = str(tool_name).strip() if isinstance(tool_name, str) else ""
    metadata = _catalog().get(clean_name)
    try:
        if metadata is None or not metadata.enabled:
            raise ToolSearchError("unknown_tool", f"unknown or disabled Emery tool: {clean_name}", tool=clean_name)
        if deferred_only and not metadata.deferred:
            raise ToolSearchError("tool_not_deferred", f"tool is classified as {metadata.policy}; invoke it through the normal eager path", tool=clean_name)
        clean_arguments = validate_tool_arguments(clean_name, arguments)
        result = metadata.handler(**clean_arguments)
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "tool": clean_name, "result": result}
    except ToolSearchError as exc:
        return exc.to_dict()
    except Exception as exc:  # handler errors are structured for the model bridge
        return {"ok": False, "error": {"code": "tool_execution_error", "message": str(exc), "tool": clean_name}}


async def invoke_deferred_tool(tool_name: str, arguments: Mapping[str, Any] | None = None) -> dict:
    return await invoke_tool(tool_name, arguments, deferred_only=True)


def _bridge_error_call(call_name: str) -> dict:
    return {"ok": False, "error": {"code": "unknown_bridge", "message": f"unknown bridge call: {call_name}"}}


async def dispatch_bridge_call(call_name: str, arguments: Mapping[str, Any] | None = None) -> dict:
    """Dispatch one of the three model-facing bridge functions."""
    if not isinstance(arguments, Mapping):
        return {"ok": False, "error": {"code": "invalid_bridge_arguments", "message": "bridge arguments must be a JSON object"}}
    if call_name == "tool_search":
        arguments = dict(arguments)
        return await tool_search(
            arguments.get("queries", arguments.get("query", "")),
            limit=arguments.get("limit", 8),
            include_deferred=arguments.get("include_deferred", True),
        )
    if call_name == "tool_describe":
        arguments = dict(arguments)
        return await tool_describe(
            arguments.get("tool_name", ""),
            include_schema=arguments.get("include_schema", True),
        )
    if call_name == "tool_call":
        arguments = dict(arguments)
        return await invoke_deferred_tool(arguments.get("tool_name", ""), arguments.get("arguments", {}))
    return _bridge_error_call(call_name)


async def tool_search(
    queries: str | Sequence[str],
    limit: int = 8,
    include_deferred: bool = True,
) -> dict:
    """Model-facing search bridge with a stable result envelope."""
    try:
        if type(include_deferred) is not bool:
            raise ToolSearchError("invalid_include_deferred", "include_deferred must be a boolean")
        return {
            "ok": True,
            "results": search_tools(queries, limit=limit, include_deferred=include_deferred),
        }
    except ToolSearchError as exc:
        return exc.to_dict()


async def tool_describe(tool_name: str, include_schema: bool = True) -> dict:
    """Model-facing schema-description bridge with a stable result envelope."""
    try:
        if type(include_schema) is not bool:
            raise ToolSearchError("invalid_include_schema", "include_schema must be a boolean")
        return {"ok": True, "tool": describe_tool(tool_name, include_schema=include_schema)}
    except ToolSearchError as exc:
        return exc.to_dict()


async def tool_call(tool_name: str, arguments: Mapping[str, Any] | None = None) -> dict:
    """Model-facing deferred invocation bridge."""
    return await invoke_deferred_tool(tool_name, arguments)


def assemble_visible_tool_schemas(
    request_context: Any = None,
    *,
    include_deferred: bool = False,
    include_bridge: bool = True,
) -> list[dict]:
    """Stable prompt-cache hook; request context is reserved for future policy."""
    del request_context
    return get_visible_tool_schemas(
        include_deferred=include_deferred,
        include_bridge=include_bridge,
    )


BRIDGE_TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "tool_search", "description": "Find enabled Emery tools by one or more natural-language queries. Returns compact metadata; use tool_describe before calling a deferred tool.", "parameters": {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_QUERIES, "description": "One to eight search queries."}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}, "include_deferred": {"type": "boolean"}}, "required": ["queries"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "tool_describe", "description": "Return the canonical description, domain, activation policy, and JSON schema for one Emery tool.", "parameters": {"type": "object", "properties": {"tool_name": {"type": "string", "maxLength": MAX_QUERY_CHARS}, "include_schema": {"type": "boolean"}}, "required": ["tool_name"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "tool_call", "description": "Validate arguments and invoke one discovered deferred Emery tool.", "parameters": {"type": "object", "properties": {"tool_name": {"type": "string", "maxLength": MAX_QUERY_CHARS}, "arguments": {"type": "object"}}, "required": ["tool_name"], "additionalProperties": False}}},
]

# Explicit aliases make the integration surface easy to discover and preserve
# stable names if Worker 3 prefers a verb-first naming convention.
TOOL_SEARCH_SCHEMAS = BRIDGE_TOOLS_SCHEMA
TOOL_BRIDGE_SCHEMAS = BRIDGE_TOOLS_SCHEMA
BRIDGE_SCHEMAS = BRIDGE_TOOLS_SCHEMA
get_visible_schemas = get_visible_tool_schemas
search_tool_catalog = search_tools
describe_tool_schema = describe_tool
dispatch_tool_call = dispatch_bridge_call
dispatch_tool_search = dispatch_bridge_call
dispatch_capability = dispatch_bridge_call
