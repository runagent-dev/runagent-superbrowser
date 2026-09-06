"""Cost per task and per successful task, by role, for any set of experiments.

    python -m eval.experiments.e11_cost.analyze --experiments e1_main,e2_memory_policy
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from eval.core import pricing, report
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_records
from eval.core.metrics.cost import cost_of_record

NAME = "e11_cost"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cost accounting")
    ap.add_argument("--experiments", default="e1_main,e2_memory_policy,e3_memory_pressure,e4_deadend,e5_perception_reuse,e7_click_cascade,e8_topology,e10_robustness")
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    args = ap.parse_args(argv)
    table = pricing.load()
    rows = []
    for exp in [e.strip() for e in args.experiments.split(",") if e.strip()]:
        recs = load_records(exp, runs_root=Path(args.runs))
        groups: dict[tuple[str, str], list] = defaultdict(list)
        for r in recs:
            r.cost = cost_of_record(r, table)
            groups[(exp, str(r.ids.get("arm")))].append(r)
        for (e, arm), rs in sorted(groups.items()):
            n = len(rs)
            k = sum(1 for r in rs if r.outcome.get("success"))
            usd = sum(float(r.cost.get("usd") or 0) for r in rs)
            usdc = sum(float(r.cost.get("usd_cached") or 0) for r in rs)
            by_role: dict[str, float] = defaultdict(float)
            for r in rs:
                for role, v in (r.cost.get("by_role") or {}).items():
                    by_role[role] += float(v.get("usd") or 0)
            rows.append({"experiment": e, "arm": arm, "model": rs[0].protocol.get("model"), "n": n, "k": k,
                         "usd_per_task": usd / n, "usd_cached_per_task": usdc / n,
                         "usd_per_success": (usd / k) if k else None, "usd_cached_per_success": (usdc / k) if k else None,
                         "usd_judge_per_task": sum(float(r.cost.get("usd_judge") or 0) for r in rs) / n,
                         **{f"usd_{role}_per_task": v / n for role, v in sorted(by_role.items())},
                         "input_tokens_per_task": sum((r.tokens.get("input_tokens") or 0) for r in rs) / n,
                         "output_tokens_per_task": sum((r.tokens.get("output_tokens") or 0) for r in rs) / n,
                         "cache_read_tokens_per_task": sum((r.tokens.get("cache_read_tokens") or 0) for r in rs) / n,
                         "unpriced_runs": sum(1 for r in rs if not r.cost.get("priced"))})
    out = report.out_dir(NAME)
    report.write_csv(out / "cost_by_arm.csv", rows)
    report.write_json(out / "price_table_used.json", table)
    report.write_tex_table(out / "cost_by_arm.tex", ["Experiment", "Arm", "n", "\\$/task", "\\$/task (cached)", "\\$/success", "Judge \\$/task"],
                           [[report.tex_escape(r["experiment"]), report.tex_escape(r["arm"]), str(r["n"]), report.fmt(r["usd_per_task"], "usd"),
                             report.fmt(r["usd_cached_per_task"], "usd"), report.fmt(r["usd_per_success"], "usd"), report.fmt(r["usd_judge_per_task"], "usd")] for r in rows],
                           note=f"list prices as of {table.get('as_of')} from {table.get('source')}")
    if not rows:
        print("[e11] no records found for the requested experiments")
    for r in rows:
        print(f"{r['experiment']:22s} {r['arm']:16s} n={r['n']:3d} $/task={r['usd_per_task']:.2f} $/success={r['usd_per_success'] if r['usd_per_success'] is None else round(r['usd_per_success'], 2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
