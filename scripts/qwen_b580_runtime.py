#!/usr/bin/env python3
"""Exclusive Ornith/B580 Qwen Image runtime broker.

Port 8188 remains the stable EmeryChat image endpoint. The actual ComfyUI
process is launched only after a prompt arrives, on the modern XPU stack and
an isolated runtime directory. Once the image is downloaded (or the request
fails/idles), ComfyUI is stopped and Ornith is restored before the broker
returns to its idle state.

The broker intentionally supports only the small ComfyUI API surface used by
EmeryChat: /upload/image, /prompt, /history/<id>, /view, /system_stats, and /health.
"""

from __future__ import annotations

import json
import base64
import hashlib
import logging
import os
import signal
import socket
import struct
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
COMFYUI_ROOT = ROOT / "downloads/qwen-image-2.1/software/ComfyUI"
PYTHON = ROOT / ".venv-qwen-xpu-modern/bin/python"
BASE_DIR = ROOT / "runtime/qwen-b580-comfyui"
LOG_DIR = ROOT / "logs"
COMFY_LOG = LOG_DIR / "qwen-b580-comfyui.log"
PORT = int(os.environ.get("QWEN_RUNTIME_PORT", "8188"))
BACKEND_PORT = int(os.environ.get("QWEN_RUNTIME_BACKEND_PORT", "8190"))
BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
ENCODER_HEALTH_URL = os.environ.get("QWEN_RUNTIME_ENCODER_HEALTH_URL", "http://127.0.0.1:8086/health")
ORNITH_UNIT = os.environ.get("QWEN_RUNTIME_ORNITH_UNIT", "llama-ornith-aot.service")
START_TIMEOUT_SECONDS = float(os.environ.get("QWEN_RUNTIME_START_TIMEOUT", "240"))
IDLE_TIMEOUT_SECONDS = float(os.environ.get("QWEN_RUNTIME_IDLE_TIMEOUT", "300"))
HTTP_TIMEOUT_SECONDS = float(os.environ.get("QWEN_RUNTIME_HTTP_TIMEOUT", "30"))

logging.basicConfig(
    level=os.environ.get("QWEN_RUNTIME_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("qwen-b580-runtime")


def _run_systemctl(action: str) -> None:
    result = subprocess.run(
        ["systemctl", "--user", action, ORNITH_UNIT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout or "").strip()[-800:]
        raise RuntimeError(f"systemctl --user {action} {ORNITH_UNIT} failed: {detail}")


def _ornith_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", ORNITH_UNIT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def _urlopen(
    method: str,
    url: str,
    body: bytes | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
    content_type: str | None = None,
):
    request = Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", content_type or "application/json")
    return urlopen(request, timeout=timeout)


class _ProgressWatcher:
    """Small dependency-free ComfyUI WebSocket client for execution events."""

    def __init__(self, client_id: str, on_event) -> None:
        self.client_id = str(client_id)
        self.on_event = on_event
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.socket: socket.socket | None = None
        self.socket_lock = threading.Lock()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="qwen-comfy-progress", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.socket_lock:
            sock = self.socket
            self.socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < size:
            try:
                chunk = sock.recv(size - len(chunks))
            except socket.timeout:
                return b""
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)

    @staticmethod
    def _send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
        # Client-to-server WebSocket frames must be masked.
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sock.sendall(header + mask + masked)

    def _handshake(self, sock: socket.socket) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /ws?clientId={self.client_id} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("ComfyUI WebSocket closed during handshake")
            response.extend(chunk)
            if len(response) > 65536:
                raise ConnectionError("ComfyUI WebSocket handshake was too large")
        header = bytes(response).split(b"\r\n", 1)[0]
        if not header.startswith(b"HTTP/1.1 101"):
            raise ConnectionError(f"ComfyUI WebSocket handshake failed: {header[:120]!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        response_headers = {}
        for line in bytes(response).decode("latin1").split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        if response_headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("ComfyUI WebSocket handshake returned an invalid accept key")

    def _run(self) -> None:
        try:
            sock = socket.create_connection(("127.0.0.1", BACKEND_PORT), timeout=5)
            sock.settimeout(1.0)
            with self.socket_lock:
                self.socket = sock
            self._handshake(sock)
            log.info("Connected to ComfyUI progress WebSocket client_id=%s", self.client_id)
            while not self.stop_event.is_set():
                header = self._recv_exact(sock, 2)
                if header == b"":
                    continue
                if header is None:
                    break
                first, second = header
                opcode = first & 0x0F
                length = second & 0x7F
                if length == 126:
                    extended = self._recv_exact(sock, 2)
                    if not extended:
                        break
                    length = struct.unpack(">H", extended)[0]
                elif length == 127:
                    extended = self._recv_exact(sock, 8)
                    if not extended:
                        break
                    length = struct.unpack(">Q", extended)[0]
                if length > 4 * 1024 * 1024:
                    raise ValueError("ComfyUI progress WebSocket frame is too large")
                mask = self._recv_exact(sock, 4) if (second & 0x80) else None
                payload = self._recv_exact(sock, length)
                if payload is None or payload == b"":
                    if length:
                        break
                    payload = b""
                if mask:
                    payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._send_frame(sock, 0xA, payload)
                    continue
                if opcode != 0x1:
                    continue
                try:
                    message = json.loads(payload.decode("utf-8"))
                    event = message.get("type")
                    data = message.get("data")
                    if isinstance(event, str) and isinstance(data, dict):
                        if self.on_event(event, data):
                            break
                except (UnicodeDecodeError, ValueError, TypeError):
                    log.debug("Ignoring malformed ComfyUI progress event", exc_info=True)
        except Exception as exc:
            if not self.stop_event.is_set():
                log.warning("ComfyUI progress WebSocket unavailable: %s", exc)
        finally:
            with self.socket_lock:
                if self.socket is not None:
                    try:
                        self.socket.close()
                    except OSError:
                        pass
                    self.socket = None


class Runtime:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.process: subprocess.Popen[bytes] | None = None
        self.child_log = None
        self.active_prompt_id: str | None = None
        self.started_mono_ns: int | None = None
        self.request_started_mono: float | None = None
        self.last_activity = time.monotonic()
        self.stopping = False
        self.progress_watcher: _ProgressWatcher | None = None
        self.progress: dict[str, Any] = {}
        self.uploaded_image: dict[str, str] | None = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            active = self.process is not None and self.process.poll() is None
            return {
                "status": "active" if active else "idle",
                "backend": BACKEND_URL if active else None,
                "prompt_id": self.active_prompt_id,
                "started_mono_ns": self.started_mono_ns,
                "ornith_active": _ornith_active(),
            }

    def progress_status(self, prompt_id: str) -> dict[str, Any]:
        with self.lock:
            if prompt_id != self.active_prompt_id and prompt_id != self.progress.get("prompt_id"):
                return {"status": "unknown", "prompt_id": prompt_id}
            return dict(self.progress or {"status": "starting", "prompt_id": prompt_id})

    def _record_progress(self, event: str, data: dict[str, Any]) -> bool:
        prompt_id = data.get("prompt_id") or self.active_prompt_id
        with self.lock:
            if not prompt_id or (self.active_prompt_id and str(prompt_id) != self.active_prompt_id):
                return False
            prompt_id = str(prompt_id)
            progress = dict(self.progress)
            progress["prompt_id"] = prompt_id
            progress["event"] = event
            if event == "progress":
                value = data.get("value")
                maximum = data.get("max")
                progress.update(
                    status="running",
                    value=int(value) if isinstance(value, (int, float)) else None,
                    max=int(maximum) if isinstance(maximum, (int, float)) else None,
                    node=str(data.get("node")) if data.get("node") is not None else None,
                )
            elif event == "execution_start":
                progress.update(status="starting", value=None, max=None, node=None)
            elif event == "executing":
                node = data.get("node")
                if node is None:
                    progress.update(status="complete", value=None, max=None, node=None)
                    self.progress = progress
                    return True
                progress.update(status="running", value=None, max=None, node=str(node))
            elif event == "execution_cached":
                progress.update(status="running", value=None, max=None, node="cached")
            elif event == "execution_error":
                progress.update(status="error", value=None, max=None, node=data.get("node_id"))
                self.progress = progress
                return True
            else:
                return False
            self.progress = progress
        return False

    def _wait_backend_ready(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        last_error = "not attempted"
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("B580 ComfyUI exited during startup")
            try:
                with _urlopen("GET", f"{BACKEND_URL}/system_stats", timeout=5) as response:
                    body = json.loads(response.read())
                devices = body.get("devices") or []
                if not any(
                    item.get("type") == "xpu" and "B580" in str(item.get("name", ""))
                    for item in devices
                    if isinstance(item, dict)
                ):
                    raise RuntimeError(f"B580 XPU was not reported by ComfyUI: {devices!r}")
                log.info("B580 ComfyUI ready on backend port %d", BACKEND_PORT)
                return
            except (OSError, ValueError, RuntimeError, HTTPError, URLError) as exc:
                last_error = str(exc)
                time.sleep(0.5)
        raise TimeoutError(f"B580 ComfyUI did not become ready: {last_error}")

    def _require_encoder_ready(self) -> None:
        try:
            with _urlopen("GET", ENCODER_HEALTH_URL, timeout=5) as response:
                health = json.loads(response.read())
            if health.get("status") != "ready" or health.get("device") != "cuda:0":
                raise RuntimeError(f"encoder is not the warmed GTX 1070 CUDA service: {health!r}")
        except (OSError, ValueError, HTTPError, URLError) as exc:
            raise RuntimeError(f"1070 Qwen encoder is unavailable: {exc}") from exc

    def _assert_backend_port_free(self) -> None:
        """Confirm no stale ComfyUI/backend process is claiming the B580 port."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", BACKEND_PORT))
        except OSError as exc:
            raise RuntimeError(
                f"B580 backend port {BACKEND_PORT} is not free; refusing concurrent ComfyUI"
            ) from exc
        finally:
            probe.close()
        log.info("B580 backend port %d is free", BACKEND_PORT)

    def _start_locked(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        startup_started = time.monotonic()
        self._require_encoder_ready()
        log.info("GTX 1070 Qwen encoder is healthy and ready")
        if _ornith_active():
            log.info("Stopping Ornith before claiming B580")
            _run_systemctl("stop")
        deadline = time.monotonic() + 45
        while _ornith_active() and time.monotonic() < deadline:
            time.sleep(0.25)
        if _ornith_active():
            raise RuntimeError("Ornith did not stop before the B580 claim")
        log.info("Ornith stopped; verifying B580 backend is free")
        self._assert_backend_port_free()

        BASE_DIR.mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "output").mkdir(exist_ok=True)
        (BASE_DIR / "temp").mkdir(exist_ok=True)
        (BASE_DIR / "user").mkdir(exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        child_env = os.environ.copy()
        child_env.update(
            {
                "ONEAPI_DEVICE_SELECTOR": "level_zero:gpu",
                "TRITON_DEFAULT_BACKEND": "intel",
                "PYTHONPATH": str(COMFYUI_ROOT),
            }
        )
        command = [
            str(PYTHON),
            str(COMFYUI_ROOT / "main.py"),
            "--listen",
            "127.0.0.1",
            "--port",
            str(BACKEND_PORT),
            "--disable-auto-launch",
            "--base-directory",
            str(BASE_DIR),
            "--output-directory",
            str(BASE_DIR / "output"),
            "--temp-directory",
            str(BASE_DIR / "temp"),
            "--user-directory",
            str(BASE_DIR / "user"),
            "--disable-triton-backend",
            "--disable-manager-ui",
            "--disable-xformers",
            "--log-stdout",
        ]
        self.child_log = COMFY_LOG.open("ab")
        self.child_log.write(
            (f"\n--- B580 request start monotonic_ns={time.monotonic_ns()} ---\n"
             f"command={' '.join(command)}\n"
             "ONEAPI_DEVICE_SELECTOR=level_zero:gpu\n"
             "TRITON_DEFAULT_BACKEND=intel\n"
             "REMOTE_QWEN_TOKEN=<inherited, redacted>\n").encode()
        )
        self.child_log.flush()
        self.process = subprocess.Popen(
            command,
            cwd=str(COMFYUI_ROOT),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=self.child_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.started_mono_ns = time.monotonic_ns()
        self.active_prompt_id = None
        self.last_activity = time.monotonic()
        try:
            self._wait_backend_ready()
            log.info("B580 ComfyUI startup completed in %.3f seconds", time.monotonic() - startup_started)
        except Exception:
            self._stop_comfy_locked()
            self._restore_ornith_locked()
            raise

    def _stop_comfy_locked(self) -> None:
        self._stop_progress_watcher_locked()
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            log.info("Stopping B580 ComfyUI pid=%d", process.pid)
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if self.child_log is not None:
            self.child_log.close()
            self.child_log = None

    def _start_progress_watcher_locked(self, client_id: str | None) -> None:
        self._stop_progress_watcher_locked()
        if not client_id:
            log.warning("No ComfyUI client_id supplied; live sampler progress is unavailable")
            return
        watcher = _ProgressWatcher(client_id, self._record_progress)
        self.progress_watcher = watcher
        watcher.start()

    def _stop_progress_watcher_locked(self) -> None:
        watcher = self.progress_watcher
        self.progress_watcher = None
        if watcher is not None:
            watcher.stop()

    def _restore_ornith_locked(self) -> None:
        if _ornith_active():
            return
        restore_started = time.monotonic()
        log.info("Restoring Ornith after B580 image request")
        _run_systemctl("start")
        deadline = time.monotonic() + 90
        last_error = "not ready"
        while time.monotonic() < deadline:
            if _ornith_active():
                try:
                    with _urlopen("GET", "http://127.0.0.1:8081/health", timeout=5) as response:
                        health = json.loads(response.read())
                    if health.get("status") == "ok":
                        log.info("Ornith restored and healthy in %.3f seconds", time.monotonic() - restore_started)
                        return
                except (OSError, ValueError, HTTPError, URLError) as exc:
                    last_error = str(exc)
            time.sleep(0.5)
        raise TimeoutError(f"Ornith did not become healthy after image request: {last_error}")

    def _teardown_locked(self, reason: str) -> None:
        if self.stopping:
            return
        self.stopping = True
        try:
            elapsed = None
            if self.request_started_mono is not None:
                elapsed = time.monotonic() - self.request_started_mono
            log.info(
                "Tearing down B580 runtime: reason=%s request_elapsed_seconds=%s",
                reason,
                f"{elapsed:.3f}" if elapsed is not None else "unknown",
            )
            self._cleanup_uploaded_image_locked()
            self._stop_comfy_locked()
            self._restore_ornith_locked()
        finally:
            self.active_prompt_id = None
            self.started_mono_ns = None
            self.request_started_mono = None
            self.progress = {}
            self.last_activity = time.monotonic()
            self.stopping = False

    def _cleanup_uploaded_image_locked(self) -> None:
        uploaded = self.uploaded_image
        self.uploaded_image = None
        if not uploaded:
            return

        asset_id = uploaded.get("asset_id")
        if asset_id and self.process is not None and self.process.poll() is None:
            status, _, _ = self._proxy("DELETE", f"/api/assets/{asset_id}")
            if status >= 400 and status != HTTPStatus.NOT_FOUND:
                log.warning("Unable to remove temporary Qwen edit asset reference (HTTP %d)", status)

        input_root = (BASE_DIR / "input").resolve()
        candidate = (input_root / uploaded.get("subfolder", "") / uploaded["name"]).resolve()
        if input_root not in candidate.parents:
            log.warning("Refusing to remove Qwen edit input outside ComfyUI input directory")
            return
        try:
            candidate.unlink(missing_ok=True)
            log.info("Removed temporary Qwen edit input after generation")
        except OSError:
            log.warning("Could not remove temporary Qwen edit input", exc_info=True)

    def _proxy(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ):
        try:
            with _urlopen(
                method,
                f"{BACKEND_URL}{path}",
                body=body,
                content_type=content_type,
            ) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def upload_image(self, body: bytes, content_type: str):
        with self.lock:
            if self.active_prompt_id is not None:
                return HTTPStatus.SERVICE_UNAVAILABLE, {}, b'{"error":"image runtime is busy"}'
            if not content_type.lower().startswith("multipart/form-data;"):
                return HTTPStatus.BAD_REQUEST, {}, b'{"error":"multipart image upload required"}'
            if self.request_started_mono is None:
                self.request_started_mono = time.monotonic()
            try:
                self._start_locked()
                status, headers, response_body = self._proxy(
                    "POST", "/upload/image", body, content_type=content_type
                )
                if status >= 400:
                    self._teardown_locked(f"backend image upload HTTP {status}")
                    return status, headers, response_body
                try:
                    upload_result = json.loads(response_body)
                    name = upload_result.get("name")
                    subfolder = upload_result.get("subfolder", "")
                    if not isinstance(name, str) or Path(name).name != name:
                        raise ValueError("ComfyUI returned an invalid uploaded image name")
                    if not isinstance(subfolder, str) or Path(subfolder).is_absolute() or ".." in Path(subfolder).parts:
                        raise ValueError("ComfyUI returned an invalid uploaded image subfolder")
                    asset = upload_result.get("asset")
                    asset_id = asset.get("id") if isinstance(asset, dict) else None
                    self.uploaded_image = {
                        "name": name,
                        "subfolder": subfolder,
                        "asset_id": str(asset_id) if asset_id else "",
                    }
                except (TypeError, ValueError) as exc:
                    self._teardown_locked(f"invalid image upload response: {exc}")
                    return HTTPStatus.BAD_GATEWAY, {}, json.dumps({"error": str(exc)}).encode()
                self.last_activity = time.monotonic()
                log.info("Accepted ComfyUI source-image upload (%d bytes)", len(body))
                return status, headers, response_body
            except Exception as exc:
                log.exception("Unable to start ComfyUI or upload source image")
                try:
                    self._teardown_locked(f"image upload exception: {exc}")
                except Exception:
                    log.exception("Failed while restoring Ornith after image upload failure")
                return HTTPStatus.BAD_GATEWAY, {}, json.dumps({"error": str(exc)}).encode()

    def prompt(self, body: bytes):
        with self.lock:
            if self.active_prompt_id is not None:
                return HTTPStatus.SERVICE_UNAVAILABLE, {}, b'{"error":"image runtime is busy"}'
            if self.process is not None and self.process.poll() is not None:
                self._teardown_locked("B580 ComfyUI exited between batch items")
            had_active_process = self.process is not None and self.process.poll() is None
            if self.request_started_mono is None:
                self.request_started_mono = time.monotonic()
            try:
                self._start_locked()
                try:
                    request_payload = json.loads(body)
                except (TypeError, ValueError):
                    request_payload = {}
                self._start_progress_watcher_locked(request_payload.get("client_id"))
                if had_active_process:
                    log.info("Reusing active B580 ComfyUI for the next batch item")
                status, headers, response_body = self._proxy("POST", "/prompt", body)
                if status >= 400:
                    self._teardown_locked(f"backend prompt HTTP {status}")
                    return status, headers, response_body
                payload = json.loads(response_body)
                prompt_id = payload.get("prompt_id")
                if not prompt_id:
                    self._teardown_locked("backend returned no prompt_id")
                    return HTTPStatus.BAD_GATEWAY, {}, b'{"error":"backend returned no prompt_id"}'
                self.active_prompt_id = str(prompt_id)
                self.progress = {
                    "prompt_id": self.active_prompt_id,
                    "status": "queued",
                    "event": "submitted",
                    "value": None,
                    "max": None,
                    "node": None,
                }
                self.last_activity = time.monotonic()
                log.info(
                    "Accepted ComfyUI prompt_id=%s after %.3f seconds",
                    self.active_prompt_id,
                    time.monotonic() - self.request_started_mono,
                )
                return status, headers, response_body
            except Exception as exc:
                log.exception("Unable to start or submit B580 image request")
                try:
                    self._teardown_locked(f"prompt exception: {exc}")
                except Exception:
                    log.exception("Failed while restoring Ornith after prompt failure")
                return HTTPStatus.BAD_GATEWAY, {}, json.dumps({"error": str(exc)}).encode()

    def history(self, prompt_id: str, path: str):
        with self.lock:
            if prompt_id != self.active_prompt_id or self.process is None:
                return HTTPStatus.NOT_FOUND, {}, b"{}"
            self.last_activity = time.monotonic()
            if self.process.poll() is not None:
                self._teardown_locked("B580 ComfyUI exited during generation")
                return HTTPStatus.BAD_GATEWAY, {}, b'{"error":"B580 ComfyUI exited"}'
            status, headers, body = self._proxy("GET", path)
            if status == 200:
                try:
                    payload = json.loads(body)
                    history = payload.get(prompt_id) if isinstance(payload, dict) else None
                    if isinstance(history, dict) and history.get("outputs"):
                        self.progress.update(status="complete", value=None, max=None, node=None)
                except (TypeError, ValueError):
                    pass
            return status, headers, body

    def view(self, prompt_id: str | None, path: str, keep_alive: bool = False):
        with self.lock:
            if self.active_prompt_id is None or self.process is None:
                return HTTPStatus.NOT_FOUND, {}, b"{}", False
            if prompt_id and prompt_id != self.active_prompt_id:
                return HTTPStatus.NOT_FOUND, {}, b"{}", False
            self.last_activity = time.monotonic()
            status, headers, body = self._proxy("GET", path)
            if status < 400:
                # The image is fully buffered before returning. The handler
                # sends those bytes first, then calls finish_view() so model
                # reload for Ornith is not added to user-visible image time.
                return status, headers, body, keep_alive
            elif status >= 500:
                self._teardown_locked(f"backend view HTTP {status}")
            return status, headers, body, False

    def finish_view(self, keep_alive: bool = False) -> None:
        with self.lock:
            self._cleanup_uploaded_image_locked()
            if keep_alive:
                log.info("Keeping B580 ComfyUI active for the next batch item")
                self.active_prompt_id = None
                self.last_activity = time.monotonic()
                return
            if self.process is not None:
                self._teardown_locked("image downloaded")

    def release(self, reason: str = "client release") -> None:
        with self.lock:
            if self.process is not None:
                self._teardown_locked(reason)

    def expire_idle(self) -> None:
        with self.lock:
            if self.process is not None and time.monotonic() - self.last_activity > IDLE_TIMEOUT_SECONDS:
                self._teardown_locked("request idle timeout")

    def shutdown(self) -> None:
        with self.lock:
            self._teardown_locked("broker shutdown") if self.process is not None else None


RUNTIME = Runtime()


class Handler(BaseHTTPRequestHandler):
    server_version = "EmeryChatQwenB580Runtime/1.0"

    def _send(self, status: int, headers: dict[str, Any], body: bytes) -> None:
        self.send_response(status)
        content_type = headers.get("Content-Type") or headers.get("content-type") or "application/json"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if content_type.startswith("image/"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # Health probes may time out while teardown holds the lifecycle
            # lock; a disconnected probe is not a runtime failure.
            log.debug("Client disconnected before response was written")

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            self._send(HTTPStatus.OK, {}, json.dumps(RUNTIME.status(), separators=(",", ":")).encode())
            return
        if path == "/system_stats":
            with RUNTIME.lock:
                if RUNTIME.process is None:
                    self._send(HTTPStatus.OK, {}, json.dumps(RUNTIME.status()).encode())
                    return
                status, headers, body = RUNTIME._proxy("GET", path)
            self._send(status, headers, body)
            return
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            status, headers, body = RUNTIME.history(prompt_id, path)
            self._send(status, headers, body)
            return
        if path.startswith("/progress/"):
            prompt_id = path.rsplit("/", 1)[-1]
            self._send(HTTPStatus.OK, {}, json.dumps(RUNTIME.progress_status(prompt_id), separators=(",", ":")).encode())
            return
        if path == "/view":
            query = parse_qs(parsed.query)
            prompt_id = query.get("prompt_id", [None])[0]
            keep_alive = query.get("keep_alive", ["0"])[0].lower() in {"1", "true", "yes"}
            status, headers, body, finish = RUNTIME.view(prompt_id, self.path, keep_alive=keep_alive)
            try:
                self._send(status, headers, body)
            finally:
                if status < 400:
                    RUNTIME.finish_view(keep_alive=finish)
            return
        self._send(HTTPStatus.NOT_FOUND, {}, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/release":
            RUNTIME.release()
            self._send(HTTPStatus.OK, {}, b'{"status":"released"}')
            return
        if path == "/upload/image":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 20 * 1024 * 1024:
                    raise ValueError("invalid image upload length")
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                status, headers, response_body = RUNTIME.upload_image(body, content_type)
            except Exception as exc:
                log.exception("Invalid source-image upload request")
                status, headers, response_body = HTTPStatus.BAD_REQUEST, {}, json.dumps({"error": str(exc)}).encode()
            self._send(status, headers, response_body)
            return
        if path != "/prompt":
            self._send(HTTPStatus.NOT_FOUND, {}, b'{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8 * 1024 * 1024:
                raise ValueError("invalid request body length")
            body = self.rfile.read(length)
            status, headers, response_body = RUNTIME.prompt(body)
        except Exception as exc:
            log.exception("Invalid prompt request")
            status, headers, response_body = HTTPStatus.BAD_REQUEST, {}, json.dumps({"error": str(exc)}).encode()
        self._send(status, headers, response_body)

    def log_message(self, format, *args):  # noqa: A002
        log.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Starting Qwen B580 runtime broker on 0.0.0.0:%d; backend 127.0.0.1:%d", PORT, BACKEND_PORT)
    log.info("Modern Python=%s; ComfyUI=%s; base=%s", PYTHON, COMFYUI_ROOT, BASE_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    stop_event = threading.Event()

    def reaper() -> None:
        while not stop_event.wait(5):
            try:
                RUNTIME.expire_idle()
            except Exception:
                log.exception("Runtime idle reaper failed")

    reaper_thread = threading.Thread(target=reaper, name="qwen-runtime-reaper", daemon=True)
    reaper_thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        server.server_close()
        RUNTIME.shutdown()


if __name__ == "__main__":
    main()
