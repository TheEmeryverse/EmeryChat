"""Candidate-only wrapper that times the two real conditioning passes."""

from __future__ import annotations

import logging
import sys
import time

import server


_log = logging.getLogger("remote-qwen-encoder-timing")
_original = server.encode_conditioning
_call_number = 0


def timed_encode_conditioning(clip, prompt):
    global _call_number
    _call_number += 1
    started = time.monotonic_ns()
    result = _original(clip, prompt)
    elapsed = (time.monotonic_ns() - started) / 1e9
    role = "positive" if _call_number % 2 else "negative"
    _log.info("conditioning_phase=%s call=%d seconds=%.6f", role, _call_number, elapsed)
    return result


server.encode_conditioning = timed_encode_conditioning
sys.argv[0] = "timed_remote_qwen_encoder_server.py"
server.main()
