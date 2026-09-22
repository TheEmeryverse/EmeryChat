"""HTTP client for ComfyUI API-format image workflows.

The workflow is supplied by configuration so model-specific sampler/VAE
settings stay on the Mac. For the distributed Qwen setup it must contain the
RemoteQwenImage21TextEncode node.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from emery.config import (
    COMFYUI_AUTH_TOKEN,
    COMFYUI_POLL_INTERVAL_SECONDS,
    COMFYUI_TIMEOUT_SECONDS,
    COMFYUI_URL,
    COMFYUI_WORKFLOW_PATH,
    QWEN_ENCODER_URL,
)
import emery.globals as globals


REMOTE_ENCODER_NODE = "RemoteQwenImage21TextEncode"


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


def _prepare_workflow(prompt: str, seed: int | None = None) -> dict[str, dict[str, Any]]:
    workflow = _load_workflow()
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


async def generate_comfyui_image(prompt: str, seed: int | None = None) -> tuple[bytes, str]:
    """Run the configured workflow and return the first generated image."""
    workflow = _prepare_workflow(prompt, seed=seed)
    client_id = uuid.uuid4().hex
    base_url = COMFYUI_URL.rstrip("/")
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

    deadline = time.monotonic() + COMFYUI_TIMEOUT_SECONDS
    history: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = await globals.http_client.get(
            f"{base_url}/history/{prompt_id}",
            headers=_headers(),
            timeout=15,
        )
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

    for output in (history.get("outputs") or {}).values():
        for image in (output or {}).get("images", []):
            if not isinstance(image, dict) or not image.get("filename"):
                continue
            response = await globals.http_client.get(
                f"{base_url}/view",
                params={
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                },
                headers=_headers(),
                timeout=60,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"ComfyUI image download HTTP {response.status_code}: {response.text[:500]}"
                )
            mime_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
            return bytes(response.content), mime_type

    raise RuntimeError(f"ComfyUI completed prompt {prompt_id} without an image output")
