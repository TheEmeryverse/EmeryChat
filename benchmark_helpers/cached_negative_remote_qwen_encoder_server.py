"""Candidate-only wrapper caching the fixed EmeryChat negative conditioning."""

from __future__ import annotations

import logging
import sys
import time

import server


NEGATIVE_PROMPT = (
    "low resolution, low quality, blurry, out of focus, distorted anatomy, malformed anatomy, "
    "unnatural proportions, deformed hands, extra fingers, missing fingers, fused fingers, "
    "extra limbs, phantom limbs, duplicated body parts, asymmetrical eyes, malformed facial "
    "features, waxy skin, plastic skin, over-smoothed skin, airbrushed skin, oversaturated "
    "colors, harsh HDR, unnatural lighting, artificial AI look, watermark, logo, text artifacts"
)

_log = logging.getLogger("remote-qwen-encoder-cache")
_original = server.encode_conditioning
_negative_cache = {}


def cached_encode_conditioning(clip, prompt):
    if prompt == NEGATIVE_PROMPT and prompt in _negative_cache:
        _log.info("conditioning_phase=negative cache_hit=true")
        return _negative_cache[prompt]
    started = time.monotonic_ns()
    result = _original(clip, prompt)
    elapsed = (time.monotonic_ns() - started) / 1e9
    if prompt == NEGATIVE_PROMPT:
        _negative_cache[prompt] = result
        _log.info("conditioning_phase=negative cache_hit=false seconds=%.6f", elapsed)
    else:
        _log.info("conditioning_phase=positive seconds=%.6f", elapsed)
    return result


server.encode_conditioning = cached_encode_conditioning
sys.argv[0] = "cached_negative_remote_qwen_encoder_server.py"
server.main()
