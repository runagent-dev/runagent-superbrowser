# Eval workflow — why it's two phases (and why Chinese models run twice)

You're looking at **two different "twos."** Both are intentional. This doc explains each.

```
# for each model: set agents.defaults.model in ~/.nanobot/config.json, then
python -m eval.run_eval --seeds 3
# rescue (CN models): SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --label <m>_rescue
python -m eval.analyzer && python -m eval.figures.make_figure && python -m eval.figures.make_appendix_traces
```

- **Two phases**: *collect* (`run_eval`, run once per model) → *aggregate* (`analyzer` + figures, run once at the very end).
- **Two runs per Chinese model**: a normal run **and** a "rescue" run (the schema-reminder ablation).

---

## Mental model

```
PHASE 1: COLLECT  (repeat per model — writes raw data to eval/runs/)
   edit config.json → python -m eval.run_eval        ┐
   edit config.json → python -m eval.run_eval        │  one invocation = ONE model,
   edit config.json → python -m eval.run_eval        │  all tasks × all seeds
   ... (once per model; + rescue runs for CN models) ┘

PHASE 2: AGGREGATE  (run ONCE, after every model is collected)
   python -m eval.analyzer            → results/*.csv
   python -m eval.figures.make_figure → paper figure + table
   python -m eval.figures.make_appendix_traces → appendix traces
```

Phase 1 is **data collection**. Phase 2 turns the collected data into the figure. You don't interleave
them per model — you collect everything first, then aggregate once.

---

## Phase 1 — why one `run_eval` per model

The candidate model lives in **`~/.nanobot/config.json` (`agents.defaults.model`)**, which is **global** to
the process. There's no per-model flag — a single `run_eval` invocation tests whatever model is in that
file *right now*. So you test models by hand-looping:

1. Edit `~/.nanobot/config.json` → set the model.
2. `python -m eval.run_eval --seeds 3` (runs **all** active tasks × 3 seeds for that one model).
3. Repeat for the next model.

Each invocation auto-labels its output by the active model, so runs never collide:

```
eval/runs/
  gpt-5-5/        <- one run_eval invocation
  claude/         <- another
  moonshot-kimi-k2/
  ...
```

They're sequential (one model in config.json at a time) — you can't parallelize models in one process.

---

## The "second run": rescue ablation (Chinese models)

This is the other "two." For each **Chinese** model you run `run_eval` a **second time** with one extra env
flag:

```bash
SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --label kimi_rescue
```

- `SUPERBROWSER_EVAL_SCHEMA_REMINDER=1` prepends a short paragraph to the Worker prompt that simply
  **restates the tool contract** (exact tool/arg names, 1-based rotating `[Vn]` indices, prefer the
  procedural click). It is the **only** change vs a normal run — same model, same tasks, same everything
  else.
- `--label kimi_rescue` keeps it in a separate folder so it doesn't overwrite the base run.

**Why do this?** It's the experiment that proves *why* the gap exists. If a prompt that adds **no new
capability** — only a reminder of the rules — sharply lifts a Chinese model's schema fidelity and index
discipline, then the gap is a **post-training/prompting artifact, not a capability ceiling**. That's the
paper's core thesis, and `fig_rescue` is its evidence.

- Run rescue for the **CN models** (the ones that fail without it).
- US models are already at ceiling, so rescue isn't needed (the figure omits them; you *can* run them to
  confirm they don't move).
- The analyzer **auto-pairs** `kimi_rescue` ↔ `kimi` (it strips the `_rescue` suffix), so the rescue figure
  draws base-vs-reminder bars per CN model automatically.

So a Chinese model = **2 invocations** (base + rescue); a US model = **1 invocation**.

---

## Phase 2 — why the three commands are chained with `&&`

These run **once, at the end**, after all models (and rescues) are collected. They're a **dependency
chain**, which is exactly what `&&` expresses ("run the next only if the previous succeeded"):

| Command | Reads | Writes |
|---|---|---|
| `python -m eval.analyzer` | `eval/runs/**` (all transcripts) | `eval/results/per_call.csv`, `per_model.csv` |
| `python -m eval.figures.make_figure` | `eval/results/per_model.csv` | `paper/figures/fig_modelsplit.tex`, `fig_rescue.tex`, `paper/tables/tab_toolselection.tex` |
| `python -m eval.figures.make_appendix_traces` | `eval/runs/**` | `paper/appendix/traces_modelsplit.tex` |

`make_figure` **needs the CSV** that `analyzer` produces — hence it runs after. `&&` just means "stop if
something fails" so you don't plot a stale CSV. You can also run them separately; the chain is only a
convenience.

You can run Phase 2 **any time** to see partial results — `analyzer` simply scans whatever is in
`eval/runs/` so far. Re-running it after adding more models is safe and idempotent (it overwrites the CSVs).

---

## Full worked example (4 US + 3 CN models)

```bash
cd /root/agentic-browser/runagent-superbrowser
source venv/bin/activate
# (TS server running in another shell: npm start)

# --- Phase 1: collect (edit config.json between each) ---
# US models — base run only:
#   set agents.defaults.model = "gpt-5.5"      then:
python -m eval.run_eval --seeds 3
#   set agents.defaults.model = "claude-opus-4-8"   then:
python -m eval.run_eval --seeds 3
#   set ... "gemini-3-pro" ...                 then:
python -m eval.run_eval --seeds 3
#   set ... "nemotron-3-super" ...             then:
python -m eval.run_eval --seeds 3

# CN models — base run AND rescue run each:
#   set ... "moonshot/kimi-k2" ...             then:
python -m eval.run_eval --seeds 3
SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --seeds 3 --label kimi_rescue
#   set ... "qwen3-max" ...                    then:
python -m eval.run_eval --seeds 3
SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --seeds 3 --label qwen_rescue
#   set ... "z-ai/glm-5" ...                   then:
python -m eval.run_eval --seeds 3
SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --seeds 3 --label glm_rescue

# --- Phase 2: aggregate (once) ---
python -m eval.analyzer && python -m eval.figures.make_figure && python -m eval.figures.make_appendix_traces

# --- build the paper ---
cd ../paper && latexmk -pdf main.tex
```

Total `run_eval` invocations here: 4 US + (3 CN × 2) = **10**. Each = `tasks × seeds` browser runs
(e.g. 5 × 3 = 15), so ~150 browser sessions and real API spend. **Start with `--seeds 1` on one model**
to confirm the pipeline end-to-end before committing to the full matrix.

---

## FAQ

- **Do I run `analyzer` after every model?** No — only at the end. (You *can* run it anytime for partial
  results; it just rescans `eval/runs/`.)
- **Why not one command for everything?** Because Phase 1 is interleaved with you editing `config.json` by
  hand (the model is global). The harness can't change your config for you, so collection is a manual loop.
- **What if I skip the rescue runs?** Everything still works; `fig_rescue.tex` becomes a placeholder and the
  paper still builds. You just lose the ablation evidence.
- **Where do labels come from?** Auto-derived from the active model id (e.g. `moonshot/kimi-k2` →
  `moonshot-kimi-k2`), or whatever you pass to `--label`. The `_rescue` suffix is what pairs a rescue run to
  its base in the figure.
