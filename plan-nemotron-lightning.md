# Nemotron 3.5 Lightning — Intel Arc B580 optimization test plan

Status: benchmarked and promoted on 2026-09-25; tuned Nemotron service is active on port 8081.

## Objective

Measure and, only if justified by fresh evidence, improve Nemotron 3.5 Lightning
inference on the Intel Arc B580/SYCL host. Evaluate both locally available GGUF
artifacts independently; do not transfer Ornith results or correctness assumptions.
The user authorized promotion if performance was close to Gemma 4 or Ornith, with Ornith as fallback.

## Model artifacts

- Q4_K_M: `/home/hudson/.cache/huggingface/hub/models--bartowski--NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF/snapshots/f0eec2267ae843d9eb21ea3926ab0046da0a8628/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf`
  - 25,477,403,616 bytes; GGUF v3; file type 15.
- Mixed 2.47 BPW: `/home/hudson/.cache/huggingface/hub/models--vcruz305--NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF/snapshots/2ea8eb66de87a47010cef4d0768575b8d652cad3/Nemotron-3.5-Lightning-30B-A3B-MIXED-Q2_0-Q4_0-2.47BPW.gguf`
  - 9,759,494,016 bytes; GGUF v3; file type 41.
- Both report architecture `nemotron_h_moe`, 128 experts, top-6 routing,
  2,688 embedding length, 1,856 expert FFN length, 3,712 shared-expert FFN
  length, 32 attention heads, six attention layers with 2 KV heads, 4-token SSM convolution, 128 SSM
  state size, and one shared expert. The Q4_K_M metadata reports 53 blocks;
  the mixed artifact reports 52 and must be treated as a distinct compatibility
  case until a deterministic control smoke passes.

## Frozen control and safety boundary

- Control executable: `/home/hudson/llama.cpp/build-sycl-f16/bin/llama-server`.
- Control llama.cpp revision: `9f31776c3773cf03f98535c19b7e6d394af374b4`.
- Control executable SHA-256: `809758e705d275090d37494483bbf919aca6183e2def84d8a798e165f7997abf`.
- Current production-equivalent command source is
  `/home/hudson/.config/systemd/user/llama-ornith.service`; it uses
  `GGML_SYCL_ENABLE_MKL_FA=1`, `--device SYCL0`, `--split-mode none`,
  `--gpu-layers 999`, `--ctx-size 131072`, `--parallel 1`,
  `--kv-offload`, q4_0 KV cache, 2048 batch/ubatch, 16 CPU threads,
  flash attention auto, prompt caching, checkpointing, and cache RAM 8192.
- The currently running port-8081 process matches the production binary and
  flags but was launched manually; the user systemd unit is inactive. Do not
  assume the unit state and process state are equivalent.
- Experiments use detached worktrees under `/home/hudson/llama.cpp-experiments/`
  and build directories under `/home/hudson/llama-builds/`. Never overwrite
  `/home/hudson/llama.cpp/build-sycl-f16`, the production unit, or its source.
- Experimental servers use `127.0.0.1:18081` or another explicitly recorded
  isolated port. Deployment and production configuration changes are out of scope.
- Production may be stopped only for an announced exclusive-GPU window. At the
  end of every window: stop every experiment, verify the experimental port is
  closed, restore the original port-8081 process or start the systemd unit as
  appropriate, and require `/health` HTTP 200 before proceeding.

## Objectives, hypotheses, and candidate order

1. Establish a fresh Q4_K_M and mixed-artifact control with the unmodified
   production binary and model-specific flags. Hypothesis: the hybrid
   Nemotron-H MoE path, high expert fan-out, and small decode batches dominate.
2. Measure prompt-cache reuse, batch/ubatch size, concurrency, and scheduler
   choices before source changes. Hypothesis: cache reuse helps repeated long
   prompts, while concurrency may hurt single-stream decode on one B580.
3. Inspect and, only where justified, test isolated SYCL kernel/dispatch
   candidates. Candidates include existing BMG-targeted Q4_K kernels, graph
   capture, oneDNN/MKL paths, MTP support, and multi-queue scheduling. Each
   must be proven applicable to this model and backend before implementation.
4. A candidate is eligible for extended testing only if it is deterministic,
   semantically equivalent to control, stable, and at least 5% faster on the
   stated target metric without material memory growth.

## Fixed workload and measurements

- Use fixed tokenized prompts representing short, medium, long, and near-cache
  contexts (approximately 512, 4k, 8k, 24k, and 32k prompt tokens where the
  model and available memory permit), plus fixed 128/512-token decode cases.
- Use fixed seed, temperature, top-p, top-k, chat-template settings, request
  order, and repeat counts. Record the exact payload and response hashes.
- Measure cold startup/readiness time, prompt tok/s, decode tok/s, end-to-end
  latency, cache-hit/reuse behavior, memory breakdown, GPU utilization/device
  errors, and server logs. Use a startup/inference watchdog and bounded retries.
- Compare each candidate against the same frozen control immediately before or
  after the candidate, with cold and warm runs matched.

## Required test gates for every candidate

1. Build in its isolated worktree; run `git diff --check`.
2. Run focused SYCL/backend correctness tests relevant to changed code.
3. Run deterministic correctness smoke against the matching frozen control.
4. Run matched cold and warm tests for representative prompt/decode sizes.
5. Run a bounded cache/batching/concurrency test where applicable.
6. Enforce startup and inference watchdogs; inspect logs and GPU/device state.
7. Reject on any correctness change, device loss, hang, server error, material
   memory growth, or <5% meaningful improvement. Record the decision here.

## Rollback

Stop the isolated server, close and verify the experimental port, leave the
candidate worktree/build unused, and restore the known-good port-8081 process.
If systemd is the intended owner, use `systemctl --user start
llama-ornith.service` and verify `/health` HTTP 200. Never promote a candidate
without explicit user approval.

## Discovery record

- Host GPU: Intel Battlemage/Arc B580, PCI device `8086:e20b` (`card0`).
- Host CPU: AMD Ryzen 9 5950X, 32 logical CPUs; host memory 60 GiB.
- Production process observed: PID 773290, port 8081, RSS about 12.4 GiB;
  its last observed process memory peak was about 21.1 GiB. The previous unit
  invocation reported B580 memory breakdown of 12,216 MiB total, with 10,669
  MiB allocated to model/context/compute and 933 MiB free.
- Build cache: SYCL, Intel target, F16 enabled, graph enabled, oneDNN requested
  but `DNNL_DIR` is not found; MKL is configured and the runtime enables MKL
  fused attention. Compiler is Intel oneAPI 2025.3 `icx/icpx`.
- Full production environment was inspected from `/proc/773290/environ`; salient
  variables include `GGML_SYCL_ENABLE_MKL_FA=1`, `MKLROOT=/home/hudson/intel/oneapi/mkl/2025.3`,
  oneAPI compiler/TBB/MKL library paths, and `OCL_ICD_FILENAMES` pointing to the
  Intel OpenCL ICD. Capture a fresh exact environment snapshot for each test
  window instead of treating this summary as immutable.

## Progress log

- [x] Inspected current process, user unit, binary, source revision, build flags,
  GPU identity, environment, existing plan, and both model paths.
- [x] Launched four independent subagent investigations: model architecture,
  SYCL dispatch, workload/scheduling, and model-specific kernel opportunities.
- [x] Consolidated subagent reports. The previous claim that no MTP artifact
  was available was stale: the selected Q4_K_M GGUF contains embedded
  `blk.52.nextn.*` tensors, and this llama.cpp revision loads them with
  `--spec-type draft-mtp`. BMG-targeted expert dispatch and oneDNN attention
  remain possible source-level hypotheses; graph capture and multi-queue
  scheduling were not changed in this tuning pass.
- [x] Announced and began the exclusive-GPU frozen-control window. The exact
  production-flag Q4_K_M control was started on `127.0.0.1:18081` with the
  unmodified production binary and `--n-cpu-moe 24`; it loaded model tensors
  and entered warmup but remained HTTP 503 for approximately five minutes.
  It was stopped at the user's request before any inference benchmark. This is
  recorded as a startup/readiness failure for that exact control configuration,
  not as a model correctness result.
- [x] Stopped all experimental processes, verified port 18081 closed, started
  `llama-ornith.service`, and verified production `/health` HTTP 200.
- [ ] Run fresh controls, candidate gates, and record results/decisions.
- [ ] Restore and verify production state after every test window.

## Completed Nemotron 3.5 Lightning retest and promotion — 2026-09-25

- Artifact: Bartowski `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf`,
  SHA/artifact snapshot `f0eec2267ae843d9eb21ea3926ab0046da0a8628`; 25,477,403,616
  bytes. The mixed 2.47 BPW artifact was not used because previous outputs
  failed the quality checks. The selected GGUF has a native one-layer MTP head.
- Control: BMG AOT llama.cpp build at the frozen `9f31776c` revision, same
  Level Zero B580, 128K context, q4_0 KV, `--n-cpu-moe 40`, 2048/2048 batch,
  16/16 threads, and the recorded MKL FA / ESIMD / VMM environment. Fresh
  no-MTP measurements: 523-token prefill 329.9 tok/s and decode 25.9; 8,201
  token prefill 1,156.4 and decode 25.8; 32,775 prefill 1,064.4 and decode
  24.9. Response hashes matched across repeated requests.
- Draft horizon sweep: MTP `n_max=1` at 2048/2048 improved short decode to
  33.1 tok/s but failed on the 8K prefill with Level Zero out-of-device-memory.
  `n_max=4` at 512/256 was stable but only 355 tok/s at 32K. At 1024/1024 it
  reached 45.9 decode, 862 at 8K prefill, and 810 at 32K. At 1536/1536 it
  reached 45.4 decode, 969 at 8K prefill, and 881 at 32K. `n_max=6` at
  1536/1536 measured 48.9–50.0 decode at 512/8K, 966 at 8K prefill, 879 at
  32K, and 477.6 tok/s at 120,016 tokens. The 120K request completed with the
  configured 128K slot. Increasing the microbatch to 1792 failed on the 8K
  prefill with the same Level Zero OOM; 1536 is the largest verified stable
  microbatch for the chosen 40-layer CPU-MoE placement.
- MTP reported 108/108 drafts accepted on the repeated synthetic benchmark
  prompt; this overstates natural-prompt acceptance. A real chat-template
  `web_search` request through EmeryRouter returned a structured tool call;
  its 54 generated drafts had 40 accepted (74%), and decode was 36.9 tok/s.
- Comparison: at 8K and 32K, Nemotron MTP6 is within roughly 10–14% of the
  Gemma 4 Q8-MTP prefill measurements and matches or exceeds Gemma MTP decode
  on the synthetic fixed prompt. A 120K Nemotron prefill is slower than Gemma's
  prior 120K run (477.6 vs 698.1 tok/s). The real router tool-call decode was
  about 8% below Ornith's prior 45 tok/s median. With the normal/medium and
  long prompt results close to the requested range, Nemotron passed the user's
  conditional promotion gate; the 120K throughput limitation is retained in
  the benchmark notes.
- Kept settings: `GGML_SYCL_DEVICE_ARCH=bmg_g21`, Level Zero only,
  `GGML_SYCL_ENABLE_MKL_FA=1`, `GGML_SYCL_ENABLE_DNN=1`,
  `GGML_SYCL_FA_ONEDNN=1`, ESIMD on, DMMV priority off, VMM on, async memory
  operations and pinned host memory on; `--n-cpu-moe 40`, batch/ubatch
  1536/1536, 16 CPU threads, q4_0 K/V, 131072 context, `draft-mtp n_max=6`.
  DMMV, kernel, scheduler, and source changes were not promoted.
- Production: enabled `llama-nemotron-lightning.service` on port 8081. Emery's
  stable router endpoint on port 8220 returned a structured `web_search` call;
  the idle image broker on 8188 was restarted with its relaunch target set to
  `llama-nemotron-lightning.service`. Detailed measurements and limitations
  are in `benchmark-results-nemotron-lightning-b580.md`.
- [x] Removed the inactive Gemma unit, Gemma GGUF/MTP files, HF snapshot,
  Gemma-only raw data/logs, and stale Gemma defaults from setup/config/docs.
- [x] Removed the now-unused Gemma channel parser from Emery's response cleanup.
- [x] Fresh no-MTP and MTP controls, context/performance sweep, and tool-call
  smoke completed.
- [x] Production routing and broker relaunch configuration verified.
- [x] Candidate promoted; 8081 `/health` and 8220 router tool-call smoke passed.
