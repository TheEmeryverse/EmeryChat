#!/usr/bin/env python3
"""Reproducible raw-prompt benchmark for llama.cpp MTP experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def request(url: str, payload: dict, timeout: float) -> tuple[dict, float]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc
    return json.loads(raw), time.perf_counter() - started


def make_prompt(target: int) -> str:
    # The prefix differs per size so cold measurements cannot reuse a prior
    # shorter prompt. The server timings report the exact token count.
    header = f"MTP-BENCHMARK-SIZE-{target:06d}\n"
    row = "The benchmark records a stable marker sequence for throughput measurement.\n"
    return header + row * max(1, target // 11)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--sizes", type=int, nargs="+", default=[512, 4096, 16384])
    args = parser.parse_args()

    records: list[dict] = []
    for target in args.sizes:
        prompt = make_prompt(target)
        for repeat in range(args.repeats + 1):
            payload = {
                "prompt": prompt,
                "n_predict": args.max_tokens,
                "temperature": 0.0,
                "seed": 1718,
                "cache_prompt": True,
                "stream": False,
            }
            response, wall_s = request(args.url + "/completion", payload, args.timeout)
            timings = response.get("timings") or {}
            text = response.get("content") or ""
            record = {
                "label": args.label,
                "prompt_target": target,
                "repeat": repeat,
                "cache_mode": "cold" if repeat == 0 else "warm",
                "prompt_tokens": timings.get("prompt_n"),
                "cache_n": timings.get("cache_n"),
                "completion_tokens": timings.get("predicted_n"),
                "prompt_tok_s": timings.get("prompt_per_second"),
                "decode_tok_s": timings.get("predicted_per_second"),
                "prompt_ms": timings.get("prompt_ms"),
                "decode_ms": timings.get("predicted_ms"),
                "draft_n": timings.get("draft_n"),
                "draft_n_accepted": timings.get("draft_n_accepted"),
                "wall_s": round(wall_s, 6),
                "finish_reason": response.get("stop_type") or response.get("stop"),
                "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "response_preview": text[:160],
            }
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)

    result = {
        "schema": 1,
        "label": args.label,
        "url": args.url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "max_tokens": args.max_tokens,
            "temperature": 0.0,
            "seed": 1718,
            "sizes": args.sizes,
            "repeats": args.repeats,
        },
        "records": records,
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
