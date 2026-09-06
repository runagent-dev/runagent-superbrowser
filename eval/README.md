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
