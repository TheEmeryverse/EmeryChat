from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit


class BackendError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class OpenAIBackend:
    """OpenAI-compatible model provider adapter."""

    def __init__(self):
        self.url = os.environ.get("ROUTER_MODEL_URL", "http://host.docker.internal:8081/v1/chat/completions")
        self.api_key = os.environ.get("ROUTER_MODEL_API_KEY", "")
        self.timeout = float(os.environ.get("ROUTER_MODEL_TIMEOUT", "900"))

    def complete(self, body: dict, on_chunk=None) -> tuple[bytes, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if body.get("stream") else "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "application/json")
                if not body.get("stream"):
                    return response.read(), content_type
                chunks = []
                while True:
                    read = getattr(response, "read1", response.read)
                    chunk = read(16 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if on_chunk:
                        on_chunk(chunk)
                return b"".join(chunks), content_type
        except urllib.error.HTTPError as exc:
            content = exc.read(800).decode("utf-8", "replace")
            retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
            raise BackendError(f"model backend HTTP {exc.code}: {content}", retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(f"model backend unavailable: {type(exc).__name__}") from exc

    def control(self, body: dict) -> tuple[bytes, str]:
        parts = urlsplit(self.url)
        control_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/control", "", ""))
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(control_url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.read(), response.headers.get("Content-Type", "application/json")
        except Exception as exc:
            raise BackendError(f"model control unavailable: {type(exc).__name__}") from exc

    def health(self) -> None:
        parts = urlsplit(self.url)
        health_url = os.environ.get("ROUTER_MODEL_HEALTH_URL") or urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
        request = urllib.request.Request(health_url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                body = json.loads(response.read(2048))
                if response.status != 200 or body.get("status") != "ok":
                    raise BackendError("model backend is loading")
        except Exception as exc:
            raise BackendError("model backend is loading") from exc
