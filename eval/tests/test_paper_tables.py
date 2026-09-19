"""The paper's results tables and prose macros are generated, never typed.

Golden test: regenerating from the frozen records must reproduce the tracked
copy byte for byte. When a number legitimately changes (re-judging, a new
exclusion), regenerate with ``python -m eval.experiments.paper_tables`` and
commit both the paper's tables and the golden copy together.
"""
import re
from pathlib import Path

import pytest

from eval.experiments import paper_tables as pt

FROZEN = pt.FROZEN


@pytest.fixture(scope="module")
def computed():
    if not (FROZEN / "results.jsonl").exists():
        pytest.skip("frozen results.jsonl not present")
    return pt.compute(pt.load_inputs(FROZEN))


def test_golden_tables_match_frozen_records():
    if not (FROZEN / "tables").exists():
        pytest.skip("no golden copy yet")
    diffs = pt.check(FROZEN, FROZEN / "tables")
    assert diffs == [], f"stale generated files: {diffs}; run python -m eval.experiments.paper_tables"


def test_primary_tables_apply_the_pre_registered_exclusion(computed):
    c = computed
    n = {s["n"] for s in c["summ"].values()}
    assert n == {23}, "every arm must share the primary denominator"
    assert {s["n"] for s in c["summ_all"].values()} == {24}
    # the excluded task was failed by every arm: numerators and p-values are unchanged
    for arm, pb in c["pairs"].items():
        pa = c["pairs_all"][arm]
        assert (pb["a_only"], pb["b_only"]) == (pa["a_only"], pa["b_only"])
        assert abs(pb["p_value"] - pa["p_value"]) < 1e-12


def test_confirmatory_pairs_unadjusted_and_secondary_holm(computed):
    P = computed["pairs"]
    assert P["fifo"]["p_holm"] is None and P["full_history"]["p_holm"] is None
    sec = [a for a in P if P[a]["p_holm"] is not None]
    assert len(sec) == 5
    assert all(P[a]["p_holm"] >= P[a]["p_value"] for a in sec)
    assert P["no_ladder"]["p_value"] < 0.05 < P["no_ladder"]["p_holm"], "the one nominal effect does not survive Holm"


def test_csd_is_event_pooled_and_ledger_ties_full_history(computed):
    S = computed["summ"]
    for arm in ("ledger", "full_history"):
        s = S[arm]
        assert s["csd_lost"] == 2 and s["csd_events"] > 150
        assert abs(s["csd_observed_pooled"] - (s["csd_events"] - s["csd_lost"]) / s["csd_events"]) < 1e-9
    assert abs(S["ledger"]["csd_observed_pooled"] - S["full_history"]["csd_observed_pooled"]) < 0.005
    assert S["ledger"]["csd_observed"] > S["full_history"]["csd_observed"], "the macro mean is the number earlier drafts reported"


def test_peak_context_reports_the_maximum_not_only_the_mean(computed):
    fh = computed["summ"]["full_history"]
    assert fh["prompt_peak_max"] > 90_000 and fh["prompt_peak"] < 60_000
    assert fh["prompt_peak_max"] < pt.CONTEXT_WINDOW / 2
    assert sum(s["n_over_snip_threshold"] for s in computed["summ"].values()) == 0


def test_every_macro_used_in_the_paper_is_defined(tmp_path):
    """Every \\Num... the paper cites must be generated, and every generated
    macro name must be a valid LaTeX control sequence (letters only)."""
    files = pt.generate(FROZEN, tmp_path) if (FROZEN / "results.jsonl").exists() else pytest.skip("no frozen records")
    defined = set(re.findall(r"\\newcommand\{\\(Num[A-Za-z]+)\}", files["numbers.tex"]))
    assert defined and all(re.fullmatch(r"Num[A-Za-z]+", d) for d in defined)
    paper = pt.PAPER_DIR
    if not paper.exists():
        pytest.skip("paper checkout not present")
    used = set()
    for tex in list(paper.glob("sections/*.tex")) + list(paper.glob("appendix/*.tex")) + list(paper.glob("tables/*.tex")):
        used |= set(re.findall(r"\\(Num[A-Za-z]+)", tex.read_text(encoding="utf-8")))
    used.discard("Num")
    missing = sorted(u for u in used if u not in defined)
    assert not missing, f"macros cited in the paper but not generated: {missing}"
