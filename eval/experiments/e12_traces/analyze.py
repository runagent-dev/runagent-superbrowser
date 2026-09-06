"""Pick and dump traces that illustrate a measured aggregate effect.

    python -m eval.experiments.e12_traces.analyze --experiment e2_memory_policy --arm-a ledger --arm-b fifo

For each paired (task, seed) where arm A succeeded and arm B failed (or vice
versa), rank by the metric gap that the experiment is about and write a
markdown excerpt per case: the lost critical-state items (CSD), the revisited
dead ends (DRR), the redundant vision sequence (RPR), and the last actions +
final answers of both arms. Traces explain the aggregate; they never replace it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.core import analysis, report
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_steps

NAME = "e12_traces"


def _excerpt(rec, *, n_steps: int = 12) -> str:
    run_dir = Path(rec.ids["run_dir"])
    steps = load_steps(run_dir)
    lines = [f"**{rec.ids['arm']}** — success={rec.outcome.get('success')} ({rec.outcome.get('decided_by')}), "
             f"stop={rec.outcome.get('stop_reason')}, failure={rec.outcome.get('failure_reason')}, "
             f"steps={len(steps)}, vision={rec.counts.get('vision_calls')}, peak prompt={rec.tokens.get('prompt_tokens_per_iter_peak')}"]
    csd = rec.metrics.get("csd") or {}
    if csd.get("observed_lost"):
        lines.append("- CSD lost items: " + "; ".join(f"`{x['item']}` (seen step {x['introduced_step']}, needed step {x['reuse_step']})" for x in csd["observed_lost"][:5]))
    drr = rec.metrics.get("drr") or {}
    if drr.get("revisit_examples"):
        lines.append("- Dead-end revisits: " + "; ".join(f"step {x['step']} re-issued `{x['tool']}({x['target']})` first failed at {x['first_failure_step']}" for x in drr["revisit_examples"][:5]))
    rpr = rec.metrics.get("rpr") or {}
    if rpr.get("redundant"):
        lines.append(f"- Redundant vision passes: {rpr['redundant']} of {rpr['vision_calls']} (cached {rpr.get('redundant_cached')}, uncached {rpr.get('redundant_uncached')})")
    lines.append("- Last actions:")
    for s in steps[-n_steps:]:
        lines.append(f"  - `{s.get('tool')}({str(s.get('args'))[:80]})` → {str(s.get('result'))[:100].replace(chr(10), ' ')}")
    fa = (run_dir / "result.txt").read_text(encoding="utf-8")[:600] if (run_dir / "result.txt").exists() else ""
    lines.append(f"- Final answer: {fa.replace(chr(10), ' ')}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trace picker")
    ap.add_argument("--experiment", default="e2_memory_policy")
    ap.add_argument("--arm-a", default="ledger")
    ap.add_argument("--arm-b", default="fifo")
    ap.add_argument("--metric", default="csd_observed", help="metric gap used for ranking (see analysis.METRIC_GETTERS)")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    args = ap.parse_args(argv)
    recs = analysis.load(args.experiment, runs_root=Path(args.runs))
    arms = analysis.by_arm(recs)
    if args.arm_a not in arms or args.arm_b not in arms:
        print(f"[e12] arms {args.arm_a}/{args.arm_b} not both present in {args.experiment}")
        return 0
    pairing = analysis.pair(arms, args.arm_a, args.arm_b)
    getter = analysis.METRIC_GETTERS.get(args.metric, lambda r: None)
    cases = []
    for ra, rb in pairing.rows(arms):
        sa, sb = bool(ra.outcome.get("success")), bool(rb.outcome.get("success"))
        if sa == sb:
            continue
        ga, gb = getter(ra), getter(rb)
        gap = (float(ga) - float(gb)) if isinstance(ga, (int, float)) and isinstance(gb, (int, float)) else 0.0
        cases.append((abs(gap), ra, rb))
    cases.sort(key=lambda x: -x[0])
    out = report.out_dir(NAME)
    md = [f"# Traces: {args.experiment} — {args.arm_a} vs {args.arm_b}", "",
          f"{len(cases)} discordant pairs (one arm succeeded, the other failed) of {len(pairing.keys)} paired runs; "
          f"ranked by |Δ {args.metric}|. Selected AFTER the aggregate comparison in `eval/artifacts/{args.experiment}/`.", ""]
    for gap, ra, rb in cases[: args.top]:
        md += [f"## {ra.ids['task_id']} (seed {ra.ids['seed']}) — |Δ {args.metric}| = {gap:.3f}", "",
               f"Task: {json.loads((Path(ra.ids['run_dir']) / 'spec.json').read_text())['task']['instruction'] if (Path(ra.ids['run_dir']) / 'spec.json').exists() else ''}", "",
               _excerpt(ra), "", _excerpt(rb), ""]
    path = out / f"{args.experiment}__{args.arm_a}_vs_{args.arm_b}.md"
    path.write_text("\n".join(md))
    print(f"[e12] {len(cases)} discordant pairs; wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
