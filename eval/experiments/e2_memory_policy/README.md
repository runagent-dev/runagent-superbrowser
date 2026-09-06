# e2_memory_policy — Matched memory policies: full history / FIFO / LLM summary / Ledger

**Question.** Holding model, prompts, vision tier, click system, task ids, budgets and evaluator fixed, does the structured Ledger + eviction retain task-critical state and succeed more often than recency or LLM compression, and at what cost relative to full history?

**Arms.** `ledger` (structured Ledger + six-phase eviction), `fifo` (last K turns verbatim), `summary` (last K turns + one regenerated LLM summary, same host model), `full_history` (nothing evicted, no Ledger); `--with-noevict` adds `ledger_noevict` (Ledger without eviction). Same K (5) and budget (2048 tokens) everywhere; `SUPERBROWSER_MEMORY_POLICY` is the only difference between arms.

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** TSR; mean/peak prompt tokens per iteration; steps, tool calls, vision calls, compressor calls; cost (list and cache-discounted); **CSD** (observation-derived critical state present at reuse; task-given floor check); DRR and repeated actions. Outputs `memory_table.tex`, `paired_binary.tex`, the accuracy–cost plane and the success-vs-peak-context plane.

**Confirmatory comparisons.** `ledger` vs `fifo`, `ledger` vs `full_history`.

```bash
python -m eval.experiments.e2_memory_policy.run --dry-run            # schedule + env
python -m eval.experiments.e2_memory_policy.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e2_memory_policy.analyze                  # -> eval/artifacts/e2_memory_policy/
```
Pre-registered: **C1** `ledger` vs `fifo` on TSR (exact McNemar + bootstrap CI), **C2** `ledger` vs `full_history` on cost with non-inferior TSR. Everything else is secondary. Use `--seeds 3` for the paper run.
