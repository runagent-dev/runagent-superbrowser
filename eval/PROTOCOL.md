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

## Toolset (identical in every topology)

The agent sees browser and orchestration tools only. nanobot's default host tools
— `exec`, `run_cli_app`, `spawn`, `long_task`, filesystem read/write, `glob`/`grep`,
`web_search`/`web_fetch` — are unregistered in all three eval topologies
(orchestrator, delegated worker, flat). Two reasons: a run that shell-scrapes a
site with curl measures nothing about browser navigation, and since each arm
would reach for the shell differently it confounds the comparison. It also keeps
an LLM from running arbitrary commands on the evaluation host.
`SUPERBROWSER_EVAL_KEEP_HOST_TOOLS=1` opts out and must be reported if used.

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

## As-run deviations — `ablate10` (the sweep the paper reports)

The sections above are the protocol as frozen. This section records where the reported sweep departed
from it. Every item is checked by `python -m eval.core.audit_sweep --experiment ablate10`
(`eval/artifacts/ablate10_paper/audit.json`) and is disclosed in the paper (§7.5).

| Item | As frozen | As run |
|---|---|---|
| Task subset | `ablation24`: 24 tasks from the 74-task hard split, stratified by site family, seeded, pre-registered | **`ablate24`**: 22 task ids supplied by the author, screened for reachability through the evaluation proxy (2 dropped for 403), plus 3 medium-level fills; 16 easy / 3 medium / 5 hard from the 300-task file. Reachability screening biases the absolute success rate upward; paired comparisons are unaffected. |
| Subset stability | "never edit after results exist" | **Five swaps after runs existed** (2026-09-12 to 09-14; `benchmarks/subsets.json` `ablate24.note`): petfinder 0b2623e9→coursera, disney 157f4a79→wanderlog (1 completed run orphaned), webmd 3443e9c3→rottentomatoes, thumbtack 4e801ba1→spothero (7 completed runs, 4 successes, archived), and petfinder c94551d2→foxsports on cost grounds **after all four arms run had failed it** (17–29 min each). The last swap is outcome-dependent and violates the rule above; the archived runs are kept under `eval/runs/ablate10/_orphaned_by_task_swaps/` and `_discarded_petfinder/` and are listed in the supplement. |
| Instruction wording | verbatim benchmark instruction | Task **85b284c1** (student.com) ran in all 8 arms with an author-rewritten instruction ("…University of Texas Austin under 2500 dollars"; benchmark: "…University of Leeds with bills that include WIFI and cleaning services."). An intermediate variant was discarded (`_discarded_student_com/`). Recorded in `benchmarks/exclusions.json` `instruction_deviations`; the frozen benchmark file is unchanged. Kept in the primary analysis because every arm saw identical text. Reason: pending from the author. |
| Host model pin | one model per study | Task **dd44c665** (spothero): 4 of 8 arms ran on `minimax/minimax-m3` after an operator config change. **Excluded from every primary table** under the pre-registered-style rule `host_model_mismatch` (`exclusions.json`; not outcome-based — all 8 arms failed it). Primary n = 23; the n = 24 numbers are reported as a sensitivity line. |
| Code version | one commit per sweep | **8 commits** (8fb8ce9 → a87c2d0, 2026-09-11 to 09-14), 176/192 records `git_dirty`. The interleaved schedule gives every arm the same commit histogram except for task 662ae0f2 (wanderlog), whose arms span three adjacent commits (two Tier-3 browser fixes). 64 runs predate commit 536706c, which removed shell/filesystem tools from the orchestrator: **no non-browser tool was executed in any of the 192 runs** (54 runs *attempted* hallucinated names such as `list_exec_sessions`; all were rejected). |
| Instrumentation | `vision_calls.jsonl`, `clicks.jsonl` per run | **Never written** (tracer read the wrong attribute; fixed in 270b880 after the sweep). RPR and click-recovery success are therefore unmeasured; first-path success is computed from tool-result tags, as defined above; CSD (observation-derived) is available for the 142/192 runs that produced at least one scored reuse event. |
| Judge model | default `gpt-4o` | `gpt-5.4-mini` for all 192 WebJudge verdicts; answer judge `gemini-3-flash-preview`; one run (fresh_vision, 9bb63ad0) had no screenshots and was decided by the answer judge. WebJudge evidence differs by arm: mean screenshots handed to the judge range from 10.5–15.5 per run, but only **4.4** in the no-perception-reuse arm. |
| Exclusion rule | per-task, all arms | Applied as written: no task carried the same marker in every arm, so the excluded-set complement equals the full set. Four runs carry a marker (3 `site_unavailable`, 1 `captcha_unsolved`) and are counted as failures. |
| Confirmatory comparisons | C1, C2 only | The E4/E5/E7/E8 experiment specs had marked their own pair `confirmatory` in code; corrected on 2026-09-16 to match this document. The five secondary comparisons share one Holm family in the paper. |
| Browser tier | Tier-1 (Puppeteer) by default, Tier-3 (patchright) on escalation | **Tier-1 was unusable through the authenticated proxy**: the engine passed `user:pass@host` to Chrome's `--proxy-server`, which Chrome rejects (`ERR_NO_SUPPORTED_PROXIES`), so every Tier-1 `/session/create` returned HTTP 500. 325 `browser_open` calls failed this way across 86 of the 192 runs; the agent recovered only by retrying with `tier="t3"`, and 184 of 213 first page opens landed on Tier-3. Every arm was affected alike (the retries inflate iterations and tool calls equally), so paired comparisons stand, but the sweep effectively measured the Tier-3 browser after wasted retries. Fixed on 2026-09-17 (`splitProxyCredentials` + `page.authenticate`; `tests/proxy-auth.test.ts`); sweeps launched after that commit run Tier-1 through the proxy. |
| Cost | priced per run | The host model was absent from `pricing.json` during the sweep (`priced:false`, total read $17.80); re-priced in 927ed0f. Final: $59.47 agent + vision, $8.16 judging, $67.63 total; summed run time 22 h over 73 elapsed hours. |

Nothing in the run directories was altered to produce these numbers: `eval/runs/ablate10` is read-only, the
analyzers write only `run_record.json` (metrics) and `eval/artifacts/`, and the frozen copy the paper is
generated from is `eval/artifacts/ablate10_paper/` (`python -m eval.experiments.paper_tables`).
