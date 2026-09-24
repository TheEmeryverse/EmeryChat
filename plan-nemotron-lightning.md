# Nemotron 3.5 Lightning — Intel Arc B580 optimization test plan

Status: discovery complete; frozen-control setup pending an exclusive-GPU test window.

## Objective

Measure and, only if justified by fresh evidence, improve Nemotron 3.5 Lightning
inference on the Intel Arc B580/SYCL host. Evaluate both locally available GGUF
artifacts independently; do not transfer Ornith results or correctness assumptions.
No deployment is authorized by this plan.

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
- [x] Consolidated subagent reports. Highest-value hypotheses are Nemotron-H
  `MUL_MAT_ID` routing/launch overhead, B580-specific Q8_0/Q5_0 expert dispatch,
  and oneDNN attention only if a build with actual DNNL support is produced.
  Graph capture is currently incompatible with the MoE routing path; MTP needs
  a matching draft artifact not present in the local cache; real multi-queue
  scheduling is an architectural prototype rather than a safe toggle.
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
