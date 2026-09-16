import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|key|secret|password|authorization|cookie|session|credential|bearer)",
    re.IGNORECASE,
)


def safe_preview(value, max_len: int = 160) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def redact_string(value: str) -> str:
    text = str(value)
    text = re.sub(
        r"([?&](?:token|key|api_key|password|authorization|cookie|session)=[^&\s]+)",
        lambda match: match.group(0).split("=", 1)[0] + "=<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(https?://[^/\s:@]+:)[^@/\s]+@", r"\1<redacted>@", text)
    return text


def sanitize_for_logging(value, key_name: str = ""):
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if SENSITIVE_KEY_PATTERN.search(str(key))
                else sanitize_for_logging(inner_value, str(key))
            )
            for key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_logging(item, key_name) for item in value]
    if isinstance(value, str):
        if key_name and SENSITIVE_KEY_PATTERN.search(key_name):
            return "<redacted>"
        return redact_string(value)
    return value


def format_logging_payload(value, max_len: int = 240) -> str:
    try:
        text = json.dumps(sanitize_for_logging(value), ensure_ascii=True, sort_keys=True)
    except TypeError:
        text = str(sanitize_for_logging(value))
    return safe_preview(text, max_len=max_len)


def cache_report(response_json: dict | None) -> dict:
    """Extract only endpoint-reported cache facts.

    A local endpoint that omits these fields is represented as ``reported:``
    false; callers must not infer a hit from a warmup request or a hash match.
    """
    response_json = response_json if isinstance(response_json, dict) else {}
    usage = response_json.get("usage") or {}
    details = usage.get("prompt_tokens_details") or usage.get("prompt_token_details") or {}
    cached_tokens = first_number(details, "cached_tokens", "cache_read_input_tokens")
    if cached_tokens is not None:
        return {"reported": True, "hit": cached_tokens > 0, "cached_tokens": cached_tokens}

    for source in (response_json, usage, details):
        for key in ("prompt_cache_hit", "cache_hit", "cached"):
            if key in source and isinstance(source[key], bool):
                return {"reported": True, "hit": source[key]}
    return {"reported": False, "hit": None}


def format_cache_diagnostics(diagnostics: dict | None, response_json: dict | None = None, max_len: int = 640) -> str:
    """Format hash/count cache diagnostics without logging prompt contents."""
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    safe = {
        key: diagnostics.get(key)
        for key in (
            "epoch", "model", "message_count", "stable_prefix_hash",
            "tool_schema_hash", "request_shape_hash",
        )
        if key in diagnostics
    }
    safe["endpoint_cache"] = cache_report(response_json)
    return format_logging_payload(safe, max_len=max_len)


def first_number(mapping: dict, *keys):
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def format_perf_rate(tokens, millis) -> str:
    if not tokens or not millis:
        return "n/a"
    seconds = millis / 1000
    if seconds <= 0:
        return "n/a"
    return f"{tokens / seconds:.2f} t/s"


def format_token_count(value) -> str:
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"{value / 1000:.2f}k"
    return str(int(value))


def format_duration_ms(value) -> str:
    if value is None:
        return "n/a"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def format_llama_perf_line(label: str, response_json: dict, wall_seconds: float) -> str:
    usage = response_json.get("usage") or {}
    timings = response_json.get("timings") or response_json.get("timing") or {}

    prompt_tokens = first_number(usage, "prompt_tokens", "prompt_n")
    completion_tokens = first_number(usage, "completion_tokens", "completion_n", "predicted_n")
    total_tokens = first_number(usage, "total_tokens")
    cached_tokens = first_number(usage.get("prompt_tokens_details") or {}, "cached_tokens")

    llama_prompt_n = first_number(timings, "prompt_n", "prompt_tokens")
    llama_prompt_ms = first_number(timings, "prompt_ms", "prompt_time_ms")
    llama_predicted_n = first_number(timings, "predicted_n", "completion_n", "predicted_tokens")
    llama_predicted_ms = first_number(timings, "predicted_ms", "predicted_time_ms", "completion_ms")

    cache_fragment = f" | cache {format_token_count(cached_tokens)}" if cached_tokens is not None else ""
    return (
        f"⚡ {label}: in {format_token_count(prompt_tokens)} | out {format_token_count(completion_tokens)} | "
        f"total {format_token_count(total_tokens)} | wall {wall_seconds:.2f}s{cache_fragment} | "
        f"prefill {format_perf_rate(llama_prompt_n, llama_prompt_ms)} ({format_token_count(llama_prompt_n)}, {format_duration_ms(llama_prompt_ms)}) | "
        f"decode {format_perf_rate(llama_predicted_n, llama_predicted_ms)} ({format_token_count(llama_predicted_n)}, {format_duration_ms(llama_predicted_ms)})"
    )
