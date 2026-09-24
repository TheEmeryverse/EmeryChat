# Ornith 1.5 9B on Intel Arc B580 — active plan

## Scope

- Active target: `ornith-ai/Ornith-1.5-9B-GGUF`, beginning with `Ornith-1.5-9B-Q4_K_M.gguf`.
- Qwen3.8 artifacts were removed at the user’s request; historical Qwen logs remain outside the model directory for audit only.
- All runtime tests use Intel Arc B580 through `ONEAPI_DEVICE_SELECTOR=level_zero:gpu`, `CUDA_VISIBLE_DEVICES=`, llama.cpp `SYCL0`, and `--split-mode none`.
- Production remains `/home/hudson/.config/systemd/user/llama-ornith.service` on port 8081. No experimental binary, model, source, or unit is deployed.

## Exact artifacts

- Model: `/home/hudson/EmeryChat/models/ornith15-9b/Ornith-1.5-9B-Q4_K_M.gguf`.
- Model size: 5,780,090,816 bytes.
- Model SHA-256: `70c112196e0b7023803c9762752e46d29e612a92c83f995bc3ba1ceb07e8fab6`.
- Build worktree: `/home/hudson/llama.cpp-experiments/ornith15-9b-control`.
- Build directory: `/home/hudson/llama.cpp-build-ornith15-9b-control-make`.
- llama.cpp commit: `9f31776c3773cf03f98535c19b7e6d394af374b4`.
- Candidate binary hashes: `llama-bench` `cf210ef3dd1e5f1b2a66a4957f3d044f33c409fdb6e1909a88c94e9f46e2b609`; `llama-server` `e861eacaf6271db9e6ba262890fdd27922b6d6affb15dca73391f2a6e3f0051a`.

## Device/build verification

Every test is preceded by `sycl-ls --verbose` with the Level Zero GPU selector and records Intel Arc B580 Graphics, driver `1.15.38308+4`, architecture `intel_gpu_bmg_g21`, subgroups 16/32. Candidate build uses IntelLLVM 2025.3.2, `GGML_SYCL=ON`, Intel target, F16, graph, Level Zero API, host fallback, oneMKL SYCL BLAS, and oneDNN disabled because no DNNL package was found.

Relevant source/docs: `/home/hudson/llama.cpp/docs/backend/SYCL.md`, `/home/hudson/llama.cpp/ggml/src/ggml-sycl/ggml-sycl.cpp`, `/home/hudson/llama.cpp/ggml/src/ggml-sycl/CMakeLists.txt`, `/home/hudson/llama.cpp/tools/llama-bench/README.md`, `/home/hudson/llama.cpp/tools/server/README.md`, and the [official Ornith GGUF repository](https://huggingface.co/ornith-ai/Ornith-1.5-9B-GGUF).

## Frozen Q4_K_M control

Flags: `--gpu-layers 999 --device SYCL0 --split-mode none --main-gpu 0 --cache-type-k q4_0 --cache-type-v q4_0 --batch-size 512 --ubatch-size 256 --threads 16 --flash-attn auto`, graph disabled for runtime comparability, MKL flash attention enabled.

At prompt 128/512 and generation 64, three repetitions:

| Workload | Mean ± standard deviation | Samples |
|---|---:|---|
| Prompt 128 | 823.26 ± 0.98 tok/s | 822.14, 823.87, 823.78 |
| Prompt 512 | 1,399.53 ± 0.50 tok/s | 1,399.10, 1,399.42, 1,400.07 |
| Decode 64 | 63.24 ± 0.02 tok/s | 63.27, 63.23, 63.23 |

Peak host RSS: 6,775,696 KB. The model was fully resident with `--gpu-layers 999`; no device loss, paging failure, or invalid output occurred.

## Isolated server control

Candidate server used port `18084`, context 8192, one slot, q4_0 KV, batch 512/ubatch 256, 16 threads, and prompt caching. Load logs showed `qwen35` 9B, 34 total loaded layers, SYCL0 model buffer 4,812.25 MiB, CPU-mapped buffer 545.62 MiB, 72 MiB q4_0 KV, 50.25 MiB recurrent state, and 64.27 MiB compute reservation. Shutdown reported 6,928 MiB free on the 12,216 MiB SYCL device allocation after model/context/compute usage.

The raw completion endpoint was deterministic but not a valid chat-format correctness test: it began with `4` and then continued with prompt-like text. The `/v1/chat/completions` endpoint used the native `peg-native` chat format and returned exactly `4` in all three deterministic repetitions. Response wall times were 0.234, 0.145, and 0.143 seconds. Warm prompt-cache requests reused the 9-token checkpoint; the first chat request evaluated 13 prompt tokens at 65.96 tok/s and 16 generated tokens at 61.55 tok/s, while warm requests evaluated 4 new tokens at about 52 tok/s and decoded at 61.17–61.63 tok/s.

## Next experiments

1. Isolated server correctness, cold/warm prompt-cache reuse, first-token and total latency.
2. Context depths 2k/8k/16k with q4_0 KV.
3. KV types q4_0/q5_0/q8_0/F16 where memory permits.
4. Q4_K_S, IQ4_XS, Q5_K_M or Q8_0 quality comparisons if requested.
5. MTP only if the 9B GGUF contains a native head; verify metadata before enabling.
6. Batch/ubatch/thread sweeps and any backend candidate only in separate detached worktrees with focused correctness tests.

## Paused optimization pass — 2026-09-20

The user asked to pause before completing the Q4_K_M 128k run. The run was
stopped after 6:27.76 wall-clock seconds of the benchmark process with no
device-loss, paging, or allocator error; it produced no completed throughput
sample and is recorded as aborted, not as a result. Production remains
stopped by explicit user instruction.

Completed Q4_K_M tuning checks at context depth 8192, q4_0 K/V, batch 512,
ubatch 256 unless noted, on the verified Intel Arc B580:

| Candidate | Prompt pp512 | Decode tg64 | Decision |
|---|---:|---:|---|
| 16 threads, graph off, MKL FA on | 907.18 +/- 16.11 | 53.31 +/- 0.03 | control |
| 24 threads, graph off, MKL FA on | 910.13 +/- 16.84 | 53.43 +/- 0.02 | reject: no material gain |
| 32 threads, graph off, MKL FA on | 910.50 +/- 16.47 | 53.45 +/- 0.01 | reject: no material gain |
| 16 threads, graph on, MKL FA on | 912.45 +/- 17.91 | 53.34 +/- 0.06 | neutral within spread |
| 16 threads, batch 256/ubatch 128 | 534.88 +/- 2.49 | 53.52 +/- 0.02 | reject: prefill regression |
| 16 threads, MKL FA off | 873.32 +/- 0.94 | 53.42 +/- 0.02 | reject: prefill regression |

The 1024/512 run was materially CPU-heavy and exceeded the control's normal
runtime; its output was not captured as a completed sample and it is rejected
pending a rerun only if needed. `--flash-attn off` failed context creation for
this model/build, so it was not treated as a performance result. The current
Q4 recommendation remains 16 threads, batch/ubatch 512/256, MKL flash
attention enabled, graph disabled for the frozen comparison.

## 9B versus 35B status snapshot

The 9B and 35B results are not a strict apples-to-apples benchmark: the 9B
measurements below are fresh isolated Q4_K_M runs, while the 35B figures are
archived runs using a different server workload and CPU-expert placement.

| | Ornith 1.5 9B Q4_K_M | Ornith 1.5 35B A3B Q4_K_M |
|---|---|---|
| Architecture | dense `qwen35`, 9.20B params, `n_expert=0` | MoE `qwen35moe`, 35.51B total, 256 experts / 8 active |
| GGUF file | 5,780,090,816 bytes | 20.21 GiB in archived control |
| Device placement | 34/34 layers, about 4,812 MiB SYCL model buffer | 42/42 layers, about 8,809 MiB SYCL model buffer plus about 11,907 MiB CPU-mapped model buffer |
| Fresh 8k pp512 | 907.18 +/- 16.11 tok/s | not measured under this control |
| Fresh 8k tg64 | 53.31 +/- 0.03 tok/s | not measured under this control |
| Archived long-context evidence | 128k run paused before a completed sample | 57,610-token prompt: 722.85 tok/s; 7,210-token prompt: 912.70 tok/s; long-context decode about 18.36 tok/s |
| Archived short interactive median | not yet run with the same driver | about 45.0 decode / 35.2 prompt tok/s |

The practical result is that the 9B is the much better B580 fit and gives about
53 tok/s short-context decode with all weights resident on the GPU. The 35B
offers the larger MoE capacity but consumes almost the available device
budget, relies on CPU-mapped expert storage, and falls to roughly 18 tok/s at
long context. The 9B quality advantage is not claimed from these throughput
tests alone; a matched quality suite is still needed if model quality is the
decision criterion.

## Matched 9B versus 35B isolated run — 2026-09-20

Both servers used the same isolated binary/build, Intel Arc B580 SYCL0 device,
Level Zero selector, 131072-token context, one slot, q4_0 K/V, batch/ubatch
512/256, 16 threads, flash attention auto, prompt caching, seed 1718,
temperature 0, and 128 generated tokens. The 35B additionally used
`--n-cpu-moe 24`, required for its MoE weights to fit without unsafe VRAM
pressure. Device diagnostics were run immediately before each server launch.

| Prompt case | 9B cold pp / tg | 35B cold pp / tg | 9B warm tg | 35B warm tg |
|---|---:|---:|---:|---:|
| 566 tokens | 904.5 / 61.05 | 85.3 / 38.08 | 61.23 | 37.98 |
| 8,942 tokens | 1,062.4 / 51.82 | 734.6 / 36.55 | 51.82 | 36.10 |
| 35,750 tokens | 704.5 / 34.39 | 650.5 / 31.16 | 34.33 | 31.24 |

All six cold/warm cases produced the same response hash between models and
within each model. The context was configured to 131072 for every run; the
long prompt itself was 35,750 tokens, not a full 128k prompt. Raw records are
in `data/ornith-compare-9b-128k.json` and `data/ornith-compare-35b-128k.json`;
live logs are `logs/ornith-compare-9b.log` and `logs/ornith-compare-35b.log`.

## Current production-config benchmark — 35B — 2026-09-20

The production unit was inspected but remained stopped. An isolated server used
the exact production binary (`/home/hudson/llama.cpp/build-sycl-f16/bin/llama-server`,
SHA-256 `809758e705d275090d37494483bbf919aca6183e2def84d8a798e165f7997abf`)
and the unit's flags, with only host/port changed to `127.0.0.1:18093`.
Notable production settings are batch/ubatch `2048/2048`, 16/16 threads,
`--ctx-checkpoints 8`, `--checkpoint-min-step 256`, `--cache-ram 8192`, and
SYCL graph execution enabled by the production environment/build.

Against the matched isolated 512/256 benchmark, production-config prefill was
faster by 262.0% at 566 tokens, 73.9% at 8,942 tokens, and 94.2% at 35,750
tokens. Cold decode changed by +1.4%, +1.6%, and +5.4%; warm decode changed
by +12.1%, +2.8%, and -1.2%, respectively. Thus the production batch/ubatch
configuration is a real prefill improvement, while decode is effectively
neutral except for normal run variance.

Raw results: `data/ornith-prodconfig-35b-128k.json`. Live log:
`logs/ornith-prodconfig-35b.log`. Device diagnostic:
`logs/device-prodconfig-35b.log`.

The same production-config pass was then run against the 9B, without the
35B-only CPU-MoE flag. Relative to the 35B production configuration, the 9B
was faster by 276.2%, 69.0%, and 27.0% in cold prefill for the 566-, 8,942-,
and 35,750-token prompts. Cold decode was faster by 58.8%, 39.3%, and 4.7%;
warm decode was faster by 44.0%, 39.4%, and 11.5%.

The 9B production-config run used about 4,812 MiB of SYCL model storage,
1,202 MiB context, and 1,248 MiB compute, versus the 35B's 8,808 MiB model,
782 MiB context, and 1,078 MiB compute. Raw results are in
`data/ornith-prodconfig-9b-128k.json`; live log and device diagnostic are
`logs/ornith-prodconfig-9b.log` and `logs/device-prodconfig-9b.log`.

## 35B decode-tuning pass — 2026-09-20

### Keeper rule for this pass

The user-defined advancement rule is now authoritative:

- keep a candidate with at least 2% improvement in one axis and no loss in
  the other;
- keep a candidate with at least 5% improvement and less than 2% loss in the
  other axis;
- keep a candidate with at least 10% improvement and no more than 5% loss in
  the other axis.

The axes are prompt/prefill throughput and decode throughput. Decisions are
recorded per context profile (short, medium, long) and separately for cold
and warm/cache-reused turns. A candidate that fails the long-context profile
is not a global 128k keeper, even if it is retained as a short/medium profile
candidate for the next pass.

All candidates below used the isolated production-equivalent 35B Q4_K_M
server, 131072-token context, q4_0 K/V, batch/ubatch 2048/2048, 16/16
threads, SYCL0 Level Zero on the verified Intel Arc B580, and the same three
deterministic prompt lengths (566, 8,942, and 35,750 tokens). Each candidate
was stopped after its run; production remains inactive. No candidate was
deployed.

| Candidate | 566 cold pp/tg | 8,942 cold pp/tg | 35,750 cold pp/tg | Decision |
|---|---:|---:|---:|---|
| control, `--n-cpu-moe 24` | 308.71 / 38.62 | 1,277.59 / 37.14 | 1,262.95 / 32.86 | frozen baseline |
| `--n-cpu-moe 25` | 497.83 / 37.34 | 1,411.22 / 35.69 | 1,122.16 / 30.58 | reject: decode regression at all lengths; long prefill lower |
| `--n-cpu-moe 26` | 468.21 / 35.33 | 1,425.52 / 31.70 | 1,254.62 / 32.52 | reject: large decode regression |
| `--threads 24`, batch threads 16 | 522.83 / 33.03 | 1,450.01 / 22.26 | 1,268.13 / 31.00 | reject: severe short/medium decode regression |
| `GGML_SYCL_PRIORITIZE_DMMV=1` | 534.01 / 35.81 | 1,490.01 / 37.40 | 1,260.46 / 32.60 | hold for repeat: mixed decode, prefill improved |

The DMMV result improved prompt prefill by 73.0%, 16.6%, and approximately
0.2% lower at the three lengths, while cold decode changed by -7.3%, +0.7%,
and -0.8%. Warm decode changed by -1.6%, +9.7%, and +1.6%. Its output hash
matched the control and it showed no device-loss or paging error, but the
short-context decode regression means it is not yet a keep.

### Read-only SYCL/backend audit

The three parallel source audits used commit `9f31776c3`, made no edits, and
ran no inference. The active 35B path is fused MoE `MUL_MAT_ID` with Q8_1
activations and specialized MMVQ kernels:

- Architecture tensors: `src/llama-arch.cpp:896-902`,
  `src/models/qwen35moe.cpp:494-512`.
- Fused single-sequence MoE dispatch: `ggml/src/ggml-sycl/ggml-sycl.cpp:5003-5060`.
- MMVQ workgroup shape and `GGML_SYCL_MMV_Y=1`:
  `ggml/src/ggml-sycl/mmvq.cpp:2704-2772`,
  `ggml/src/ggml-sycl/common.hpp:108-114`.
- Dense Q4_K decode dispatch and ESIMD DMMV:
  `ggml/src/ggml-sycl/ggml-sycl.cpp:4775-4823`,
  `ggml/src/ggml-sycl/dmmv.cpp:1958-1973`,
  `ggml/src/ggml-sycl/esimd.hpp:10`.
- Graph capture rejects `MUL_MAT_ID`:
  `ggml/src/ggml-sycl/ggml-sycl.cpp:6051-6089`.
- oneMKL flash attention is prefill-oriented; decode uses native TILE/VEC:
  `ggml/src/ggml-sycl/fattn.cpp:132-175`, `:250-270`.
- Host expert placement and transfers:
  `common/arg.cpp:2755-2771`, `src/llama-model-loader.cpp:1227-1256`,
  `src/llama-model.cpp:1712-1801`, and scheduler transfer logic in
  `ggml/src/ggml-backend.cpp:1690-1753`.
- SYCL offload threshold: `ggml/src/ggml-sycl/ggml-sycl.cpp:6721-6738`;
  backend threshold use: `ggml/src/ggml-backend.cpp:951-980`.
- MMQ remains disabled because of the backend accuracy guard:
  `ggml/src/ggml-sycl/ggml-sycl.cpp:3984-3988`.
- Production build options and oneDNN absence:
  `build-sycl-f16/CMakeCache.txt:309`,
  `ggml/src/ggml-sycl/CMakeLists.txt:108-184`.

The audits indicate that increasing `--n-cpu-moe` trades VRAM residency for
per-token host-to-device transfers and synchronization, and increasing
`--threads` mostly adds CPU scheduling overhead because SYCL GPU work does not
use that thread count. The next runtime-only candidate is
`GGML_OP_OFFLOAD_MIN_BATCH`, tested one value at a time with prefill threads
fixed at 16. A candidate is retained only after repeatable warm-decode gain,
stable output, no paging/device loss, and no material prefill regression.

### Offload-threshold sweep

The threshold-16 and threshold-8 runs used three warm repetitions per prompt
length. Both preserved the deterministic response hash and completed without
device loss or paging, but both harmed decode enough to reject them despite
their prefill gains:

| Candidate | Prefill change vs control (short / medium / long) | Warm decode change vs control (short / medium / long) | Decision |
|---|---:|---:|---|
| `GGML_OP_OFFLOAD_MIN_BATCH=16` | +74.9% / +17.5% / -0.6% | -11.0% / +0.0% / -0.3% | reject: short decode regression |
| `GGML_OP_OFFLOAD_MIN_BATCH=8` | +72.7% / +17.5% / -0.7% | -8.1% / +0.0% / -3.2% | reject: short and long decode regression |

The values increased prompt throughput by changing scheduler/offload behavior,
but did not preserve interactive decode. Threshold 1 remains the final value
in this runtime sweep before moving to the isolated ESIMD/MMV kernel candidates.

For end-to-end latency, threshold 8 is more favorable than the decode-only
table suggests: cold total time changed from 5.122 to 4.692 seconds at the
short prompt (+8.4% faster), 10.418 to 9.388 seconds at medium (+9.9%), and
32.172 to 32.721 seconds at long (-1.7%). Warm/cache-reused total time changed
from 3.100 to 3.379 seconds (-9.0%), 3.550 to 3.549 seconds (neutral), and
4.252 to 4.424 seconds (-4.0%). It is therefore a provisional workload-specific
candidate for uncached short/medium turns, not a universal interactive keep.

Threshold 1 was stopped after short and medium cold/warm samples because it
dropped decode to 16.7–20.1 tok/s and emitted a repeated-slash response. It is
rejected for severe latency and output instability; its partial log is
`logs/ornith-decode-offload1-35b.log` and its device diagnostic is
`logs/device-decode-offload1-35b.log`.

### ESIMD-off runtime A/B

`GGML_SYCL_ENABLE_ESIMD=0` was tested against the threshold-32 control on the
same build and device. It improved cold prefill to 540.3 / 1,501.8 / 1,260.2
tok/s versus 308.7 / 1,277.6 / 1,263.0, but it reduced warm decode to a
median 36.0 / 35.8 / 29.5 tok/s versus 42.6 / 37.1 / 30.9. Warm total
latency became 3.658 / 3.677 / 4.442 seconds versus 3.100 / 3.550 / 4.252.
It is rejected because the prefill gain does not offset the cached-turn
decode loss. Logs are `logs/ornith-decode-esimd0-35b.log` and
`logs/device-decode-esimd0-35b.log`; the raw record is
`data/ornith-decode-esimd0-35b-128k.json`.

### Keeper ledger — updated during the pass

This ledger applies the user-defined thresholds above. “Profile keeper” means
the candidate qualifies for that context/profile and advances to the next
tuning pass for that profile; it does not mean deployment.

| Candidate | Qualified profile(s) so far | Blocker / next check |
|---|---|---|
| `GGML_SYCL_PRIORITIZE_DMMV=1` | Medium cold/warm; short warm is within the 5% loss rule | Repeat with varied prompts; long is not yet a keeper |
| `GGML_OP_OFFLOAD_MIN_BATCH=8` | Medium cold | Short decode loss exceeds 5%; warm turns regress; validate varied prompts |
| `GGML_OP_OFFLOAD_MIN_BATCH=16` | Medium cold | Short decode loss exceeds 5%; warm turns regress; validate varied prompts |
| `GGML_SYCL_ENABLE_ESIMD=0` | Short/medium cold only | Warm decode regresses; long profile fails |
| `--threads 8`, batch threads 16 | Short/medium preliminary only | Long context collapsed to about 4 tok/s warm; global rejection |
| `--threads 12`, batch threads 16 | None | Aborted: first short prefill was about 56 tok/s; no completed result |
| `K_QUANTS_PER_ITERATION=2` | Short cold; medium warm | Long profile fails; tune only as a profile candidate |
| BMG AOT `bmg_g21` | Global 128k | Next pass should combine/tune with surviving runtime candidates |
| Graph single-token `MUL_MAT_ID` | None | Correctness passed, but server initialization stalled; rejected |

Additional runtime sweep results:

- `--n-cpu-moe 23` projected 9,240 MiB of SYCL model storage and produced
  only 57.8 prompt tok/s and 7.0 decode tok/s on the first 566-token sample;
  it was stopped and rejected for memory-pressure behavior.
- `--n-cpu-moe 27` projected 7,447 MiB of SYCL model storage but produced
  only 43.8 prompt tok/s and 27.3 cold decode tok/s, with about 31.5 tok/s
  warm decode; it was stopped and rejected.
- `--threads 8` produced promising short/medium samples but collapsed at
  35,750 tokens to about 24.6 cold and 3.9 warm decode tok/s; it is not a
  global keeper.
- `--threads 12` was aborted after a 566-token prefill sample at about 56
  tok/s; no completed JSON result was written.

### Varied-prompt validation

The five-prompt varied driver was run against the unchanged control, DMMV,
offload threshold 8, and offload threshold 16. Each used cold and warm
repetitions and the same Level Zero B580 verification.

| Candidate | Cold median pp / decode / wall | Warm median pp / decode / wall | Hash stability | Decision |
|---|---:|---:|---|---|
| control | 77.45 / 36.53 / 3.810s | 33.21 / 31.86 / 4.187s | stable | reference |
| DMMV | 79.81 / 36.41 / 3.791s | 30.73 / 38.37 / 3.539s | stable | warm-turn keeper; repeat with long context |
| offload 8 | 50.86 / 38.49 / 3.778s | 30.49 / 39.48 / 3.442s | unstable | reject: cold/warm hash mismatch |
| offload 16 | 66.30 / 38.05 / 3.740s | 30.51 / 37.53 / 3.607s | stable | warm-turn keeper; cold prefill tradeoff |

The warm-turn keeper decisions use absolute total-turn improvement as the
user requested: DMMV improved median wall time by about 15.5%, and threshold
16 by about 13.8%, while the warm prompt portion is only four tokens and its
small timing change is not a meaningful prefill workload. Both still require
long-context validation in the next pass.

### Compile-time candidate results

The isolated BMG AOT candidate was built at commit
`9f31776c3773cf03f98535c19b7e6d394af374b4` with
`GGML_SYCL_DEVICE_ARCH=bmg_g21`, IntelLLVM `icpx/icx`, SYCL, Level Zero, and
CUDA off. Its backend suite passed `2039/2039` for `MUL_MAT` and `MUL_MAT_ID`
on SYCL0. It improved the production-equivalent control as follows:

| Context | Control cold pp/decode | AOT cold pp/decode | AOT warm decode |
|---|---:|---:|---:|
| 566 tokens | 308.7 / 38.6 | 537.9 / 41.1 | 44.7 median |
| 8,942 tokens | 1,277.6 / 37.1 | 1,511.8 / 42.3 | 41.7 median |
| 35,750 tokens | 1,263.0 / 32.9 | 1,266.4 / 33.2 | 32.6 median |

The AOT candidate qualifies as a global keeper under the user’s rule: it
improves prefill or decode at every measured context without a material loss
in the other axis. It is isolated only; no deployment approval has been
requested or assumed. Build and raw result paths are recorded in the next
experiment artifacts:
`/home/hudson/llama.cpp-experiments/ornith-bmg-aot-9f31776c`,
`/home/hudson/llama-builds/ornith-bmg-aot-9f31776c-make`, and
`data/ornith-bench-aot-35b-128k.json`.

The B580 attention VEC candidate passed the narrowed decode-shaped correctness
suite (`327/327` plus FA vector slice tests), but failed performance badly:
long decode fell to `15.9` cold and `17.9` warm tok/s. It is rejected. The
MMV_Y=2 and MMV_Y=4 candidates were both stopped at the same q4_K fused
`MUL_MAT_ID_FUSION` correctness case after several minutes without progress;
neither is eligible for inference. Their isolated worktrees and builds remain
available for diagnosis but are not keepers.

The K_QUANTS_PER_ITERATION=2 candidate was built in the isolated worktree
`/home/hudson/llama.cpp-experiments/ornith35b-kqpi2-9f31776c` and build
directory `/home/hudson/llama.cpp-builds/ornith35b-kqpi2-9f31776c`. Its focused
runtime result was stable and repeatable: short cold prefill/decode
`444.81/40.42`, medium `1047.48/40.82`, and long `931.56/30.47` tok/s; warm
decode medians were `44.63/38.88/29.58` tok/s. Relative to control, it is a
short-context keeper (both cold axes improve) and a warm-medium profile keeper,
but it fails the long profile with -26.2% cold prefill and -7.3% cold decode.
Raw data and logs are `data/ornith-bench-kqpi2-35b-128k.json`,
`logs/ornith-bench-kqpi2-35b.log`, and
`logs/ornith-bench-kqpi2-35b-benchmark.log`.

The isolated graph single-token `MUL_MAT_ID` candidate was prepared in
`/home/hudson/llama.cpp-experiments/b580-sycl-graph-matid-vec` and built in
`/home/hudson/llama.cpp-build-b580-sycl-graph-matid-vec`. Its guarded seam is
`ggml/src/ggml-sycl/ggml-sycl.cpp:6050-6135`; it admits only single-token,
F32-activation fused-MoE shapes and preserves the existing host-wait fallback
for all other shapes. The focused backend suite passed `2039/2039` on the
verified B580, but the 35B server stalled after thread-pool initialization
before reporting model loaded/listening. It was stopped under the hang-safety
rule and is rejected pending code diagnosis; no inference result was recorded.
Correctness artifacts are `logs/device-correctness-graph-matid.log` and
`logs/correctness-graph-matid.log`.

The readable rolling dashboard is
`/home/hudson/EmeryChat/benchmark-results-ornith35b.md`; it is updated with
each candidate's raw paths, percentages, and keeper status.

### Combination pass — AOT, DMMV, and offload threshold

The combination pass held the production-equivalent model, 131072 context,
q4_0 KV, 2048/2048 batching, 16/16 threads, deterministic prompts, and
Level Zero B580 verification constant. It restarted the isolated server for
each environment combination so the backend latched each variable during
initialization. All response hashes were stable and all four runs completed
without device loss or paging.

Relative to the frozen stock control, cold prompt/decode and warm decode
median changes were:

| Candidate | Short cold pp/decode; warm decode | Medium cold pp/decode; warm decode | Long cold pp/decode; warm decode | Decision |
|---|---|---|---|---|
| AOT alone | +74.2%/+6.4%; +5.0% | +18.3%/+13.9%; +12.4% | +0.3%/+1.2%; +5.7% | Global keeper |
| AOT + DMMV | +71.8%/-7.3%; -13.3% | +17.9%/-2.2%; -1.1% | -0.2%/-5.4%; -2.4% | Medium keeper only |
| AOT + offload-16 | +67.5%/-8.7%; -4.8% | +14.2%/+8.3%; -3.4% | -3.1%/-4.4%; +7.1% | Medium keeper only |
| DMMV + offload-16 | +64.1%/-13.9%; -25.5% | +17.0%/+11.9%; +4.7% | -1.5%/-6.2%; -0.6% | Medium keeper only; short warm outlier |
| AOT + DMMV + offload-16 | +66.2%/-10.7%; -12.3% | +17.5%/-0.8%; -1.1% | -1.1%/-5.8%; -2.0% | Medium keeper only |

The combination result confirms that the BMG AOT target is the strongest
global candidate. DMMV priority and the offload threshold affect runtime
dispatch/placement independently of the AOT device image, but the backend
audit found that DMMV does not improve fused MoE `MUL_MAT_ID`, while threshold
16 can move low-batch work to CPU. Those interactions explain the medium
prefill gains and decode losses. Relevant sources are
`ggml/src/ggml-sycl/ggml-sycl.cpp:340-350,4775-4849,6721-6738,7115-7131`,
`ggml/src/ggml-backend.cpp:951-979`, and
`ggml/src/ggml-sycl/CMakeLists.txt:201-223` in the isolated AOT worktree.

Combination raw records are in
`data/ornith-combo-aot-dmmv-35b-128k.json`,
`data/ornith-combo-aot-offload16-35b-128k.json`,
`data/ornith-combo-dmmv-offload16-35b-128k.json`, and
`data/ornith-combo-aot-dmmv-offload16-35b-128k.json`, with matching server,
benchmark, and device-diagnostic logs under `logs/`.

### Comparison correction

The earlier AOT-vs-stock percentages referred to per-axis throughput, not
total request latency. The raw records show AOT cold prompt throughput changes
of approximately `+74.2%/+18.3%/+0.3%` and cold decode changes of
`+6.4%/+13.9%/+1.2%` at short/medium/long contexts, but total cold wall-time
improves by only `19.4%/14.6%/0.5%`. Warm wall-time changes are approximately
`4.5%/11.0%/6.1%` faster using the two AOT warm samples. The stock file has
one warm repetition while the AOT file has two, so warm figures are provisional;
the cold comparison is directly matched.

This distinction is now reflected in
`/home/hudson/EmeryChat/benchmark-results-ornith35b.md` and should govern any
future production claim: report prefill, decode, and end-to-end latency as
separate metrics.

### Production promotion — 2026-09-20

The user approved promoting AOT alone. The original production unit and stock
binary were preserved unchanged and remain inactive for rollback. A separate
unit was created and enabled:

- Unit: `/home/hudson/.config/systemd/user/llama-ornith-aot.service`
- Binary: `/home/hudson/llama-builds/ornith-bmg-aot-9f31776c-make/bin/llama-server`
- Binary SHA256: `93b977791bdce09e285be0a6dfe70ef8d7bcfffc4a4c0ce654258594d545cf4d`
- Port/model/flags: unchanged from stock production configuration
- Runtime additions: `ONEAPI_DEVICE_SELECTOR=level_zero:gpu` and
  `GGML_SYCL_ENABLE_MKL_FA=1`; no DMMV or offload-threshold overrides
- Device verification: Intel Arc B580 Graphics, `intel_gpu_bmg_g21`, Level Zero
  in `logs/device-production-aot.log` and
  `logs/device-production-aot-smoke2.log`
- Health: active on port 8081; completed smoke response `READY.` with EOS

Rollback is `systemctl --user disable --now llama-ornith-aot.service` followed
by `systemctl --user enable --now llama-ornith.service`; the stock unit was not
modified.

The keeper ledger is intentionally profile-specific. The next pass will tune
the surviving profile candidates, then select a single configuration for the
full short/medium/long and cache-cold/cache-warm validation.
