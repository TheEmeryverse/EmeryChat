# Ornith Performance Experiment Plan

This plan covers configuration, backend-build, and isolated SYCL kernel experiments for the live Ornith 1.5 Q4_K_M deployment.

## Safety rules

- Do not modify or restart the production `llama-ornith.service` during the first pass.
- Use an isolated llama.cpp process and a separate port for server-level A/B tests.
- Keep the model file, llama.cpp commit, oneAPI runtime, driver, prompts, seed, and sampling parameters fixed within each comparison.
- Change one performance variable at a time.
- Reject any configuration that changes deterministic output, exhausts memory, loses the Xe device, or fails a mixed prefill/decode soak.
- Preserve the pre-existing EmeryChat worktree changes.

## Baseline

Capture endpoint metadata and run fixed workloads at approximately 512, 4k, 16k, and 32k prompt tokens. Generate a fixed short completion and repeat each case three times after one warmup request.

Record:

- prompt tokens and prompt-evaluation time/tokens per second;
- generated tokens and decode time/tokens per second;
- time to first token and total wall time;
- llama.cpp cache/timing fields;
- process RSS, page faults, CPU usage, GPU memory/utilization when available;
- server build, model path, command line, and relevant environment.

The endpoint harness is `scripts/ornith_perf_bench.py`.

## Experiment order

### 1. Runtime scheduling and memory

Run the following in isolated processes:

- `batch/ubatch`: 1024/512, 2048/512, 2048/1024, 2048/2048, 4096/1024, 4096/2048;
- `n_cpu_moe`: 20, 22, 24, 26, 28;
- CPU threads: 8, 12, 16, 24;
- mmap versus `--load-mode none`.

Do a coarse sweep first, then repeat only the two best candidates with three runs per workload.

### 2. Prompt cache

Measure exact repeats, append-only suffixes of 16/128/512 tokens, alternating conversations, restart restoration, and requests that currently log full prompt reprocessing. Vary checkpoint count, minimum checkpoint spacing, and cache RAM only after identifying whether the application is preserving a stable prefix.

### 3. Speculative decoding

Compare no speculation with embedded MTP draft lengths 2 and 3. Measure acceptance, decode speed, first-token latency, memory, and deterministic output agreement. This targets decode, not prompt prefill.

### 4. Build/runtime variants

Build separately and compare:

- generic SYCL JIT versus `bmg_g21` AOT;
- `GGML_SYCL_F16=OFF` versus `ON`;
- runtime SYCL graph execution disabled versus enabled;
- oneDNN-linked versus current `GGML_SYCL_DNNL=0` build, if a clean dependency is available.

Do not enable persistent SYCL JIT caching in the baseline.

### 5. KV and attention

Compare q4_0/q4_0, q8_0/q8_0, and FP16 KV where memory permits. Test native versus MKL/oneDNN attention only when the relevant path is actually present. Run fixed-output comparisons and a mixed-load soak for every candidate.

### 6. Application latency and throughput

Measure reasoning effort separately from backend speed. Compare low, medium, high, and disabled reasoning where supported. Only after single-request tuning, test `parallel=2` and `parallel=4` for aggregate throughput and queue latency.

## Acceptance criteria

A candidate is recommended only if it improves the target metric by at least 5% across repeated runs, has no correctness difference in fixed-seed checks, stays within the memory budget, and survives a mixed prefill/decode soak.

## First-pass execution status

The production service was stopped for the isolated tests and restored after
the tests. No systemd runtime flags were changed during the first pass. The first pass
used `thinking=off`, a deterministic synthetic prompt, 64 generated tokens,
one warmup request, and one or two measured requests per prompt size. The
approximate 4k and 16k targets tokenized to about 5.9k and 23.3k tokens.

| Candidate | ~5.9k prefill | ~23.3k prefill | Decode | Decision |
| --- | ---: | ---: | ---: | --- |
| Generic SYCL control | 800 tok/s | 818 tok/s | 42.8 / 35.4 tok/s | Reference |
| `GGML_SYCL_DEVICE_ARCH=bmg_g21` AOT | 808 tok/s | 817 tok/s | 43.2 / 36.8 tok/s | Neutral; not worth rollout alone |
| `GGML_SYCL_F16=ON` | 1,047 tok/s | 1,064 tok/s | 42.3 / 35.5 tok/s | Strong candidate; repeat-validated |
| FP16 + `n_cpu_moe=28` | 992 tok/s | 1,013 tok/s | 39.6 / 34.2 tok/s | Discard; slower than FP16 alone |
| `ubatch=1024` | 643 tok/s | 704 tok/s | 35.9 / 36.1 tok/s | Discard |
| `n_cpu_moe=28` | 745 tok/s | 783 tok/s | 34.8 / 33.9 tok/s | Tradeoff; no clear win |
| q8/q8 KV | 801 tok/s | 819 tok/s | 42.7 / 36.9 tok/s | Neutral |
| `batch=4096`, `ubatch=2048` | 1,028 tok/s | 1,065 tok/s | 42.9 / 36.4 tok/s | Neutral vs FP16 |

The strongest immediate experiment is the FP16 SYCL build: it improves long
prompt prefill by about 30% without a meaningful decode change. Exact prompt
cache reuse is also a strong application-level win: repeated 5.9k-token prompts
fell from roughly 9.0 seconds to 1.6 seconds, with `cache_n` around 5.9k and
decode around 42.6 tok/s. The application still needs stable-prefix validation
because its dynamic budget text can invalidate that reuse.

MTP is recognized by the current backend and reports draft acceptance, but the
exact Ornith model's MTP path reduced prompt processing to roughly 100–160
tok/s and did not complete a bounded 32-token decode sample in the allotted
test window. It is therefore not a rollout candidate from this pass.

The FP16 backend passed a six-request, two-worker mixed soak across short,
medium, and long prompts with zero nonterminal results. It was deterministic
within its own build: repeated fixed-key requests produced the same response
hash. However, its deterministic response differed from the generic build's
response hash and stopped at 53 tokens instead of the generic build's 64-token
limit. Both previews were coherent, but strict fixed-seed output agreement is
not yet demonstrated. The user authorized deployment; the systemd unit now
uses `/home/hudson/llama.cpp/build-sycl-f16/bin/llama-server`. The service is
active on port 8081 and passed a live smoke request. The output difference
remains a monitoring caveat for future semantic regression testing.

Runtime SYCL graphs were tested and disabled by llama.cpp because this model's
graph contains an unsupported `CONCAT` node. Flash-attention-off is invalid
with the current quantized V cache; llama.cpp requires flash attention for
that cache format. oneDNN is not installed, so no dependency installation was
attempted.

## Follow-up execution status

The prompt-cache change is implemented in `emery/engine.py`: request-local
loop budget/status text now attaches to the newest user message, while
compaction/system notices retain their prior behavior. This keeps older user
turns static for checkpoint reuse. The file and new unit-test syntax parse
cleanly; the checkout does not include pytest or the runtime Telegram
dependencies, so the full test suite could not be executed here.

MKL diagnostics on the active FP16 build confirmed that the default path uses
both `MKL` and native `TILE` attention dispatch. Disabling MKL reduced the
~23.3k-token prefill from 1,061 tok/s to 872 tok/s and increased wall time
from 23.95s to 28.77s. MKL remains enabled.

MTP was retested with the exact embedded Ornith MTP head, prompt caching
disabled, and GPU layers reduced for memory headroom:

| MTP configuration | Prefill | Decode | Acceptance |
| --- | ---: | ---: | ---: |
| 28 GPU layers, `n_max=1` | 387 tok/s | 13.2 tok/s | 3/3 |
| 28 GPU layers, `n_max=2` | 369 tok/s | 25.4 tok/s | 4/5 |

This recovered the catastrophic ~2 tok/s behavior caused by near-exhausted
VRAM, but MTP still loses to ordinary FP16 decode (~42 tok/s) and substantially
reduces prefill. It is not recommended on this B580 configuration.

An isolated `parallel=2` two-worker soak completed without failures, but was
slightly worse than `parallel=1` at the same 64-token workload (median 22.2s
versus 20.7s; maximum 48.0s versus 47.5s). The server remains at
`--parallel 1`; Emery's global model semaphore would also serialize requests
until application concurrency is deliberately redesigned.

## Kernel experiment status

Kernel work was performed in detached worktrees at llama.cpp commit `9f31776c3`,
with the production FP16 binary and systemd unit left unchanged. Production was
stopped only while the isolated port-18081 processes used the GPU, then restored
and health-checked.

Two subagent-reviewed candidates were tested:

| Candidate | Result | Evidence |
| --- | --- | --- |
| MKL FlashAttention query tile `8192 -> 16384` | Reject | At ~33.1k actual prompt tokens, unique-prefix cold runs measured ~1,006 tok/s at 8,192 rows versus ~744 tok/s at 16,352 rows. MKL scratch grew from ~424 MiB to ~815 MiB, and softmax time rose from ~67.5 ms to ~236 ms. |
| Opt-in Q4_K MMQ for `MUL_MAT_ID` MoE prefill | Reject / unsafe | The guarded build compiled, but the first 809-token prefill stalled before a response. The same stall remained after removing the global `GGML_SYCL_FORCE_MMQ` override and retaining the normal batch-size gate. No production binary was changed. |

The attention control and candidate logs are in `data/performance/` under
`kernel-mkl-qtile8192-control*` and `kernel-mkl-qtile16384*`. The MoE smoke
logs are under `kernel-moe-q4k-mmq*`. The experimental source remains isolated
in `/home/hudson/llama.cpp-experiments/kernel-moe`; its build trees are
`/home/hudson/llama.cpp/build-kernel-moe` and
`/home/hudson/llama.cpp/build-kernel-moe-safe` for follow-up debugging only.

No kernel candidate met the 5% improvement, correctness, and stability gate.
The deployed FP16 build remains the recommended backend.

## Next engineering phase

The next phase is intentionally larger than the rejected one-line kernel
experiments, but remains isolated from production. Each candidate gets a
detached worktree from commit `9f31776c3`, its own build directory and binary,
fixed-shape microbenchmarks before model tests, a request timeout/watchdog, and
the same correctness, memory, and mixed-soak acceptance gate.

Workstreams now starting in parallel:

1. **MoE instrumentation:** measure routing/gather, per-expert matmul, and
   scatter/synchronization separately while capturing exact Ornith tensor shapes.
2. **Selective graph support:** trace the `CONCAT` graph rejection and assess
   implementing or partitioning that operation so stable graph segments can be
   captured without forcing the entire model through SYCL Graph.
3. **BMG Q4_K kernels:** inspect the current MMQ/MMVQ/reorder dispatch and build
   exact-shape microbenchmarks before attempting a Battlemage-specific kernel.

The first implementation target was selected from the instrumentation evidence.
The initial results are:

| Track | Result | Evidence / disposition |
| --- | --- | --- |
| MoE instrumentation | Actionable | At an ~8.4k-token prompt, one trace recorded 864 routed MoE operations and ~87,918 expert matmul calls. Instrumented expert matmul time was ~3.13 s; gather and scatter were ~0.24 s and ~0.19 s. The dominant issue is launch fragmentation and many small expert calls. Raw traces: `moe-instrument-512*` and `moe-instrument-5900*`. |
| Grouped Q4_K MoE prefill prototype | Reject current implementation | A compile-time isolated prototype passed the 512-token correctness smoke and matched the deterministic response hash, but measured ~790 tok/s prefill at ~8.4k tokens versus ~1,050 tok/s for the control. The generic grouped Q4_K kernel is slower than the existing expert dispatches; no production code was changed. Raw results: `moe-grouped-q4k-512*` and `moe-grouped-q4k-5900*`. |
| Selective decode graph eligibility | Reject for service use | The narrow `MUL_MAT_ID` eligibility patch compiled, but the graph-enabled server remained in model warmup for over two minutes and never became health-ready. It is not an acceptable startup or operational tradeoff. Raw log: `graph-decode-512.server.log`. |
| BMG-specific Q4_K MMVQ | Reject current specialization | A compile-time gated Battlemage G21/G31 specialization selecting the existing `<2,2>` reordered MMVQ path was correct and stable, but the longer 512-token / 128-output A/B measured 44.09 tok/s median decode versus 44.77 tok/s control (about -1.5%). The short A/B was also effectively neutral. Generic MMQ remains untouched. Raw results: `bmg-q4k-512*` and `bmg-control-512*`. |

The larger engineering candidates tested in this phase are therefore all
below the promotion threshold: grouped MoE prefill is substantially slower,
selective graph enablement has unacceptable warmup behavior, and the narrow
BMG MMVQ specialization is slightly slower. The next useful work is a deeper
kernel-level profiler/microbenchmark effort aimed at the launch-fragmented MoE
path, rather than promoting any of these prototypes. No production systemd
flags or binaries were changed by this phase; Ornith was restored and
health-checked on port 8081 after the isolated tests.

## Result files

Store raw results outside the application runtime data, under `data/performance/` when the first benchmark run is authorized. The harness writes JSONL so results can be aggregated without losing raw measurements.
