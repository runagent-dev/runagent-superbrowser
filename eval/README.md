# SuperBrowser research evaluation suite

Reproducible experiments behind the paper. Every experiment is a small package under
`eval/experiments/` that drives the shared harness in `eval/core/`; every run leaves one machine-readable
row (`results.jsonl`) plus a self-contained run directory, and every table/figure is regenerated from those
rows. The protocol (task set, caps, pins, evaluators, metrics, statistics) is frozen in
[`PROTOCOL.md`](PROTOCOL.md).

```
eval/
  PROTOCOL.md               frozen protocol + pre-registered comparisons
  benchmarks/               frozen task files, manifest, subsets, critical-state items, checks, exclusions
  core/                     harness: protocol, tasks, arms, runner, run_one, harvest, records, judges,
                            metrics, stats, loaders, pricing
  experiments/<name>/       README.md (hypothesis, arms, metrics), run.py, analyze.py
  artifacts/<name>/         tracked small outputs (csv / tex / png) the paper inputs
  tests/                    offline tests (pytest; no browser, no network, no LLM)
  runs/  results/  token_runs/   raw run data (git-ignored, regenerable)
```

## Quick start

```bash
source venv/bin/activate
npm run build && npm start &                    # TypeScript browser server on :3100 (default state)
python -m eval.core.tasks --list                # benchmarks + pre-registered subsets
python -m eval.core.runner --experiment demo --arms ledger,fifo --tasks smoke2 --seeds 1 \
    --model anthropic/claude-opus-4.8 --dry-run  # schedule + per-run env, launches nothing
python -m eval.core.runner --experiment demo --arms ledger,fifo --tasks smoke2 --seeds 1 \
    --model anthropic/claude-opus-4.8           # live: 3 tasks x 2 arms, judged, recorded
python -m eval.core.judge --experiment demo     # (re)judge offline, refresh records
```

Each experiment has the same interface (`python -m eval.experiments.<name>.run --help`) and fixes its own
arms/subset; `--dry-run`, `--resume`, `--no-judge`, `--manage-server` (restart the TS server for TS-side
arms) and `--model` are shared flags (`eval/core/runner.py`).

## How a run works

`runner.py` builds the schedule (`seed → task → arm`, arms interleaved per task, TS-side arms grouped) and
launches **one subprocess per run** (`eval.core.run_one`) with the arm's env merged over the protocol pins
and per-run paths. `run_one.py` writes a per-run nanobot config (pins + `--model`), runs the task through
the orchestrator→worker pipeline (or the flat single-agent topology), and leaves:

```
eval/runs/<experiment>/<arm>/<task_id>/seed<k>/
  spec.json  meta.json  result.txt  usage.json  config.redacted.json  run.log
  workers/<wid>.json           full worker transcript + tool registry (delegation tap)
  ledgers/<id>/                events.jsonl steps.jsonl ledger.json vision_calls.jsonl clicks.jsonl
                               live_context.jsonl.gz task_summary.json step_history.json
  screenshots/NNN-*.jpg        ordered trajectory screenshots (WebJudge input) + index.jsonl
  judges/{deterministic,webjudge,answer_judge}.json
  run_record.json              the RunRecord (also appended to ../../../results.jsonl)
```

`harvest.py` rebuilds a record from those files at any time (`python -m eval.core.harvest <run_dir>`);
analyzers only read records + run directories.

## Arms (env toggles; empty = full system)

| Arm | Env | Side |
|---|---|---|
| `ledger` | — | python |
| `full_history` / `fifo` / `summary` / `ledger_noevict` | `SUPERBROWSER_MEMORY_POLICY=…` (+ `_RECENT_K`, `_BUDGET_TOKENS`, `_KEEP_SCREENSHOTS`) | python |
| `no_deadend` | `ABLATE_DEAD_END_MEMORY=1` | python |
| `fresh_vision` | `ABLATE_VISION_REUSE=1` | python |
| `snap_center` / `snap_dom_alt` (vs `snap_chevron`) | `SUPERBROWSER_SNAP_STRATEGY=…` | ts |
| `no_ladder` | `ABLATE_CLICK_LADDER=1` (+`CLICK_LADDER_AUTO=0`, `SUPERBROWSER_CLICK_TIERS=tier1`) | both |
| `flat` | `SUPERBROWSER_TOPOLOGY=flat` | python |
| derived | `pressure_arm(base, tokens)` → `SUPERBROWSER_EVAL_DISTRACTOR_TOKENS`; `budget_arm(base, B, K)` | — |

All toggles default to today's production behaviour; see `docs/CONFIG.md` for the semantics of each.

## Experiments

| Id | Package | Question | Status |
|---|---|---|---|
| E0 | `e0_headline_audit` | recompute the headline as k/N from records; report exclusions and the 66-vs-74 discrepancy | P4 |
| E1 | `e1_main` | full system on the frozen hard split | P4 |
| E2 | `e2_memory_policy` | matched memory policies (full / fifo / summary / ledger) — main experiment | P4 |
| E3 | `e3_memory_pressure` | pressure ladder × memory policy | P4 |
| E4 | `e4_deadend` | dead-end memory on/off → DRR | P4 |
| E5 | `e5_perception_reuse` | perception reuse on/off → RPR, cost | P4 |
| E6 | `e6_subelement` | snapper strategy on local fixtures (offline, no LLM) | P5 |
| E7 | `e7_click_cascade` | click recovery ladder on/off | P4 |
| E8 | `e8_topology` | orchestrator→worker vs flat single agent | P4 |
| E9 | `e9_evaluator_validation` | human vs automatic evaluators (κ, FP/FN) | P4 |
| E10 | `e10_robustness` | budgets, seeds, second host model | P4 |
| E11 | `e11_cost` | cost per task / per success by role | P4 |
| E12 | `e12_traces` | traces selected from aggregate effects | P4 |
| — | `modelsplit` | legacy §7.4 tool-economy study (secondary) | kept |

## Tests

```bash
python -m pytest eval/tests -q
```

## Cost & scale

Every experiment is `arms × tasks × seeds` runs. `--dry-run` prints the exact count; the numbers below
are the defaults (`ablation24` = 24 tasks, `--seeds 1`, except E2 which the paper runs at `--seeds 3`):

| Experiment | Runs (default) | Runs at paper scale |
|---|---|---|
| E1 main | 74 (`--tasks all`) | 74 × seeds |
| E2 memory policy | 96 (4 arms × 24) | 288 (4 × 24 × 3 seeds) |
| E3 memory pressure | 216 (9 cells × 24) | 216 |
| E4 / E5 / E7 / E8 | 48 each (2 × 24) | 48 each |
| E10 robustness | 216 (budget sweep) | + window sweep + a 2nd model |
| E6 sub-element | 0 API runs (offline fixtures) | committed |

**Per-run cost** (one recorded 41-iteration run ≈ 2.2M input + ~40K output tokens, list price from
`core/pricing.json`; prompt caching cuts the input term severalfold, so treat these as an upper bound and
refresh from pilots with `python -m eval.experiments.e11_cost.analyze`):

| Brain model | ≈ $/run (list) | Vision + WebJudge |
|---|---|---|
| anthropic/claude-opus-4.8 | ~$12 | +~$0.1 vision, +~$0.3 WebJudge (gpt-4o) |
| openai/gpt-5.4 | ~$6 | same |
| google/gemini-3.5-flash | ~$4 | same |

So E1 on 74 tasks is roughly $300 (Flash) to $900 (Opus); the full ablation set (E2 at 3 seeds + E3 + E4 +
E5 + E7 + E8) is ~700 runs, ~$3–8K on Opus and well under half that on Flash. Run a pilot first:
`--tasks smoke2 --seeds 1` (3 tasks) end-to-end, then `--tasks ablation24 --seeds 1` before scaling.

## Running the full study (user-launched; spends credits)

```bash
source venv/bin/activate
npm run build && npm start &                       # default browser server on :3100
python -m eval.rehearse                            # offline pre-flight: dry-runs + replay of recorded data
export SUPERBROWSER_EVAL_WEBJUDGE_MODEL=gpt-4o SUPERBROWSER_EVAL_JUDGE_MODEL=gpt-5.5
M=anthropic/claude-opus-4.8

python -m eval.experiments.e1_main.run           --model $M --tasks all
python -m eval.experiments.e2_memory_policy.run  --model $M --seeds 3 --with-noevict
python -m eval.experiments.e3_memory_pressure.run --model $M
python -m eval.experiments.e4_deadend.run        --model $M
python -m eval.experiments.e5_perception_reuse.run --model $M
python -m eval.experiments.e7_click_cascade.run  --model $M --manage-server   # TS-side arm
python -m eval.experiments.e8_topology.run       --model $M
python -m eval.experiments.e10_robustness.run    --model $M --sweep budget
python -m eval.experiments.e6_subelement.run     --manage-server              # offline, no model

for e in e1_main e2_memory_policy e3_memory_pressure e4_deadend e5_perception_reuse e7_click_cascade e8_topology e10_robustness e6_subelement; do
  python -m eval.experiments.$e.analyze; done
python -m eval.experiments.e0_headline_audit.analyze
python -m eval.experiments.e11_cost.analyze
python -m eval.experiments.e12_traces.analyze --experiment e2_memory_policy --arm-a ledger --arm-b fifo --metric csd_observed
```

TS-side arms (`e6`, and `e7`'s `no_ladder`) need `--manage-server`: the harness starts its **own** browser
server on a free port with the arm's env baked in and never touches your `:3100` container.

## User-owned follow-ups (left deliberately)

- **Headline reconciliation** — `benchmarks/exclusions.json` `legacy_66_task_reconciliation` is open: which
  task ids (or exclusion rule) turned the 74-task hard split into the draft's "66". E0 reports both.
- **Brain model** — pass `--model`; `pricing.json` covers the three candidates. Set it once E1 is run.
- **E9 human labels** — `make_sheet` builds the stratified sheet; two people fill `label_h1/label_h2`, then
  `analyze` reports κ.
- **checks.json / critical_state.json** — deterministic checks are a skeleton; critical-state items are
  hand-reviewed for all 74 hard tasks and can be extended.
