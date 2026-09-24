# Qwen Image 2.1 ComfyUI Linux benchmark

Date: 2026-09-22

## Outcome

The adopted default is Q4_K_M, `--lowvram`, FP32/manual-cast, Euler/simple,
CFG 4.0, AuraFlow shift 3.1, 768x768, 12 steps. It was successfully measured
three times and is the best quality/speed compromise tested. The fastest stable
tested setting was 512x512, 8 steps, but it loses visible fine detail and is a
draft mode rather than the default.

## Fixed benchmark input

- Prompt: `A realistic red apple on a weathered wooden table beside a window, soft natural daylight, accurate materials and natural shadows, documentary food photography.`
- Negative prompt: the configured negative prompt in `config/comfyui/qwen-image-2.1-api.json`
- Sampler/scheduler: Euler/simple
- CFG: 4.0; AuraFlow shift: 3.1; denoise: 1.0
- Transformer: `qwen-image-2.1-Q4_K_M.gguf`
- VAE: `qwen_image_2.1_vae_bf16.safetensors`
- Comparison seed: `3208495402395521377`
- Harness: `scripts/benchmark_qwen_comfyui.py`

The harness changes only the in-memory API prompt. It does not edit the
workflow or restart ComfyUI.

## Generation results

| Label | Size | Steps | Comfy execution | Wall time | Output |
|---|---:|---:|---:|---:|---|
| baseline-512-20 | 512x512 | 20 | 184.56 s | 186.08 s | `QwenBench_baseline-512-20_00001_.png` |
| baseline-512-8 | 512x512 | 8 | 77.05 s | 78.04 s | `QwenBench_baseline-512-8_00001_.png` |
| baseline-768-20 | 768x768 | 20 | 397.94 s | 398.19 s | `QwenBench_baseline-768-20_00001_.png` |
| candidate-768-12-r1 | 768x768 | 12 | 214.83 s | 216.09 s | `QwenBench_candidate-768-12-r1_00001_.png` |
| candidate-768-12-r2 | 768x768 | 12 | 214.59 s | 216.10 s | `QwenBench_candidate-768-12-r2_00002_.png` |
| candidate-768-12-r3 | 768x768 | 12 | 214.73 s | 216.10 s | `QwenBench_candidate-768-12-r3_00001_.png` |
| baseline-1024-20-fixed | 1024x1024 | 20 | 630.70 s | 632.27 s | `QwenBench_baseline-1024-20-fixed_00001_.png` |

Outputs are under `downloads/qwen-image-2.1/linux-comfyui/output/`.

The three 768x768/12 runs averaged 214.72 s with a 0.12 s spread in Comfy
execution time. All were successful and visually coherent. Relative to the
768x768/20 control, 12 steps reduced execution time by 46.0%. The 512x512/8
control reduced time by 58.3% versus 512x512/20, but had softer apple texture
and shadow detail. Compared with the fixed 1024x1024/20 control, the adopted
candidate is about 2.94x faster.

The nominal second same-seed 768x768/12 attempt was excluded: ComfyUI returned
its execution cache in 0.09 s rather than generating. Repeated runs used seeds
`3208495402395521378` and `3208495402395521379`.

## Runtime and resource evidence

- Q4_K_M loaded completely: 4,487.23 MB loaded, `full load: True`; it was not
  layer-by-layer CPU offloaded during sampling.
- Cold-load logs showed roughly 5 seconds for transformer load and roughly 1
  second for VAE load. Warm executions cached model/text nodes.
- A direct uncached remote encoder request took approximately 55 s. ComfyUI
  does not expose a per-node timing breakdown; identical prompt requests were
  cached after the first encode.
- Sampling dominated total time. VAE transition/decode was only a small tail
  in the service logs and was not the bottleneck.
- Observed NVIDIA memory snapshots were approximately 5,985 MiB during 768x768
  tests and 7,365 MiB during 1024x1024. The Q4 model remained fully resident.
- At investigation start, the host had 8 GiB of swap allocated and essentially
  full. During Qwen runs it fell to roughly 1-4 MiB; final state was 60 GiB RAM,
  about 16 GiB available, and no active swap-in/out in short samples. ComfyUI
  and the encoder had zero `VmSwap` in the resource audit.
- Ornith remained active throughout: service PID `2958548` remained running and
  it uses Intel Level Zero/SYCL on the B580, with no NVIDIA CUDA allocation.

## Runtime A/B tests

All tests used 512x512, 8 steps, the same prompt and seed, and succeeded.

| Runtime | Execution | Result |
|---|---:|---|
| Current `--lowvram`, default FP32/manual cast | 77.05 s | Keeper |
| `--lowvram --force-fp16` | 181.84 s | 2.36x slower; reverted |
| Normal VRAM mode, no `--lowvram` | 125.92 s | 1.63x slower; reverted |

PyTorch attention was active. FlashAttention, cuDNN attention, xformers, and
SageAttention are not viable speedups for this Pascal workload. CUDA/Triton
comfy-kitchen backends were disabled because the installed backend requires a
newer CUDA/PyTorch combination. Missing optional nodes (`kornia`, `spandrel`,
`comfy_angle`) do not affect this workflow.

## Model/workflow investigation

- Q4_K_M and Q5_K_M are present. Q4_0 is absent and was not benchmarked.
- Q5_K_M adds about 590 MiB to the model file and is unlikely to reduce
  latency; it was not made active because Q4 is already fully resident.
- The local Lightning reference is for Qwen Image 2512, not Qwen Image 2.1.
  It is not a drop-in for this Q4_K_M model. No matching Qwen Image 2.1
  Lightning/distilled weights are installed.
- The experimental QwenImage21Cache node was not enabled without a dedicated
  correctness/performance A/B; it is not required for the measured win.

## Changes and rollback

Permanent change:

- `config/comfyui/qwen-image-2.1-api.json`: width/height changed from 1024/1024
  to 768/768; steps changed from 20 to 12. All other settings and model paths
  are unchanged.

Temporary service tests were fully reverted. The active service remains:

`--listen 0.0.0.0 --port 8188 --lowvram --base-directory ...`

Rollback the adopted workflow with this targeted patch:

```diff
-      "width": 768,
-      "height": 768,
+      "width": 1024,
+      "height": 1024,
@@
-      "steps": 12,
+      "steps": 20,
```

No models, Ornith files, ComfyUI sources, or unrelated dirty-worktree files
were removed or overwritten.

## Recommendation

Adopt the documented 768x768/12-step default for normal EmeryChat image
generation. The improvement is large enough to adopt: approximately 2.94x
faster than the fixed 1024x1024/20-step control, with acceptable quality on all
three repeated candidate generations. Use 512x512/8 for drafts and restore
1024x1024/20 when maximum detail is required.
