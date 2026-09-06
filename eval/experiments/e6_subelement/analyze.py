"""Target-selection accuracy per family x strategy (with Wilson CIs) from per_item.csv."""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from eval.core import report, stats

NAME = "e6_subelement"


def _b(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="E6 analysis")
    ap.add_argument("--artifacts", default=str(report.out_dir(NAME)), help="dir holding per_item__<strategy>.csv")
    args = ap.parse_args(argv)
    files = sorted(Path(args.artifacts).glob("per_item__*.csv"))
    if not files:
        print(f"[e6] no per_item__<strategy>.csv under {args.artifacts}; run python -m eval.experiments.e6_subelement.run first")
        return 0
    rows = [r for f in files for r in csv.DictReader(f.open(encoding="utf-8"))]
    report.write_csv(Path(args.artifacts) / "per_item.csv", rows)
    cells: dict[tuple[str, str, str], list[bool]] = defaultdict(list)
    for r in rows:
        key_family = r["family"] + (" (control)" if _b(r.get("control")) else "")
        cells[(r["condition"], r["strategy"], key_family)].append(_b(r["hit"]))
        cells[(r["condition"], r["strategy"], "ALL non-control" if not _b(r.get("control")) else "ALL control")].append(_b(r["hit"]))
    table = []
    for (cond, strat, fam), hits in sorted(cells.items()):
        k, n = sum(hits), len(hits)
        lo, hi = stats.wilson_ci(k, n)
        table.append({"condition": cond, "strategy": strat, "family": fam, "n": n, "k": k, "accuracy": k / n, "wilson_low": lo, "wilson_high": hi})
    out = report.out_dir(NAME)
    report.write_csv(out / "accuracy.csv", table)
    # paired McNemar between strategies on the merged-row condition (same items)
    by_item: dict[tuple[str, str], dict[str, bool]] = defaultdict(dict)
    for r in rows:
        by_item[(r["condition"], r["id"])][r["strategy"]] = _b(r["hit"])
    pairs = []
    strategies = sorted({r["strategy"] for r in rows})
    for cond in sorted({r["condition"] for r in rows}):
        for i, a in enumerate(strategies):
            for b in strategies[i + 1:]:
                xs, ys = [], []
                for (c, _id), d in by_item.items():
                    if c == cond and a in d and b in d:
                        xs.append(d[a])
                        ys.append(d[b])
                if xs:
                    m = stats.mcnemar_exact(xs, ys)
                    pairs.append({"condition": cond, "arm_a": a, "arm_b": b, **m.to_dict()})
    report.write_csv(out / "paired.csv", pairs)
    families = [f for f in sorted({t["family"] for t in table}) if not f.startswith("ALL")] + ["ALL non-control", "ALL control"]
    tex_rows = []
    for fam in families:
        row = [report.tex_escape(fam)]
        for strat in strategies:
            cell = next((t for t in table if t["condition"] == "merged_row" and t["strategy"] == strat and t["family"] == fam), None)
            row.append(f"{cell['k']}/{cell['n']} ({report.fmt(cell['accuracy'], 'pct')})" if cell else "--")
        tex_rows.append(row)
    report.write_tex_table(out / "accuracy.tex", ["Family (merged-row bbox)"] + strategies, tex_rows,
                           note="hit = the click landed on the intended sub-element (or inside it); control rows = the large label is the intended target")
    labels, vals, errs = [], [], []
    for t in table:
        if t["condition"] == "merged_row" and t["family"] in ("ALL non-control", "ALL control"):
            labels.append(f"{t['strategy']}\n{t['family'].replace('ALL ', '')}")
            vals.append(t["accuracy"])
            errs.append((t["wilson_low"], t["wilson_high"]))
    if labels:
        report.bar_png(out / "accuracy.png", labels, vals, errors=errs, ylabel="Target-selection accuracy", title="E6: snapper strategy", ylim=(0, 1))
    for t in table:
        if t["family"].startswith("ALL"):
            print(f"{t['condition']:11s} {t['strategy']:8s} {t['family']:16s} {t['k']}/{t['n']} = {100 * t['accuracy']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
