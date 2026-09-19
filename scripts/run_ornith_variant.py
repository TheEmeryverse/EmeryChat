#!/usr/bin/env python3
"""Launch one isolated Ornith llama.cpp variant, benchmark it, and stop it.

This never edits the systemd unit. It is intended for controlled A/B runs while
the production llama-ornith.service is stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


DEFAULT_MODEL = "/home/hudson/.cache/huggingface/hub/models--0xKitkat--Ornith-1.5-35B-A3B-Uncensored-GGUF/snapshots/ab0eed77c73880afda789a3914003db2273fd64a/Ornith-1.5-35B-Uncensored-Q4_K_M.gguf"
DEFAULT_SERVER = "/home/hudson/llama.cpp/build-sycl/bin/llama-server"
DEFAULT_SET_VARS = "/home/hudson/intel/oneapi/setvars.sh"


def wait_for_health(base_url: str, process: subprocess.Popen[str], timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with code {process.returncode}; inspect the server log")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=3) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # startup is expected to take time
            last_error = str(exc)
        time.sleep(1)
    raise TimeoutError(f"server did not become healthy: {last_error}")


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="short result/log label")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--setvars", default=DEFAULT_SET_VARS)
    parser.add_argument("--out-dir", default="data/performance")
    parser.add_argument("--prompt-tokens", type=int, nargs="+", default=[512, 4096])
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=2048)
    parser.add_argument("--ubatch-size", type=int, default=2048)
    parser.add_argument("--n-cpu-moe", type=int, default=24)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--threads-batch", type=int, default=16)
    parser.add_argument("--gpu-layers", type=int, default=999)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--flash-attn", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--load-mode", default="auto", choices=("auto", "none", "mmap", "mlock", "mmap+mlock"))
    parser.add_argument("--cache-ram", type=int, default=8192)
    parser.add_argument("--cache-type-k", default="q4_0")
    parser.add_argument("--cache-type-v", default="q4_0")
    parser.add_argument("--spec-type", default=None)
    parser.add_argument("--spec-draft-n-max", type=int, default=None)
    parser.add_argument("--thinking", choices=("auto", "on", "off"), default="off")
    parser.add_argument("--cache-mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--run-key", default=None, help="fixed benchmark key for cross-build output comparison")
    parser.add_argument("--mixed-soak", action="store_true")
    parser.add_argument("--mixed-workers", type=int, default=2)
    parser.add_argument("--mixed-rounds", type=int, default=3)
    parser.add_argument("--extra-env", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"{args.label}.server.log"
    result_path = out_dir / f"{args.label}.jsonl"
    command = [
        args.server,
        "-lv", "4",
        "-m", args.model,
        "--host", "127.0.0.1",
        "--port", str(args.port),
        "--device", "SYCL0",
        "--split-mode", "none",
        "--ctx-size", "131072",
        "--parallel", str(args.parallel),
        "--gpu-layers", str(args.gpu_layers),
        "--n-cpu-moe", str(args.n_cpu_moe),
        "--kv-offload",
        "--cache-type-k", args.cache_type_k,
        "--cache-type-v", args.cache_type_v,
        "--batch-size", str(args.batch_size),
        "--ubatch-size", str(args.ubatch_size),
        "--threads", str(args.threads),
        "--threads-batch", str(args.threads_batch),
        "--flash-attn", args.flash_attn,
        "--jinja",
        "--reasoning", "auto",
        "--reasoning-effort", "high",
        "--cache-ram", str(args.cache_ram),
        "--load-mode", args.load_mode,
    ]
    if not args.no_cache_prompt:
        command += [
            "--cache-prompt",
            "--cache-reuse", "64",
            "--ctx-checkpoints", "8",
            "--checkpoint-min-step", "256",
            "--cache-idle-slots",
        ]
    if args.spec_type:
        command += ["--spec-type", args.spec_type]
    if args.spec_draft_n_max is not None:
        command += ["--spec-draft-n-max", str(args.spec_draft_n_max)]

    environment = os.environ.copy()
    for item in args.extra_env:
        if "=" not in item:
            raise ValueError(f"--extra-env requires KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        environment[key] = value
    shell_command = "source " + repr(args.setvars) + " >/dev/null 2>&1; exec " + " ".join(repr(part) for part in command)

    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(json.dumps({"label": args.label, "command": command, "environment": args.extra_env}) + "\n")
        log_handle.flush()
        process = subprocess.Popen(
            ["bash", "-lc", shell_command],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{args.port}"
            wait_for_health(base_url, process)
            bench_command = [
                sys.executable,
                "scripts/ornith_perf_bench.py",
                "--base-url", base_url,
                "--prompt-tokens", *[str(value) for value in args.prompt_tokens],
                "--max-tokens", str(args.max_tokens),
                "--repeat", str(args.repeat),
                "--cache-mode", args.cache_mode,
                "--thinking", args.thinking,
                "--out", str(result_path),
            ]
            if args.warmup:
                bench_command.append("--warmup")
            if args.run_key:
                bench_command += ["--run-key", args.run_key]
            completed = subprocess.run(bench_command, check=False, text=True)
            if completed.returncode != 0:
                return completed.returncode
            if args.mixed_soak:
                mixed_command = [
                    sys.executable,
                    "scripts/ornith_mixed_soak.py",
                    "--base-url", base_url,
                    "--workers", str(args.mixed_workers),
                    "--rounds", str(args.mixed_rounds),
                    "--max-tokens", str(args.max_tokens),
                    "--cache-mode", args.cache_mode,
                    "--thinking", args.thinking,
                    "--out", str(out_dir / f"{args.label}.mixed.jsonl"),
                ]
                mixed_completed = subprocess.run(mixed_command, check=False, text=True)
                if mixed_completed.returncode != 0:
                    return mixed_completed.returncode
            print(json.dumps({
                "label": args.label,
                "results": str(result_path),
                "server_log": str(log_path),
            }, sort_keys=True))
            return 0
        finally:
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
