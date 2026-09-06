"""Agreement between humans and automatic evaluators.

    python -m eval.experiments.e9_evaluator_validation.analyze [--sheet eval/artifacts/e9_evaluator_validation/labelling_sheet.csv]

Reports raw agreement + Cohen's kappa for human-human, human-WebJudge,
human-answer-judge, human-deterministic (where defined), confusion matrices
(FP = automatic success / human failure), and the disagreement items.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from eval.core import report, stats

NAME = "e9_evaluator_validation"
LABELS = {"success": True, "failure": False, "true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}


def _lab(v: str):
    return LABELS.get(str(v).strip().lower())


def analyze_rows(rows: list[dict]) -> dict:
    h1 = [_lab(r.get("label_h1", "")) for r in rows]
    h2 = [_lab(r.get("label_h2", "")) for r in rows]
    both = [(a, b) for a, b in zip(h1, h2) if a is not None and b is not None]
    out: dict = {"n_items": len(rows), "n_labelled_both": len(both)}
    if both:
        agree, kappa = stats.cohen_kappa([a for a, _ in both], [b for _, b in both])
        out["human_human"] = {"agreement": agree, "kappa": kappa, "n": len(both)}
    # consensus human label (both agree) vs automatic
    for auto_col in ("auto_success", "auto_webjudge", "auto_answer_judge", "auto_deterministic"):
        pairs = []
        for r, a, b in zip(rows, h1, h2):
            auto = _lab(r.get(auto_col, ""))
            if a is None or b is None or a != b or auto is None:
                continue
            pairs.append((a, auto))
        if len(pairs) >= 2:
            agree, kappa = stats.cohen_kappa([p[0] for p in pairs], [p[1] for p in pairs])
            out[auto_col] = {"agreement": agree, "kappa": kappa, "n": len(pairs),
                             "confusion": stats.confusion([p[0] for p in pairs], [p[1] for p in pairs])}
    out["disagreements"] = [{"item": r.get("item"), "run_id": r.get("run_id"), "h1": r.get("label_h1"), "h2": r.get("label_h2"),
                             "auto_success": r.get("auto_success"), "decided_by": r.get("auto_decided_by")}
                            for r, a, b in zip(rows, h1, h2)
                            if a is not None and b is not None and (a != b or _lab(r.get("auto_success", "")) not in (None, a))]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluator validation")
    ap.add_argument("--sheet", default=str(report.out_dir(NAME) / "labelling_sheet.csv"))
    args = ap.parse_args(argv)
    p = Path(args.sheet)
    if not p.exists():
        print(f"[e9] no sheet at {p}; run make_sheet first and have two annotators fill label_h1/label_h2")
        return 0
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    res = analyze_rows(rows)
    out = report.out_dir(NAME)
    report.write_json(out / "agreement.json", res)
    trows = []
    for key, label in (("human_human", "Human vs human"), ("auto_success", "Human vs automatic (protocol)"),
                       ("auto_webjudge", "Human vs WebJudge"), ("auto_answer_judge", "Human vs answer judge"),
                       ("auto_deterministic", "Human vs deterministic")):
        v = res.get(key)
        if v:
            c = v.get("confusion") or {}
            trows.append([label, str(v["n"]), report.fmt(v["agreement"], "pct"), report.fmt(v["kappa"]),
                          str(c.get("fp", "--")), str(c.get("fn", "--"))])
    report.write_tex_table(out / "agreement.tex", ["Comparison", "n", "Agreement (\\%)", "Cohen's $\\kappa$", "FP", "FN"], trows,
                           note="FP = automatic success where humans agree on failure; FN = automatic failure where humans agree on success")
    print(f"[e9] labelled both: {res['n_labelled_both']}/{res['n_items']}; " + "; ".join(
        f"{k}: agree={v['agreement']:.2f} kappa={v['kappa']:.2f} (n={v['n']})" for k, v in res.items() if isinstance(v, dict) and "kappa" in v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
