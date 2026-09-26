# Nemotron 3.5 Lightning on Intel Arc B580

Updated: 2026-09-25 UTC  
Status: promoted to the production endpoint on port 8081.

## Promoted profile

- Model: `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf` (30B total,
  about 3.5B active; 25.48 GB on disk). The GGUF includes its own MTP head.
- llama.cpp: existing BMG AOT build at commit `9f31776c3773cf03f98535c19b7e6d394af374b4`.
- Runtime: Intel Arc B580, SYCL / Level Zero only, 128K context, one slot,
  q4_0 K/V, `--n-cpu-moe 40`, batch/ubatch 1536/1536, 16/16 threads,
  flash attention auto, prompt cache/checkpoints enabled, MTP `n_max=6`.
- Stable SYCL environment: `GGML_SYCL_DEVICE_ARCH=bmg_g21`, MKL FA and DNN
  enabled, ESIMD enabled, DMMV priority disabled, VMM enabled, async memory
  ops and host-pinned memory enabled. No SYCL kernel or scheduler changes were
  made in this pass.
- Unit: `/home/hudson/.config/systemd/user/llama-nemotron-lightning.service`.

## Results

All rows below use the fixed `/completion` benchmark prompt and 128 generated
tokens unless noted. Cold prefill and decode are separated; warm repetitions
reuse the prompt cache. Raw records live under `data/nemotron-q4-*.json`.

| Profile | Prompt tokens | Cold prefill | Cold decode | Warm decode |
|---|---:|---:|---:|---:|
| No MTP, 2048/2048 | 523 | 329.9 tok/s | 25.9 tok/s | 26.3 tok/s |
| No MTP, 2048/2048 | 8,201 | 1,156.4 tok/s | 25.8 tok/s | 26.2 tok/s |
| No MTP, 2048/2048 | 32,775 | 1,064.4 tok/s | 24.9 tok/s | 24.9 tok/s |
| MTP `n_max=4`, 1536/1536 | 8,201 | 968.5 tok/s | 45.3 tok/s | 45.4 tok/s |
| MTP `n_max=4`, 1536/1536 | 32,775 | 880.7 tok/s | 43.5 tok/s | 43.8 tok/s |
| **MTP `n_max=6`, 1536/1536** | **523** | 320.7 tok/s | 49.2 tok/s | 48.9 tok/s |
| **MTP `n_max=6`, 1536/1536** | **8,201** | **965.9 tok/s** | **49.9 tok/s** | **50.0 tok/s** |
| **MTP `n_max=6`, 1536/1536** | **32,775** | **879.2 tok/s** | **48.8 tok/s** | — |
| MTP `n_max=6`, 1536/1536 | 120,016 | 477.6 tok/s | 36.4 tok/s* | — |

*The 120K row generated only 16 tokens; decode includes the near-full context.
The request completed inside the 131,072-token slot. This is a prefill check,
not a sustained throughput claim at the context limit.

Repeated benchmark response hashes matched. The synthetic repeating prompt
had 108/108 draft tokens accepted with `n_max=6`, which is unusually favorable.
A real chat-template `web_search` call through EmeryRouter returned a valid
structured tool call and accepted 40 of 54 MTP draft tokens (74%); that request
decoded at 36.9 tok/s. The tool smoke result is in
`data/nemotron-router-tool-smoke.json`.

## Knobs tried and rejected

| Change | Result | Decision |
|---|---|---|
| MTP `n_max=1`, 2048/2048 | 33.1 tok/s short decode; 8K prefill failed with Level Zero out-of-device-memory. | Reject at 128K context. |
| MTP `n_max=4`, 512/256 | Stable, but 32K prefill was 354.9 tok/s. | Decode improved; prefill loss too large. |
| MTP `n_max=4`, 1024/1024 | 45.6 tok/s decode; 861.8 at 8K and 810.4 at 32K prefill. | Stable intermediate. |
| MTP `n_max=4`, 1536/1536 | 43.5–45.4 decode; about 969 at 8K and 881 at 32K prefill. | Stable, then superseded by `n_max=6`. |
| MTP `n_max=6`, 1792/1792 | 8K prefill failed with Level Zero out-of-device-memory. | Reject. 1536 is the largest verified stable microbatch at the selected placement. |
| `GGML_SYCL_DEVICE_ARCH=bmg_g21` and MKL FA / ESIMD / VMM | Kept from the validated B580 AOT production profile; the Nemotron build loaded and ran on the B580. | Keep. |
| DMMV, KQPI, attention-vector, MMV_Y, new graph/scheduler or kernel changes | Not retuned for Nemotron in this pass; previous model-specific evidence is not transferable. | No unverified SYCL tweaks promoted. |

The model's embedded MTP head increased the loaded B580 model buffer from about
7.5 GiB to 8.2 GiB. With MTP at 2048 microbatch, prefill ran out of device
memory. Reducing microbatch to 1536 restored stability while keeping 8K/32K
prefill within about 10–14% of the prior Gemma MTP measurements and decode near
or above Gemma/Ornith's results. At 120K, prefill is materially slower than
Gemma's prior 698 tok/s measurement, so very long prompts should be expected to
take longer.

## Production integration

- `llama-nemotron-lightning.service` is enabled and active on port 8081.
- Emery's existing router endpoint on port 8220 returned a structured
  `web_search` tool call from Nemotron.
- The image broker on port 8188 now stops and relaunches
  `llama-nemotron-lightning.service` after image work; its environment and
  Python fallback both point to the new unit.
- Fast-text and vision models remain the existing separate LFM and MiniCPM
  services.
- Removed Gemma model and MTP weights, its Hugging Face snapshot, the inactive
  systemd unit, Gemma-only raw benchmark files/logs, stale Gemma defaults, and
  the Gemma-specific thought-channel parser. The comparative measurements
  needed to interpret this retest remain summarized in this report.

## Prior Nemotron comparison

The earlier API benchmark in `data/benchmark-nemotron-vs-ornith.md` measured
Nemotron without MTP at about 25.8 decode / 30.3 prompt tok/s versus Ornith at
45.0 / 35.2. Its output cap affected some prompt-quality checks. The new matched
raw-prompt benchmark isolates the MTP gain: roughly 25 tok/s becomes 49–50 on
the repeated short/medium benchmark prompt. Natural tool-call output is less
favorable at about 37 tok/s, still close to the prior Ornith decode result.

The previous `--n-cpu-moe` sweep found 40 faster than 48 or all-CPU MoE and
remains the placement used here. The 24-layer CPU-MoE configuration did not
become ready in the earlier test window. See `plan-nemotron-lightning.md` for
the full test history and startup/OOM notes.
