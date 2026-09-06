# e4_deadend — Dead-end memory on/off

**Question.** Does remembering failures as first-class dead-ends reduce revisits of failed states and step inflation, holding everything else fixed?

**Arms.** `ledger` vs `no_deadend` (`ABLATE_DEAD_END_MEMORY=1`: failures are not recorded as dead-ends, no `DEAD_ENDS` sections, no `[DEAD_ENDS_HERE]` injections).

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** **DRR** = revisits of a previously failed action signature (normalised url, tool, target label) / distinct failed signatures; repeated actions; dead-click / same-element guard refusals; url revisits; steps; TSR.

**Confirmatory comparisons.** `ledger` vs `no_deadend`.

```bash
python -m eval.experiments.e4_deadend.run --dry-run            # schedule + env
python -m eval.experiments.e4_deadend.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e4_deadend.analyze                  # -> eval/artifacts/e4_deadend/
```
Turns 'failures are first-class memory' from a design story into a paired measurement.
