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
