# Artifacts behind the paper's results tables

Frozen copy of the analyzer outputs the paper's `tables/tab_arms.tex`,
`tab_paired.tex` and `tab_bylevel.tex` were generated from, on 2026-09-15.

- Sweep: experiment `ablate10`, 24 tasks (`benchmarks/subsets.json` → `ablate24`),
  8 arms, 192 runs, one seed, host `z-ai/glm-5.3-flash`, judge `gpt-5.4-mini`.
- `results.jsonl` is the per-run record file after re-pricing (the host model was
  initially absent from the price table; see commit 927ed0f).
- `arm_summary.csv` / `paired_binary.json` / `paired_metrics.csv` are E2's outputs;
  the `e4__`, `e5__`, `e7__`, `e8__` files are the other analyzers' outputs over the
  same pool (`--experiment ablate10`).
- Regenerate: `python -m eval.experiments.<e>.analyze --experiment ablate10`.
- Known gaps in this sweep: `vision_calls.jsonl` and `clicks.jsonl` were never
  written (tracer bug, fixed in 270b880), so RPR and first-path/recovery are absent.
