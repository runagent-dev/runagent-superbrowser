# e0_headline_audit — rebuild the headline from raw records

**Question.** What is the exact k/N behind the headline task-success number, under which denominator?

**Inputs.** `eval/runs/e1_main/results.jsonl` (or any experiment/arm via `--experiment/--arm`),
`eval/benchmarks/manifest.json`, `eval/benchmarks/exclusions.json`.

**Outputs** (`eval/artifacts/e0_headline_audit/`): `audit.json`, `headline.csv/.tex` with three scopes —
all evaluated tasks, minus pre-registered exclusions, minus impossible tasks (deterministic markers) —
each as `k/N (%)` with Wilson 95% CI, plus per-site-family counts, which evaluator decided each run, missing
tasks, and the open reconciliation of the draft's "66 tasks / 89.47%" (not an integer multiple of 1/66)
against the frozen 74-task split.

```bash
python -m eval.experiments.e0_headline_audit.analyze            # after E1 has run
```
