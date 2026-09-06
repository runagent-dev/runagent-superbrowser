"""E2 analysis: standard paired tables + the success/context/cost frontier and
the Critical-State-Durability breakdown per arm."""
from __future__ import annotations

from eval.core import analysis, report
from eval.core.experiment import analyze_main
from eval.experiments.e2_memory_policy import SPEC


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    summary = result.get("arms", {})
    # frontier: peak prompt vs TSR and $ vs TSR
    pts_ctx = {a: (s["prompt_peak"] or 0.0, s["tsr"] or 0.0) for a, s in summary.items() if s["prompt_peak"]}
    if len(pts_ctx) >= 2:
        report.scatter_png(out / "success_vs_peak_context.png", pts_ctx, xlabel="Peak prompt tokens per iteration (mean over runs)",
                           ylabel="TSR", title="E2: success vs peak context", yerr={a: s["wilson"] for a, s in summary.items() if a in pts_ctx})
    rows = []
    for arm, s in summary.items():
        rows.append({"arm": arm, "tsr": s["tsr"], "csd_observed": s["csd_observed"], "csd_task_given": s["csd_task_given"],
                     "drr": s["drr"], "repeated_actions": s["repeated_actions"], "prompt_peak": s["prompt_peak"],
                     "compressor_calls": None})
    arms = analysis.by_arm(recs)
    for r in rows:
        rs = list(arms.get(r["arm"], {}).values())
        r["compressor_calls"] = (sum((x.counts.get("compressor_calls") or 0) for x in rs) / len(rs)) if rs else None
    report.write_csv(out / "memory_table.csv", rows)
    report.write_tex_table(out / "memory_table.tex",
                           ["Arm", "TSR (\\%)", "CSD (obs.)", "CSD (task)", "DRR", "Repeats", "Peak prompt", "Compressor calls"],
                           [[report.tex_escape(r["arm"]), report.fmt(r["tsr"], "pct"), report.fmt(r["csd_observed"]),
                             report.fmt(r["csd_task_given"]), report.fmt(r["drr"]), report.fmt(r["repeated_actions"]),
                             report.fmt(r["prompt_peak"], "k"), report.fmt(r["compressor_calls"])] for r in rows],
                           note="CSD (obs.) = observation-derived critical state present at reuse; CSD (task) = task-given floor check")


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
