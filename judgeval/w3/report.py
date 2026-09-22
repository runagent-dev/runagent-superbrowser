"""W3.1–W3.3 from the files that exist, plus the human totals as stated.

Human task-level labels are not in the repository. Pairwise human results are
the set of tables consistent with the stated totals, not one table.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from judgeval.w3.equivalence import MARGIN, holm, mde, summarize_margins, summarize_pairs

ROOT = Path(__file__).resolve().parents[2]
PER_RUN = ROOT / "eval/artifacts/ablate10_paper/per_run.csv"

HUMAN = {
    "ledger": 18,
    "fifo": 19,
    "full_history": 20,
    "adaptive": 20,
}
HUMAN_N = 24
HUMAN_PAIRS = (
    ("ledger", "fifo"),
    ("ledger", "full_history"),
    ("ledger", "adaptive"),
    ("fifo", "full_history"),
    ("fifo", "adaptive"),
    ("full_history", "adaptive"),
)


def webjudge_by_arm() -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    with PER_RUN.open() as handle:
        for row in csv.DictReader(handle):
            out.setdefault(row["arm"], {})[row["task_id"]] = row["success"] == "True"
    return out


def _pp(x: float) -> float:
    return round(100 * x, 2)


def _slim(row: dict) -> dict:
    return {
        "both_success": row["both_success"],
        "a_only": row["a_only"],
        "b_only": row["b_only"],
        "both_fail": row["both_fail"],
        "discordant": row["discordant"],
        "mcnemar_p": round(row["mcnemar_p"], 4),
        "bootstrap_ci95_pp": [_pp(row["ci95"][0]), _pp(row["ci95"][1])],
        "bootstrap_ci90_pp": [_pp(row["ci90"][0]), _pp(row["ci90"][1])],
        "exact_conditional_ci95_pp": [_pp(row["exact_conditional_ci95"][0]),
                                      _pp(row["exact_conditional_ci95"][1])],
        "tost_90ci_inside_5pp": row["tost"],
    }


def _verdict(block: dict) -> str:
    if block["point_outside_margin"]:
        return "not equivalent: the rate difference itself is outside ±5 points"
    if block["tost_all_tables"]:
        return "equivalent on every table consistent with the totals"
    if block["tost_any_table"]:
        return "not established: some tables consistent with the totals pass TOST and some do not"
    return "not equivalent: no table consistent with the totals has its 90% interval inside ±5 points"


def human_block() -> dict:
    raw = {f"{a}_minus_{b}": summarize_margins(HUMAN[a], HUMAN[b], HUMAN_N) for a, b in HUMAN_PAIRS}
    # Holm uses the smallest feasible p, so a difference claim cannot appear by
    # picking a quieter table. Equivalence is still a separate statement.
    adjusted = holm({name: block["mcnemar_p_min"] for name, block in raw.items()})
    pairs = []
    for name, block in raw.items():
        pairs.append({
            "contrast": name,
            "k_a": block["k_a"],
            "k_b": block["k_b"],
            "point_diff_pp": _pp(block["point_diff"]),
            "n_feasible_tables": block["n_feasible_tables"],
            "mcnemar_p_range": [round(block["mcnemar_p_min"], 4), round(block["mcnemar_p_max"], 4)],
            "holm_on_smallest_p": round(adjusted[name], 4),
            "all_feasible_p_at_least_0.05": block["all_nonsignificant_unadjusted"],
            "mde_pp_range": [_pp(block["mde_at_min_discordant"]), _pp(block["mde_at_max_discordant"])],
            "verdict": _verdict(block),
            "nonsignificant_is_not_equivalence": (
                block["all_nonsignificant_unadjusted"] and not block["tost_all_tables"]
            ),
            "extreme_tables": [_slim(block["tables"][0]), _slim(block["tables"][-1])],
        })
    return {
        "n": HUMAN_N,
        "margin": MARGIN,
        "source": "stated totals only; task-level human labels are not in the repository",
        "pairs": pairs,
    }


def webjudge_block(by_arm: dict[str, dict[str, bool]]) -> dict:
    names = [a for a in ("ledger", "fifo", "full_history", "summary") if a in by_arm]
    raw = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ids = sorted(set(by_arm[a]) & set(by_arm[b]))
            raw[f"{a}_minus_{b}"] = summarize_pairs(
                [by_arm[a][t] for t in ids], [by_arm[b][t] for t in ids])
    adjusted = holm({name: block["observed"]["mcnemar_p"] for name, block in raw.items()})
    pairs = []
    for name, block in raw.items():
        obs = _slim(block["observed"])
        obs["holm_p"] = round(adjusted[name], 4)
        pairs.append({
            "contrast": name,
            "k_a": block["k_a"],
            "k_b": block["k_b"],
            "n": block["n"],
            "point_diff_pp": _pp(block["point_diff"]),
            "table": obs,
            "mde_pp_at_observed_discordant_rate": _pp(mde(obs["discordant"] / block["n"], block["n"])),
            "tost_passes": obs["tost_90ci_inside_5pp"],
            "difference_significant_after_holm": adjusted[name] < 0.05,
        })
    return {
        "arms": {a: {"k": sum(by_arm[a].values()), "n": len(by_arm[a])} for a in names},
        "adaptive": "no WebJudge rows in eval/artifacts/ablate10_paper/per_run.csv",
        "pairs": pairs,
    }


def instrument_block(by_arm: dict[str, dict[str, bool]]) -> dict:
    rows = []
    for arm, k_h in HUMAN.items():
        if arm not in by_arm:
            rows.append({
                "arm": arm,
                "human": f"{k_h}/{HUMAN_N}",
                "webjudge": "not in the frozen sweep",
                "paired_difference": "not computable",
            })
            continue
        k_w = sum(by_arm[arm].values())
        n = len(by_arm[arm])
        bound = summarize_margins(k_h, k_w, n)
        fn = [t["a_only"] for t in bound["tables"]]
        fp = [t["b_only"] for t in bound["tables"]]
        rows.append({
            "arm": arm,
            "human": f"{k_h}/{n} ({_pp(k_h / n)}%)",
            "webjudge": f"{k_w}/{n} ({_pp(k_w / n)}%)",
            "rate_gap_pp": _pp((k_h - k_w) / n),
            "false_negatives_human_pass_judge_fail": [min(fn), max(fn)],
            "false_positives_human_fail_judge_pass": [min(fp), max(fp)],
            "n_feasible_confusion_matrices": bound["n_feasible_tables"],
            "bootstrap_ci95_pp_at_extreme_tables": [
                [_pp(bound["tables"][0]["ci95"][0]), _pp(bound["tables"][0]["ci95"][1])],
                [_pp(bound["tables"][-1]["ci95"][0]), _pp(bound["tables"][-1]["ci95"][1])],
            ],
        })
    for arm, outcomes in sorted(by_arm.items()):
        if arm in HUMAN:
            continue
        rows.append({
            "arm": arm,
            "human": "not re-scored",
            "webjudge": f"{sum(outcomes.values())}/{len(outcomes)}",
            "paired_difference": "not computable",
        })
    return {"rows": rows}


def build() -> dict:
    by_arm = webjudge_by_arm()
    return {
        "margin_pp": 5,
        "tost_rule": "Equivalence at α=0.05 requires the 90% bootstrap interval inside ±5 points. "
                     "A 95% interval that covers zero is only a non-significant difference.",
        "holm": "Holm is applied within each difference-test family. It is not removed.",
        "human_equivalence": human_block(),
        "webjudge_paired": webjudge_block(by_arm),
        "table2_instruments": instrument_block(by_arm),
        "framing": {
            "judge_human_by_condition": "not estimable",
            "reason": "No human label exists for any framing condition. "
                      "The stated kappas 0.894 and 0.937 are not in the repository and were not recomputed.",
            "figure": "not drawn",
        },
        "calibration": {
            "rubric_verbatim": None,
            "bu_bench_objective_n40": "not in the repository; agreement with regex was not imputed",
            "phrase_100_percent_exact_parity": "absent from the repository; no sentence was rewritten",
        },
        "design_mde_pp_at_n24": {
            "discordant_rate_0.25": _pp(mde(0.25, 24)),
            "discordant_rate_0.10": _pp(mde(0.10, 24)),
        },
    }


def main() -> None:
    report = build()
    dest = ROOT / "judgeval/artifacts/w3/equivalence.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "human": [(p["contrast"], p["point_diff_pp"], p["verdict"]) for p in report["human_equivalence"]["pairs"]],
        "webjudge": [(p["contrast"], p["point_diff_pp"], p["tost_passes"], p["difference_significant_after_holm"])
                     for p in report["webjudge_paired"]["pairs"]],
    }, indent=2))


if __name__ == "__main__":
    main()
