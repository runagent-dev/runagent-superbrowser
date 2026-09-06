"""E1 analysis: headline TSR with Wilson CI, breakdown by site family and
failure reason, efficiency and cost distribution."""
from __future__ import annotations

from collections import defaultdict

from eval.core import report, stats
from eval.core.experiment import analyze_main
from eval.core.tasks import Task, site_type
from eval.experiments.e1_main import SPEC


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    by_site: dict[str, list] = defaultdict(list)
    by_level: dict[str, list] = defaultdict(list)
    for r in recs:
        t = Task(task_id=r.ids["task_id"], instruction="", start_url=r.ids.get("website"))
        by_site[site_type(t)].append(r)
        by_level[str(r.ids.get("level"))].append(r)
    rows = []
    for name, groups in (("site_family", by_site), ("level", by_level)):
        for key, rs in sorted(groups.items()):
            k = sum(1 for x in rs if x.outcome.get("success"))
            lo, hi = stats.wilson_ci(k, len(rs))
            rows.append({"stratum": name, "value": key, "n": len(rs), "k": k, "tsr": k / len(rs), "wilson_low": lo, "wilson_high": hi,
                         "mean_tool_calls": sum((x.counts.get("tool_calls_executed") or 0) for x in rs) / len(rs),
                         "mean_usd": sum(float((x.cost or {}).get("usd") or 0) for x in rs) / len(rs)})
    report.write_csv(out / "strata.csv", rows)
    report.write_tex_table(out / "strata.tex", ["Stratum", "n", "Success", "TSR (\\%)", "95\\% CI", "Tool calls", "\\$/task"],
                           [[f"{report.tex_escape(r['stratum'])}: {report.tex_escape(r['value'])}", str(r["n"]), f"{r['k']}/{r['n']}",
                             report.fmt(r["tsr"], "pct"), f"[{report.fmt(r['wilson_low'], 'pct')}, {report.fmt(r['wilson_high'], 'pct')}]",
                             report.fmt(r["mean_tool_calls"]), report.fmt(r["mean_usd"], "usd")] for r in rows])
    fails = defaultdict(int)
    for r in recs:
        if not r.outcome.get("success"):
            fails[str(r.outcome.get("failure_reason"))] += 1
    report.write_csv(out / "failure_reasons.csv", [{"failure_reason": k, "n": v} for k, v in sorted(fails.items(), key=lambda kv: -kv[1])])


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
