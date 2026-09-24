"""Candidate-only telemetry imported through PYTHONPATH.

This is deliberately small and read-only with respect to the benchmarked
process: it samples process RSS, host memory/swap, and PyTorch XPU allocator
statistics into the path named by QWEN_RESOURCE_LOG.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.request


def _sample() -> None:
    path = os.environ.get("QWEN_RESOURCE_LOG")
    if not path:
        return
    try:
        import psutil
    except Exception:
        psutil = None

    try:
        import torch
    except Exception:
        torch = None

    proc = psutil.Process(os.getpid()) if psutil is not None else None
    if torch is not None:
        try:
            if torch.xpu.is_available():
                torch.xpu.reset_peak_memory_stats()
        except Exception:
            pass

    with open(path, "a", encoding="utf-8", buffering=1) as out:
        out.write("mono_ns,wall_ns,pid,rss_bytes,host_available_bytes,swap_used_bytes,xpu_allocated_bytes,xpu_reserved_bytes,xpu_peak_allocated_bytes,xpu_peak_reserved_bytes\n")
        while True:
            try:
                rss = proc.memory_info().rss if proc is not None else 0
                vm = psutil.virtual_memory() if psutil is not None else None
                sm = psutil.swap_memory() if psutil is not None else None
                values = [0, 0, 0, 0]
                peaks = [0, 0]
                if torch is not None:
                    try:
                        values = [
                            int(torch.xpu.memory_allocated()),
                            int(torch.xpu.memory_reserved()),
                        ]
                        peaks = [
                            int(torch.xpu.max_memory_allocated()),
                            int(torch.xpu.max_memory_reserved()),
                        ]
                    except Exception:
                        pass
                out.write(
                    f"{time.monotonic_ns()},{time.time_ns()},{os.getpid()},"
                    f"{rss},{getattr(vm, 'available', 0)},"
                    f"{getattr(sm, 'used', 0)},{values[0]},{values[1]},"
                    f"{peaks[0]},{peaks[1]}\n"
                )
            except Exception as exc:
                try:
                    out.write(f"error,{type(exc).__name__},{exc}\n")
                except Exception:
                    pass
            time.sleep(float(os.environ.get("QWEN_RESOURCE_INTERVAL", "0.5")))


def _wrap_encoder_requests() -> None:
    log_path = os.environ.get("QWEN_ENCODER_TIMING_LOG")
    if not log_path:
        return
    original = urllib.request.urlopen

    def timed_urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "/v1/qwen-image-2.1/encode" not in url:
            return original(request, *args, **kwargs)
        started = time.monotonic_ns()
        try:
            return original(request, *args, **kwargs)
        finally:
            with open(log_path, "a", encoding="utf-8", buffering=1) as out:
                out.write(f"{time.monotonic_ns()},{time.monotonic_ns() - started},{url}\n")

    urllib.request.urlopen = timed_urlopen


if os.environ.get("QWEN_RESOURCE_LOG"):
    threading.Thread(target=_sample, name="qwen-resource-sampler", daemon=True).start()
_wrap_encoder_requests()
