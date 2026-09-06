"""E4 analysis: DRR / repeats / guard refusals per arm + paired tests (standard layer)."""
from __future__ import annotations

from eval.core import analysis, report
from eval.core.experiment import analyze_main
from eval.experiments.e4_deadend import SPEC


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    arms = analysis.by_arm(recs)
    rows = []
    for arm, runs in arms.items():
        rs = list(runs.values())
        d = [r.metrics.get("drr") or {} for r in rs]
        rows.append({"arm": arm, "n": len(rs),
                     "drr": sum((x.get("drr") or 0) for x in d) / len(d) if d else None,
                     "revisits": sum((x.get("revisits") or 0) for x in d),
                     "dead_end_signatures": sum((x.get("dead_end_signatures") or 0) for x in d),
                     "repeated_actions": sum((x.get("repeated_actions") or 0) for x in d) / len(d) if d else None,
                     "dead_click_blocked": sum((x.get("dead_click_blocked") or 0) for x in d),
                     "same_element_blocked": sum((x.get("same_element_blocked") or 0) for x in d),
                     "url_revisits": sum((x.get("url_revisits") or 0) for x in d),
                     "steps": sum((x.get("steps") or 0) for x in d) / len(d) if d else None})
    report.write_csv(out / "deadend_table.csv", rows)
    report.write_tex_table(out / "deadend_table.tex", ["Arm", "DRR", "Revisits", "Dead ends", "Repeats/run", "Dead-click blocks", "Same-element blocks", "Steps/run"],
                           [[report.tex_escape(r["arm"]), report.fmt(r["drr"]), str(r["revisits"]), str(r["dead_end_signatures"]),
                             report.fmt(r["repeated_actions"]), str(r["dead_click_blocked"]), str(r["same_element_blocked"]), report.fmt(r["steps"])] for r in rows])


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
