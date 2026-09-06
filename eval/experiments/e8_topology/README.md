# e8_topology — Orchestrator->worker vs flat single agent

**Question.** With identical tools, memory hook and budgets, does the strategic orchestrator layer change success, step count, repeated actions or premature completion, and what is its overhead?

**Arms.** `ledger` (Orchestrator → `delegate_browser_task` → Worker) vs `flat` (one agent with the browser tools registered directly, same memory hook, worker hook and iteration cap; `SUPERBROWSER_TOPOLOGY=flat`, read by the eval runner).

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** TSR, worker iterations, tool calls, repeated actions, premature completion (run ended with a claimed answer that the evaluator rejected), orchestrator overhead (iterations and token share).

**Confirmatory comparisons.** `ledger` vs `flat`.

```bash
python -m eval.experiments.e8_topology.run --dry-run            # schedule + env
python -m eval.experiments.e8_topology.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e8_topology.analyze                  # -> eval/artifacts/e8_topology/
```
The paper's periodic Planner exists only in the disabled TypeScript agent, so role separation is tested as a topology ablation on the real pipeline.
