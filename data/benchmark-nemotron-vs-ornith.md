# Nemotron vs. Ornith benchmark

Date: 2026-09-20

Both models were tested through the existing llama.cpp OpenAI-compatible
benchmark environment on the Intel Arc B580. The workload used a fixed seed,
temperature 0.2, top-p 0.95, top-k 20, thinking enabled, a 512-token output
cap, one warmup request, and two measured requests per case.

## Results

| Model/configuration | Decode tok/s (median across cases) | Prompt tok/s (median across cases) |
| --- | ---: | ---: |
| Ornith 1.5 35B A3B Q4_K_M | 45.0 | 35.2 |
| Nemotron 3.5 Lightning 30B A3B Q4_K_M, `--n-cpu-moe 40` | 25.8 | 30.3 |

Nemotron reached about 57% of Ornith's decode throughput and was not a
production improvement for this workload. The JSON and arithmetic checks were
correct. The code and long-list prompts hit the shared 512-token cap while
thinking was enabled; the code response began correctly but did not reach the
requested `ValueError` branch before the cap.

## Selected B580 tuning

- `--n-cpu-moe 40`: 25.6 tok/s on the local 512-token llama.cpp workload.
- All CPU MoE (`--cpu-moe`): about 22.5–23.7 tok/s.
- `--n-cpu-moe 48`: 24.5 tok/s.
- 32 CPU threads: 19.4 tok/s; 16 threads was retained.
- `--n-cpu-moe 24` did not become ready within a practical load window and was discarded.

The final production state was restored to the original Ornith command and
EmeryChat was restarted. Nemotron was not promoted.

Raw results:

- [`benchmark-ornith.json`](benchmark-ornith.json)
- [`benchmark-nemotron-b580.json`](benchmark-nemotron-b580.json)
- [`benchmark-nemotron-b580-ncpu40.json`](benchmark-nemotron-b580-ncpu40.json)
