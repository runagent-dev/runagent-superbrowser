# e1_main — Full system on Online-Mind2Web hard

**Question.** What is the task success rate, cost and step profile of the full system on the frozen 74-task hard split, by level and site family?

**Arms.** `ledger` (full system).

**Task set / seeds.** `all` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** TSR with Wilson CI (raw counts always shown), by site family and level; worker/orchestrator iterations, tool calls, vision calls, prompt tokens per iteration (mean/peak), wall clock, USD per task and per successful task; failure-reason distribution.

**Confirmatory comparisons.** none (descriptive).

```bash
python -m eval.experiments.e1_main.run --dry-run            # schedule + env
python -m eval.experiments.e1_main.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e1_main.analyze                  # -> eval/artifacts/e1_main/
```
E0 (`e0_headline_audit`) recomputes the headline from these records with and without the pre-registered exclusions. Run on the full split: `--tasks all`.
