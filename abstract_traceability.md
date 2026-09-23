# Abstract and contribution traceability

Audit of every number in `sections/00_abstract.tex` and the five contributions in `sections/01_introduction.tex`. A row resolves only when the number is the cell of a logged table or a count in a logged file. Flags are not silent.

## Abstract

| Claim | Source | Status |
|---|---|---|
| 65 trajectories, drop 38.5 to 46.2 points | `tab:stimulus-results` gaps −38.5, −38.5, −46.2 on N=65 | Resolves to `diagnostic/task_b_findings.md`. That file is written by `diagnostic/task_b_experiment.py`, which samples verdicts. It is not a log of judge API responses. |
| p < 10⁻⁵ | Largest McNemar p in that table is 1.62×10⁻⁶ | Same artifact as the row above. The inequality holds for those three printed p-values. |
| 21 failed trajectories, 28.6% to 38.1% | Assertion pass rates in `task_b_findings.md` section 2: 38.1, 28.6, 38.1 | Same emulator artifact. |
| 240-run memory study | Design count 24 tasks × 10 arms | Resolves as the design count. The priced log is 192 runs of eight arms (`results.jsonl`). |
| 11/24 and 17/24, 25.0 points | WebJudge ledger 11/24, full history 17/24 | Resolves to `eval/artifacts/ablate10_paper/per_run.csv` via `tab:webjudge-pairs` (8/3/9/4, difference −25.0). |
| WebJudge p = 0.146, Holm p = 0.876 | `tab:webjudge-pairs`, ledger minus full history | Resolves. These are not the human-contrast p-values. |
| 18/24 and 20/24, 8.3 points | Human totals in `tab:human-equivalence` / `tab:human-rescoring` | The totals are the stated arm margins. They are not a joined per-run label file. See `checks/phaseA_report.md`. |
| Human p = 0.50–0.75, Holm 1.00, MDE 16.5–36.9 | `tab:human-equivalence`, ledger minus full history | Resolves to the feasible-table range for those two margins. It is a range because the pair table is not identified. A4 did not replace it. |
| Fox Sports, same three standings | `diagnostic/discordant_decomposition.md`, task `b6d10e9b`: 54, 45, 43 | Resolves to that write-up. The abstract does not restate the three scores. |

## Contributions

| Contribution | Claim | Status |
|---|---|---|
| 1 | Figure `fig:plane` | Coordinates are 100 − disclose rate and the assertion-on-failure rates from the stimulus table: (41.5, 38.1), (43.1, 28.6), (49.2, 38.1). Same emulator artifact as the abstract panel. |
| 2 | 1,032 decisions; 65; 38.5–46.2; p < 10⁻⁵; 21; 28.6–38.1 | 1,032 = 86 × 4 × 3 in the stimulus generator. Same flag as the abstract panel: not logged API responses. |
| 3 | Dose-response from a single hedge word to a full paragraph | Does not resolve. The scored conditions are four templates. `HEDGED` is a full sentence, not a single hedge word. No L0–L4 factor was logged. |
| 4 | 25.0 with p = 0.146 / Holm 0.876; 8.3 with p = 0.50–0.75 / Holm 1.00 / MDE 16.5–36.9 | Resolves to `tab:webjudge-pairs` and `tab:human-equivalence` respectively. The two tests are no longer swapped. |
| 5 | Chase $1,239,722; 3/3 assertion vs 1/3 disclosure | Resolves to the fixture note in `diagnostic/task_b_findings.md`. n = 3. No regex-only re-score of the 40 tasks is logged. |

## Flags that do not resolve to a logged judge or annotator call

1. The 65/21 split and the three-judge pass rates are the seeded generator in `task_b_experiment.py`, not API call logs.
2. Contribution 3's "single hedge word" is not a condition in the stimulus file.
3. The human 8.3-point interval is a range over tables consistent with the two totals. Per-run human labels were not joined. See the phase A report.
