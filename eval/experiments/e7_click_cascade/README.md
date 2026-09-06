# e7_click_cascade — Click recovery ladder on/off

**Question.** How often does the first click path succeed, how often does escalation recover a silent click, and does removing the ladder change task success?

**Arms.** `ledger` vs `no_ladder` (`ABLATE_CLICK_LADDER=1` + `CLICK_LADDER_AUTO=0` + `SUPERBROWSER_CLICK_TIERS=tier1`; the last one is read by the TypeScript server, so use `--manage-server` or restart the server with it).

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** First-path execution success, recovery success (escalations landed / (escalations + silent)), escalation strategies, snapper methods, grounding errors, TSR.

**Confirmatory comparisons.** `ledger` vs `no_ladder`.

```bash
python -m eval.experiments.e7_click_cascade.run --dry-run            # schedule + env
python -m eval.experiments.e7_click_cascade.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e7_click_cascade.analyze                  # -> eval/artifacts/e7_click_cascade/
```
Framed as execution robustness across browser control paths. If the effect is small, the cascade is demoted to an implementation detail.
