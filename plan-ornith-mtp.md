# Ornith 1.5 35B A3B — llama.cpp MTP investigation plan

Status: discovery complete; no production configuration or binary has been changed.

## Scope and safety boundary

- Target model: `Ornith-1.5-35B-Uncensored-Q4_K_M.gguf` (Ornith 1.5 35B A3B,
  Q4_K_M), loaded from the Hugging Face snapshot path recorded below.
- Production service, systemd unit, production llama.cpp source tree, and
  `build-sycl-f16` binary are read-only control references.
- Every candidate must use a detached worktree under
  `/home/hudson/llama.cpp-experiments/` and a separate build directory under
  `/home/hudson/llama-builds/`. Experimental servers must use an isolated
  loopback port and a watchdog. No deployment is authorized.
- Because the model consumes most of the B580, starting a second full model
  requires an announced exclusive-GPU window. Until such a window is approved,
  only read-only inspection and analysis of existing artifacts are performed.

## Production-equivalent control snapshot

- Unit: `/home/hudson/.config/systemd/user/llama-ornith.service`
- Current process: PID 794582, port 8081, active at discovery time.
- Binary: `/home/hudson/llama.cpp/build-sycl-f16/bin/llama-server`
- Binary SHA-256: `809758e705d275090d37494483bbf919aca6183e2def84d8a798e165f7997abf`
- llama.cpp revision: `9f31776c3773cf03f98535c19b7e6d394af374b4`
  (`v0.4.1-24-g9f31776c3`), with working-tree untracked files preserved.
- Model: `/home/hudson/.cache/huggingface/hub/models--0xKitkat--Ornith-1.5-35B-A3B-Uncensored-GGUF/snapshots/ab0eed77c73880afda789a3914003db2273fd64a/Ornith-1.5-35B-Uncensored-Q4_K_M.gguf`
- Model size at discovery: 21,713,463,264 bytes.
- Model SHA-256: `081fa0babd0e32432acf67d6af80b7e7550ae8a102a6ca6c48e4e39aab685bc9`
  (matches the publisher's `SHA256SUMS` for snapshot `ab0eed77…`).
- Host: Intel Arc B580 (`8086:e20b`), SYCL/Intel target, oneAPI 2025.3,
  `GGML_SYCL_ENABLE_MKL_FA=1`, `GGML_SYCL=ON`, `GGML_SYCL_GRAPH=ON`,
  `GGML_SYCL_F16=ON`, `GGML_SYCL_DNN=ON` but `DNNL_DIR` is not found.
- Control flags are exactly the unit `ExecStart`: `--device SYCL0`,
  `--split-mode none`, `--ctx-size 131072`, `--parallel 1`, `--gpu-layers 999`,
  `--n-cpu-moe 24`, `--kv-offload`, q4_0 K/V, batch/ubatch 2048, 16 threads,
  flash-attn auto, jinja/reasoning high, prompt cache/checkpoints/cache RAM.
- Control MTP state: disabled. `/props` reported `speculative.types = none`
  and `/slots` reported `speculative = false`.

## Model support result

This model natively supports MTP/NextN. The GGUF loader log records:

- architecture `qwen35moe`;
- `qwen35moe.block_count = 41` and `qwen35moe.nextn_predict_layers = 1`;
- matching llama.cpp code in `src/models/qwen35moe.cpp` loads the final MTP
  block and provides `LLM_GRAPH_TYPE_DECODER_MTP`;
- `--spec-type draft-mtp` creates an MTP context against the same target model;
- the historical MTP log initialized `draft-mtp` and produced acceptance data.

Therefore the investigation continues with supported runtime behavior; no
unsupported metadata or forced tensor behavior is needed.

### Important model-artifact finding — native head quality

The exact abliterated GGUF is based on the 2026-08-20 `0xKitkat` snapshot
(`ab0eed77…`), before the later replacement-head work. The official Ornith
discussion reports initializer-like statistics for the shipped `mtp.*` tensors
and chance-like acceptance. The later `shisa-ai/Ornith-1.5-35B-A3B-MTP-ONLY`
artifact is a separate 844.6M-parameter BF16 head, initialized from the
compatible Qwen3.6 MTP head and KL-distilled against Ornith; its README says
explicitly that it replaces the shipped native head. The abliterated fork's
abliteration does not imply that this head was retrained or replaced.

This means the current GGUF is MTP-compatible but likely carries the known
poor-quality native head. A trained-head graft/requantization is a separate
model-artifact experiment, not a llama.cpp runtime candidate. It must be
tested only after the runtime control is frozen, and only with exact tensor,
license, output, and memory provenance recorded.

## Historical baseline evidence (not yet a fresh frozen-control run)

Existing `data/performance/ornith-live-baseline.jsonl` was produced against the
same model family and production endpoint with deterministic settings. It has
matching output hash `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
Warm-cache samples report approximately:

| Cached prompt target | Prompt tok/s | Decode tok/s | Cache tokens |
| ---: | ---: | ---: | ---: |
| 512 | 33.34–35.47 | 43.46–44.19 | 773 |
| 4,096 | 34.20–34.78 | 42.34–42.43 | 5,856 |
| 16,384 | 32.52–33.09 | 35.99–36.29 | 23,264 |

The historical MTP attempt is in `data/performance/mtp-current-ornith.server.log`
and used `--spec-type draft-mtp --spec-draft-n-max 2 --cache-reuse 64` on the
same model path. It initialized successfully, allocated an additional 256 MiB
draft KV cache, and reported 18 accepted / 32 generated draft tokens (0.5625
acceptance; mean accepted length 2.12). Its first uncached 790-token request
reported 60.96 prompt tok/s, 0.77 final eval tok/s, and 189.079 ms draft time;
the run was cancelled before a repeatable benchmark was completed. These are
diagnostic observations, not a keep decision.

## Candidate order and gates

### User success criterion

Prefill and decode are evaluated independently. An improvement in either
dimension is valuable when the other remains statistically neutral; a
candidate is rejected when it causes a material regression in the other path.
MTP is expected to affect the decode/speculation path; any prefill improvement
must be evaluated as a separate optimization rather than assumed to come from
MTP.

1. Runtime-only supported MTP flags.
2. Conservative draft/verification settings.
3. Scheduler changes that leave ordinary prefill on the existing path.
4. Backend/kernel work only if MTP-specific SYCL profiling justifies it.
5. Larger graph/KV/batching changes as separate experiments only.

For each candidate: record hypothesis, exact command/flags, isolated worktree and
build path, startup/first-request/long-request watchdog results, memory, device
state, correctness and output hashes, cold/warm prompt-cache behavior, prompt
tok/s, decode tok/s, draft count, acceptance, verification cost, and net tok/s.
Run `git diff --check` and focused correctness tests. Reject device loss, hangs,
invalid or nondeterministic output, material memory growth, or prefill regression
unless a repeatable net decode gain clearly outweighs it. MTP must remain on the
decode/speculation path; normal prompt prefill must retain the control path.

## Experiment log

### Discovery — 2026-09-20

- Hypothesis: Ornith's `nextn_predict_layers=1` is a native MTP head and can be
  enabled without a separate draft model, but the draft graph/KV/scheduler may
  affect prefill or cache reuse.
- Exact flags inspected: production control plus historical
  `--spec-type draft-mtp --spec-draft-n-max 2 --cache-reuse 64`.
- Keep/reject: support confirmed; historical MTP run remains inconclusive and
  is not promoted. Fresh isolated controls are pending an exclusive-GPU window.

### Model-head provenance — 2026-09-20

- Hypothesis: the prior MTP slowdown is dominated by the shipped native head's
  low acceptance, not by unsupported metadata.
- Evidence: local GGUF snapshot/hash/date; official Ornith discussion #10;
  replacement-head README and reported acceptance/throughput comparison.
- Decision: do not graft or force a replacement yet. Freeze runtime controls
  first, then evaluate a replacement head as a separate candidate if the user
  approves obtaining/using that artifact.

### Independent investigation synthesis — 2026-09-20

Five read-only investigations converged on the following:

- The exact GGUF contains `blk.40.nextn.*` tensors and a one-layer native MTP
  head. `--spec-type draft-mtp` is the supported runtime selector; a separate
  draft model is not required for this native path.
- Target graph execution exports `h_nextn`; a second MTP context runs the full
  extra attention/MoE block and maintains its own filtered KV state. The server
  calls `common_speculative_process()` after every successful target batch,
  including prompt-prefill batches.
- `common_speculative_impl_draft_mtp::process()` copies each target batch,
  transfers hidden rows, and calls `llama_decode(ctx_dft, batch)`. This is the
  direct structural reason normal prefill is not isolated from MTP.
- The previous full-GPU run also oversubscribed the B580: approximately 12,244
  MiB projected versus 11,898 MiB available, ending with about 69 MiB free.
  This is a separate high-confidence amplifier of the catastrophic decode tail.
- The MTP context disables `cache_reuse` and prompt-cache state includes draft
  state. The previous run therefore cannot be treated as prompt-cache-neutral.
- The SYCL backend has additional risk for MTP's small routed-MoE batches:
  host synchronization around `MUL_MAT_ID`, graph exclusions for routed/concat
  operations, and split-buffer restrictions. The recent OpenCL MTP dispatch
  optimization does not apply to SYCL.

The design constraint therefore cannot be met by runtime flags alone. A future
scheduler candidate would need a correct way to preserve MTP prompt KV state
while preventing the MTP catch-up work from serializing the ordinary target
prefill. Skipping `process()` blindly is invalid and is expected to degrade or
corrupt drafting.

External model-artifact evidence is recorded for follow-up:

- Official Ornith discussion: https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B/discussions/10
- Replacement trained head: https://huggingface.co/shisa-ai/Ornith-1.5-35B-A3B-MTP-ONLY

These are not runtime candidates. Any replacement-head test must use a separate
artifact and provenance record after the runtime control is frozen.

### Live runtime and memory experiments — 2026-09-20

All live runs used an exclusive B580 window: the production systemd service
was stopped before each run and restored afterward. Production is currently
healthy again on port 8081. The isolated source/build pair was:

- Worktree: `/home/hudson/llama.cpp-experiments/ornith-mtp-runtime-9f31776c`
- Build: `/home/hudson/llama-builds/ornith-mtp-runtime-9f31776c-make`
- Binary: `llama-server` at commit `9f31776c3`, SHA256
  `e8b110a21ad64ad4685558cae079a8c6519c076879a2d3e84bdcd98fb498f17c`
- Common production flags: `--device SYCL0 --split-mode none --ctx-size 131072
  --parallel 1 --gpu-layers 999 --kv-offload --cache-type-k q4_0
  --cache-type-v q4_0 --batch-size 2048 --ubatch-size 2048 --threads 16
  --threads-batch 16 --flash-attn auto --jinja --reasoning auto
  --reasoning-effort high --cache-prompt --ctx-checkpoints 8
  --checkpoint-min-step 256 --cache-ram 8192 --cache-idle-slots`
- Benchmark driver: `scripts/benchmark_mtp.py`; it records exact token counts,
  cold/warm cache counts, prompt/decode timings, draft counts, acceptance, and
  response hashes. The driver passed syntax compilation; the isolated source
  worktree passed `git diff --check` and remains clean.

Fresh no-MTP control with the production binary and flags is archived at
`data/performance/control-prod-flags-20260920.json`. It measured approximately
532.5 / 41.2 tok/s cold for 566 prompt tokens / 128-token decode, 1,376 / 43.8
tok/s cold for 4,478 / 128, and 1,483 / 38.2 tok/s cold for 17,882 / 128.
Exact warm repeats reused 562, 4,474, and 17,878 cached tokens. The control
output hash was stable across these requests.

Candidate results:

1. `--spec-type draft-mtp --spec-draft-n-max 1 --n-cpu-moe 24` (full-GPU
   production placement). Rejected. The 566-token cold prefill fell to 81.7
   tok/s and decode to 0.70 tok/s; acceptance was high (63/64), proving that
   acceptance was not the limiting factor. Device free memory fell to roughly
   88 MiB. This reproduces the earlier VRAM-thrashing failure.
2. Same MTP settings with `--n-cpu-moe 28`. Viable but not final. Cold prefill
   was 488 / 1,264 / 1,341 tok/s for 566 / 4,478 / 17,882 tokens, while
   one-token decode was 46.8 / 44.0 / 46.0 tok/s. Acceptance was 31/32 or
   31/31. This restores headroom and improves decode, but cold prefill is
   consistently about 8–10% below the production-placement control.
3. Same MTP settings with `--n-cpu-moe 26`. Promising, pending repeats.
   Cold prefill was 505 / 1,280 / 1,339 tok/s and decode was 47.6 / 41.8 /
   45.4 tok/s for the same sizes; exact warm repeats reused the full prompt
   cache and reported 48.4 / 51.6 / 44.5 tok/s decode. Acceptance was 31/32
   or 31/31. A matched no-MTP `--n-cpu-moe 26` control produced the same
   output hash, `387651c1...`, as this candidate, isolating the hash change
   from production placement to CPU/GPU expert placement rather than MTP.
4. Same MTP settings with `--n-cpu-moe 25`. Rejected. Although the short
   prompt looked healthy, 4,478-token cold prefill collapsed to 723 tok/s and
   decode to 16.1 tok/s, indicating near-VRAM eviction/thrashing.
5. `--spec-draft-n-max 2 --n-cpu-moe 28`. Rejected. It generated 61 drafts
   for 32 accepted tokens (roughly 0.52 draft acceptance), with decode only
   34.0–39.1 tok/s and lower than one-token MTP/control. This confirms that
   increasing the draft horizon is counterproductive for the shipped head.

The current keep/reject state is therefore: full-GPU MTP rejected, 25 CPU-MoE
rejected, two-token MTP rejected, 28 CPU-MoE provisional, and 26 CPU-MoE the
best candidate to repeat. No runtime or model candidate has been deployed.

### Repeatability pass — 2026-09-20

An alternating fresh-server pass compared `--n-cpu-moe 26` without MTP and with
`--spec-type draft-mtp --spec-draft-n-max 1`. The second control run measured
508 / 35.7, 1,340 / 42.2, and 1,459 / 37.3 prompt/decode tok/s cold for 566,
4,478, and 17,882 prompt tokens. The paired MTP run measured 470 / 46.5,
1,217 / 41.5, and 1,324 / 47.2 tok/s. Warm decode was 40.7 / 41.9 / 37.4
tok/s without MTP versus 48.5 / 50.9 / 47.2 with MTP. Acceptance remained
31/32 for the short case and 31/31 for the longer cases; all paired output
hashes matched the no-MTP `n-cpu-moe 26` control.

Conclusion: one-token MTP at `n-cpu-moe 26` has a repeatable decode benefit,
especially on warm and long decode, but also a repeatable roughly 7–9% cold
prefill penalty in this pass. It remains provisional rather than a keep because
the objective is not to trade a material prompt regression for decode speed.
The next candidate should target MTP prompt catch-up/fusion or an independent
prefill optimization; raising the draft horizon is already rejected.

### Longer decode and CPU-MoE placement follow-up — 2026-09-20

The same isolated binary and flags were rerun at `--n-predict 256` through the
benchmark driver, with a fresh no-MTP control using the same `--n-cpu-moe 26`
placement. The exact artifacts are:

- MTP: `data/performance/mtp-runtime-n1-ncpu26-n256-20260920.json`
- Control: `data/performance/control-ncpu26-n256-20260920.json`

The MTP cold prompt/decode pairs were 511 / 49.3, 1,282 / 48.5, and 1,351 /
47.2 tok/s for 566, 4,478, and 17,882 prompt tokens. Warm decode was 50.3,
51.2, and 45.4–45.7 tok/s. The matched no-MTP control was 512 / 39.6, 1,336 /
40.3, and 1,400 / 29.0 cold, with warm decode of 40.9, 36.3–39.9, and
35.8–37.0 tok/s. Thus the longer run confirms a roughly 20–25% decode gain on
short/medium requests and a roughly 24–62% gain on the long request, while
prefill is neutral-to-faster against the matched placement in this pass. All
nine response hashes matched between MTP and control; acceptance was 127/128
for the short prompt and 127/127 for both longer prompts. Warm cache counts
were exactly 562, 4,474, and 17,878.

This is the strongest runtime result so far, but the prompt is deliberately
repetitive and therefore likely favorable to the shipped MTP head. It is a
candidate keep for further varied-prompt testing, not a production approval.

I also tested the missing intermediate placement with
`--n-cpu-moe 27 --spec-type draft-mtp --spec-draft-n-max 1` at 128 generated
tokens. It projected sufficient headroom and measured cold decode 48.4 / 47.7 /
47.2 tok/s and warm decode 49.8 / 51.6 / 47.8 tok/s for short / 4k / 18k
prompts, with 63/64 or 63/63 accepted. It is comparable to ncpu26, slightly
better on the short and long cases in this single pass, but has no matched
no-MTP control yet and is therefore exploratory only. Production was stopped
for both live windows and restored healthy afterward.

### Varied-prompt correctness and acceptance gate — 2026-09-20

To test whether the strong repetitive-prompt result generalized, the isolated
ncpu26/one-token candidate and a matched no-MTP ncpu26 control were each run
against five short prompts covering explanation, Python, arithmetic, fiction,
and systems design. Each prompt was run cold and warm at temperature 0 with up
to 128 generated tokens. Artifacts:

- MTP: `data/performance/mtp-runtime-n1-ncpu26-varied-20260920.json`
- Control: `data/performance/control-ncpu26-varied-20260920.json`
- Harness: `scripts/benchmark_mtp_varied.py`

Acceptance was content-sensitive: 49/77 and 43/83 for the explanation prompt,
51/75 for Python, 58/69 for arithmetic, 44/82 for fiction, and only 1/6 for
systems design. MTP decode ranged from 28.1 to 44.6 tok/s, while the matched
control was about 40.0–43.6 tok/s on the same requests; the low-acceptance
cases did not provide a net speedup.

More importantly, none of the five MTP response hashes matched the matched
no-MTP control hashes. The explanation prompt also changed hash between MTP
cold and warm runs, while the control was stable cold-to-warm. The repetitive
benchmark remains hash-stable, so this is not explained by a universal
placement difference. Under the required deterministic-output gate, the
ncpu26/one-token runtime candidate is rejected for now despite its excellent
synthetic decode throughput. Production was stopped for the candidate and
control windows and restored healthy afterward.

A follow-up with `cache_prompt=false` repeated the explanation prompt twice on
freshly serviced requests. MTP produced hashes `4fec8781...` and `ecc5df89...`;
the matched no-MTP control produced `a9ee4d0d...` and `f2d20dbc...`. This shows
that independent no-cache requests on this SYCL setup are not themselves
bit-stable, so it does not prove that MTP alone causes all nondeterminism. It
does confirm that the cache-enabled MTP run failed the stronger cold/warm
stability check that the cache-enabled control passed. The issue remains open
for a focused determinism investigation; no candidate is approved for
deployment.

### Conservative speculation and sampler isolation — 2026-09-20

The diagnostic `--spec-draft-p-min 1.0` run emitted no drafts, measured only
35.7–38.8 decode tok/s on the representative prompt, and exactly matched the
no-MTP cold/warm hashes. This isolates the output drift to active
draft/verify/rollback rather than merely enabling the target MTP graph.

A `--spec-draft-p-min 0.75` run improved acceptance on the varied prompts to
22/25, 42/44, 52/54, 33/35, and 25/28, but decode was about 28.9–45.7 tok/s
and hashes still differed from the no-MTP control. It therefore does not
recover a safe net improvement.

Finally, `--no-spec-draft-backend-sampling` produced the same varied-prompt
hashes and acceptance counts as backend draft sampling, while making the
Python prompt substantially slower (27.4 tok/s cold versus 42.6 tok/s for the
matched control). SYCL backend sampling is not the root cause and this variant
is rejected. Production was restored after each live run.

### Forced target-checkpoint rollback diagnostic — 2026-09-20

To test whether partial KV removal caused the content-sensitive drift, an
isolated source candidate changed only the speculative verification branch to
always restore `slot.spec_ckpt` on partial acceptance. It used:

- Worktree: `/home/hudson/llama.cpp-experiments/ornith-mtp-force-ckpt-9f31776c`
- Build: `/home/hudson/llama-builds/ornith-mtp-force-ckpt-9f31776c-make`
- Binary SHA256: `9e85a96435858318d5c6398c8d27b2e781b900cdce4ae24819fe3fcdf52ff70c`
- Change: `tools/server/server-context.cpp`, forced `use_ckpt_tgt = true`

The build passed `git diff --check`, but the first varied request aborted the
isolated server with `failed to remove sequence 0 with p0=20, p1=-1` from the
server post-decode path. This diagnostic is rejected for a device-safety and
correctness failure. A real checkpoint-based fix would need coordinated prompt
token and KV bookkeeping; the one-line override is not viable. Production was
restored healthy after the failure.

The current replacement-head documentation also changes the risk assessment:
the Shisa sidecar is a BF16 head-only artifact with 19 fused `mtp.*` tensors,
initialized from Qwen3.6 and KL-distilled against Ornith-1.5. It reports much
higher acceptance than the shipped head in its own vLLM tests, but explicitly
warns that output differences were reproducible in some greedy AR/spec rows
and that automatic prefix caching was disabled for the hybrid GDN+MTP path.
It also says the sidecar must replace the native tensors in a model index; it
is not safe to copy the file over the current GGUF. Therefore an updated-head
experiment remains separate and is not a correctness fix or an automatic
deployment candidate for this abliterated target.

A final `--spec-draft-p-min 0.9` sweep reached high acceptance on the varied
set (13/14, 29/29, 40/40, 25/25, and 21/22) but delivered only 36.2–41.7
decode tok/s, at or below the no-MTP control, and still produced different
response hashes. It is rejected. The shipped head can be made conservative by
raising `p_min`, but the resulting draft rate is too low to recover a robust
speedup, and the verifier path still does not satisfy the deterministic-output
gate.

One diagnostic attempt to force sequential target verification with
`--batch-size 1 --ubatch-size 1` could not initialize: llama.cpp aborted at
`src/llama-batch.cpp:609` (`n_ubatch > n_keep_tail`). This is not a candidate
result, but it confirms that the hybrid recurrent target imposes a batching
constraint; any fix for batch-shape-dependent verification must preserve that
constraint rather than simply switching the target to one-token batches.
