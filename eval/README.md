# §7.4 Model-Family Evaluation

Empirically reproduces the paper's claim (sections/07_evaluation.tex, `\label{sec:modelfamilies}`):
recent large Chinese models degrade on web-navigation in the Worker slot despite strong public
reasoning benchmarks, via three failure modes — **tool-schema fidelity**, **`[Vn]` index
discipline**, **procedural/declarative discipline**. Produces the figure + table for §7.4.

The candidate model is whatever you set in `~/.nanobot/config.json` (`agents.defaults.model`). Swap it
there, re-run, repeat. The harness auto-labels each run by the active model. Everything else
(orchestrator/planner scaffolding, the Gemini vision tier, prompts, budgets) is held fixed.

## How it works
- A **gated tap** in `superbrowser_bridge/orchestrator_tools/delegation.py` dumps each delegated
  Worker's full transcript (raw tool calls incl. rejected ones) + its live tool registry + telemetry
  to `$SUPERBROWSER_EVAL_CAPTURE_DIR/<worker_task_id>.json`. No-op unless that env var is set.
- `run_eval.py` runs the task suite through the full orchestrator (batch `test_superbrowser.py`),
  setting the capture dir per run, harvesting transcripts + step ledgers, and scoring the final
  answer with a fixed LLM judge.
- `analyzer.py` classifies every Worker tool attempt into 9 mutually-exclusive outcome classes (using
  the captured registry + the result-string tags the system already emits) and writes
  `results/per_call.csv` + `results/per_model.csv`.
- `figures/make_figure.py` emits the pgfplots figure + booktabs table (bare-tikzpicture `.tex`,
  `\input` by the paper — house style, no matplotlib). `figures/make_appendix_traces.py` emits
  representative failing traces.
- `figures/make_heatmap.py` emits a model×failure-signal heatmap (`paper/figures/fig_modelsplit_heatmap.tex`,
  Fig `fig:modelsplit-heatmap`) — the "which signals drive the gap" view. Reads `per_model.csv`
  (+ `per_model_task.csv` for optional per-task panels, also written by `analyzer.py`).

## Run it
```bash
cd /root/agentic-browser/runagent-superbrowser && source venv/bin/activate
# 0. Start the TS SuperBrowser server in another shell:  cd .. && npm start
# 1. EDIT eval/tasks.py  (your 5 tasks)  and  eval/models.py  (lab + cited benchmark numbers)
# 2. For EACH candidate model: set agents.defaults.model in ~/.nanobot/config.json, then:
python -m eval.run_eval --seeds 3            # auto-labels by the active model
# 3. Rescue ablation (Chinese models): set a CN model in config.json, then:
SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --seeds 3 --label <model>_rescue
# 4. Aggregate + plot + traces:
python -m eval.analyzer
python -m eval.figures.make_figure          # add --verify to compile-check standalone
python -m eval.figures.make_heatmap         # model×signal heatmap (--verify to compile-check)
python -m eval.figures.make_appendix_traces
# 5. Rebuild the paper:
cd ../paper && latexmk -pdf main.tex
```

> Reduced run (low credits): pass `--seeds 2 --tasks "id1,id2"` for a 3-model × 2-task side-by-side;
> the heatmap normalises per column, so the US-vs-CN contrast still reads with few models.

## Metrics (per model, mean ± s.d. over tasks × seeds)
- **task_success** — fixed LLM judge vs the task rubric (heuristic fallback).
- **schema_fidelity** = well-formed calls / all tool-emitting attempts (fails: bad tool name,
  synonym/missing arg, prose-instead-of-call).
- **index_discipline** = 1 − stale/out-of-range `[Vn]` clicks / vision-grounded clicks.
- **procedural_share** = procedural clicks / (procedural + declarative clicks).
- **dead_click_violation_rate** = dead-click-guard hits / click attempts.
- **vision_calls_per_task** — from worker telemetry.

## Single-component ablations (Table 1)
Populates `paper/tables/tab1_ablations.tex` (`tab:ablation`): full system + six one-mechanism-removed
configs, holding model/vision/budgets fixed. Set ONE capable model in `~/.nanobot/config.json` first
(paper: "same LLM") so a success drop is attributable to the removed mechanism.
```bash
# Python-side configs run against the standing default server; the 2 TS-side configs need a
# rebuilt + restarted server (the runner prints the exact per-config sequence, or use --manage-server).
python -m eval.run_ablations --tasks "petfinder_rabbits,bestbuy_qled_240hz_monitor" --seeds 2
python -m eval.run_ablations --list           # configs + their toggle env vars
python -m eval.figures.make_ablation_table    # fills tab1_ablations.tex (--annotate-n for interim note)
```
- **Toggles** (default = full system; ablate only when set): `ABLATE_MEMORY_EVICTION`,
  `ABLATE_STRUCTURED_LEDGER`, `WORKER_CHEVRON_FOCUS=0`+`BBOX_COMPOUND_ROW_SPLIT=0`,
  `VISION_ASYNC_PREFETCH=0` (python-side, read in-process); `SUPERBROWSER_CLICK_TIERS=tier1`,
  `MOTOR_HUMANIZATION=off` (TS-side — read by the server, so they need `npm run build` + a restart
  with the env baked in; `--manage-server` automates that).
- **Tokens/iter** is pooled from the harvested Worker `events.jsonl` `iteration` events; a provider
  returning empty usage (>50% zero-token iters) leaves that cell `\todo` with a warning.
- Un-run rows stay `\todo{value}`, so a partial table still compiles; rows are rebuilt each run, so
  re-running with more data refreshes them. Keep the caption's "20-task/three-seed" framing honest
  with `--annotate-n` for interim drafts.

## You must fill in
- `eval/tasks.py` — your five tasks (two examples + three `TODO` placeholders; placeholders are skipped).
- `eval/models.py` — confirm a `match` resolves each model id; replace each `bench_composite` with a
  **cited** number and set `confirmed=True`. Until then the figure + table render a red
  "PLACEHOLDER" note so unfilled numbers can't silently ship.
- `SUPERBROWSER_EVAL_JUDGE_MODEL` — pin a fixed strong judge (never a candidate model).

## Tunables (env)
`SUPERBROWSER_EVAL_CAPTURE_DIR` (set by the runner), `SUPERBROWSER_EVAL_SCHEMA_REMINDER` (rescue),
`SUPERBROWSER_EVAL_JUDGE_MODEL` / `_JUDGE_API_KEY` / `_JUDGE_BASE_URL`, `SUPERBROWSER_WORKER_MAX_ITER`.

> Note: the figure/table `.tex` committed under `paper/figures` + `paper/tables` currently reflect
> **synthetic placeholder data** (so the paper builds). Re-run steps 2-4 to populate real numbers.
