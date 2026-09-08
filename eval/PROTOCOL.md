# Evaluation protocol (frozen)

Everything below is encoded in `eval/core/protocol.py` (`Protocol`, hashed into every run record) and
`eval/benchmarks/`. A reader should be able to recompute every number in the paper from
`eval/runs/<experiment>/results.jsonl` plus the per-run directories.

## Benchmark

| Item | Value |
|---|---|
| Task set | Online-Mind2Web **hard** split, frozen 2026-09-06 from the vendored task file (see `benchmarks/manifest.json` for source path + sha256): **74 tasks / 59 sites** |
| Task ids | upstream `query_id`; instructions verbatim; `start_url` is the benchmark's starting page |
| Levels | `online_mind2web_all.jsonl` keeps easy 83 / medium 143 / hard 74 for level-stratified reporting |
| Paired-ablation subset | `ablation24` (`benchmarks/subsets.json`): 24 tasks, stratified by site family (shopping 6, government/health 6, info/media 5, travel/booking 3, vehicles 2, housing/jobs 2), seeded allocation, **pre-registered before any arm ran** |
| Smoke subset | `smoke2` (petfinder + accuweather tasks) for end-to-end pipeline checks |
| Dev suite | `custom_dev.jsonl` (the five hand tasks from the legacy §7.4 study) |
| Headline provenance | The draft paper reported "66 tasks". No per-task record of that run exists; `benchmarks/exclusions.json` carries an open reconciliation entry. E0 reports counts with and without any pre-registered exclusions. |

## Run budgets and pins

| Setting | Value | Where |
|---|---|---|
| Worker step cap | 50 iterations (`SUPERBROWSER_WORKER_MAX_ITER`) | `Protocol.max_iterations` |
| Wall clock | 1800 s per run (internal graceful stop at 1710 s; hard kill at +180 s) | `Protocol.wall_clock_s` |
| Host context pins | `contextWindowTokens=200000`, `maxTokens=16384`, `temperature=1.0`, `maxToolIterations=50` patched into a per-run copy of `~/.nanobot/config.json` (applies to orchestrator AND workers via `set_config_path`) | `Protocol.nanobot_config_overrides()` |
| Host history snip | engages only above ~182 592 prompt tokens; a crossing shows up in `context_size` events | — |
| Captcha policy | solver first, then human handoff; a handoff counts as failure | recorded |
| Start URL policy | agent starts at `start_url`; direct navigation allowed within the pinned domain | recorded |
| Brain model | chosen at launch (`--model`), recorded per run in `protocol.model`; vision model from `VISION_MODEL`; judge models recorded per verdict | run record |

## Confound controls (forced env for every run)

`SUPERBROWSER_CROSS_TASK_MEMORY=0` (no site-model ingest/merge between runs), `LEARNING_READS_ENABLED=0`,
`SUPERBROWSER_COOKIE_JAR=0`, `SUPERBROWSER_IDENTITY_JAR=0`, `SUPERBROWSER_EVAL_SCHEMA_REMINDER` unset.
Arms are interleaved per task (`seed → task → arm`) so paired conditions run minutes apart on live sites;
`timing.started_at` is recorded for drift audits. Each run is a fresh process and a fresh browser session.

## Instrumentation (forced on)

`SUPERBROWSER_TRACE_VISION=1` (`vision_calls.jsonl`), `SUPERBROWSER_TRACE_CLICKS=1` (`clicks.jsonl`),
`SUPERBROWSER_TRACE_SCREENSHOTS=1` (ordered per-run screenshots for WebJudge),
`SUPERBROWSER_EVAL_CONTEXT_DUMP=1` (`live_context.jsonl.gz` + `context_size` events for CSD).

## Evaluators

1. **Deterministic checks** (`benchmarks/checks.json`, URL/answer regexes) — ground truth where defined.
2. **WebJudge** (Online-Mind2Web's 3-step screenshot judge, prompts verbatim; model pinned by
   `SUPERBROWSER_EVAL_WEBJUDGE_MODEL`, default `gpt-4o`) — primary automatic evaluator.
   The judge model must be vision-capable and is recorded in every verdict; it is pinned for a whole
   study and never equals the candidate model. Re-judging a finished sweep with a different judge is
   allowed only as a reported robustness check, never as a silent replacement of the headline number.
3. **Answer judge** (text-only, `SUPERBROWSER_EVAL_JUDGE_MODEL`) — secondary.

`outcome.success` = deterministic if it decided, else WebJudge, else answer judge (`outcome.decided_by`).
E9 validates the automatic evaluators against two human annotators (raw agreement, Cohen's κ, FP/FN).

## Impossible-task rule

A run's `failure_reason` is classified from deterministic markers (`site_unavailable`, `geo_blocked`,
`captcha_unsolved`, `bot_block`, `loop`, `grounding`, `premature_done`, `timeout`, `api_error`, `other`).
A task is *excluded* from a paired comparison only when the same exclusion-class marker
(`site_unavailable | geo_blocked | captcha_unsolved`) fires in **every** arm of that comparison. Both the
all-task and the excluded-set results are reported. Nothing is removed after seeing a single arm's result.

## Metric definitions

* **TSR** — successes / evaluated tasks; always shown as `k/N (%)`.
* **CSD** (Critical State Durability) — for each task-critical item (the hand-reviewed task-given values in
  `benchmarks/critical_state.json`, plus observation-derived values first seen in a tool result at step *i*
  and reused in an action argument or the final answer at step *j > i*), 1 iff the exact normalised string is
  present in the live context the acting agent saw at the reuse step (`live_context.jsonl.gz`); averaged
  over items, then tasks.
* **DRR** (Dead-End Revisit Rate) — re-issued action signatures `(normalised url, tool, normalised target)`
  that previously failed / number of distinct failed signatures; reported with repeated-action count and
  step inflation relative to the paired arm.
* **RPR** (Redundant Perception Rate) — vision calls whose `(url, dom_hash, dom_text_hash)` equals the
  previous vision call's / total vision calls.
* **First-path execution success** — click tool results carrying none of `[click_escalated`,
  `[click_silent`, `[no_effect:`; **recovery success** — `[click_escalated]` / (`[click_escalated]` +
  `[click_silent]`).
* **Grounding error** — failed runs whose trajectory carries `label_mismatch`/`element_mismatch` or ≥3
  `[click_at_failed:*]` tags.
* **Efficiency** — worker/orchestrator iterations, executed tool calls, vision calls (+ cache hits),
  prompt tokens per iteration (mean/peak), wall clock, USD cost from `core/pricing.json` (list prices,
  dated) reported per task and per successful task.

## Statistics (pre-registered)

Confirmatory comparisons: **C1** `ledger` vs `fifo` on TSR (E2), **C2** `ledger` vs `full_history` on cost
with non-inferior TSR (E2). Everything else is secondary/diagnostic. Paired binary outcomes → exact
McNemar with the paired difference and a Wilson/bootstrap CI; paired continuous → paired bootstrap CI and
Wilcoxon signed-rank; ordered ladders (E3, E10) → Spearman / Cochran–Armitage trend; evaluator agreement →
Cohen's κ. Same task ids across arms, same model/decoding, same vision model, same caps.
