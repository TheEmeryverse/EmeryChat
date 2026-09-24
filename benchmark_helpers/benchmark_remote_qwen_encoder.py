#!/usr/bin/env python3
"""Benchmark real authenticated remote Qwen encoder requests.

The request body is the fixed Qwen Image 2.1 text-to-image workload. Tokens,
payloads, prompts, and response contents are never printed; only timings and
shape/byte-count validation are written.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

import psutil
import torch


PROMPT = (
    "A realistic red apple on a weathered wooden table beside a window, "
    "soft natural daylight, accurate materials and natural shadows, "
    "documentary food photography."
)


def negative_prompt(workflow: Path) -> str:
    data = json.loads(workflow.read_text())
    data = data.get("prompt", data)
    for node in data.values():
        if node.get("class_type") == "RemoteQwenImage21TextEncode":
            return node["inputs"]["negative_prompt"]
    raise RuntimeError("negative prompt node not found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8086")
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()

    token = os.environ.get("REMOTE_QWEN_TOKEN")
    if not token:
        raise RuntimeError("REMOTE_QWEN_TOKEN is not set")
    process = psutil.Process(args.pid)
    body = json.dumps(
        {"schema": 1, "prompt": PROMPT, "negative_prompt": negative_prompt(args.workflow)},
        separators=(",", ":"),
    ).encode()
    request_url = args.url.rstrip("/") + "/v1/qwen-image-2.1/encode"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}

    samples: list[dict[str, int | float]] = []
    stop = False

    def monitor() -> None:
        nonlocal stop
        last = process.cpu_times()
        last_time = time.monotonic()
        while not stop:
            try:
                now = time.monotonic()
                current = process.cpu_times()
                elapsed = max(now - last_time, 1e-6)
                cpu = ((current.user + current.system) - (last.user + last.system))
                samples.append(
                    {
                        "mono_ns": time.monotonic_ns(),
                        "cpu_percent_all": cpu / elapsed / psutil.cpu_count() * 100,
                        "rss_bytes": process.memory_info().rss,
                        "threads": process.num_threads(),
                    }
                )
                last, last_time = current, now
            except (psutil.Error, OSError):
                pass
            time.sleep(0.2)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    requests = []
    try:
        for run in range(1, args.runs + 1):
            request = urllib.request.Request(
                request_url, data=body, headers=headers, method="POST"
            )
            t0 = time.monotonic_ns()
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read()
            t1 = time.monotonic_ns()
            decoded = json.loads(raw)
            t2 = time.monotonic_ns()
            payload = base64.b64decode(decoded["payload"])
            t3 = time.monotonic_ns()
            archive = torch.load(io.BytesIO(payload), map_location="cpu", weights_only=True)
            t4 = time.monotonic_ns()
            if decoded.get("schema") != 1 or decoded.get("encoding") != "torch.save/base64":
                raise RuntimeError("unsupported encoder response")
            if archive.get("schema") != 1 or not archive.get("positive") or not archive.get("negative"):
                raise RuntimeError("invalid conditioning archive")
            requests.append(
                {
                    "run": run,
                    "http_response_s": (t1 - t0) / 1e9,
                    "json_parse_s": (t2 - t1) / 1e9,
                    "base64_decode_s": (t3 - t2) / 1e9,
                    "torch_load_s": (t4 - t3) / 1e9,
                    "total_client_s": (t4 - t0) / 1e9,
                    "response_bytes": len(raw),
                    "payload_bytes": len(payload),
                    "positive_items": len(archive["positive"]),
                    "negative_items": len(archive["negative"]),
                }
            )
            time.sleep(1)
    finally:
        stop = True
        watcher.join(timeout=1)

    result = {
        "pid": args.pid,
        "torch_threads": torch.get_num_threads(),
        "torch_interop_threads": torch.get_num_interop_threads(),
        "requests": requests,
        "peak_rss_bytes": max((int(x["rss_bytes"]) for x in samples), default=0),
        "peak_threads": max((int(x["threads"]) for x in samples), default=0),
        "peak_cpu_percent_all": max((float(x["cpu_percent_all"]) for x in samples), default=0),
        "samples": samples,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))


if __name__ == "__main__":
    main()
