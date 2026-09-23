# Phase A gate report

API spend this phase: $0. Running total against the $50 ceiling: $0. No paid call was made.

## A2. Token length by condition

Computed with `o200k_base` on the 86 constructed reports from `diagnostic/task_b_experiment.py` (`generate_stimuli_dataset`). Artifact: `artifacts/token_length_by_condition.json`. These are the template reports, not harvested agent prose.

| Condition | n | Mean tokens | SD |
|---|---:|---:|---:|
| ORIGINAL | 86 | 27.0 | 4.9 |
| ASSERT | 86 | 30.0 | 4.6 |
| HEDGED | 86 | 36.4 | 7.1 |
| DISCLOSE | 86 | 49.8 | 8.5 |

HEDGED is 6.4 tokens longer than ASSERT (36.4 vs 30.0), not a one-word insertion. DISCLOSE is 19.8 tokens longer than ASSERT. The hedge carries the intermediate pass-rate drop (13.9 to 23.1 points) and disclosure carries 38.5 to 46.2. Length and penalty move together on these templates, and the templates are short by construction. This does not retire verbosity, and it does not make the length-matched re-run unnecessary. The pre-specified VERBOSE_CONF arm still cleared 0 of 24 units. That sentence is in Section 5.4.

## A3. Bidirectional length argument

Stated in Section 5.4 under that name. Mayo Clinic in `diagnostic/discordant_decomposition.md`: ledger 709 characters failed, full history 1,412 characters passed. In the constructed probe the longer condition is DISCLOSE and it has the lower pass rate. One comparison is across arms. The other holds a payload fixed. They are not the same test.

## A4. Join status: failed for every run

Requested key: `(task_id, arm)` between human labels and `eval/artifacts/ablate10_paper/per_run.csv` (96 rows: fifo, full_history, ledger, summary, 24 each).

`diagnostic/annotation_queue_labeled.csv` has 96 rows and 24 task ids each repeated 4 times, which is the right shape, and it has no `arm` column. 60 of 96 `final_report_text` values are the placeholder "Agent executed navigation...". The file is written by `diagnostic/task_c_rescoring.py`, which samples annotator labels around a hardcoded `true_reality` (`Simulate independent human reviews`). It is not an annotation export. Join refused for all 96 rows: arm key absent, and the file is a sampler.

`artifacts/labels.csv` has 84 rows, not 96. Arms are an incomplete mix (flat 14, ledger 13, fresh_vision 12, no_deadend 11, summary 11, fifo 9, full_history 7, no_ladder 7). Labels are `premature_completion` (50), `environment_failure` (22), `budget_exhaustion` (9), and three singletons. Annotators disagree on 3 of 84 rows. This vocabulary is not TRUE_SUCCESS / TRUE_FAILURE and does not reproduce 18/24, 19/24, 20/24, 20/24. Join refused. No run in the per-run file received a human label from this file.

Tables 3, 4, and 6 and Figure 3 were not rewritten into exact paired counts. Replacing the ranges with either of these files would present sampled or off-schema labels as the human 2×2. The ranges stay, and this report is the record that the join failed.

## A5. Ground truth for the stimulus cohort

There is no block of 43 Online-Mind2Web trajectories inside the 86. The generator builds 43 tasks × 2 harnesses. The 43 tasks are 40 regex-target task specs plus 3 fixtures.

`y*` is not the C.3 human protocol and not a regex match on a stored run. `generate_stimuli_dataset` draws a seeded reached set (37/43 and 29/43, then fixture overrides). The draw yields 65 reached and 21 not reached: 61 + 19 on the regex-task list, 4 + 2 on fixtures. Section 4 and `tab:cohort-gt` state that.

The 40-task regex-only sensitivity was not added. The pass rates on that slice would be further draws from the same verdict sampler. They are not a logged judge evaluation on deterministic ground truth. Human labels for that cohort do not exist. That annotation is 4–6 person-hours and was not started.

## A8. Remediation logs

No. Searched `judgeval/` and the paper tree for ignore-tone, two-stage, and remediation call logs. None. Section 7.1–7.3 were not logged with per-call records. Paid item T5 is not covered by an existing log. The prompt texts are in Appendix C.3 with SHA-256 hashes and are marked not executed, consistent with Appendix E.

## What Phase B still needs

| Item | Still needed? | Why |
|---|---|---|
| Length-matched VERBOSE_CONF re-run | Yes, if verbosity is to be separated | A2 does not refute verbosity. Gate clearance is 0/24. |
| Human labels on the 96 memory runs | Yes | A4 failed. Exact tables cannot be built until a real per-run file with `task_id` and `arm` exists. |
| Ground truth for a live 43-trajectory Online-Mind2Web stimulus block | The block is not in the 86 | If the panel is to rest on executed trajectories, those runs and their `y*` labels have to be created. The current 65/21 split is the seeded draw. |
| Regex-only sensitivity of Table 1 | Not from current logs | Needs real judge calls on the 40 tasks, or it will be another slice of the sampler. |
| Remediation strategies 1–3 | Yes, if Contribution 5 is to include them | No call log. Do not schedule them until the $50 line is approved. |
| More judges on Figure 2 | Only after real calls | The axes leave room. Do not plot a judge that was not run. |

Stop. No paid phase is authorized.
