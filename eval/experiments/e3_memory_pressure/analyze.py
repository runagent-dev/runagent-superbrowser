"""E3 analysis: per (policy, pressure level) TSR / CSD / peak context with trend tests."""
from __future__ import annotations

import re
from collections import defaultdict

from eval.core import analysis, report, stats
from eval.core.experiment import analyze_main
from eval.experiments.e3_memory_pressure import SPEC

_ARM_RE = re.compile(r"^(?P<base>[a-z_]+)__p(?P<level>\d+)$")


def _extra(args, recs, result):
    out = report.out_dir(SPEC.name)
    cells: dict[tuple[str, int], list] = defaultdict(list)
    for r in recs:
        m = _ARM_RE.match(str(r.ids.get("arm")))
        if m:
            cells[(m["base"], int(m["level"]))].append(r)
    rows = []
    curves: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for (base, lv), rs in sorted(cells.items()):
        n = len(rs)
        k = sum(1 for x in rs if x.outcome.get("success"))
        csd = [(x.metrics.get("csd") or {}).get("csd_observed") for x in rs]
        csd = [c for c in csd if isinstance(c, (int, float))]
        peak = [x.tokens.get("prompt_tokens_per_iter_peak") for x in rs]
        peak = [p for p in peak if isinstance(p, (int, float))]
        row = {"policy": base, "pressure_tokens": lv, "n": n, "k": k, "tsr": k / n if n else None,
               "wilson_low": stats.wilson_ci(k, n)[0] if n else None, "wilson_high": stats.wilson_ci(k, n)[1] if n else None,
               "csd_observed": sum(csd) / len(csd) if csd else None,
               "prompt_peak": sum(peak) / len(peak) if peak else None}
        rows.append(row)
        curves[base]["level"].append(lv)
        curves[base]["tsr"].append(row["tsr"] or 0.0)
        curves[base]["csd"].append(row["csd_observed"] if row["csd_observed"] is not None else float("nan"))
        curves[base]["peak"].append(row["prompt_peak"] or 0.0)
    report.write_csv(out / "ladder.csv", rows)
    trends = []
    for base, c in curves.items():
        succ = [r["k"] for r in rows if r["policy"] == base]
        tot = [r["n"] for r in rows if r["policy"] == base]
        z, p = stats.cochran_armitage(succ, tot, scores=c["level"])
        rho, sp = stats.spearman_trend(c["level"], c["csd"]) if all(x == x for x in c["csd"]) else (float("nan"), None)
        trends.append({"policy": base, "levels": c["level"], "tsr_trend_z": z, "tsr_trend_p": p, "csd_spearman_rho": rho, "csd_spearman_p": sp})
    report.write_csv(out / "trends.csv", trends)
    report.write_tex_table(out / "ladder.tex", ["Policy", "Pressure (tok/step)", "Success", "TSR (\\%)", "CSD (obs.)", "Peak prompt"],
                           [[report.tex_escape(r["policy"]), str(r["pressure_tokens"]), f"{r['k']}/{r['n']}", report.fmt(r["tsr"], "pct"),
                             report.fmt(r["csd_observed"]), report.fmt(r["prompt_peak"], "k")] for r in rows])
    if curves:
        levels = sorted({lv for c in curves.values() for lv in c["level"]})
        if all(c["level"] == levels for c in curves.values()):
            report.lines_png(out / "tsr_vs_pressure.png", levels, {b: c["tsr"] for b, c in curves.items()},
                             xlabel="Distractor tokens per step", ylabel="TSR", title="E3: success under memory pressure")
            report.lines_png(out / "csd_vs_pressure.png", levels, {b: c["csd"] for b, c in curves.items()},
                             xlabel="Distractor tokens per step", ylabel="CSD (observation-derived)", title="E3: critical-state durability")
            report.lines_png(out / "peak_vs_pressure.png", levels, {b: c["peak"] for b, c in curves.items()},
                             xlabel="Distractor tokens per step", ylabel="Peak prompt tokens / iteration", title="E3: peak context")


main = analyze_main(SPEC, _extra)

if __name__ == "__main__":
    raise SystemExit(main())
