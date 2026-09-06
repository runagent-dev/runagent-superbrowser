"""E6 analyzer merges per-strategy result files into an accuracy table (offline)."""
import csv

from eval.core import report
from eval.experiments.e6_subelement import analyze as e6


def _write(dirp, strategy, rows):
    cols = ["strategy", "condition", "id", "family", "control", "hit"]
    with (dirp / f"per_item__{strategy}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_analyzer_builds_accuracy_and_paired(tmp_path):
    report.set_artifacts_root(tmp_path)
    d = tmp_path / "e6_subelement"
    d.mkdir()
    def rows(strategy, chev_hits, ctrl_hit):
        out = []
        for i in range(5):
            out.append({"strategy": strategy, "condition": "merged_row", "id": f"row-{i}", "family": "label_chevron",
                        "control": "False", "hit": "True" if i < chev_hits else "False"})
        out.append({"strategy": strategy, "condition": "merged_row", "id": "row-help", "family": "label_chevron",
                    "control": "True", "hit": "True" if ctrl_hit else "False"})
        return out
    _write(d, "chevron", rows("chevron", 5, True))
    _write(d, "center", rows("center", 1, True))
    e6.main(["--artifacts", str(d)])
    acc = {(r["strategy"], r["family"]): r for r in csv.DictReader((d / "accuracy.csv").open())}
    assert acc[("chevron", "label_chevron")]["k"] == "5" and acc[("chevron", "label_chevron")]["n"] == "5"
    assert acc[("center", "label_chevron")]["k"] == "1"
    paired = list(csv.DictReader((d / "paired.csv").open()))
    p = next(r for r in paired if {r["arm_a"], r["arm_b"]} == {"chevron", "center"})
    assert int(p["a_only"]) + int(p["b_only"]) >= 4     # discordant on the 4 chevron items center missed
    assert (d / "accuracy.tex").exists() and (d / "per_item.csv").exists()
