"""E7 analysis: first-path success, recovery success, escalation strategies, snapper methods."""
from __future__ import annotations

from collections import Counter

from eval.core import analysis, report
from eval.core.experiment import analyze_main
from eval.experiments.e7_click_cascade import SPEC


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    rows = []
    for arm, runs in analysis.by_arm(recs).items():
        rs = list(runs.values())
        g = [r.metrics.get("grounding") or {} for r in rs]
        strategies: Counter = Counter()
        methods: Counter = Counter()
        for x in g:
            strategies.update(x.get("strategies") or {})
            methods.update(x.get("methods") or {})
        fp = [x.get("first_path_success") for x in g if isinstance(x.get("first_path_success"), (int, float))]
        rc = [x.get("recovery_success") for x in g if isinstance(x.get("recovery_success"), (int, float))]
        rows.append({"arm": arm, "n": len(rs), "clicks": sum((x.get("clicks") or 0) for x in g),
                     "first_path_success": sum(fp) / len(fp) if fp else None,
                     "recovery_success": sum(rc) / len(rc) if rc else None,
                     "escalated": sum((x.get("escalated") or 0) for x in g), "silent": sum((x.get("silent") or 0) for x in g),
                     "strategies": dict(strategies), "methods": dict(methods),
                     "grounding_errors": sum(1 for x in g if x.get("grounding_error"))})
    report.write_csv(out / "execution_table.csv", rows)
    report.write_tex_table(out / "execution_table.tex", ["Arm", "Clicks", "First-path success", "Recovery success", "Escalated", "Silent", "Grounding errors"],
                           [[report.tex_escape(r["arm"]), str(r["clicks"]), report.fmt(r["first_path_success"], "pct"), report.fmt(r["recovery_success"], "pct"),
                             str(r["escalated"]), str(r["silent"]), str(r["grounding_errors"])] for r in rows])


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
