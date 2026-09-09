# e8_topology — Orchestrator->worker vs flat single agent

**Question.** With identical tools, memory hook and budgets, does the strategic orchestrator layer change success, step count, repeated actions or premature completion, and what is its overhead?

**Arms.** `ledger` (Orchestrator → `delegate_browser_task` → Worker) vs `flat` (one agent with the browser tools registered directly, same memory hook, worker hook and iteration cap; `SUPERBROWSER_TOPOLOGY=flat`, read by the eval runner).

**Task set / seeds.** `ablation24` (see `eval/benchmarks/subsets.json`), 1 seed(s) by default.

**Metrics.** TSR, worker iterations, tool calls, repeated actions, premature completion (run ended with a claimed answer that the evaluator rejected), orchestrator overhead (iterations and token share).

**Confirmatory comparisons.** `ledger` vs `flat`.

```bash
python -m eval.experiments.e8_topology.run --dry-run            # schedule + env
python -m eval.experiments.e8_topology.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.e8_topology.analyze                  # -> eval/artifacts/e8_topology/
```
The paper's periodic Planner exists only in the disabled TypeScript agent, so role separation is tested as a topology ablation on the real pipeline.

## Known confounds — read before reporting this experiment

An audit of `eval/core/run_one.py` found that `flat` is **not yet a matched control**. It reproduces the
delegated worker's construction but not its inputs, so an orchestrator-vs-flat difference is not
attributable to topology alone:

1. **Prompt.** The delegated worker receives a large per-task prompt built in
   `orchestrator_tools/delegation.py` (execution plan, tool contract including the "open the browser at
   most once" rule, structured-extraction contract, verify-before-reporting rule, domain pin, prior
   learnings). The flat agent receives only the instruction and the start URL.
2. **Eval hooks.** The flat path does not install `eval_worker_hooks`, so
   `SUPERBROWSER_EVAL_DISTRACTOR_TOKENS` is a no-op under `flat`. Never combine E3's pressure arms with
   the flat topology.
3. **Subgoal compaction.** `flat` never calls `compact_subgoal`, so `evictions.subgoal_compacted` is
   structurally 0 for it and positive for the orchestrator. That metric is not comparable across arms.
4. **Transcript loss on failure.** The flat tap records the transcript from `after_run`, which nanobot
   calls only on the success path, so a timed-out or errored flat run writes an empty message list while
   the orchestrator arm writes no worker file at all. The two arms therefore lose evidence differently
   on exactly the runs that matter most.

Until 1 and 2 are fixed, treat E8 as descriptive: report it as "an unstructured single agent with the
same tools and memory", not as an isolation of role separation. The notes ask for role separation to be
demoted from the contribution list if it shows no benefit — that conclusion needs a fair control first.
