#!/usr/bin/env python3
"""Run a small, reproducible OpenAI-compatible chat benchmark.

The workload intentionally matches the local llama.cpp benchmark settings used
for EmeryChat: fixed seed, low temperature, top-k/top-p sampling, and thinking
enabled through the chat template.  The JSON output is suitable for archiving
and post-processing without depending on a third-party benchmark package.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from statistics import mean, median


CASES = [
    (
        "list",
        "Think briefly, then write a numbered list of 80 short observations "
        "about datacenter power demand, bond yields, and manufacturing capacity. "
        "Keep each item under twelve words.",
    ),
    (
        "json",
        "Return only valid JSON with keys sum, product, and even. Calculate "
        "17 + 25, 17 * 25, and whether 42 is even.",
    ),
    (
        "reasoning",
        "A machine completes a job in 12 minutes. A second identical machine "
        "works independently at the same rate. How long do they take together? "
        "Answer with the number and unit, then one short explanation.",
    ),
    (
        "code",
        "Write a Python function named chunked(items, size) that returns a list "
        "of lists, rejects non-positive sizes with ValueError, and handles an "
        "empty input. Return only the code.",
    ),
]


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
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc}") from exc
    return json.loads(raw), time.perf_counter() - started


def quality_checks(case: str, content: str) -> dict[str, bool | int]:
    if case == "json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {"valid_json": False}
        return {
            "valid_json": isinstance(parsed, dict),
            "correct_sum": parsed.get("sum") == 42 if isinstance(parsed, dict) else False,
            "correct_product": parsed.get("product") == 425 if isinstance(parsed, dict) else False,
            "correct_even": parsed.get("even") is True if isinstance(parsed, dict) else False,
        }
    if case == "reasoning":
        return {"contains_6_minutes": bool(re.search(r"\b6\s*minutes?\b", content, re.I))}
    if case == "code":
        return {
            "has_function": bool(re.search(r"def\s+chunked\s*\(", content)),
            "has_value_error": "ValueError" in content,
        }
    numbered = re.findall(r"(?m)^\s*(?:\d+[.)]|[-*])\s+", content)
    return {"numbered_items": len(numbered), "at_least_20_items": len(numbered) >= 20}


def summarize(records: list[dict]) -> dict:
    measured = [record for record in records if not record["warmup"]]
    def values(key: str) -> list[float]:
        return [float(record[key]) for record in measured if record.get(key) is not None]

    summary = {}
    for case in sorted({record["case"] for record in measured}):
        rows = [record for record in measured if record["case"] == case]
        summary[case] = {
            "runs": len(rows),
            "wall_s_median": median(row["wall_s"] for row in rows),
            "wall_s_mean": mean(row["wall_s"] for row in rows),
            "prompt_tok_s_median": median(row["prompt_tok_s"] for row in rows if row.get("prompt_tok_s") is not None),
            "decode_tok_s_median": median(row["decode_tok_s"] for row in rows if row.get("decode_tok_s") is not None),
            "prompt_tokens": rows[0].get("prompt_tokens"),
            "completion_tokens_median": median(row["completion_tokens"] for row in rows),
            "quality": rows[-1]["quality"],
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", default="local")
    parser.add_argument("--label", required=True)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    records = []
    for case, prompt in CASES:
        payload = {
            "model": args.model,
            "seed": 12345,
            "messages": [
                {"role": "system", "content": "You are a concise benchmark assistant. Follow the requested format exactly."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 20,
            "max_tokens": args.max_tokens,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        for repeat in range(args.repeats + 1):
            response, wall_s = request(args.url, payload, args.timeout)
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            timings = response.get("timings") or {}
            usage = response.get("usage") or {}
            record = {
                "label": args.label,
                "case": case,
                "warmup": repeat == 0,
                "repeat": repeat,
                "wall_s": round(wall_s, 6),
                "prompt_tokens": timings.get("prompt_n", usage.get("prompt_tokens")),
                "completion_tokens": timings.get("predicted_n", usage.get("completion_tokens", 0)),
                "prompt_tok_s": timings.get("prompt_per_second"),
                "decode_tok_s": timings.get("predicted_per_second"),
                "finish_reason": choice.get("finish_reason"),
                "quality": quality_checks(case, content),
                "response": content,
            }
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)

    result = {
        "schema": 1,
        "label": args.label,
        "url": args.url,
        "model": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "seed": 12345,
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 20,
            "max_tokens": args.max_tokens,
            "thinking": True,
            "repeats": args.repeats,
        },
        "records": records,
        "summary": summarize(records),
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"output": args.output, "summary": result["summary"]}, indent=2))


if __name__ == "__main__":
    main()
