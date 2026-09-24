from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request
import urllib.error

from .backend import BackendError, OpenAIBackend
from .storage import JobStore

log = logging.getLogger("emeryrouter.scheduler")


class Scheduler:
    def __init__(self, store: JobStore, backend: OpenAIBackend, on_chunk=None):
        self.store = store
        self.backend = backend
        self.on_chunk = on_chunk
        self.max_attempts = max(1, int(os.environ.get("ROUTER_MAX_ATTEMPTS", "3")))
        self.image_broker_url = os.environ.get("ROUTER_IMAGE_BROKER_URL", "http://host.docker.internal:8188").rstrip("/")
        token_file = os.environ.get("ROUTER_IMAGE_BROKER_TOKEN_FILE", "")
        self.image_broker_token = os.environ.get("ROUTER_IMAGE_BROKER_TOKEN", "")
        if token_file:
            try:
                self.image_broker_token = open(token_file, encoding="utf-8").read().strip()
            except OSError:
                pass
        self.admission_lock = threading.RLock()
        self._wake = threading.Event()
        self._image_hold_maybe = True
        self._thread = threading.Thread(target=self._run, name="emeryrouter-scheduler", daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    def preempt_image_runtime(self):
        headers = {"Content-Type": "application/json"}
        if self.image_broker_token:
            headers["Authorization"] = f"Bearer {self.image_broker_token}"
        request = urllib.request.Request(f"{self.image_broker_url}/priority/pause", data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=float(os.environ.get("ROUTER_IMAGE_PREEMPT_TIMEOUT", "180"))) as response:
                if response.status != 200:
                    raise RuntimeError(f"image broker handoff returned HTTP {response.status}")
                self._image_hold_maybe = True
                log.info("shared_gpu_handoff=restored")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # A non-broker ComfyUI endpoint has no shared GPU lifecycle.
                return
            raise RuntimeError(f"image broker handoff returned HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"image broker handoff failed: {type(exc).__name__}") from exc

    def resume_image_runtime(self):
        headers = {"Content-Type": "application/json"}
        if self.image_broker_token:
            headers["Authorization"] = f"Bearer {self.image_broker_token}"
        request = urllib.request.Request(f"{self.image_broker_url}/priority/resume", data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError(f"image resume returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            raise RuntimeError(f"image resume returned HTTP {exc.code}") from exc

    def _run(self):
        while True:
            with self.admission_lock:
                if self._image_hold_maybe and self.store.pending_interactive() == 0:
                    try:
                        self.resume_image_runtime()
                        self._image_hold_maybe = False
                    except Exception:
                        log.exception("image_runtime_resume_failed at scheduler idle check")
                job = self.store.next_job(self.max_attempts)
            if job is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            start = time.monotonic()
            log.info("job_start id=%s source=%s type=%s priority=%d attempt=%d", job["id"], job["source"], job["request_type"], job["priority"], job["attempts"])
            retry = self._process_job(job)
            if job["request_type"] == "interactive":
                with self.admission_lock:
                    if self.store.pending_interactive() == 0:
                        try:
                            self.resume_image_runtime()
                            self._image_hold_maybe = False
                        except Exception:
                            log.exception("image_runtime_resume_failed job_id=%s", job["id"])
            if retry:
                time.sleep(retry)
                self.wake()
            elif self.store.result(job["id"], job["source"])["status"] == "complete":
                log.info("job_complete id=%s elapsed=%.3f", job["id"], time.monotonic() - start)

    def _process_job(self, job):
        streamed = False
        try:
            import json
            on_chunk = getattr(self, "on_chunk", None)
            def forward_chunk(chunk):
                nonlocal streamed
                streamed = True
                if on_chunk:
                    on_chunk(job["id"], chunk)
            if on_chunk:
                result, content_type = self.backend.complete(json.loads(job["body"]), forward_chunk)
            else:
                result, content_type = self.backend.complete(json.loads(job["body"]))
            self.store.complete(job["id"], result, content_type)
            return 0
        except BackendError as exc:
            if streamed:
                exc.retryable = False
            delay = min(15.0, 0.5 * (2 ** max(0, job["attempts"] - 1)))
            self.store.retry_or_fail(job, str(exc), self.max_attempts, delay)
            if exc.retryable and job["attempts"] < self.max_attempts:
                log.warning("job_retry id=%s attempt=%d delay=%.1f error=%s", job["id"], job["attempts"], delay, exc)
                return delay
            log.error("job_failed id=%s error=%s", job["id"], exc)
            return 0
        except Exception as exc:
            self.store.retry_or_fail(job, f"internal error: {type(exc).__name__}", self.max_attempts, 0)
            log.exception("job_failed id=%s", job["id"])
            return 0
