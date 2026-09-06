# e10_robustness — Budget sweep, seed variance, cross-model check

**Question.** Does the memory result hold across history budgets, repeated seeds and a second host model, and where does bounded memory lose?

**Arms.** `--sweep budget`: `{ledger,fifo,summary}__B{1024,2048,3072}` (history budget 0.5×/1×/1.5×); `--sweep window`: `__K{3,5,8}`; `--sweep seeds --seeds 3`: plain arms repeated; cross-model: rerun with `--model <second host>`.

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** TSR (+ per-seed SE), peak prompt (mean/max), cost per (model, policy, variant); `where_bounded_memory_loses.csv` lists cells where a bounded policy trails full history (negative results are reported, not hidden).

**Confirmatory comparisons.** none (descriptive).

```bash
python -m eval.experiments.e10_robustness.run --dry-run            # schedule + env
python -m eval.experiments.e10_robustness.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e10_robustness.analyze                  # -> eval/artifacts/e10_robustness/
```

