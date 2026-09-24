# Qwen3.8-27B on Intel Arc B580 — benchmark plan

Status: investigation phase; no Qwen inference or candidate build has been run.
Scope: Intel Arc B580 only, through llama.cpp SYCL/Level Zero. No CUDA/NVIDIA test path.

## Current control and safety state

- Workspace: `/home/hudson/EmeryChat`
- llama.cpp checkout: `/home/hudson/llama.cpp`
- llama.cpp source commit: `9f31776c3773cf03f98535c19b7e6d394af374b4`
- Existing SYCL server: `/home/hudson/llama.cpp/build-sycl-f16/bin/llama-server`, port `8081`, model `Ornith-1.5-35B-Uncensored-Q4_K_M.gguf`, `--device SYCL0`, `--split-mode none`, `--gpu-layers 999`, context `131072`.
- The existing SYCL server is online and healthy. Its current device inventory reports only `SYCL0: Intel(R) Arc(TM) B580 Graphics (12216 MiB, 902 MiB free)`.
- Because the existing server consumes the B580, any Qwen runtime benchmark will require an announced stop/pause, a separate localhost port, and restoration afterward. No stop has yet been performed.
- The current llama.cpp worktree contains unrelated uncommitted files; production source and binary will not be overwritten. Candidate changes must use detached worktrees and separate build directories.

## Device verification required before every executable test

Run and record the output before each test:

```bash
env ONEAPI_DEVICE_SELECTOR=level_zero:gpu \
  LD_LIBRARY_PATH=/home/hudson/intel/oneapi/compiler/2025.3/lib:/home/hudson/intel/oneapi/2025.3/lib:/home/hudson/intel/oneapi/umf/1.0/lib:/home/hudson/intel/oneapi/mkl/2025.3/lib:/home/hudson/intel/oneapi/tbb/2022.3/lib \
  /home/hudson/intel/oneapi/compiler/2025.3/bin/sycl-ls --verbose
```

Required recorded identity: `Intel(R) Arc(TM) B580 Graphics`, Level Zero, driver `1.15.38308+4`, architecture `intel_gpu_bmg_g21`.

For llama.cpp executables, additionally use `--list-devices` with `ONEAPI_DEVICE_SELECTOR=level_zero:gpu`, `CUDA_VISIBLE_DEVICES=` and the same oneAPI library path, and require exactly `SYCL0: Intel(R) Arc(TM) B580 Graphics` before proceeding.

## Model hypotheses to verify

- The requested model is `Qwen/Qwen3.8-27B`, not Qwen3-8B or Qwen3-30B-A3B.
- It is a dense 27B causal language model with a vision encoder; the language-model trunk has 64 layers, hidden size 5120, padded vocabulary 248,320, and a repeating hybrid layout of three Gated DeltaNet/FFN blocks followed by one Gated Attention/FFN block.
- It is not an MoE model; `--n-cpu-moe`/`--cpu-moe` must not be used. The recurrent Gated DeltaNet state and the 16 full-attention layers have different memory behavior, so ordinary CPU FFN offload and KV/recurrent-state measurements must be distinguished.
- Native context is 262,144 tokens, but the benchmark will first characterize 128, 512, 2k, 8k, and 16k, then extend only if memory and latency remain sane.

## Quantization matrix

Selected source: `bartowski/Qwen3.8-27B-GGUF`, standard (non-UD) files. API-reported byte sizes:

| Quant | GGUF file | Bytes | Approx. GiB | Local path/status |
|---|---|---:|---:|---|
| Q4_K_M | `Qwen3.8-27B-Q4_K_M.gguf` | 17,442,399,968 | 16.25 | `/home/hudson/EmeryChat/models/qwen38-bartowski/Qwen3.8-27B-Q4_K_M.gguf` complete/tested |
| Q4_K_S | `Qwen3.8-27B-Q4_K_S.gguf` | 16,363,513,568 | 15.24 | `/home/hudson/EmeryChat/models/qwen38-bartowski/Qwen3.8-27B-Q4_K_S.gguf` complete/tested |
| IQ4_XS | `Qwen3.8-27B-IQ4_XS.gguf` | 15,475,951,328 | 14.41 | pending |
| Q3_K_M | `Qwen3.8-27B-Q3_K_M.gguf` | 13,404,051,168 | 12.49 | pending |
| Q3_K_S | `Qwen3.8-27B-Q3_K_S.gguf` | 12,739,884,768 | 11.86 | pending |
| IQ3_M | `Qwen3.8-27B-IQ3_M.gguf` | 14,860,629,728 | 13.84 | pending |
| Q5_K_S | `Qwen3.8-27B-Q5_K_S.gguf` | 19,566,913,248 | 18.22 | pending |
| IQ2_M | `Qwen3.8-27B-IQ2_M.gguf` | 10,522,248,928 | 9.80 | `/home/hudson/EmeryChat/models/qwen38-bartowski/Qwen3.8-27B-IQ2_M.gguf` complete/tested full residency |

These are file sizes only; VRAM fit must include norms, activations, recurrent state, full-attention KV, graph buffers, allocator margin, and any CPU-offloaded weight traffic. Record SHA256 after each download.

## Added comparison target: Ornith 1.5 9B

Use the official `ornith-ai/Ornith-1.5-9B-GGUF` repository as the primary source, beginning with `Ornith-1.5-9B-Q4_K_M.gguf` (the current model page lists it at about 5.78 GB). This is a separate model-quality/performance comparison, not a Qwen quantization result. It should be tested fully resident on the same Intel Arc B580/SYCL control, with the same deterministic prompts and a small correctness check; if its GGUF carries an MTP head, test that only after the baseline. The official repository page is https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF and the bartowski quantization matrix is https://huggingface.co/bartowski/Ornith-1.5-9B-GGUF/blob/main/README.md.

Use one consistent public GGUF source and record SHA256, file size, and exact path for each file:

1. `Q4_K_M` quality baseline, conservative GPU offload.
2. `Q4_K_S`, `IQ4_XS`.
3. `Q3_K_M`, `Q3_K_S`, `IQ3_M`.
4. `Q5_K_S` or `Q5_K_M` only if CPU offload remains practical.

For each quant record: file size, model-buffer placement, VRAM resident bytes, CPU-offloaded tensor bytes, RSS/system RAM, load time, usable context, prompt-cache reuse, and output stability. Do not infer fit from file size alone; include runtime buffers, recurrent state, KV cache, graph allocations, and allocator margin.

## Frozen-control benchmark hypotheses

- Control binary: unchanged `/home/hudson/llama.cpp/build-sycl-f16/bin/llama-server`.
- Control device selection: `--device SYCL0 --split-mode none --main-gpu 0`.
- Initial controls: `--gpu-layers all`, `--ctx-size` stepped upward, `--batch-size`/`--ubatch-size` tested separately, explicit `--threads` and `--threads-batch`, flash attention `auto/on/off`, and KV K/V types `q4_0`, `q5_0`, `q8_0`, `f16` only where memory permits.
- Prefill and decode are separate objectives. Mixed interactive selection will be made only after reporting both.
- Use deterministic prompts at approximately 128/512/2k/8k/16k tokens, fixed generation settings, multiple repetitions, median and spread, and output hashes/text checks.

## Source and documentation references

Local llama.cpp paths:

- `src/models/qwen35.cpp` — Qwen3.8/Qwen3.5-family architecture loading, recurrent-layer detection, Gated DeltaNet and full-attention graph construction.
- `ggml/src/ggml-sycl/ggml-sycl.cpp` — SYCL device initialization, feature logging, MMQ/MMVQ/DMMV dispatch decisions, graph execution, and Level Zero guarded paths.
- `ggml/src/ggml-sycl/mmq.cpp`, `mmvq.cpp`, `dmmv.cpp`, `gemm.hpp`, `quants.hpp` — quantized matrix and vector kernel paths.
- `ggml/src/ggml-sycl/CMakeLists.txt` — Level Zero API, F16, graph, oneDNN, and AOT configuration guards.
- `ggml/CMakeLists.txt` — `GGML_SYCL_SUPPORT_LEVEL_ZERO_API` and backend configuration.
- `docs/backend/SYCL.md` — llama.cpp SYCL design, Intel GPU/oneAPI requirements, memory notes, and known issues.
- `tools/server/README.md` and `tools/llama-bench/README.md` — current command-line semantics for offload, split mode, context/batching, flash attention, KV cache, and benchmarking.
- `include/llama.h`, `common/common.h`, `src/llama-context.cpp` — context, batching, offload, split-mode, flash-attention, and graph parameters.
- `/home/hudson/llama.cpp/build-sycl-f16/compile_commands.json` — current production-equivalent compile definitions: `GGML_USE_SYCL`, `GGML_SYCL_F16`, `GGML_SYCL_GRAPH`, `GGML_SYCL_SUPPORT_LEVEL_ZERO_API`, `GGML_SYCL_DNNL=0`, `GGML_SYCL_HOST_MEM_FALLBACK`, `GGML_SYCL_WARP_SIZE=16`.
- `ggml/src/ggml-sycl/ggml-sycl.cpp:3984-3988,4724-4825` — MMQ is currently hard-disabled; small-batch quantized matmuls select MMVQ/DMMV, with larger batches falling back to generic GEMM.
- `ggml/src/ggml-sycl/ggml-sycl.cpp:5003-5061,6051-6089` — dense/MoE indexed-matmul seam and graph-capture exclusions. The dense Qwen3.8 target should not use MoE-only paths.
- `ggml/src/ggml-sycl/fattn.cpp`, `fattn-mkl.cpp`, `fattn-onednn.cpp` — SYCL flash-attention dispatch; oneDNN is compile-disabled in the current control while oneMKL is linked.
- `ggml/src/ggml-sycl/CMakeLists.txt:131-165,201-223` — oneDNN and `GGML_SYCL_DEVICE_ARCH`/`spir64_gen` AOT guards; active control has no AOT target.

Authoritative online references:

- https://huggingface.co/Qwen/Qwen3.8-27B — model architecture and native context.
- https://huggingface.co/unsloth/Qwen3.8-27B-GGUF — available GGUF quant families and sizes.
- https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md
- https://github.com/ggml-org/llama.cpp/blob/master/ggml/src/ggml-sycl/ggml-sycl.cpp
- https://github.com/ggml-org/llama.cpp/blob/master/ggml/CMakeLists.txt

## Candidate policy

- One compile-time-guarded change per detached worktree/build.
- Read-only code map and focused backend correctness test before model inference.
- Preserve unchanged fallback path.
- Stop immediately on device loss, hang, invalid output, severe paging, or unrecoverable memory pressure.
- No deployment, production binary replacement, production source replacement, or systemd-unit modification without explicit approval.

## Results / rejected candidates / rollback

### Q4_K_M frozen-control results

Model: `/home/hudson/EmeryChat/models/qwen38-bartowski/Qwen3.8-27B-Q4_K_M.gguf`, 17,442,399,968 bytes; llama.cpp reports 16.23 GiB and 27.32B parameters. Build: detached unchanged worktree `/home/hudson/llama.cpp-experiments/qwen38-b580-control`, build `/home/hudson/llama.cpp-build-qwen38-b580-control`, commit `9f31776c3773cf03f98535c19b7e6d394af374b4`. Device: one `level_zero:gpu:0`, Intel Arc B580 Graphics, driver `1.15.38308+4`, architecture `intel_gpu_bmg_g21`.

Fixed runtime flags: `--device SYCL0 --split-mode none --main-gpu 0 --ctx-size 4096 --batch-size 512 --ubatch-size 256 --threads 16 --threads-batch 16 --cache-type-k q4_0 --cache-type-v q4_0 --flash-attn auto --gpu-layers N`, `GGML_SYCL_ENABLE_GRAPH=0`, `GGML_SYCL_ENABLE_MKL_FA=1`, three repetitions for the initial control and two for the layer sweep.

| GPU layers | Prefill, 512 tok | Decode, 64 tok | Notes |
|---:|---:|---:|---|
| 8 | 32.146 ± 0.467 tok/s | 2.548 ± 0.036 tok/s | stable |
| 16 | 35.356 ± 2.089 tok/s | 2.892 ± 0.028 tok/s | initial control also measured 128-token prefill 18.256 ± 0.452 and 512-token prefill 36.038 ± 1.974; decode 2.809 ± 0.187 |
| 24 | 42.029 ± 0.676 tok/s | 3.263 ± 0.019 tok/s | stable |
| 32 | 49.900 ± 0.417 tok/s | 3.906 ± 0.009 tok/s | stable |
| 40 | 61.106 ± 0.190 tok/s | 4.735 ± 0.014 tok/s | current leader; no allocation/paging failure |

Peak host RSS was approximately 17.5 GiB across these runs; `/usr/bin/time -v` reported no swap faults. The 40-layer CLI correctness run produced deterministic `4` for the prompt `What is 2+2? Reply with exactly one integer.`

Detailed 40-layer residency at 4096 context:

- 866 GGUF tensors: F32 456, Q4_0 8, Q8_0 64, Q4_K 257, Q5_K 2, Q6_K 79.
- `n_layer=64`, `n_layer_all=65` (one unused MTP layer), `n_expert=0`, `n_expert_used=0`.
- Model buffers: SYCL0 9,366.61 MiB; CPU-mapped 7,029.36 MiB.
- q4_0 KV: 72.00 MiB total, 16 full-attention layers, 4096 cells.
- Recurrent state: 149.62 MiB total, including SYCL0 87.28 MiB and CPU 62.34 MiB.
- Compute reservation: SYCL0 150.79 MiB; host 17.39 MiB.
- llama.cpp memory fit projected 9,649 MiB device use against 12,004 MiB free and retained a 2,354 MiB margin.

Production rollback status: the Ornith service was paused only for the Q4_K_M B580 runtime phase and restored afterward; `systemctl --user is-active llama-ornith.service` is `active` and `http://127.0.0.1:8081/health` returns `{"status":"ok"}`. No experimental binary, source, kernel, quant, or unit was deployed.

### Q4_K_M interactive server smoke test

Using the same detached control build and Intel Arc B580 device, with the candidate server isolated on `127.0.0.1:18081`, 40 GPU layers, context 4096, q4_0 KV, batch 512/ubatch 256, and deterministic temperature/seed:

- Cold 54-token prompt: 7.490 s total; server timing 4.289 s prompt evaluation plus 3.193 s generation for 16 tokens.
- Same prompt with prompt cache reuse: 3.522 s total; 0.360 s prompt evaluation for the 4 uncached tokens plus 3.160 s generation.
- Appended prompt: 3.890 s total; 0.717 s prompt evaluation for 12 tokens plus 3.172 s generation.
- Repeated non-streaming outputs were byte-stable for the same prompt/seed. The `/completion` smoke test emitted a `<think>` section; this is endpoint formatting, not a correctness failure. The separate CLI arithmetic check returned exactly `4`.

### Pending quantification

`Qwen3.8-27B-Q4_K_S.gguf` is complete at 16,363,513,568 bytes. A smaller full-residency candidate, `Qwen3.8-27B-IQ2_M.gguf` (10,522,248,928 bytes), is now being downloaded to establish an actual sub-12-GB model-buffer case. The remaining requested Q3/Q4 IQ family files, KV-cache comparison, batch/thread/parallel matrix, and any detached SYCL kernel candidate remain unrun and must not be represented as measured results.

### MTP comparison on Q4_K_M

The Q4_K_M GGUF contains one embedded `nextn`/MTP layer (`qwen35.nextn_predict_layers = 1`); current `src/models/qwen35.cpp` loads it only when the MTP context is requested. With the same 40-layer, q4_0-KV, 4096-context B580 server configuration, `--spec-type draft-mtp --spec-draft-n-max 1` initialized successfully and shared the target weights. A deterministic 32-token continuation produced the same text as the non-MTP server. MTP reported 14 accepted of 16 draft tokens and 7.91 tok/s decode on one cold request (5.096 s total); the matched non-MTP server measured 4.73-4.76 tok/s decode on warm requests (6.877-6.922 s total) and 7.43 tok/s cold (7.425 s total). This is promising but needs repeated cold/warm MTP runs before adoption; no deployment occurred.

### Q4_K_S layer follow-up

At 48 GPU layers, Q4_K_S completed on the verified Intel Arc B580: 77.14 ± 22.43 tok/s prompt processing and 6.74 ± 0.03 tok/s decode over two repetitions at prompt 512/generation 64, q4_0 KV, batch 512/ubatch 256. The 56-layer attempt produced no benchmark output after several minutes and was interrupted as a memory-pressure/initialization hang candidate; no device loss occurred.

The three-repetition rerun at 48 layers measured prompt samples 87.8643, 76.0802, 87.2017 tok/s (mean 83.7154, standard deviation 6.6206) and decode samples 6.80962, 6.70787, 6.72212 tok/s (mean 6.7465, standard deviation 0.0551). The Q4_K_S GGUF is 16,363,513,568 bytes on disk; llama.cpp reports 15.23 GiB and 27.32B parameters.

### Full-residency IQ2_M candidate

Source file: `/home/hudson/EmeryChat/models/qwen38-bartowski/Qwen3.8-27B-IQ2_M.gguf`, 10,522,248,928 bytes; llama.cpp reports 10,511,253,504 bytes model size and 2.7 bpw. With `--gpu-layers 999`, `--split-mode none`, q4_0 KV, context 4096, batch 512/ubatch 256, 16 threads, and the same verified Intel Arc B580/SYCL environment, all 65 model layers were resident on SYCL0. Three repetitions measured 231.789/231.860/231.866 tok/s prompt processing (mean 231.838 ± 0.043) and 12.7518/12.7550/12.7583 tok/s decode (mean 12.7550 ± 0.0032). Peak host RSS was 11,403,204 KB; no swap or device-loss event occurred. A deterministic CLI smoke test returned `4` for the arithmetic prompt, with 28.73 tok/s prompt processing and 12.62 tok/s generation in that single run.

This is the first tested quant that fits as a full model-buffer residency candidate on the 12-GB B580. It is a quality/size tradeoff and should not replace Q4_K_M without an evaluation set; it is the best current low-memory performance baseline.

### KV-cache comparison on full-residency IQ2_M

At context 4096, 512/256 batching, all layers on SYCL0, and two repetitions per case:

| K/V type | Prompt tok/s | Decode tok/s |
|---|---:|---:|
| q4_0 | 231.80 ± 0.03 | 12.76 ± 0.00 |
| q5_0 | 231.66 ± 0.01 | 12.72 ± 0.00 |
| q8_0 | 231.56 ± 0.32 | 12.74 ± 0.00 |
| f16 | 231.92 ± 0.06 | 12.79 ± 0.01 |

The differences are within the run-to-run noise at this short context; q4_0 remains the practical default because it minimizes context memory. Longer-context KV pressure is still pending.
