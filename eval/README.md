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
| E0 | `e0_headline_audit` | recompute the headline as k/N from records; report exclusions and the 66-vs-74 discrepancy | ready (needs E1 records) |
| E1 | `e1_main` | full system on the frozen hard split | ready to launch |
| E2 | `e2_memory_policy` | matched memory policies (full / fifo / summary / ledger) — main experiment | ready to launch |
| E3 | `e3_memory_pressure` | pressure ladder × memory policy | ready to launch |
| E4 | `e4_deadend` | dead-end memory on/off → DRR | ready to launch |
| E5 | `e5_perception_reuse` | perception reuse on/off → RPR, cost | ready to launch |
| E6 | `e6_subelement` | snapper strategy on local fixtures (offline, no LLM) | **run; results in `artifacts/e6_subelement/`** |
| E7 | `e7_click_cascade` | click recovery ladder on/off | ready (TS-side arm: `--manage-server`) |
| E8 | `e8_topology` | orchestrator→worker vs flat single agent | ready to launch |
| E9 | `e9_evaluator_validation` | human vs automatic evaluators (κ, FP/FN) | sheet builder + κ ready (needs runs + 2 annotators) |
| E10 | `e10_robustness` | budgets, seeds, second host model | ready to launch |
| E11 | `e11_cost` | cost per task / per success by role | ready (any experiment's records) |
| E12 | `e12_traces` | traces selected from aggregate effects | ready (after an analyzer) |
| — | `modelsplit` | legacy §7.4 tool-economy study (secondary) | kept |

## Tests

```bash
python -m pytest eval/tests -q          # offline harness tests (also in CI)
python -m eval.rehearse                 # dry-run every schedule + replay recorded runs
python -m eval.preflight                # live check before spending: keys, judges, server, session
```

`preflight` is the one to run before any sweep. It pings the BRAIN provider at the protocol's real
`max_tokens` (a 16-token probe passes on an account that cannot fund a single run), reports the provider's
remaining balance where the API exposes it, and only then checks the judges, server and benchmark. It resolves the brain model and its key, the vision
provider, and BOTH judges, opens and closes one browser session, and exits non-zero if anything is wrong. `--skip-llm --skip-session` makes it free.
Note that WebJudge (primary) and the answer judge take separate credentials: give WebJudge
`SUPERBROWSER_EVAL_WEBJUDGE_API_KEY` whenever the shared `SUPERBROWSER_EVAL_JUDGE_*` pair points at a
different provider, or it will call its OpenAI-defined model against that provider's endpoint.

Any vision-capable judge model works. `gpt-4o` is what Online-Mind2Web reports, so it is the default and
the safest thing to cite; `gpt-5.4-mini` was verified to agree with it on the same trajectory at about a
third of the input price. Reasoning models reject the benchmark's `temperature`/`max_tokens` pair — the
judge learns the accepted shape on its first call and widens the cap if hidden reasoning swallows an
answer, so no verdict is silently lost. Whichever you pick, record it: the model is stamped into every
verdict, and judging is re-runnable offline with `python -m eval.core.judge --experiment <name> --force`.

The replay copies each legacy `eval/runs/<model>/<task>/seedN/` run into
`eval/runs/modelsplit_replay/…` and adapts only the copy; the recorded originals are never modified.

## Cost & scale

Every experiment is `arms × tasks × seeds` runs. `--dry-run` prints the exact count; the numbers below
are the defaults (`ablation24` = 24 tasks, `--seeds 1`, except E2 which the paper runs at `--seeds 3`):

| Experiment | Runs (default) | Runs at paper scale |
|---|---|---|
| E1 main | 74 (`--tasks all`) | 74 × seeds |
| E2 memory policy | 96 (4 arms × 24) | 288 (4 × 24 × 3 seeds) |
| E3 memory pressure | 216 (9 cells × 24) | 216 |
| E4 / E5 / E7 / E8 | 48 each (2 × 24) | 48 each |
| E10 robustness | 144 (budget sweep: 2 policies × 3 budgets × 24) | + window sweep (216) + a 2nd model |
| E6 sub-element | 0 API runs (offline fixtures) | committed |

**Per-run cost** (one recorded 41-iteration run ≈ 2.2M input + ~40K output tokens, list price from
`core/pricing.json`; prompt caching cuts the input term severalfold, so treat these as an upper bound and
refresh from pilots with `python -m eval.experiments.e11_cost.analyze`):

| Brain model | ≈ $/run (list) | Vision + WebJudge |
|---|---|---|
| anthropic/claude-opus-4.8 | ~$12 | +~$0.1 vision, +~$0.3 WebJudge (gpt-4o) or +~$0.1 (gpt-5.4-mini) |
| openai/gpt-5.4 | ~$6 | same |
| google/gemini-3.5-flash | ~$4 | same |

So E1 on 74 tasks is roughly $300 (Flash) to $900 (Opus); the full ablation set (E2 at 3 seeds + E3 + E4 +
E5 + E7 + E8) is ~700 runs, ~$3–8K on Opus and well under half that on Flash. Run a pilot first:
`--tasks smoke2 --seeds 1` (3 tasks) end-to-end, then `--tasks ablation24 --seeds 1` before scaling.

## Running the full study (user-launched; spends credits)

Terminal 1 keeps the browser server up (`npm run dev`, or `npm run build && npm start`). Everything
below runs in a second terminal from the repo root; the harness loads `.env` itself, so no `source .env`.

```bash
source venv/bin/activate
python -m eval.rehearse                            # offline pre-flight: dry-runs + replay, no network
python -m eval.preflight --model <id>              # LIVE pre-flight: config, keys, judges, one session
M=anthropic/claude-opus-4.8                        # whatever --model you pinned above

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

### When the model provider refuses

A provider refusal (out of credit, rate limit, bad key) is a fact about the account, not about the agent.
Such a run finishes in about a second with zero iterations, so if it were scored as a task it would look
like a agent giving up early. It is classified `api_error` and carries an exclusion label, so it leaves the
denominator instead of inventing a failure. After three consecutive provider errors the sweep aborts and
tells you to fix the key and re-run with `--resume`; without that a dead key part-way through a sweep would
spend the remaining wall-clock writing a success rate made of billing errors.

### Choosing which tasks to run

`eval/benchmarks/online_mind2web_all.jsonl` holds all 300 Online-Mind2Web tasks (easy 83, medium 143,
hard 74). Each carries three human annotations from the TinyFish workbook: an instruction `category`, an
`antibot_risk` rating, and an `attention_level`. `task_catalog.json` holds the same annotations plus, under
`external`, one third-party system's pass/fail per task and six annotators' human labels of *that* system's
runs. Nothing under `external` is a SuperBrowser result and none of it reaches the benchmark rows.

`--tasks` accepts an annotation filter wherever it accepts a subset name. Any term containing `=` switches
it into filter mode:

```bash
python -m eval.core.tasks --benchmark online_mind2web_all --select "level=hard,n=10"
python -m eval.core.tasks --benchmark online_mind2web_all --select "n=10,stratify=level,antibot=low|medium"
```

Fields are `level`, `category`, `antibot`, `attention`, `website`, each accepting a `|`-separated set, plus
`n` to cap the count, `stratify=<field>` to spread that cap evenly, and `seed`. Selection is deterministic:
the same filter yields the same tasks on any machine, so a run is reproducible from the string alone.

**Registered sets.** Three are already frozen (see `benchmarks/subsets.json` for the exact ids):

| Subset | n | Levels | Anti-bot | Purpose |
|---|---|---|---|---|
| `pilot5` | 5 | 2 easy / 1 med / 2 hard | low only | smoke test, not a paper result |
| `pilot10` | 10 | 4 / 3 / 3 | low + medium | cost calibration, not a paper result |
| `paper24` | 24 | 8 / 8 / 8 | unfiltered (4 high) | the paper's evaluation set |

`paper24` is an equal-allocation level-stratified random sample of the full 300-task split, seeded from its
filter string and frozen before any arm ran. Anti-bot risk is deliberately **not** filtered there: dropping
hard-to-reach sites would bias the headline upward, and blocked sites are handled by the impossible-task
rule in `PROTOCOL.md` instead. The pilots do filter, because their job is to exercise the pipeline rather
than to measure it — never report a pilot as a result.

**Freeze the selection before you run arms.** A task set must be fixed before results exist, otherwise
dropping a task later is indistinguishable from dropping one that an arm failed:

```bash
python -m eval.core.tasks --benchmark online_mind2web_all \
  --filter "n=10,stratify=level,antibot=low|medium" --make-subset pilot10 --note "first live sweep"
python -m eval.experiments.e2_memory_policy.run --model <id> --tasks pilot10   # 10 tasks x 4 arms = 40 runs
```

A frozen subset records the benchmark it came from, so `--tasks pilot10` resolves even though the
experiments default to the hard-only split. `pilot10` above is already registered: 10 tasks, 4 easy,
3 medium, 3 hard, no high-anti-bot sites. Rebuild the catalog after editing the workbook with
`python -m eval.benchmarks.build_catalog`.

### Running every ablation in one sweep (recommended on a small task set)

The five paired ablations all use `ledger` as their baseline arm. Run them as separate experiments and you
pay for that baseline five times. Run them as ONE experiment and you pay once:

```bash
python -m eval.core.runner --experiment combined10 \
  --arms ledger,fifo,summary,full_history,no_deadend,fresh_vision,no_ladder,flat \
  --model <id> --tasks pilot10 --manage-server
```

| | Separate experiments | One combined sweep |
|---|---|---|
| Runs on 10 tasks | 120 | **80** |
| Baseline runs | 50 (40 redundant) | 10 |
| Drift between comparisons | uncontrolled | all arms of a task run back to back |

The second row is the cost saving; the third is the methodological gain. Interleaving every arm of a task
in one window is the only way a cross-mechanism statement ("the Ledger helps more than dead-end memory
does") is defensible at all.

Then point each analyzer at the shared pool. It selects its own arms and says how many records matched:

```bash
for e in e2_memory_policy e4_deadend e5_perception_reuse e7_click_cascade e8_topology; do
  python -m eval.experiments.$e.analyze --experiment combined10; done
```

Drop `no_ladder` from the arm list to avoid TypeScript-side server restarts (it is the only arm that needs
them), and drop `--manage-server` with it.

### Watching a sweep

Every sweep prints a clickable URL when it starts:

```
  live viewer: http://127.0.0.1:8700   (Tier-1 and Tier-3; follows the active run)
```

It shows the newest frame from whichever run is currently writing, with the arm, the task, the page URL,
the tier and the frame count, refreshing on its own. `--viewer-port N` moves it, `--no-viewer` turns it
off; it is read-only, so starting and stopping it mid-sweep is safe. It also runs standalone against
finished runs: `python -m eval.viewer --experiment ablate10`.

This deliberately does not use the browser server's `/session/:id/view` route. That route only knows
Tier-1 sessions the TypeScript server holds itself, so a Tier-3 run is invisible through it, and the
harness-managed server picks a new port every sweep. Both tiers write frames into the run directory, so
serving those follows the sweep whichever tier a task ends up on.

### Running experiments one at a time

Each experiment is independent: its own arms, its own `eval/runs/<experiment>/` tree, its own
`results.jsonl`, its own analyzer. You can run one, analyse it, and come back weeks later for the next.
Three things to know:

- **Interrupting is safe.** Stop with Ctrl-C and re-launch the same command with `--resume`; finished runs
  are skipped. An unfinished attempt is archived to `eval/runs/<experiment>/_failed_attempts/` before the
  retry starts, so its screenshots can never be judged as part of the new run, and nothing you paid for is
  deleted.
- **Arms interleave per task, so drift is controlled inside an experiment but not between them.** The
  schedule is seed → task → arm: every arm for one task runs back to back. That is why each experiment
  re-runs its own baseline arm instead of reusing another experiment's, and why you should not compare a
  TSR from E2 against a TSR from E4 run a month later.
- **A partial sweep still analyses.** Pairing is per (task, seed); the analyzer reports how many runs were
  unpaired. Judging is a separate offline pass (`python -m eval.core.judge --experiment <name>`), so you
  can sweep now and judge later.

## User-owned follow-ups (left deliberately)

- **Headline reconciliation** — `benchmarks/exclusions.json` `legacy_66_task_reconciliation` is open: which
  task ids (or exclusion rule) turned the 74-task hard split into the draft's "66". E0 reports both.
- **Brain model** — pass `--model`; `pricing.json` covers the three candidates. Set it once E1 is run.
- **E9 human labels** — `make_sheet` builds the stratified sheet; two people fill `label_h1/label_h2`, then
  `analyze` reports κ.
- **checks.json / critical_state.json** — deterministic checks are a skeleton; critical-state items are
  hand-reviewed for all 74 hard tasks and can be extended.
