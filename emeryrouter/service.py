from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .backend import OpenAIBackend
from .policy import priority_for
from .scheduler import Scheduler
from .source import identify
from .storage import JobStore

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("emeryrouter")
store = JobStore()
backend = OpenAIBackend()
live_streams: dict[str, set[queue.Queue]] = {}
live_streams_lock = threading.Lock()


def publish_chunk(job_id: str, data: bytes):
    with live_streams_lock:
        subscribers = tuple(live_streams.get(job_id, ()))
    for subscriber in subscribers:
        subscriber.put(data)


scheduler = Scheduler(store, backend, publish_chunk)
MAX_BODY_BYTES = int(os.environ.get("ROUTER_MAX_BODY_BYTES", str(32 * 1024 * 1024)))
WAIT_SECONDS = float(os.environ.get("ROUTER_SYNC_WAIT_SECONDS", "900"))


class Handler(BaseHTTPRequestHandler):
    server_version = "EmeryRouter/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: bytes, content_type: str = "application/json", extra: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            log.info("client_disconnected path=%s", self.path)

    def _json(self, status: int, value, extra=None):
        self._send(status, json.dumps(value, separators=(",", ":")).encode(), extra=extra)

    def _send_stream(self, job_id: str, subscriber: queue.Queue):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-EmeryRouter-Job-ID", job_id)
        self.end_headers()
        try:
            while True:
                try:
                    data = subscriber.get(timeout=0.25)
                    if data:
                        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                except queue.Empty:
                    pass
                row = store.result(job_id, self._principal_source)
                if row and row["status"] in {"complete", "failed", "cancelled"}:
                    while True:
                        try:
                            data = subscriber.get_nowait()
                        except queue.Empty:
                            break
                        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    if row["status"] != "complete":
                        payload = json.dumps({"error": {"message": row["error"] or row["status"], "job_id": job_id}}, separators=(",", ":")).encode()
                        frame = b"event: error\ndata: " + payload + b"\n\n"
                        self.wfile.write(f"{len(frame):X}\r\n".encode() + frame + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            log.info("stream_client_disconnected job_id=%s", job_id)
        finally:
            with live_streams_lock:
                live_streams.get(job_id, set()).discard(subscriber)

    def _principal(self):
        principal = identify(self.headers)
        if principal is None:
            # Identity labels are required before policy is applied. Close
            # because POST bodies have not been consumed at this point.
            self.close_connection = True
            self._json(400, {"error": {"message": "Set X-EmeryRouter-Source and X-EmeryRouter-Request-Type."}})
        return principal

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("invalid request body size")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        return body

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            try:
                OpenAIBackend().health()
                self._json(200, {"status": "ok", "pending": store.pending(scheduler.max_attempts)})
            except Exception:
                self._json(503, {"status": "loading model", "pending": store.pending(scheduler.max_attempts)})
            return
        if parsed.path == "/metrics" and self.client_address[0] in {"127.0.0.1", "::1"}:
            self._json(200, {"pending_jobs": store.pending(scheduler.max_attempts), "interactive_jobs": store.pending_interactive()})
            return
        principal = self._principal()
        if not principal:
            return
        if parsed.path == "/v1/queues/interactive":
            self._json(200, {"pending": store.pending_interactive()})
            return
        match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})(?:/(result))?", parsed.path)
        if not match:
            self._json(404, {"error": {"message": "Not found"}})
            return
        job_id, result_path = match.groups()
        if result_path:
            row = store.result(job_id, principal.source)
            if row is None:
                self._json(404, {"error": {"message": "Job not found"}})
            elif row["status"] == "complete":
                self._send(200, row["result"], row["content_type"] or "application/json")
            elif row["status"] in {"failed", "cancelled"}:
                self._json(409, {"error": {"message": row["error"] or row["status"], "job_id": job_id}})
            else:
                self._json(202, {"job_id": job_id, "status": row["status"], "position": store.queue_position(job_id)})
            return
        row = store.status(job_id, principal.source)
        if row is None:
            self._json(404, {"error": {"message": "Job not found"}})
        else:
            row["position"] = store.queue_position(job_id)
            self._json(200, row)

    def do_DELETE(self):  # noqa: N802
        principal = self._principal()
        if not principal:
            return
        match = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})", urlsplit(self.path).path)
        if not match:
            self._json(404, {"error": {"message": "Not found"}})
            return
        status = store.cancel(match.group(1), principal.source)
        if status is None:
            self._json(404, {"error": {"message": "Job not found"}})
        else:
            self._json(202, {"status": "cancellation_requested", "previous_status": status})

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/metrics":
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                self._json(404, {"error": {"message": "Not found"}})
                return
            self._json(200, {"pending_jobs": store.pending(scheduler.max_attempts)})
            return
        principal = self._principal()
        if not principal:
            return
        if path == "/v1/chat/completions/control":
            if principal.source != "emerychat":
                self._json(403, {"error": {"message": "Only the EmeryChat model adapter may send completion control."}})
                return
            try:
                result, content_type = backend.control(self._body())
                self._send(200, result, content_type)
            except Exception as exc:
                self._json(502, {"error": {"message": str(exc)[:300]}})
            return
        if path not in {"/v1/chat/completions", "/v1/jobs"}:
            self._json(404, {"error": {"message": "Not found"}})
            return
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            self._json(400, {"error": {"message": "messages must be a non-empty array"}})
            return
        key = self.headers.get("Idempotency-Key")
        if key and (len(key) > 200 or any(ord(c) < 32 for c in key)):
            self._json(400, {"error": {"message": "Invalid Idempotency-Key"}})
            return
        priority = priority_for(principal.source, principal.request_type)
        interactive_priority = min(priority_for("portal", "interactive"), priority_for("jellyfin", "interactive"))
        wants_stream = path == "/v1/chat/completions" and bool(body.get("stream")) and "respond-async" not in self.headers.get("Prefer", "").lower()
        subscriber = queue.Queue() if wants_stream else None
        with scheduler.admission_lock:
            job_id, created = store.enqueue(principal.source, principal.request_type, priority, body, key)
            if created and priority == interactive_priority:
                try:
                    scheduler.preempt_image_runtime()
                except Exception as exc:
                    log.exception("image_handoff_failed job_id=%s", job_id)
                    store.fail_pending(job_id, "image runtime handoff failed")
                    self._json(503, {"error": {"message": "Image runtime handoff failed before model execution. Retry the request with a new idempotency key.", "job_id": job_id}})
                    return
            if subscriber is not None:
                if created:
                    with live_streams_lock:
                        live_streams.setdefault(job_id, set()).add(subscriber)
                else:
                    existing = store.result(job_id, principal.source)
                    if existing and existing["status"] == "complete":
                        self._send(200, existing["result"], existing["content_type"] or "text/event-stream", {"X-EmeryRouter-Job-ID": job_id})
                        return
                    # A duplicate streaming request can attach only to future
                    # chunks; return a durable job handle so callers fetch its
                    # complete result instead of receiving a truncated stream.
                    self._json(202, {"job_id": job_id, "status": "working", "status_url": f"/v1/jobs/{job_id}", "result_url": f"/v1/jobs/{job_id}/result"}, {"Location": f"/v1/jobs/{job_id}"})
                    return
            scheduler.wake()
        if subscriber is not None:
            self._principal_source = principal.source
            self._send_stream(job_id, subscriber)
            return
        if path == "/v1/jobs" or self.headers.get("Prefer", "").lower().find("respond-async") >= 0:
            self._json(202, {"job_id": job_id, "status": "queued", "created": created, "status_url": f"/v1/jobs/{job_id}", "result_url": f"/v1/jobs/{job_id}/result"}, {"Location": f"/v1/jobs/{job_id}"})
            return
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            row = store.result(job_id, principal.source)
            if row and row["status"] == "complete":
                self._send(200, row["result"], row["content_type"] or "application/json", {"X-EmeryRouter-Job-ID": job_id, "Idempotency-Key": key or ""})
                return
            if row and row["status"] in {"failed", "cancelled"}:
                self._json(503, {"error": {"message": row["error"] or row["status"], "job_id": job_id}})
                return
            time.sleep(0.2)
        self._json(202, {"job_id": job_id, "status": "working", "status_url": f"/v1/jobs/{job_id}", "result_url": f"/v1/jobs/{job_id}/result"}, {"Location": f"/v1/jobs/{job_id}"})

    def log_message(self, fmt, *args):  # noqa: A002
        log.info("client=%s %s", self.client_address[0], fmt % args)


def main():
    host = os.environ.get("ROUTER_HOST", "0.0.0.0")
    port = int(os.environ.get("ROUTER_PORT", "8220"))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("service_start host=%s port=%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
