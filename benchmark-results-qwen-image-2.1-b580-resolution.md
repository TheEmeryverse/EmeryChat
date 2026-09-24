# Qwen Image 2.1 B580 resolution check

Date: 2026-09-23

## Setup

- Runtime: transient ComfyUI on Intel Arc B580, with the remote Qwen encoder
- Workflow: Q4_K_M, 12 steps, CFG 4.0, Euler/simple, AuraFlow shift 3.1
- Prompt: the red apple benchmark prompt from the 512x512 lifecycle validation
- 512x512 baseline: seed `3208495402395521377`
- 768x768 matched run: same prompt and seed

## Results

| Size | Seed | ComfyUI execution | Client wall time | Output |
|---|---:|---:|---:|---|
| 512x512 | 3208495402395521377 | 43.93 s | 53.67 s | `data/qwen-b580-emerychat-validation-20260923.png` |
| 768x768 | 3208495402395521377 | 52.88 s | 64.47 s | `data/qwen-b580-768-12-matched-20260923.png` |
| 768x768 | 3208495402395521380 | 41.38 s | 52.19 s | `data/qwen-b580-768-12-20260923.png` |

The matched run took about 20% longer end to end than the recorded 512x512
baseline. A second 768x768 run was close to baseline time, showing notable
request-to-request variation in startup/encoding overhead. Both 768 outputs
were valid 768x768 PNGs. The matched-seed image retains the scene and shows
finer apple and tabletop detail when viewed at native size.

## Outcome

768x768 is a useful higher-detail setting while keeping 12 steps; the matched
B580 run added about 11 seconds end to end. The active workflow remains
512x512, preserving its pre-existing local override. Choose 768x768 when the
added detail is worth the extra time; keep 512x512 when minimizing latency.
