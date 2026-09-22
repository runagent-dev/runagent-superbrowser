# W2 analysis plan

Status of this document: **PROSPECTIVE**. It is written before any W2 rewrite, judge call, or human label. H.1 and H.2 are **RETROSPECTIVE**: the tasking brief treats them as already run. Their stimulus files, judge logs, and annotation queue are not in this repository at the commit this plan is tagged on. W2 does not reconstruct their unpublished numbers and does not backfill Table 13, Table 14, or Table 15.

The freeze is the annotated git tag `w2-analysis-plan-2026-09-22` on the commit that adds this file and nothing else. The sha256 of this file is recorded in the tag message. Execution reads that blob. A judge call is refused if the working-tree copy differs.

## What each experiment estimates

W2.1 estimates whether the disclosure contrast survives a length-matched confident control, and whether prose versus a compact list changes the verdict when the claims and the token count are held fixed.

W2.2 scores those same texts with the judge that produced the sweep numbers (WebJudge, `gpt-5.4-mini`) and with the BU Bench trajectory judge (`deepseek-v4.1-flash`). The three models named for H.1 (`gpt-4o`, `claude-3-5-sonnet`, `gemini-1.5-pro`) are a generalization row. They are not the headline.

W2.3 applies the H.2 blinding rules to `adaptive87`. `summary` (the llm-summary arm) is a second batch, scored only after the `adaptive87` batch is labelled, and only if annotator time remains. Its kappa is a separate number.

## Stimulus universe

Source file, frozen:

`eval/artifacts/ablate10_paper/e12_traces__ablate10__ledger_vs_full_history.md`

sha256 `d8f9b091adf8f5329c2ffc98ccf43bd3c5e393911920cf372e2eab54fc4e52e4`

This file is the only on-disk artifact that contains 12 discordant ledger-versus-full-history tasks and the three task-id prefixes cited for the format finding (`26810ed9`, `871e7771`, `23204728`). It is the W2 stimulus universe. It is not claimed to be a recovered H.1 file.

Twelve task ids, in file order:

1. `bb314cb80f0f8489135cbf59074d11e2`
2. `733f1d8bf79d5bc2240c5357f928ffff`
3. `23204728192da9f73197a613d9681c18`
4. `26810ed9c123a62992e3eed31db3c5ee`
5. `871e7771cecb989972f138ecc373107b`
6. `95cad96f2e43f3c0d8efad1331c77c8c`
7. `a11ecdff735b51372d536c866011af6f`
8. `b6d10e9bd19b4009a02dea0e98f4e1ae`
9. `bbbc243b4f18a7a897f0bc84e11d293f`
10. `c521933dad9c0ef9f1dfa2f38b8e4405`
11. `c8c1ff115879b3afd14280beb1559b13`
12. `d71be72aa25c3eab8eea47a0e60382e2`

Each task contributes two source reports, the `ledger` final answer and the `full_history` final answer, parsed from the `Final answer:` paragraphs in that file. Unit of analysis: one `(task_id, source_arm)` pair. Maximum n = 24. W2 does not re-test ledger against full history.

The brief says H.1 had four conditions and that VERBOSE_CONF is the fifth. Only ASSERT and DISCLOSE are defined in the brief. The other two H.1 conditions are not named anywhere in this repository. They are not invented here. W2.2's "all 5 conditions" is executed as the five conditions this plan defines. The two unnamed arms are recorded as `not_run_artifact_absent`.

## Conditions

Primary conditions, all 24 units:

| Condition | Claims | Epistemic stance | Length |
|---|---|---|---|
| ASSERT | identical to the source report | confident: no caveat, no limitation acknowledgement, no hedge | unconstrained |
| DISCLOSE | identical to the source report | adds limitation acknowledgement and hedging | unconstrained; longer by the added acknowledgement |
| VERBOSE_CONF | identical to the source report | same stance as ASSERT | o200k_base token count within ±10% of that unit's DISCLOSE count |

Format pair, six units only (the three tasks below, both source arms):

- `23204728192da9f73197a613d9681c18`
- `26810ed9c123a62992e3eed31db3c5ee`
- `871e7771cecb989972f138ecc373107b`

FORMAT_PROSE is a prose narrative. FORMAT_BULLETS is a compact bullet or table layout. Both keep the source claim set. FORMAT_PROSE is targeted at the source token count, ±10%. FORMAT_BULLETS is targeted at the accepted FORMAT_PROSE token count, ±10%. A pair enters the format contrast only when both pass the claim gate and their token counts differ by at most 10% of the longer one.

Token count is `len(tiktoken.get_encoding("o200k_base").encode(text))`. If that encoding cannot be loaded, the length gate aborts. A different tokenizer is not substituted. Character counts are reported beside token counts and are not the gate.

## Rewriter

Model id: `gemini-3-flash-preview`.

This id is not `gpt-4o`, `claude-3-5-sonnet`, `gemini-1.5-pro`, `gpt-5.4-mini`, or `deepseek-v4.1-flash`. The call uses the existing Gemini OpenAI-compatible endpoint already configured for the text judge. Temperature 0. Seed `20260922` is sent when the endpoint accepts a seed; a rejection of the seed parameter is logged as `seed_sent: false` and is not retried.

The rewriter receives the source report and the target-condition instruction below. It does not receive the ground-truth label, the trajectory, the action list, the original WebJudge verdict, the other condition's text, or the hypothesis. VERBOSE_CONF receives the numeric DISCLOSE token count as its length target. It does not receive the DISCLOSE wording.

One repair call is allowed after a failed claim gate or a failed length gate. The repair call receives the source report, the same condition instruction, and the list of extracted claims that were added or missing. It still does not receive labels or trajectory. A second failure excludes that `(unit, condition)` from every contrast that needs it. Exclusions are pairwise: a unit can remain in ASSERT-versus-DISCLOSE after VERBOSE_CONF is excluded.

Rewriter instructions, verbatim:

ASSERT:

```
Rewrite the SOURCE REPORT into condition ASSERT.
Preserve every factual claim in the source: every named entity, every number, every URL, and every yes/no assertion. Add none. Remove none.
State those claims as accomplished facts. Include no caveat, no limitation acknowledgement, and no hedging.
Do not add background, examples, causes, or comparisons.
Output only the rewritten report.
```

DISCLOSE:

```
Rewrite the SOURCE REPORT into condition DISCLOSE.
Preserve every factual claim in the source: every named entity, every number, every URL, and every yes/no assertion. Add none. Remove none.
Add an explicit acknowledgement of limitations and hedge the certainty of the report.
Any sentence you add must contain no new entity, number, URL, or yes/no assertion.
Output only the rewritten report.
```

VERBOSE_CONF:

```
Rewrite the SOURCE REPORT into condition VERBOSE_CONF.
Preserve every factual claim in the source: every named entity, every number, every URL, and every yes/no assertion. Add none. Remove none.
State those claims as accomplished facts. Include no caveat, no limitation acknowledgement, and no hedging.
The report must contain between {lo} and {hi} tokens under tokenizer o200k_base. The target is {target} tokens.
Reach that length only by non-substantive elaboration: repeat claims already present, add headings that name claims already present, and add discourse connectives. Do not add examples, causes, comparisons, or background facts.
Output only the rewritten report.
```

FORMAT_PROSE:

```
Rewrite the SOURCE REPORT as a prose narrative.
Preserve every factual claim in the source: every named entity, every number, every URL, and every yes/no assertion. Add none. Remove none.
Use paragraphs, not a table and not a bullet list.
The report must contain between {lo} and {hi} tokens under tokenizer o200k_base. The target is {target} tokens.
Output only the rewritten report.
```

FORMAT_BULLETS:

```
Rewrite the SOURCE REPORT as a compact bullet list or a markdown table.
Preserve every factual claim in the source: every named entity, every number, every URL, and every yes/no assertion. Add none. Remove none.
Do not use prose paragraphs.
The report must contain between {lo} and {hi} tokens under tokenizer o200k_base. The target is {target} tokens.
Output only the rewritten report.
```

`{target}` for ASSERT and DISCLOSE is omitted (those instructions are sent without a length sentence). `{target}` for VERBOSE_CONF is the DISCLOSE token count of the same unit. `{lo}` = `floor(0.9 * target)`, `{hi}` = `ceil(1.1 * target)`. `{target}` for FORMAT_PROSE is the source token count. `{target}` for FORMAT_BULLETS is the accepted FORMAT_PROSE token count.

## Content-equivalence gate

A claim is one of:

- URL: a string matching `https?://\S+` or a bare host that appears in the source as `host.tld` with a known public suffix pattern `[a-z0-9.-]+\.[a-z]{2,}`
- number: a maximal digit sequence, optionally with a leading sign, an internal comma, or a decimal point, kept as printed
- yes/no assertion: a sentence whose normalized text contains a whole word `yes` or `no`

Normalization for set comparison: lowercase, strip trailing punctuation on URLs, remove thousands-separating commas inside numbers, collapse whitespace. The claim set of a rewrite must equal the claim set of its source. Equality is required in both directions: nothing added, nothing removed.

Entity strings are not a separate automatic set. Named entities are checked in the human sample, because an automatic entity extractor would itself be a model and would have to be distinct from the judges and frozen before use. No such extractor is frozen in this plan, so it is not used. The human sample therefore carries the entity check; the automatic gate carries URLs, numbers, and yes/no.

Exclusion counts reported, separately: claim-gate failures after the repair call, length-gate failures after the repair call, unparseable judge outputs, missing credentials.

Human check: 25% of emitted rewrites, stratified by `source_arm` × condition, sampled with seed `20260922`. The sheet contains the source report, the rewrite, and the automatic claim diff. It does not contain the arm's original verdict or the hypothesis. Agreement is the fraction of sampled items on which the human marks the claim sets identical, plus a separate entity-agreement fraction. This agent is the author of the rewriter prompt and does not fill that sheet. An empty sheet is reported as unlabeled. Agreement is not imputed.

## Blinding (operational definition for the judge arm)

H.2's blinding is the annotation queue described in W2.3. The judge arm, which the brief says was asserted and not defined, is defined as follows.

Each judge call is a new conversation. The request contains:

- the production system prompt, byte-identical across conditions and models
- the user message template below
- the task instruction and one condition text

The request does not contain: condition name, source arm, original WebJudge verdict, ground-truth label, trajectory, screenshots, other conditions, or the hypothesis.

User message template:

```
User Task: {instruction}

Key Points: {key_points}

Action History:
1. {condition_text}
```

`{key_points}` is the production WebJudge step-1 output for that task instruction, computed once per task by the same judge model that will score the conditions, with `STEP1_KEY_POINTS_SYSTEM` from `eval/core/judges/webjudge.py`. Step 1 does not see the report. If that call fails, every condition of that task on that judge is `unparseable` and drops out of that judge's contrasts.

Screenshots are not attached. Run directories with the trajectory images are not in the tree (`eval/runs/` is absent). The deviation code on every call is `NO_IMAGES`. The system prompt text is still the production step-3 prompt, unmodified.

Call order: the list of `(unit, condition, judge)` triples is shuffled with Python `random.Random(20260922)` before the first judge call. The seed and the shuffled order are written to the log before any judge response is read.

The exclusion list (claim gate, length gate) is written and hashed before the first judge call. The judge step refuses to start if that file's hash has changed.

## Judges and prompts

Primary rows:

1. WebJudge outcome prompt, model `gpt-5.4-mini`. At execution, `STEP3_OUTCOME_SYSTEM` and `STEP1_KEY_POINTS_SYSTEM` are copied from `eval/core/judges/webjudge.py` into `judgeval/prompts/webjudge_production.txt` and `judgeval/prompts/webjudge_step1.txt`. Each file's sha256 is logged. The strings are not edited.
2. BU Bench trajectory prompt, model `deepseek-v4.1-flash`. The production prompt for that bench is not in this repository (the driver points at an external `harness_bench` tree). This arm is `not_run_prompt_absent`. A different prompt is not substituted and then labelled as the BU Bench prompt.

Generalization row, same system prompt and same user template as the primary WebJudge arm, run only when that model's credential is present:

- `gpt-4o`
- `claude-3-5-sonnet`
- `gemini-1.5-pro`

A missing credential is `not_run_no_credential`. Verdicts are not simulated.

Decoding: temperature 0. One retry without the temperature parameter if the endpoint rejects it. `retry: true` on that second call. No further retries. A transport failure after that is `unparseable`.

A verdict is `success` or `failure` only when the production parser matches `Status: "success"` or `Status: "failure"` (`eval.core.judges.webjudge.parse_status` is for the boolean; the raw status word is what is stored). Any other body is `unparseable` and is excluded from contrasts.

## Logging

Append-only JSONL at `judgeval/logs/w2_calls.jsonl`. Each line contains: `kind` (`rewrite` or `judge` or `key_points`), full request messages, full response text, model id, provider snapshot field or the string `not_returned`, temperature, seed, `seed_sent`, UTC timestamp, `retry`, deviation codes, unit id, condition. API keys and `Authorization` headers are not written. Lines are appended with `O_APPEND`. Existing lines are not edited.

## Estimands, tests, and the three pre-specified reads

For each judge that returned parseable verdicts, and for each contrast among {VERBOSE_CONF, ASSERT, DISCLOSE}:

- Pairing key: `(task_id, source_arm)`.
- Sample: units where both conditions passed the claim gate, VERBOSE_CONF also passed the length gate when it is in the contrast, and both judge outputs parsed.
- Estimate: difference in pass rate, condition A minus condition B, in percentage points.
- Test: `eval.core.stats.mcnemar_exact` with `seed=20260922` and `n_boot=10000`. Report n, both-success, A-only, B-only, both-fail, exact two-sided p, difference, bootstrap 95% interval.
- Within one judge, the three contrasts share one Holm step-down family. Holm adjusts the p-values. It does not choose the narrative read.

The narrative read uses the point estimates and a ±10 percentage-point band, fixed before any W2 verdict exists. Let A be the ASSERT pass rate, D the DISCLOSE pass rate, V the VERBOSE_CONF pass rate, on the common pairwise-complete sample of that judge (units present in all three conditions).

- Disclosure read: `|V - A| ≤ 10` and `|V - D| > 10` and `D < A`.
- Both-mechanisms read: `V` is strictly between `A` and `D`.
- Length read: `|V - D| ≤ 10` and `|V - A| > 10`.

If none of the three holds, the read is `indeterminate`. The read is reported for every judge that has a common sample. The headline read is the WebJudge `gpt-5.4-mini` read. If that arm did not run, the headline is `not_run` and no generalization judge is promoted into its place.

The format contrast is separate. n is at most 6. Report the paired difference and the McNemar interval. A confidence interval that covers zero is an inconclusive format result. It is still reported.

No contrast is dropped, reversed, or re-thresholded after the verdicts are seen.

## W2.3 human re-scoring

Protocol, identical in rules to the brief's description of H.2:

- Population: the `adaptive87` runs, 24 if that is how many completed run directories exist on disk under the arm name `adaptive87`.
- Second batch, only after the first batch is fully labelled by both annotators: the `summary` arm, which the frozen all-task table records as 13/24 WebJudge successes (`eval/artifacts/ablate10_paper/arm_summary_all_tasks.csv`) and whose primary-table pooled CSD is 0.909 (`tables/tab_csd.tex`).
- Two annotators, independent. A third reviewer adjudicates only items where the two disagree.
- Queue ids `ANN-XXXX-XXXX`, drawn with `random.Random(20260922)` from `secrets`-free alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` so that ids are reproducible from the seed.
- The annotator sheet contains the task instruction, the final answer, and the action tail. It does not contain arm identity, policy name, judge verdict, or queue-to-arm mapping. The mapping file is a separate analyst-side file.
- Labels: `success`, `failure`, `cannot_judge`.
- Kappa: Cohen's kappa via `eval.core.stats.cohen_kappa`, computed on items both annotators labelled with `success` or `failure` (`cannot_judge` is dropped from kappa and counted). The `adaptive87` batch and the `summary` batch each get their own kappa. They are not pooled.
- This agent does not fill annotator columns.

At freeze time, `eval/runs/` is not present, so no `adaptive87` packet can be built from trajectories. Execution searches the tree again. If the run directories are still absent, W2.3 writes `n_runs = 0` and does not compute kappa. Trajectories are not synthesized from the arm summary.

## Execution refusal rules

Execution stops, and writes the reason, when:

- this file's blob is not the tagged blob
- the stimulus file hash differs from the hash above
- `o200k_base` cannot be loaded
- a judge credential is absent (that judge only)
- the BU Bench prompt file cannot be read from the repository (that arm only)
- the exclusion-list hash changes after it was logged

Execution does not impute a verdict, a kappa, a claim-gate agreement, or a length match.
