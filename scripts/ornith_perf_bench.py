#!/usr/bin/env python3
"""Small, dependency-free benchmark client for an OpenAI-compatible llama.cpp server.

The script is deliberately read-only with respect to the repository and service
configuration. It sends inference requests and records raw response/timing data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = os.environ.get("ORNITH_BASE_URL", "http://192.168.1.121:8081")
DEFAULT_OUT = "data/performance/ornith-bench.jsonl"


def http_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> tuple[int, Any, float]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read()
            elapsed = time.perf_counter() - started
            return response.status, json.loads(raw.decode("utf-8")), elapsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {raw[:1000]}") from exc


def command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr).strip()
    return output or None


def system_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "kernel": command_output(["uname", "-a"]),
        "git_commit": command_output(["git", "rev-parse", "HEAD"]),
    }
    for key, command in {
        "free": ["free", "-b"],
        "uptime": ["uptime"],
        "lspci_gpu": ["bash", "-lc", "lspci | grep -Ei 'vga|3d|display'"],
    }.items():
        snapshot[key] = command_output(command)
    return snapshot


def make_prompt(target_tokens: int) -> str:
    seed = (
        "This is a deterministic performance benchmark passage. Preserve the facts, "
        "the ordering, and the repeated structure. The target is latency measurement, "
        "not creative completion. "
    )
    passage = seed + " ".join(
        f"Record {index:05d}: the stable benchmark marker is {index % 97:02d}."
        for index in range(max(1, target_tokens // 12))
    )
    # Character counts are only an approximation; the response records actual usage.
    return passage


def model_id(base_url: str) -> str:
    status, payload, _ = http_json(f"{base_url.rstrip('/')}/v1/models")
    if status != 200 or not payload.get("data"):
        raise RuntimeError(f"No model returned by {base_url}/v1/models")
    return str(payload["data"][0]["id"])


def run_request(
    base_url: str,
    model: str,
    prompt_tokens: int,
    max_tokens: int,
    seed: int,
    reasoning_effort: str | None,
    thinking: str,
    cache_mode: str,
    run_key: str,
) -> dict[str, Any]:
    system_content = "Answer concisely and do not use tools."
    if cache_mode == "cold":
        # Changing the first message prevents reuse of the previous prompt
        # prefix. This approximates a cold prefill without clearing the live
        # server's internal cache between every sample.
        system_content += f" Benchmark run key: {run_key}."
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": make_prompt(prompt_tokens) + "\nReturn exactly a short numbered summary."},
        ],
        "temperature": 0,
        "top_p": 1,
        "seed": seed,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if thinking != "auto":
        payload["chat_template_kwargs"] = {"enable_thinking": thinking == "on"}
    status, response, wall_seconds = http_json(
        f"{base_url.rstrip('/')}/v1/chat/completions", method="POST", payload=payload
    )
    usage = response.get("usage") or {}
    timings = response.get("timings") or {}
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "status": status,
        "wall_seconds": wall_seconds,
        "prompt_target_tokens": prompt_tokens,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "timings": timings,
        "finish_reason": choice.get("finish_reason"),
        "response_text_sha256": hashlib.sha256(
            str(message.get("content", "")).encode("utf-8")
        ).hexdigest(),
        "response_text_preview": str(message.get("content", ""))[:240],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt-tokens", type=int, nargs="+", default=[512, 4096, 16384])
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--cache-mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--seed", type=int, default=1718)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--thinking", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--run-key", default=None, help="fixed cold-cache key for cross-server comparisons")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    health_status, health, health_elapsed = http_json(f"{base_url}/health")
    if health_status != 200:
        raise RuntimeError(f"Health check failed: {health_status} {health}")
    model = args.model or model_id(base_url)
    metadata: dict[str, Any] = {"kind": "metadata", "timestamp": time.time(), "base_url": base_url, "model": model}
    for endpoint in ("/props", "/slots"):
        try:
            status, data, elapsed = http_json(base_url + endpoint)
            metadata[endpoint.lstrip("/")] = {"status": status, "elapsed": elapsed, "data": data}
        except Exception as exc:  # endpoint availability varies by build
            metadata[endpoint.lstrip("/")] = {"error": str(exc)}
    metadata["health"] = {"status": health_status, "elapsed": health_elapsed, "data": health}
    metadata["system"] = system_snapshot()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata, sort_keys=True) + "\n")
        for target in args.prompt_tokens:
            if args.warmup:
                run_request(
                    base_url, model, target, args.max_tokens, args.seed,
                    args.reasoning_effort, args.thinking, args.cache_mode,
                    f"warmup-{target}",
                )
            samples = []
            for repetition in range(args.repeat):
                started = time.time()
                result = run_request(
                    base_url, model, target, args.max_tokens, args.seed,
                    args.reasoning_effort, args.thinking, args.cache_mode,
                    args.run_key or f"{target}-{repetition}-{started}",
                )
                result.update({
                    "kind": "sample",
                    "timestamp": started,
                    "model": model,
                    "base_url": base_url,
                    "repeat": repetition,
                    "seed": args.seed,
                    "reasoning_effort": args.reasoning_effort,
                    "thinking": args.thinking,
                    "cache_mode": args.cache_mode,
                })
                samples.append(result)
                handle.write(json.dumps(result, sort_keys=True) + "\n")
                handle.flush()
            walls = [float(sample["wall_seconds"]) for sample in samples]
            print(json.dumps({
                "prompt_target_tokens": target,
                "repeats": len(samples),
                "wall_seconds_median": statistics.median(walls),
                "output": str(out_path),
            }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
