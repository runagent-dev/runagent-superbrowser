# Artifacts behind the paper's results tables

Frozen copy of everything the paper's §7 tables and prose numbers are generated from.
Regenerate the paper's `tables/*.tex` and `numbers.tex` with

```bash
python -m eval.experiments.paper_tables            # writes ../paper/tables/ and the golden copy in tables/
python -m eval.experiments.paper_tables --check    # golden test (also eval/tests/test_paper_tables.py)
```

- Sweep: experiment `ablate10`, 24 tasks (`benchmarks/subsets.json` → `ablate24`; composition history in
  `PROTOCOL.md` "As-run deviations"), 8 arms, 192 runs, one seed, host `z-ai/glm-5.3-flash`, judge `gpt-5.4-mini`.
- `results.jsonl` — the 192 per-run records **with the analyzers' process metrics** (rebuilt from the per-run
  `run_record.json` files by `python -m eval.core.record --rebuild-results --experiment ablate10 --to <here>`).
  This is the single input of `paper_tables`; every number in the paper is a function of it plus
  `benchmarks/exclusions.json` (one pre-registered task exclusion → primary n = 23).
- `audit.json` / `audit.md` — `python -m eval.core.audit_sweep --experiment ablate10`: provenance checks
  (protocol hash, host model per run, code drift per arm, non-browser tool executions, instruction wording vs the
  frozen benchmark, trace presence, peak context vs the host snip threshold, judge input per arm, infra-error
  markers, archived directories, cost).
- `arm_summary*.csv/json`, `paired_binary.json`, `paired_metrics.csv`, `per_run.csv`, `memory_table.csv` — E2's
  analyzer outputs; the `e4__`, `e5__`, `e7__`, `e8__` files are the other analyzers' outputs over the same pool
  (`--experiment ablate10`). `arm_summary_all_tasks.csv` is the n = 24 sensitivity summary.
- `modelsplit__per_run.csv` — the eight-model single-task study behind §7.10 (6 of 8 models completed the fixed task).
- `e11_cost__*` — cost per arm over all 24 tasks; `e12_traces__*` — discordant-pair excerpts (selected after the aggregate).
- `tables/` — the golden copy of the generated TeX.

Known gaps in this sweep (all disclosed in the paper): `vision_calls.jsonl` and `clicks.jsonl` were never written
(tracer bug, fixed in 270b880), so the redundant-perception rate and click-recovery success are absent;
first-path success is tag-derived. Nothing under `eval/artifacts/` other than this directory and `e6_subelement/`
is sweep evidence: `e0_headline_audit/` is a 2-task dev-set audit, `e9_evaluator_validation/` has no human labels,
and `e11_cost/` is regenerated on demand.
