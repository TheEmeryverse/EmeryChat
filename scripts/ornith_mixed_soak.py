#!/usr/bin/env python3
"""Run a bounded concurrent mixed prefill/decode soak against llama-server."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ornith_perf_bench import model_id, run_request, http_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--out", default="data/performance/ornith-mixed-soak.jsonl")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--thinking", choices=("auto", "on", "off"), default="off")
    parser.add_argument("--cache-mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--seed", type=int, default=1718)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    status, health, _ = http_json(base_url + "/health")
    if status != 200:
        raise RuntimeError(f"health check failed: {status} {health}")
    model = model_id(base_url)
    targets = (512, 4096, 16384)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Prime each prompt shape once, without including startup in the mixed-load data.
    for target in targets:
        run_request(
            base_url, model, target, args.max_tokens, args.seed,
            None, args.thinking, args.cache_mode, f"soak-warmup-{target}",
        )

    jobs = [(round_id, worker_id, targets[(round_id + worker_id) % len(targets)])
            for round_id in range(args.rounds)
            for worker_id in range(args.workers)]
    started = time.time()

    def execute(job: tuple[int, int, int]) -> dict:
        round_id, worker_id, target = job
        sample_started = time.time()
        result = run_request(
            base_url, model, target, args.max_tokens, args.seed,
            None, args.thinking, args.cache_mode,
            f"soak-{round_id}-{worker_id}-{sample_started}",
        )
        result.update({
            "kind": "mixed_soak_sample",
            "round": round_id,
            "worker": worker_id,
            "started": sample_started,
            "elapsed_since_soak_start": sample_started - started,
        })
        return result

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(execute, job) for job in jobs]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda row: (row["round"], row["worker"]))
    with out_path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, sort_keys=True) + "\n")

    walls = [float(result["wall_seconds"]) for result in results]
    failures = [result for result in results if result.get("finish_reason") not in ("stop", "length")]
    print(json.dumps({
        "output": str(out_path),
        "requests": len(results),
        "workers": args.workers,
        "rounds": args.rounds,
        "wall_median_seconds": statistics.median(walls) if walls else None,
        "wall_max_seconds": max(walls) if walls else None,
        "nonterminal_results": len(failures),
    }, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
