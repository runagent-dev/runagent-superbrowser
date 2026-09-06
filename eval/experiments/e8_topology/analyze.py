"""E8 analysis: premature completion, repeats and orchestrator overhead per arm."""
from __future__ import annotations

from eval.core import analysis, report
from eval.core.experiment import analyze_main
from eval.experiments.e8_topology import SPEC


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    rows = []
    for arm, runs in analysis.by_arm(recs).items():
        rs = list(runs.values())
        n = len(rs)
        premature = sum(1 for r in rs if r.outcome.get("failure_reason") == "premature_done")
        orch_iters = sum((r.counts.get("orchestrator_iterations") or 0) for r in rs) / n
        orch_tokens = sum(((r.tokens.get("by_role") or {}).get("orchestrator") or {}).get("input_tokens", 0) or 0 for r in rs) / n
        total_tokens = sum((r.tokens.get("input_tokens") or 0) for r in rs) / n
        rows.append({"arm": arm, "n": n, "premature_completion": premature / n,
                     "repeated_actions": sum(((r.metrics.get("drr") or {}).get("repeated_actions") or 0) for r in rs) / n,
                     "worker_iterations": sum((r.counts.get("worker_iterations") or 0) for r in rs) / n,
                     "orchestrator_iterations": orch_iters, "orchestrator_input_tokens": orch_tokens,
                     "orchestrator_token_share": (orch_tokens / total_tokens) if total_tokens else None})
    report.write_csv(out / "topology_table.csv", rows)
    report.write_tex_table(out / "topology_table.tex", ["Arm", "Premature done (\\%)", "Repeats/run", "Worker iters", "Orch. iters", "Orch. token share (\\%)"],
                           [[report.tex_escape(r["arm"]), report.fmt(r["premature_completion"], "pct"), report.fmt(r["repeated_actions"]),
                             report.fmt(r["worker_iterations"]), report.fmt(r["orchestrator_iterations"]), report.fmt(r["orchestrator_token_share"], "pct")] for r in rows])


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
