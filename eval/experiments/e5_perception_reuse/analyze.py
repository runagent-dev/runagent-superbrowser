"""E5 analysis: RPR / vision calls per arm + page-churn strata (stable / mild / dynamic)."""
from __future__ import annotations

from eval.core import analysis, report, stats
from eval.core.experiment import analyze_main
from eval.experiments.e5_perception_reuse import SPEC


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    arms = analysis.by_arm(recs)
    base = arms.get("ledger", {})
    # churn per task from the ledger arm (seed-averaged)
    churn: dict[str, list[float]] = {}
    for (task, _seed), r in base.items():
        c = (r.metrics.get("rpr") or {}).get("page_churn")
        if isinstance(c, (int, float)):
            churn.setdefault(task, []).append(float(c))
    task_churn = {t: sum(v) / len(v) for t, v in churn.items()}
    strata: dict[str, str] = {}
    if task_churn:
        vals = sorted(task_churn.values())
        t1, t2 = vals[len(vals) // 3], vals[(2 * len(vals)) // 3]
        for t, c in task_churn.items():
            strata[t] = "stable" if c <= t1 else ("mild" if c <= t2 else "dynamic")
    rows = []
    for stratum in ("stable", "mild", "dynamic", "all"):
        for arm, runs in arms.items():
            rs = [r for (task, _), r in runs.items() if stratum == "all" or strata.get(task) == stratum]
            if not rs:
                continue
            k = sum(1 for r in rs if r.outcome.get("success"))
            vc = [r.counts.get("vision_calls") or 0 for r in rs]
            rp = [(r.metrics.get("rpr") or {}).get("rpr") for r in rs]
            rp = [x for x in rp if isinstance(x, (int, float))]
            rows.append({"stratum": stratum, "arm": arm, "n": len(rs), "k": k, "tsr": k / len(rs),
                         "vision_calls": sum(vc) / len(vc), "rpr": sum(rp) / len(rp) if rp else None,
                         "wall_s": sum(float(r.timing.get("wall_s") or 0) for r in rs) / len(rs),
                         "usd": sum(float((r.cost or {}).get("usd") or 0) for r in rs) / len(rs)})
    report.write_csv(out / "churn_strata.csv", rows)
    report.write_json(out / "task_churn.json", {"task_churn": task_churn, "strata": strata})
    report.write_tex_table(out / "churn_strata.tex", ["Stratum", "Arm", "n", "TSR (\\%)", "Vision calls", "RPR", "Wall (s)", "\\$/task"],
                           [[report.tex_escape(r["stratum"]), report.tex_escape(r["arm"]), str(r["n"]), report.fmt(r["tsr"], "pct"),
                             report.fmt(r["vision_calls"]), report.fmt(r["rpr"]), report.fmt(r["wall_s"]), report.fmt(r["usd"], "usd")] for r in rows])


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
