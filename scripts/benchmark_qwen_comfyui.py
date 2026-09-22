#!/usr/bin/env python3
"""Run one deterministic Qwen Image ComfyUI API benchmark.

This intentionally changes only the in-memory API prompt. It does not edit the
configured workflow or restart ComfyUI.
"""

from __future__ import annotations

import argparse
import copy
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


DEFAULT_PROMPT = (
    "A realistic red apple on a weathered wooden table beside a window, "
    "soft natural daylight, accurate materials and natural shadows, "
    "documentary food photography."
)


def request_json(url: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1000]}") from exc


def load_workflow(path: Path) -> dict:
    workflow = json.loads(path.read_text())
    if "prompt" in workflow and isinstance(workflow["prompt"], dict):
        workflow = workflow["prompt"]
    return copy.deepcopy(workflow)


def make_workflow(args: argparse.Namespace) -> dict:
    workflow = load_workflow(Path(args.workflow))
    for node in workflow.values():
        inputs = node.get("inputs", {})
        cls = node.get("class_type")
        if cls == "RemoteQwenImage21TextEncode":
            inputs["prompt"] = args.prompt
        if cls == "EmptySD3LatentImage":
            inputs["width"] = args.width
            inputs["height"] = args.height
        if cls == "ModelSamplingAuraFlow":
            inputs["shift"] = args.shift
        if cls == "KSampler":
            inputs.update(
                seed=args.seed,
                steps=args.steps,
                cfg=args.cfg,
                sampler_name=args.sampler,
                scheduler=args.scheduler,
            )
        if "filename_prefix" in inputs:
            inputs["filename_prefix"] = f"QwenBench_{args.label}"
    return workflow


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--workflow", default="config/comfyui/qwen-image-2.1-api.json")
    parser.add_argument("--label", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--cfg", type=float, default=4.0)
    parser.add_argument("--shift", type=float, default=3.1)
    parser.add_argument("--sampler", default="euler")
    parser.add_argument("--scheduler", default="simple")
    parser.add_argument("--seed", type=int, default=3208495402395521377)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()

    base = args.url.rstrip("/")
    workflow = make_workflow(args)
    client_id = f"qwen-benchmark-{uuid.uuid4().hex}"
    started = time.time()
    submitted = request_json(
        f"{base}/prompt", {"prompt": workflow, "client_id": client_id}
    )
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"Prompt rejected: {submitted}")
    submit_done = time.time()
    deadline = time.monotonic() + args.timeout
    history = None
    while time.monotonic() < deadline:
        data = request_json(f"{base}/history/{prompt_id}")
        candidate = data.get(str(prompt_id))
        if candidate:
            history = candidate
            status = candidate.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(json.dumps(candidate))
            if status.get("completed") or candidate.get("outputs"):
                break
        time.sleep(2.0)
    if not history:
        raise TimeoutError(f"Timed out waiting for {prompt_id}")

    outputs = history.get("outputs", {})
    images = [image for output in outputs.values() for image in output.get("images", [])]
    messages = history.get("status", {}).get("messages", [])
    timestamps = {}
    for name, payload in messages:
        if isinstance(payload, dict) and "timestamp" in payload:
            timestamps[name] = payload["timestamp"]
    result = {
        "label": args.label,
        "prompt_id": prompt_id,
        "seed": args.seed,
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
        "cfg": args.cfg,
        "shift": args.shift,
        "sampler": args.sampler,
        "scheduler": args.scheduler,
        "wall_seconds": round(time.time() - started, 3),
        "submit_seconds": round(submit_done - started, 3),
        "comfy_timestamps_ms": timestamps,
        "images": images,
        "status": history.get("status"),
    }
    if "execution_start" in timestamps and "execution_success" in timestamps:
        result["execution_seconds"] = round(
            (timestamps["execution_success"] - timestamps["execution_start"]) / 1000,
            3,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
