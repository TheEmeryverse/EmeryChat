from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message


@dataclass(frozen=True)
class Principal:
    source: str
    request_type: str


_LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def identify(headers: Message) -> Principal | None:
    """Read caller labels from the trusted local-service network boundary."""
    source = (headers.get("X-EmeryRouter-Source") or "").strip().lower()
    request_type = (headers.get("X-EmeryRouter-Request-Type") or "").strip().lower()
    if not _LABEL.fullmatch(source) or not _LABEL.fullmatch(request_type):
        return None
    return Principal(source, request_type)
