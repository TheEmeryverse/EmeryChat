# Ternary Bonsai 2 27B PQ2_0 — Prism/SYCL B580 Benchmark Plan

Status: **Phase 1 executed 2026-09-21; terminal PQ2_0/SYCL compatibility failure. No stock throughput or tuning candidate was run.**

This is the live report for the requested target. When benchmarking is later authorized and individual runs complete, append verified results to this same file. Do not replace the target or reinterpret rejected runs as B580 results.

## Scope and immutable target

- Model repository: `prism-ml/Ternary-Bonsai-2-27B-gguf`
- Required revision: `6ed5e12`
- Required file: `Ternary-Bonsai-2-27B-PQ2_0.gguf`
- Required size: `7,206,168,928` bytes
- Required SHA-256: `3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1`
- Expected format: Prism-private PQ2_0, group-128, approximately 2.13 bpw
- Expected architecture facts: Qwen3.8-27B-derived, 64 blocks, hybrid attention, native context 262,144, embedded template, text-only, no vision projector

Never substitute PTQ1_0, TQ2_0, standard Q2_0_g64, another revision, another model, or CPU-only execution.

### Required Prism executable

- Checkout: `/home/hudson/llama.cpp-prism-bonsai2-experimental`
- Branch/HEAD: `prism` / `5d80cff` (`prism-b10687-5d80cff`)
- Server: `/home/hudson/llama.cpp-prism-bonsai2-experimental/build-sycl-icx/bin/llama-server`
- Required server SHA-256: `67696458bd4e85cd8f0991698b10d09755309d7d2a41729ce43779a664f7d65f`

The established production/mainline binary is not a valid candidate binary.

## Authorization boundary

No executable test begins until the user explicitly authorizes benchmarking. The main orchestrator owns coordination. It must stop the production model only immediately before an approved test window and must restore it immediately after testing or an abort.

Production currently owns the B580 and serves port `8081`. The candidate must use a separate localhost port, proposed here as `18081`; it must never bind `8081`.

## Stage 0 — read-only preparation

Before approval, only this report may be created or edited. Do not download the model, build or modify source, edit configuration, start or stop a server, probe a GPU, or run a benchmark.

The later execution plan uses the existing local benchmark conventions and Prism/SYCL documentation, especially `tools/llama-bench/README.md`, `tools/server/README.md`, `docs/backend/SYCL.md`, `scripts/benchmark_mtp.py`, and `scripts/benchmark_chat.py`.

## Stage 1 — preflight gates

All gates below must pass before any throughput measurement.

### 1. Artifact integrity and metadata

Verify and record the exact filename, source revision, byte size, SHA-256, GGUF metadata, architecture, block count, context metadata, embedded template, and absence of a vision projector.

The tensor inventory must identify the target weights as PQ2_0/group-128. Any loader rejection, unknown type, silent remapping, or appearance of PTQ1_0/TQ2_0/Q2_0_g64 is a terminal target failure.

### 2. Exact binary

Verify the server hash, Prism branch/HEAD, linked runtime identity, and complete command line. A hash mismatch, mainline executable, rebuilt binary, or unverified binary fails the gate.

### 3. B580-only device inventory

Every executable test uses:

```text
ONEAPI_DEVICE_SELECTOR=level_zero:0
CUDA_VISIBLE_DEVICES=
```

The selected llama.cpp inventory must contain exactly:

```text
SYCL0: Intel(R) Arc(TM) B580 Graphics
```

Require Level Zero execution, no NVIDIA device, no other GPU, no extra selected accelerator, and no unexpected backend. Never use `--split-mode layer`, `row`, or `tensor`; the required mode is `--split-mode none` with `--main-gpu 0`.

### 4. Model placement and memory

The stock load uses `--gpu-layers all`, `--fit off`, `--kv-offload`, and `--parallel 1`. Load logs must show the PQ2_0 model executing on SYCL0, with no CPU-offloaded PQ2_0 weights and no host-memory fallback hiding VRAM overflow.

Require at least 1 GiB of B580 headroom after model, context, KV, graph, compute, and allocator reservations. Record model-buffer placement, VRAM usage/free memory, CPU-offloaded tensor bytes, host RSS, swap/page faults, and load time.

Any OOM, allocator failure, paging pressure, CPU fallback, host fallback, unsupported operation, device loss, or Level Zero/driver error rejects the profile. If only CPU execution works, record `NON-TARGET / REJECTED` and stop; do not produce a B580 result.

### 5. One-token correctness

Before throughput, run three deterministic one-token checks using the embedded template, temperature `0`, and a fixed seed. Use a prompt equivalent to:

```text
What is 2+2? Reply with exactly one integer.
```

Expected output is exactly `4`; repeated output hashes must match. Any invalid output, template failure, unsupported operation, or unstable result rejects the profile.

## Stage 2 — production/service safety

Immediately before an approved execution window, the orchestrator records:

- Production service PID, unit, command line, model path, and port `8081`.
- Production health and B580 ownership.
- Candidate port availability.
- Existing device/service logs needed for rollback.

Then, and only then, it may stop production and launch the isolated candidate on `18081`. No candidate request is sent until the preflight gates pass.

During testing:

- Keep one server slot: `--parallel 1`.
- Send no concurrent requests.
- Monitor candidate stdout/stderr, model placement, memory headroom, RSS, swap/page faults, Level Zero errors, device-loss messages, and request progress.
- Do not use NVIDIA tooling or any alternate adapter.
- Do not set `SYCL_CACHE_PERSISTENT=1`.

Abort immediately on unsupported PQ2_0, CPU/host fallback, non-B580 selection, OOM, allocator/driver/device error, invalid output, a hung load/request, or any attempt to use port `8081`.

After every phase, normal completion, or abort:

1. Stop the candidate.
2. Confirm candidate resources are released.
3. Restore the original production service on `8081`.
4. Verify production health and original model identity.
5. Verify the B580 is again owned by production.

If restoration fails, stop reporting benchmark results and escalate to the orchestrator.

## Stage 3 — stock baseline first

The stock baseline is mandatory and is run before any candidate. It is not run until explicit approval.

### Stock runtime flags

```text
--device SYCL0
--split-mode none
--main-gpu 0
--gpu-layers all
--fit off
--parallel 1
--ctx-size 4096
--batch-size 512
--ubatch-size 256
--threads 16
--threads-batch 16
--flash-attn auto
--kv-offload
--cache-type-k q4_0
--cache-type-v q4_0
--repack
```

Stock SYCL environment:

```text
GGML_SYCL_ENABLE_GRAPH=0
GGML_SYCL_ENABLE_MKL_FA=1
GGML_SYCL_ENABLE_ESIMD=1
GGML_SYCL_PRIORITIZE_DMMV=0
GGML_SYCL_ENABLE_FUSION=1
GGML_SYCL_ENABLE_VMM=1
GGML_SYCL_USE_LEVEL_ZERO_API=1
```

`UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1` is permitted only if required for allocation; if used, it must be recorded and used consistently for baseline and candidate comparisons. It is not an untracked performance knob.

### First-pass cold prefill

Use `llama-bench` raw prompt processing with generation disabled:

| Case | Prompt tokens | Batch / ubatch | Repetitions |
|---|---:|---:|---:|
| P512 | 512 | 512 / 256 | 1 warm-up + 5 measured |
| P2048 | 2,048 | 512 / 256 | 1 warm-up + 5 measured |
| P4096 | 4,096 | 512 / 256 | 1 warm-up + 5 measured |

Record prompt/prefill tokens per second, individual samples, median, spread, exact token counts, and logs.

### First-pass decode

Use fixed generation of 128 tokens after matched context depth:

| Case | Context depth | Generation | Batch / ubatch | Repetitions |
|---|---:|---:|---:|---:|
| D512 | 512 | 128 | 512 / 256 | 1 warm-up + 5 measured |
| D2048 | 2,048 | 128 | 512 / 256 | 1 warm-up + 5 measured |

Record decode tokens per second, individual samples, median, spread, and exact depth.

### Separate warm-cache prefill

Use the server completion endpoint with `cache_prompt=true`, deterministic temperature/seed, and fixed token counts:

- 2,048-token deterministic prefix.
- 256-token deterministic extension.
- 128 generated tokens.
- One cold request followed by 5 measured warm requests.

Record cold prompt throughput, warm prompt throughput, `cache_n` or equivalent cache evidence, decode throughput, wall time, and output hashes. Warm-cache measurements are reported separately and must not replace cold prefill or decode.

## Stage 4 — candidate matrix

Candidates change one factor from stock wherever possible. A candidate must pass preflight and correctness before receiving throughput runs.

### Model/configuration candidates

| ID | Exact change | Qualification |
|---|---|---|
| M0 | Stock baseline | Required control |
| M1 | `--ctx-size 8192` | Long-context profile; memory-gated |
| M2 | `--cache-type-k f16 --cache-type-v f16` | Context 4096 only; memory-gated |
| M3 | `--no-repack` | Compatibility/performance candidate |

### Runtime candidates

| ID | Exact change | Emphasis |
|---|---|---|
| R1 | `--batch-size 1024 --ubatch-size 512` | Prefill |
| R2 | `--batch-size 2048 --ubatch-size 1024` | Long/large prefill; memory-gated |
| R3 | `--batch-size 256 --ubatch-size 128` | Decode |
| R4 | `--threads 12 --threads-batch 16` | Mixed |
| R5 | `--threads 24 --threads-batch 16` | Mixed |
| R6 | `--flash-attn on` | Mixed |
| R7 | `--flash-attn off` | Diagnostic |

### SYCL environment candidates

| ID | Exact change from stock | Emphasis |
|---|---|---|
| S1 | `GGML_SYCL_PRIORITIZE_DMMV=1` | Decode |
| S2 | `GGML_SYCL_ENABLE_ESIMD=0` | Diagnostic |
| S3 | `GGML_SYCL_ENABLE_FUSION=0` | Compatibility diagnostic |
| S4 | `GGML_SYCL_ENABLE_VMM=0` | Allocation diagnostic |
| S5 | `GGML_SYCL_ENABLE_MKL_FA=0` | Prefill alternative |
| S6 | `GGML_SYCL_ENABLE_GRAPH=1` | Only if graph initialization is valid |

Graph and MKL flash attention must not be silently combined: Prism documentation notes that MKL flash-attention calls are incompatible with SYCL graph capture/replay. If S6 requires `GGML_SYCL_ENABLE_MKL_FA=0`, treat that as a separately labeled two-factor diagnostic, not as a one-factor win.

Do not test DSpark, speculative decoding, or an unvalidated drafter for this objective.

## Stage 5 — measurement and decision rules

Each candidate is compared against a matched stock baseline using identical model, prompts, token counts, context depth, device selector, server slot count, and sampling settings. Use one warm-up plus 5 measured repetitions; confirm finalists with 7 measured repetitions.

For each metric, calculate the coefficient of variation for baseline and candidate. Define the explicit noise threshold:

```text
T = max(5%, 2 × max(baseline CV, candidate CV))
```

If `T` exceeds 10%, repeat the matched pair. If instability remains above 10%, mark the comparison indeterminate and reject it for promotion.

A candidate is accepted only when all gates pass and, for every required profile:

- Candidate prefill median is at least `baseline × (1 - T)`.
- Candidate decode median is at least `baseline × (1 - T)`.
- At least one of those two metrics improves by at least `T`.

Therefore, any candidate showing a decrease in either decode t/s or prefill t/s beyond the documented noise threshold is rejected, even if the other metric improves. A candidate that is neutral within noise is not promoted as a performance improvement. Warm-cache prefill must also demonstrate valid reuse and must not regress beyond `T` for a serving recommendation.

Report winners per profile. A candidate that passes only short/medium context is not a global long-context winner.

## Phase 1 execution record — terminal failure

Phase 1 was authorized and executed against the exact downloaded artifact. The production service was verified stopped before the final phase attempt; the isolated candidate port `18081` was closed afterward. Production was restored on `8081` and is active with health OK. No tuning candidate was started.

### Preflight results

| Gate | Result | Evidence |
|---|---|---|
| Exact artifact | **PASS** | `/home/hudson/EmeryChat/models/ternary-bonsai2/Ternary-Bonsai-2-27B-PQ2_0.gguf`; 7,206,168,928 bytes; SHA-256 `3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1` |
| GGUF metadata/tensors | **PASS** | `qwen35`, 64 blocks, context 262,144, embedded `tokenizer.chat_template`; 402 `PQ2_0`, 96 `BF16`, 353 `F32` tensors; no projector metadata observed |
| Exact Prism binary | **PASS** | Binary SHA-256 `67696458bd4e85cd8f0991698b10d09755309d7d2a41729ce43779a664f7d65f`; branch `prism`, HEAD `5d80cff` |
| B580-only inventory | **PASS** | `ONEAPI_DEVICE_SELECTOR=level_zero:0`, empty `CUDA_VISIBLE_DEVICES`; exactly `SYCL0: Intel(R) Arc(TM) B580 Graphics`; Level Zero driver `1.15.38308+4`; 12,216 MiB total / 12,157 MiB free reported |
| PQ2_0 SYCL load/compatibility | **FAIL — TERMINAL** | Runtime aborted in `ggml/src/ggml-sycl/mmvq.cpp:2562`: `fatal error: unsupport data type=pq2_0` |
| CPU/host fallback | **REJECTED** | No target result was accepted; execution reached the SYCL `mul_mat_vec_q` path and aborted rather than falling back to CPU |
| One-token correctness | **NOT RUN** | Model load/compatibility failed before a request could be served |
| Memory/OOM/driver gate | **NOT REACHED** | Failure occurred before a valid target load; no OOM or device-loss result is claimed |

The exact runtime command used stock settings: `--device SYCL0 --split-mode none --main-gpu 0 --gpu-layers all --fit off --parallel 1 --ctx-size 4096 --batch-size 512 --ubatch-size 256 --threads 16 --threads-batch 16 --flash-attn auto --kv-offload --cache-type-k q4_0 --cache-type-v q4_0 --repack --cache-prompt`, with the staged Level Zero/B580 environment.

### Phase-1 conclusion

The required PQ2_0 file is present and authentic, but this exact Prism/SYCL binary cannot execute its PQ2_0 tensors. The error is an unsupported target datatype, not an OOM, wrong-GPU selection, quant substitution, or CPU-only result. The stock baseline is therefore **not eligible to run**. Do not proceed to tuning candidates and do not substitute another quantization.

### Service safety outcome

The candidate exited and left no listener on `18081`. Final service safety state: systemd unit `llama-ornith.service` is active on `8081`, health is `{"status":"ok"}`, and no candidate listener remains.

## Required run artifacts

For every completed or rejected run, record an immutable manifest containing:

- Run ID, timestamp, profile ID, and status.
- Model path, revision, size, SHA-256, metadata/tensor inventory.
- Binary path, Prism HEAD, binary SHA-256, and runtime libraries.
- Full flags and environment variables.
- Device inventory, Level Zero/driver identity, and selected device.
- Load logs, server logs, stdout/stderr, memory placement, VRAM headroom, RSS, swap/page faults, and load time.
- Individual prefill/decode samples, medians, CVs, noise threshold `T`, and decision.
- Warm-cache `cache_n` evidence and output hashes.
- Correctness output/hash and any abort or rejection reason.

## Throughput results — intentionally empty

No valid throughput measurements were made because the required PQ2_0/SYCL compatibility gate failed.

### Preflight status

| Gate | Status | Evidence |
|---|---|---|
| Exact PQ2_0 artifact | **PASS** | Exact size/hash verified |
| Exact Prism binary | **PASS** | Exact size/hash/HEAD verified |
| B580-only Level Zero inventory | **PASS** | Exactly SYCL0 Intel Arc B580 |
| PQ2_0 SYCL execution/placement | **FAIL — TERMINAL** | `mmvq.cpp:2562`, unsupported `pq2_0` |
| Memory headroom/no fallback | **NOT REACHED** | No valid load |
| One-token correctness | **NOT RUN** | No valid load |

### Stock baseline results

| Case | Prefill tok/s median | Decode tok/s median | CV | Noise threshold | Decision |
|---|---:|---:|---:|---:|---|
| P512 | — | n/a | — | — | **NOT RUN — compatibility gate failed** |
| P2048 | — | n/a | — | — | **NOT RUN — compatibility gate failed** |
| P4096 | — | n/a | — | — | **NOT RUN — compatibility gate failed** |
| D512 | n/a | — | — | — | **NOT RUN — compatibility gate failed** |
| D2048 | n/a | — | — | — | **NOT RUN — compatibility gate failed** |
| Warm-cache 2048+256 | — | — | — | — | **NOT RUN — compatibility gate failed** |

### Candidate results

No tuning candidate runs were authorized or completed; the target compatibility failure is terminal for this phase.

| Profile | Changed factor | Prefill result | Decode result | Memory/fallback status | Decision |
|---|---|---:|---:|---|---|
| M1–M3 | — | — | — | — | **NOT RUN — phase stopped** |
| R1–R7 | — | — | — | — | **NOT RUN — phase stopped** |
| S1–S6 | — | — | — | — | **NOT RUN — phase stopped** |

## Pending questions and risks

1. The exact required PQ2_0 artifact is present locally and its size, SHA-256, and GGUF metadata were verified; artifact availability is no longer pending.
2. The Prism SYCL source audit found no verified PQ2_0 dispatch kernels in the relevant MMQ/MMVQ/convert paths; unsupported-op, CPU-only, or host-fallback failure is plausible.
3. File size alone does not prove B580 fit; runtime buffers, KV, graph allocations, and allocator margin must be measured.
4. Host-memory fallback may conceal VRAM overflow and is a target failure even if the request completes.
5. Graph capture and MKL flash attention have a documented compatibility constraint.
6. The orchestrator must obtain explicit approval and confirm the exact production stop/restore procedure before any executable step.
