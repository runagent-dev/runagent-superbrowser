"""Recompute the headline task-success number from records.

    python -m eval.experiments.e0_headline_audit.analyze [--experiment e1_main] [--arm ledger]

Prints and writes: k/N with Wilson CI on ALL tasks, on the pre-registered
exclusion set, and on the impossible-task set (deterministic markers); the
per-level and per-site breakdown; whether the percentage is an exact k/N;
and the open discrepancy between the paper's "66 tasks" and the frozen
74-task split (benchmarks/exclusions.json, user-owned).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from eval.core import report, stats
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_records
from eval.core.tasks import BENCH_DIR, Task, excluded_task_ids, load_benchmark, load_exclusions, site_type

NAME = "e0_headline_audit"


def audit(records, *, benchmark: str = "online_mind2web_hard") -> dict:
    frozen = {t.task_id: t for t in load_benchmark(benchmark)}
    # one record per task: if several seeds, majority / first seed (report both)
    by_task: dict[str, list] = defaultdict(list)
    for r in records:
        by_task[str(r.ids.get("task_id"))].append(r)
    seed0 = {t: sorted(rs, key=lambda r: int(r.ids.get("seed") or 0))[0] for t, rs in by_task.items()}
    evaluated = [t for t in seed0 if t in frozen]
    missing = sorted(set(frozen) - set(seed0))
    extra = sorted(set(seed0) - set(frozen))
    k_all = sum(1 for t in evaluated if seed0[t].outcome.get("success"))
    n_all = len(evaluated)
    pre = excluded_task_ids()
    kept_pre = [t for t in evaluated if t not in pre]
    k_pre = sum(1 for t in kept_pre if seed0[t].outcome.get("success"))
    impossible = {t: seed0[t].outcome.get("exclusion_label") for t in evaluated if seed0[t].outcome.get("exclusion_label")}
    kept_imp = [t for t in kept_pre if t not in impossible]
    k_imp = sum(1 for t in kept_imp if seed0[t].outcome.get("success"))
    strata = defaultdict(lambda: [0, 0])
    for t in evaluated:
        s = site_type(frozen[t])
        strata[s][1] += 1
        strata[s][0] += int(bool(seed0[t].outcome.get("success")))
    decided = defaultdict(int)
    for t in evaluated:
        decided[str(seed0[t].outcome.get("decided_by"))] += 1
    ex = load_exclusions()
    return {
        "benchmark": benchmark, "frozen_n": len(frozen), "evaluated_n": n_all, "missing_tasks": missing, "extra_tasks": extra,
        "all_tasks": {"k": k_all, "n": n_all, "pct": 100 * k_all / n_all if n_all else None, "wilson": stats.wilson_ci(k_all, n_all) if n_all else None,
                      "exact_fraction": f"{k_all}/{n_all}"},
        "pre_registered_exclusions": {"n_excluded": len(pre), "k": k_pre, "n": len(kept_pre),
                                      "pct": 100 * k_pre / len(kept_pre) if kept_pre else None, "which": pre},
        "impossible_tasks": {"n_excluded": len(impossible), "k": k_imp, "n": len(kept_imp),
                             "pct": 100 * k_imp / len(kept_imp) if kept_imp else None, "which": impossible},
        "by_site_family": {s: {"k": v[0], "n": v[1], "pct": 100 * v[0] / v[1]} for s, v in sorted(strata.items())},
        "decided_by": dict(decided),
        "multi_seed_tasks": sum(1 for rs in by_task.values() if len(rs) > 1),
        "legacy_66_reconciliation": ex.get("legacy_66_task_reconciliation"),
        "paper_draft_claim": {"tasks": 66, "pct": 89.47, "consistent_fraction": any(abs(100 * k / 66 - 89.47) < 0.005 for k in range(67))},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Headline audit from run records")
    ap.add_argument("--experiment", default="e1_main")
    ap.add_argument("--arm", default="ledger")
    ap.add_argument("--benchmark", default="online_mind2web_hard")
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    args = ap.parse_args(argv)
    recs = [r for r in load_records(args.experiment, runs_root=Path(args.runs)) if r.ids.get("arm") == args.arm]
    out = report.out_dir(NAME)
    if not recs:
        print(f"[e0] no records for {args.experiment}/{args.arm} under {args.runs}; the audit runs once E1 has been executed.")
        report.write_json(out / "audit.json", {"status": "no records", "experiment": args.experiment, "arm": args.arm,
                                               "legacy_66_reconciliation": load_exclusions().get("legacy_66_task_reconciliation"),
                                               "note": "paper draft: 89.47% on 66 tasks is not an integer multiple of 1/66 (59/66=89.39%, 60/66=90.91%)"})
        return 0
    a = audit(recs, benchmark=args.benchmark)
    report.write_json(out / "audit.json", a)
    rows = [{"scope": "all evaluated tasks", **a["all_tasks"]},
            {"scope": "minus pre-registered exclusions", **{k: a["pre_registered_exclusions"][k] for k in ("k", "n", "pct")}},
            {"scope": "minus impossible tasks", **{k: a["impossible_tasks"][k] for k in ("k", "n", "pct")}}]
    report.write_csv(out / "headline.csv", rows)
    report.write_tex_table(out / "headline.tex", ["Scope", "Success", "TSR (\\%)"],
                           [[report.tex_escape(r["scope"]), f"{r['k']}/{r['n']}", report.fmt((r["pct"] or 0) / 100, "pct")] for r in rows],
                           note="raw counts always accompany percentages; see audit.json for the Wilson intervals and exclusion lists")
    print(json.dumps({k: a[k] for k in ("all_tasks", "pre_registered_exclusions", "impossible_tasks", "missing_tasks", "paper_draft_claim")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
