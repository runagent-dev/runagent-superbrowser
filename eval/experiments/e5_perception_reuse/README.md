# e5_perception_reuse — Perception reuse on/off

**Question.** How much redundant perception (vision calls, cost, latency) does reuse of grounded perception save, and does the saving shrink on pages that change more?

**Arms.** `ledger` vs `fresh_vision` (`ABLATE_VISION_REUSE=1`: no prefetch, no vision cache, vision epoch expires every mutating turn, no `[CACHED VISION]` piggyback).

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** **RPR** (vision passes with an unchanged page fingerprint / all passes; cached vs uncached), vision calls, tool calls, wall clock, cost, TSR; page-churn strata (stable / mild / dynamic, terciles of the ledger arm's DOM-hash churn) in `churn_strata.tex`.

**Confirmatory comparisons.** `ledger` vs `fresh_vision`.

```bash
python -m eval.experiments.e5_perception_reuse.run --dry-run            # schedule + env
python -m eval.experiments.e5_perception_reuse.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e5_perception_reuse.analyze                  # -> eval/artifacts/e5_perception_reuse/
```
Prediction: the saving from reuse shrinks as page churn grows.
