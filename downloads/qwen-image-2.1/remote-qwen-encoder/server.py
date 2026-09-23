#!/usr/bin/env python3
"""Qwen Image 2.1 conditioning service.

This deliberately uses ComfyUI's Qwen Image 2.1 encoder implementation rather
than Transformers.  The response payload is a torch.save archive encoded as
base64; the Mac-side ComfyUI node must be treated as the only trusted client.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
COMFYUI_ROOT = Path(os.environ.get("COMFYUI_ROOT", ROOT / "../software/ComfyUI")).resolve()
ENCODER_PATH = Path(
    os.environ.get(
        "QWEN_ENCODER_PATH",
        ROOT / "../model/text_encoders/qwen3vl_8b_int8_convrot.safetensors",
    )
).resolve()
HOST = os.environ.get("REMOTE_QWEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("REMOTE_QWEN_PORT", "8086"))
AUTH_TOKEN = os.environ.get("REMOTE_QWEN_TOKEN", "")
ENCODER_DEVICE = os.environ.get("REMOTE_QWEN_DEVICE", "cpu").strip() or "cpu"
MODEL_NAME = os.environ.get(
    "REMOTE_QWEN_MODEL_NAME",
    "qwen-image-2.1-qwen3vl-8b-int8-convrot",
)
DEFAULT_NEGATIVE_PROMPT = (
    "low resolution, low quality, blurry, out of focus, distorted anatomy, malformed anatomy, "
    "unnatural proportions, deformed hands, extra fingers, missing fingers, fused fingers, "
    "extra limbs, phantom limbs, duplicated body parts, asymmetrical eyes, malformed facial "
    "features, waxy skin, plastic skin, over-smoothed skin, airbrushed skin, oversaturated "
    "colors, harsh HDR, unnatural lighting, artificial AI look, watermark, logo, text artifacts"
)

logging.basicConfig(
    level=os.environ.get("REMOTE_QWEN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("remote-qwen-encoder")


def load_comfy_clip():
    """Load the encoder through ComfyUI on the explicitly selected device."""
    if not COMFYUI_ROOT.is_dir():
        raise RuntimeError(f"COMFYUI_ROOT does not exist: {COMFYUI_ROOT}")
    if not ENCODER_PATH.is_file():
        raise RuntimeError(f"QWEN_ENCODER_PATH does not exist: {ENCODER_PATH}")

    # ComfyUI parses its device flags during import. Do not inherit service
    # arguments or accidentally let it select the B580. For CUDA, gpu-only is
    # intentional: without it ComfyUI may silently offload this model to CPU.
    sys.path.insert(0, str(COMFYUI_ROOT))
    if ENCODER_DEVICE == "cpu":
        sys.argv = [sys.argv[0], "--cpu"]
        loader_device = "cpu"
    elif ENCODER_DEVICE.startswith("cuda:"):
        cuda_index = ENCODER_DEVICE.split(":", 1)[1]
        sys.argv = [sys.argv[0], "--cuda-device", cuda_index, "--gpu-only"]
        loader_device = "default"
    else:
        raise RuntimeError(
            f"Unsupported REMOTE_QWEN_DEVICE {ENCODER_DEVICE!r}; use cpu or cuda:N"
        )

    import torch

    threads = os.environ.get("REMOTE_QWEN_CPU_THREADS")
    if threads:
        torch.set_num_threads(int(threads))

    # main.py enables CLI parsing before importing folder_paths/nodes.  Mirror
    # that ordering so the forced --cpu flag is actually honored.
    import comfy.options
    comfy.options.enable_args_parsing()
    import folder_paths
    import nodes

    folder_paths.add_model_folder_path("text_encoders", str(ENCODER_PATH.parent))
    log.info("Loading Qwen encoder on %s from %s", ENCODER_DEVICE, ENCODER_PATH)
    clip = nodes.CLIPLoader().load_clip(
        ENCODER_PATH.name,
        type="qwen_image",
        device=loader_device,
    )[0]
    log.info("Qwen encoder loaded; device is %s and process will keep it warm", ENCODER_DEVICE)
    return clip


def encode_conditioning(clip, prompt: str, image=None, keep_vision: bool = True):
    tokens = clip.tokenize(
        prompt,
        images=[] if image is None else [image],
        keep_vision=keep_vision,
        prevent_empty_text=True,
    )
    return clip.encode_from_tokens_scheduled(tokens, show_pbar=False)


def decode_reference_image(encoded: str):
    import numpy as np
    import torch
    from PIL import Image

    raw = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        rgb = image.convert("RGB")
        array = np.asarray(rgb, dtype=np.float32).copy() / 255.0
    return torch.from_numpy(array).unsqueeze(0)


def pack_payload(positive: Any, negative: Any, schema: int = 1) -> str:
    import torch

    buffer = io.BytesIO()
    torch.save(
        {
            "schema": schema,
            "model": MODEL_NAME,
            "positive": positive,
            "negative": negative,
        },
        buffer,
    )
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    server_version = "RemoteQwenEncoder/0.1"

    def _json(self, status: int, value: dict[str, Any]):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not AUTH_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {AUTH_TOKEN}"

    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._json(
            HTTPStatus.OK,
            {
                "status": "ready",
                "schema": 2,
                "model": MODEL_NAME,
                "device": ENCODER_DEVICE,
                "encoder_path": str(ENCODER_PATH),
            },
        )

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/qwen-image-2.1/encode":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bearer token"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            schema = request.get("schema", 1)
            if schema not in {1, 2}:
                raise ValueError("unsupported schema")
            prompt = request.get("prompt")
            negative_prompt = request.get("negative_prompt", "")
            if not isinstance(prompt, str) or not isinstance(negative_prompt, str):
                raise ValueError("prompt and negative_prompt must be strings")
            reference_image = None
            keep_vision = True
            if schema == 2:
                encoded_image = request.get("input_image")
                if not isinstance(encoded_image, str) or not encoded_image:
                    raise ValueError("schema 2 requires an input_image base64 value")
                reference_image = decode_reference_image(encoded_image)
                keep_vision = bool(request.get("keep_vision", False))

            with self.server.encode_lock:
                positive = encode_conditioning(
                    self.server.clip, prompt, reference_image, keep_vision
                )
                if reference_image is None:
                    # Reuse identical text-only negative prompts across normal
                    # generations. Edit negatives must include the input image.
                    negative = self.server.get_negative_conditioning(negative_prompt)
                else:
                    negative = encode_conditioning(
                        self.server.clip, negative_prompt, reference_image, keep_vision
                    )
            payload = pack_payload(positive, negative, schema=schema)
            self._json(
                HTTPStatus.OK,
                {"schema": schema, "model": MODEL_NAME, "encoding": "torch.save/base64", "payload": payload},
            )
        except NotImplementedError as exc:
            self._json(HTTPStatus.NOT_IMPLEMENTED, {"error": str(exc)})
        except Exception as exc:  # service must return a useful error to the Mac node
            log.exception("Encoding request failed")
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, format, *args):  # noqa: A002
        log.info("%s - %s", self.address_string(), format % args)


class Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, clip):
        super().__init__(address, Handler)
        self.clip = clip
        self.encode_lock = threading.Lock()
        self.negative_cache = {}

    def get_negative_conditioning(self, negative_prompt: str):
        cache_key = (MODEL_NAME, negative_prompt)
        negative = self.negative_cache.get(cache_key)
        if negative is None:
            negative = encode_conditioning(self.clip, negative_prompt)
            self.negative_cache[cache_key] = negative
            log.info("Negative conditioning cache miss; stored prompt")
        else:
            log.info("Negative conditioning cache hit")
        return negative


def main():
    if not AUTH_TOKEN:
        log.warning("REMOTE_QWEN_TOKEN is unset; service has no request authentication")
    clip = load_comfy_clip()
    server = Server((HOST, PORT), clip)
    if os.environ.get("REMOTE_QWEN_PREWARM_NEGATIVE", "1").lower() not in {"0", "false", "no", "off"}:
        started = time.monotonic()
        server.get_negative_conditioning(DEFAULT_NEGATIVE_PROMPT)
        log.info("Prewarmed default negative conditioning in %.3f seconds", time.monotonic() - started)
    log.info("Listening on http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
