# e3_memory_pressure — Memory-pressure ladder (distractor tokens per step) x memory policy

**Question.** As irrelevant observation volume grows with task semantics fixed, do FIFO and LLM-summary lose task-critical state faster than the structured Ledger (flatter CSD/TSR degradation)?

**Arms.** `{fifo,summary,ledger}__p{0,1500,4000}`: the same policies under 0 / 1500 / 4000 distractor tokens appended to every tool result (`SUPERBROWSER_EVAL_DISTRACTOR_TOKENS`); the distractor block is deterministic (seeded by task, seed, iteration, tool call) and marker-free, so task semantics are unchanged and only history volume grows.

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** TSR, CSD (observation-derived) and peak prompt per (policy, level); Cochran–Armitage trend on TSR and Spearman trend on CSD per policy (`trends.csv`); line plots per metric.

**Confirmatory comparisons.** none (descriptive).

```bash
python -m eval.experiments.e3_memory_pressure.run --dry-run            # schedule + env
python -m eval.experiments.e3_memory_pressure.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e3_memory_pressure.analyze                  # -> eval/artifacts/e3_memory_pressure/
```
Prediction: FIFO/summary degrade faster than the Ledger. This is a construct-validity ladder — the metrics must move in the predicted direction with pressure, or CSD is not measuring durability.
