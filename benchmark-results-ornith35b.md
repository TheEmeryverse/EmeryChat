# Ornith 1.5 35B — Intel Arc B580 benchmark dashboard

Last updated: 2026-09-20 UTC  
Production service: BMG AOT active on port 8081; stock unit preserved inactive for rollback.

## Frozen test identity

- GPU: `Intel(R) Arc(TM) B580 Graphics`, architecture `intel_gpu_bmg_g21`
- Backend: SYCL / Level Zero only (`ONEAPI_DEVICE_SELECTOR=level_zero:gpu`)
- Context: 131072; one slot; `--n-cpu-moe 24`; q4_0 K/V
- Batch: 2048 / ubatch 2048; threads 16 / batch threads 16
- Prompt targets: 512, 8192, 32768 (actual approximately 566, 8942, 35750 tokens)
- Repetitions: one cold request plus two prompt-cache warm requests
- Output: deterministic, temperature 0, seed 1718, 128 generated tokens

Device verification is recorded before each test in the corresponding `logs/device-*.log` file.

## Control

| Profile | Cold prefill | Cold decode | Warm decode median |
|---|---:|---:|---:|
| Short | 308.71 t/s | 38.62 t/s | 42.58 t/s |
| Medium | 1277.59 t/s | 37.14 t/s | 37.13 t/s |
| Long | 1262.95 t/s | 32.86 t/s | 30.86 t/s |

Raw: [`ornith-prodconfig-35b-128k.json`](/home/hudson/EmeryChat/data/ornith-prodconfig-35b-128k.json)  
Server log: [`ornith-prodconfig-35b.log`](/home/hudson/EmeryChat/logs/ornith-prodconfig-35b.log)

## Candidate results

| Candidate | Short | Medium | Long | Decision |
|---|---|---|---|---|
| BMG AOT target (`bmg_g21`) | prefill +74.2%; decode +6.4% cold | prefill +18.3%; decode +13.9% cold | prefill +0.3%; decode +1.2% cold | **Global keeper** |
| DMMV priority | prefill +73.0%; cold decode -7.3% | prefill +16.6%; cold decode +0.7% | prefill -0.2%; cold decode -0.8% | Profile keeper; varied warm-turn gain |
| Offload threshold 16 | prefill +74.9%; warm decode -11.0% | prefill +17.5%; warm decode neutral | prefill -0.6%; warm decode -0.3% | Warm-turn/profile keeper only |
| Offload threshold 8 | prefill +72.7%; warm decode -8.1% | prefill +17.5%; warm decode neutral | prefill -0.7%; warm decode -3.2% | Rejected: unstable varied hashes |
| Attention VEC decode | — | — | decode severely lower | Rejected |
| MMV_Y=2 / MMV_Y=4 | — | — | correctness run stalled | Rejected |
| KQPI=2 | prefill +44.1%; cold decode +4.6% | prefill -18.0%; cold decode +9.9% | prefill -26.2%; cold decode -7.3% | Short and warm-medium profile only; long rejected |
| Graph single-token `MUL_MAT_ID` | correctness passed; server init stalled | server init stalled | server init stalled | Rejected: hang before inference |

## Combination pass — percentage versus stock control

Percentages below are `(candidate / stock - 1) * 100`; positive means faster.
Cold values are prefill/decode. Warm values are decode medians from the two
cache-reused requests.

| Candidate | Short cold pp / decode; warm decode | Medium cold pp / decode; warm decode | Long cold pp / decode; warm decode | Decision |
|---|---|---|---|---|
| BMG AOT alone | **+74.2% / +6.4%; +5.0%** | **+18.3% / +13.9%; +12.4%** | **+0.3% / +1.2%; +5.7%** | **Global keeper** |
| AOT + DMMV | +71.8% / -7.3%; -13.3% | **+17.9% / -2.2%; -1.1%** | -0.2% / -5.4%; -2.4% | Medium keeper only |
| AOT + offload-16 | +67.5% / -8.7%; -4.8% | **+14.2% / +8.3%; -3.4%** | -3.1% / -4.4%; +7.1% | Medium keeper only |
| DMMV + offload-16 | +64.1% / -13.9%; -25.5% | **+17.0% / +11.9%; +4.7%** | -1.5% / -6.2%; -0.6% | Medium keeper only; short warm unstable |
| AOT + DMMV + offload-16 | +66.2% / -10.7%; -12.3% | **+17.5% / -0.8%; -1.1%** | -1.1% / -5.8%; -2.0% | Medium keeper only |

The combination pass did not beat AOT alone globally. AOT alone remains the
best short/medium/long choice; the runtime controls add no repeatable long-
context benefit and generally trade away decode speed.

### Important interpretation correction: total turn latency

The throughput table above is correct for its labeled axes, but it should not
be read as equivalent end-to-end turn-time improvement. AOT speeds up prompt
evaluation substantially, while decode remains a large part of each 128-token
request. Comparing the raw `wall_s` fields gives:

| Profile | Stock cold wall | AOT cold wall | Cold total-time change | Stock warm wall | AOT warm median | Warm total-time change |
|---|---:|---:|---:|---:|---:|---:|
| Short | 5.156s | 4.154s | **19.4% faster** | 3.103s | 2.963s | **4.5% faster** |
| Medium | 10.572s | 9.030s | **14.6% faster** | 3.584s | 3.189s | **11.0% faster** |
| Long | 32.467s | 32.297s | **0.5% faster** | 4.385s | 4.116s | **6.1% faster** |

The stock control JSON contains one warm sample per profile (`repeats=1`),
while the AOT JSON contains two (`repeats=2`), so the warm comparison is not a
perfectly matched repetition count. The cold comparison is directly matched
on prompt lengths, seed, output length, flags, and model. A follow-up paired
stock/AOT run with identical repeat counts would be needed before claiming a
final production latency percentage.

Combination raw records:

- [`ornith-combo-aot-dmmv-35b-128k.json`](/home/hudson/EmeryChat/data/ornith-combo-aot-dmmv-35b-128k.json)
- [`ornith-combo-aot-offload16-35b-128k.json`](/home/hudson/EmeryChat/data/ornith-combo-aot-offload16-35b-128k.json)
- [`ornith-combo-dmmv-offload16-35b-128k.json`](/home/hudson/EmeryChat/data/ornith-combo-dmmv-offload16-35b-128k.json)
- [`ornith-combo-aot-dmmv-offload16-35b-128k.json`](/home/hudson/EmeryChat/data/ornith-combo-aot-dmmv-offload16-35b-128k.json)

### KQPI=2 exact measurements

| Profile | Cold prefill | Cold decode | Warm decode median | Output hashes |
|---|---:|---:|---:|---|
| Short | 444.81 t/s | 40.42 t/s | 44.63 t/s | stable |
| Medium | 1047.48 t/s | 40.82 t/s | 38.88 t/s | stable |
| Long | 931.56 t/s | 30.47 t/s | 29.58 t/s | stable |

Raw: [`ornith-bench-kqpi2-35b-128k.json`](/home/hudson/EmeryChat/data/ornith-bench-kqpi2-35b-128k.json)  
Server log: [`ornith-bench-kqpi2-35b.log`](/home/hudson/EmeryChat/logs/ornith-bench-kqpi2-35b.log)  
Benchmark log: [`ornith-bench-kqpi2-35b-benchmark.log`](/home/hudson/EmeryChat/logs/ornith-bench-kqpi2-35b-benchmark.log)

## Current keeper ledger

1. BMG AOT `GGML_SYCL_DEVICE_ARCH=bmg_g21`: global 128k keeper.
2. AOT+DMMV: medium cold keeper; not global.
3. AOT+offload-16: medium cold keeper; not global.
4. DMMV+offload-16: medium cold keeper; short warm behavior unstable.
5. AOT+DMMV+offload-16: medium cold keeper; not global.
6. Standalone DMMV, standalone offload-16, and KQPI=2 remain profile candidates from the earlier pass.

The next pass tunes each surviving keeper independently, then selects the best combined configuration. A keeper is not deployed without explicit approval.

## Live artifacts

- Detailed plan: [`plan-ornith15-9b-b580.md`](/home/hudson/EmeryChat/plan-ornith15-9b-b580.md)
- Graph correctness: [`correctness-graph-matid.log`](/home/hudson/EmeryChat/logs/correctness-graph-matid.log), 2039/2039 tests passed
- Graph device diagnostic: [`device-correctness-graph-matid.log`](/home/hudson/EmeryChat/logs/device-correctness-graph-matid.log)
- Graph benchmark server log: [`ornith-bench-graph-matid-35b.log`](/home/hudson/EmeryChat/logs/ornith-bench-graph-matid-35b.log); stopped before inference after initialization stalled

## Production deployment

- Active unit: [`llama-ornith-aot.service`](/home/hudson/.config/systemd/user/llama-ornith-aot.service)
- Preserved rollback unit: [`llama-ornith.service`](/home/hudson/.config/systemd/user/llama-ornith.service), inactive
- Active binary: `/home/hudson/llama-builds/ornith-bmg-aot-9f31776c-make/bin/llama-server`
- Active binary SHA256: `93b977791bdce09e285be0a6dfe70ef8d7bcfffc4a4c0ce654258594d545cf4d`
- Configuration: `GGML_SYCL_DEVICE_ARCH=bmg_g21`, Level Zero selector, no DMMV or offload-threshold overrides
- Smoke result: completed `READY.` response with EOS; device diagnostic recorded in [`device-production-aot-smoke2.log`](/home/hudson/EmeryChat/logs/device-production-aot-smoke2.log)
