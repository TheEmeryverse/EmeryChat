#!/usr/bin/env python3
"""Run deterministic varied raw-completion prompts against llama-server."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone


PROMPTS = [
    (
        "speculative-explanation",
        "Explain in three concise numbered points why speculative decoding can improve autoregressive generation throughput.\n",
    ),
    (
        "python-function",
        "Write a small Python function that merges overlapping inclusive integer ranges, then show one test case.\n",
    ),
    (
        "arithmetic",
        "A warehouse has 17 crates with 24 items each. 15 percent of the items are shipped. How many remain? Show the arithmetic.\n",
    ),
    (
        "fiction",
        "Write a short original paragraph about a cartographer finding an impossible island, with concrete sensory details.\n",
    ),
    (
        "systems-design",
        "Give a compact design for measuring a service's cold-start time, prefill throughput, decode throughput, and cache-hit behavior.\n",
    ),
]


def request(url: str, prompt: str, max_tokens: int) -> tuple[dict, float]:
    payload = {
        "prompt": prompt,
        "n_predict": max_tokens,
        "temperature": 0.0,
        "seed": 1718,
        "cache_prompt": True,
        "stream": False,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url + "/completion",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=900) as response:
        result = json.loads(response.read())
    return result, time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()

    records = []
    for name, prompt in PROMPTS:
        for repeat in range(args.repeats + 1):
            response, wall_s = request(args.url, prompt, args.max_tokens)
            timings = response.get("timings") or {}
            text = response.get("content") or ""
            record = {
                "label": args.label,
                "prompt_name": name,
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
        "settings": {"max_tokens": args.max_tokens, "repeats": args.repeats},
        "records": records,
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
