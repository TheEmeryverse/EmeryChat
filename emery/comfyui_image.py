"""HTTP client for ComfyUI API-format image workflows.

The workflow is supplied by configuration so model-specific sampler/VAE
settings stay on the Mac. For the distributed Qwen setup it must contain the
RemoteQwenImage21TextEncode node.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from emery.config import (
    COMFYUI_AUTH_TOKEN,
    COMFYUI_POLL_INTERVAL_SECONDS,
    COMFYUI_TIMEOUT_SECONDS,
    COMFYUI_URL,
    COMFYUI_WORKFLOW_PATH,
    QWEN_ENCODER_URL,
)
import emery.globals as globals
from emery.image_profiles import DEFAULT_IMAGE_PROFILE, get_image_profile


REMOTE_ENCODER_NODE = "RemoteQwenImage21TextEncode"
log = logging.getLogger(__name__)


def _endpoint_label(url: str) -> str:
    """Return a log-safe endpoint label without query strings or credentials."""
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname or '<unknown>'}:{parsed.port or ''}{parsed.path}"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if COMFYUI_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {COMFYUI_AUTH_TOKEN}"
    return headers


def _load_workflow() -> dict[str, dict[str, Any]]:
    if not COMFYUI_WORKFLOW_PATH:
        raise RuntimeError("COMFYUI_WORKFLOW_PATH is not configured")

    path = Path(COMFYUI_WORKFLOW_PATH).expanduser()
    try:
        with path.open("r", encoding="utf-8") as handle:
            workflow = json.load(handle)
    except OSError as exc:
        raise RuntimeError(f"Unable to read ComfyUI workflow {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ComfyUI workflow is not valid JSON: {path}: {exc}") from exc

    if not isinstance(workflow, dict) or not workflow:
        raise RuntimeError("ComfyUI workflow must be a non-empty API prompt object")
    if "prompt" in workflow and isinstance(workflow["prompt"], dict):
        workflow = workflow["prompt"]

    for node_id, node in workflow.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise RuntimeError(
                f"ComfyUI workflow node {node_id!r} is not in API prompt format"
            )
    return copy.deepcopy(workflow)


def _prepare_workflow(
    prompt: str,
    seed: int | None = None,
    quality_profile: str = DEFAULT_IMAGE_PROFILE,
    orientation: str | None = None,
) -> dict[str, dict[str, Any]]:
    workflow = _load_workflow()
    profile = get_image_profile(quality_profile, orientation=orientation)
    remote_nodes = []
    generated_seed = seed if seed is not None else random.SystemRandom().randrange(2**63)

    for node in workflow.values():
        class_type = node.get("class_type")
        inputs = node["inputs"]
        if class_type == REMOTE_ENCODER_NODE:
            remote_nodes.append(inputs)
            inputs["prompt"] = prompt
            inputs.setdefault("negative_prompt", "")
            if QWEN_ENCODER_URL:
                inputs["server_url"] = QWEN_ENCODER_URL
        if class_type == "EmptySD3LatentImage":
            inputs["width"] = profile.width
            inputs["height"] = profile.height
        if class_type == "KSampler":
            inputs["steps"] = profile.steps
        if "filename_prefix" in inputs:
            inputs["filename_prefix"] = "EmeryChat"
        if "seed" in inputs:
            inputs["seed"] = generated_seed
        if "noise_seed" in inputs:
            inputs["noise_seed"] = generated_seed

    if not remote_nodes:
        raise RuntimeError(
            "ComfyUI workflow must contain RemoteQwenImage21TextEncode; "
            "a local Qwen text encoder would violate the distributed setup"
        )
    return workflow


async def _json_response(response, label: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"ComfyUI {label} HTTP {response.status_code}: {response.text[:1000]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"ComfyUI {label} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"ComfyUI {label} returned an unexpected response")
    return data


async def _release_runtime(base_url: str) -> bool:
    """Release a request-scoped broker after a failed multi-image request."""
    try:
        response = await globals.http_client.post(
            f"{base_url}/release",
            headers=_headers(),
            # Broker release includes ComfyUI shutdown and Ornith model
            # reload/warmup, which can take longer than a normal API call.
            timeout=180,
        )
        if response.status_code >= 400:
            log.debug("IMAGE: runtime release returned HTTP %s", response.status_code)
            return False
        return True
    except Exception:
        # A resident ComfyUI endpoint will not have the broker-only route.
        log.debug("IMAGE: runtime release was unavailable", exc_info=True)
        return False


async def release_comfyui_runtime() -> bool:
    """Stop a request-scoped broker after a queue drains."""
    return await _release_runtime(COMFYUI_URL.rstrip("/"))


async def generate_comfyui_images(
    prompt: str,
    seed: int | None = None,
    batch_size: int = 1,
    on_image=None,
    on_progress=None,
    keep_runtime_alive: bool = False,
    quality_profile: str = DEFAULT_IMAGE_PROFILE,
    orientation: str | None = None,
) -> list[tuple[bytes, str]]:
    """Generate several images in one broker lifecycle and return all images.

    ``on_image`` is awaited immediately after each image is downloaded, before
    the next batch item is submitted. ``on_progress`` receives the broker's
    live ComfyUI execution event state while each item is running.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    profile = get_image_profile(quality_profile, orientation=orientation)

    request_started = time.monotonic()
    client_id = uuid.uuid4().hex
    base_url = COMFYUI_URL.rstrip("/")
    endpoint = _endpoint_label(base_url)
    log.info(
        "IMAGE: starting Qwen batch endpoint=%s profile=%s size=%dx%d steps=%d batch_size=%d seed=%s",
        endpoint,
        profile.name,
        profile.width,
        profile.height,
        profile.steps,
        batch_size,
        seed,
    )
    prompt_id = None
    images: list[tuple[bytes, str]] = []
    try:
        for image_index in range(batch_size):
            current_seed = None if seed is None else seed + image_index
            workflow = _prepare_workflow(
                prompt,
                seed=current_seed,
                quality_profile=profile.name,
                orientation=orientation,
            )
            prompt_id = None
            log.info(
                "IMAGE: submitting Qwen batch item=%d/%d seed=%s",
                image_index + 1,
                batch_size,
                current_seed,
            )
            response = await globals.http_client.post(
                f"{base_url}/prompt",
                headers=_headers(),
                json={"prompt": workflow, "client_id": client_id},
                timeout=30,
            )
            submitted = await _json_response(response, "prompt submission")
            prompt_id = submitted.get("prompt_id")
            if not prompt_id:
                errors = submitted.get("node_errors") or submitted.get("error")
                raise RuntimeError(f"ComfyUI did not return prompt_id: {errors or submitted}")
            log.info(
                "IMAGE: Qwen workflow accepted prompt_id=%s item=%d/%d elapsed_seconds=%.3f",
                prompt_id,
                image_index + 1,
                batch_size,
                time.monotonic() - request_started,
            )

            deadline = time.monotonic() + COMFYUI_TIMEOUT_SECONDS
            history: dict[str, Any] | None = None
            history_poll_errors = 0
            while time.monotonic() < deadline:
                if on_progress is not None:
                    try:
                        progress_response = await globals.http_client.get(
                            f"{base_url}/progress/{prompt_id}",
                            headers=_headers(),
                            timeout=10,
                        )
                        if progress_response.status_code == 200:
                            progress = progress_response.json()
                            if isinstance(progress, dict):
                                progress["item_index"] = image_index + 1
                                progress["batch_size"] = batch_size
                                await on_progress(progress)
                    except Exception:
                        # Progress is helpful but must never abort an image.
                        log.debug("IMAGE: live ComfyUI progress poll failed", exc_info=True)
                try:
                    response = await globals.http_client.get(
                        f"{base_url}/history/{prompt_id}",
                        headers=_headers(),
                        timeout=30,
                    )
                except Exception as exc:
                    # A busy XPU backend can occasionally delay one HTTP
                    # response while the sampler continues. Do not tear down
                    # the live pipeline on a single transient read timeout.
                    history_poll_errors += 1
                    if history_poll_errors <= 3 or history_poll_errors % 10 == 0:
                        log.warning(
                            "IMAGE: history poll delayed prompt_id=%s attempt=%d error=%s",
                            prompt_id,
                            history_poll_errors,
                            type(exc).__name__,
                        )
                    await asyncio.sleep(max(1.0, COMFYUI_POLL_INTERVAL_SECONDS))
                    continue
                if response.status_code == 200:
                    data = await _json_response(response, "history")
                    candidate = data.get(str(prompt_id))
                    if isinstance(candidate, dict):
                        history = candidate
                        status = candidate.get("status") or {}
                        if status.get("status_str") == "error":
                            raise RuntimeError(f"ComfyUI workflow failed: {status}")
                        if status.get("completed") or candidate.get("outputs"):
                            break
                await asyncio.sleep(COMFYUI_POLL_INTERVAL_SECONDS)

            if not history:
                raise TimeoutError(f"ComfyUI did not finish prompt {prompt_id} before timeout")

            image = next(
                (
                    candidate
                    for output in (history.get("outputs") or {}).values()
                    for candidate in (output or {}).get("images", [])
                    if isinstance(candidate, dict) and candidate.get("filename")
                ),
                None,
            )
            if image is None:
                raise RuntimeError(f"ComfyUI completed prompt {prompt_id} without an image output")

            response = await globals.http_client.get(
                f"{base_url}/view",
                params={
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                    "keep_alive": "1" if image_index + 1 < batch_size or keep_runtime_alive else "0",
                },
                headers=_headers(),
                timeout=60,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"ComfyUI image download HTTP {response.status_code}: {response.text[:500]}"
                )
            mime_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
            image_bytes = bytes(response.content)
            images.append((image_bytes, mime_type))
            if on_image is not None:
                await on_image(image_bytes, mime_type, image_index + 1, batch_size)
            log.info(
                "IMAGE: Qwen batch item downloaded prompt_id=%s item=%d/%d bytes=%d mime=%s elapsed_seconds=%.3f",
                prompt_id,
                image_index + 1,
                batch_size,
                len(image_bytes),
                mime_type,
                time.monotonic() - request_started,
            )

        log.info(
            "IMAGE: Qwen batch complete count=%d total_seconds=%.3f",
            len(images),
            time.monotonic() - request_started,
        )
        return images
    except Exception:
        await _release_runtime(base_url)
        log.exception(
            "IMAGE: Qwen batch failed endpoint=%s prompt_id=%s completed=%d/%d elapsed_seconds=%.3f",
            endpoint,
            prompt_id or "unknown",
            len(images),
            batch_size,
            time.monotonic() - request_started,
        )
        raise


async def generate_comfyui_image(prompt: str, seed: int | None = None) -> tuple[bytes, str]:
    """Run one configured workflow and return the generated image."""
    return (await generate_comfyui_images(prompt, seed=seed, batch_size=1))[0]
