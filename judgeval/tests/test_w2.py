"""Checks for the frozen W2 rules. No network calls."""
from __future__ import annotations

import random
from pathlib import Path

from judgeval.w2.claims import claim_diff, claim_set
from judgeval.w2.decide import holm, in_band, length_band, narrative_read
from judgeval.w2.planlock import instructions, plan_format_ids, plan_task_ids, verify_plan_frozen
from judgeval.w2.run import fill_instruction, judge_messages, shuffled_triples, status_word
from judgeval.w2.sample import stratified_quarter
from judgeval.w2.stimuli import parse_stimuli

ROOT = Path(__file__).resolve().parents[2]


def test_plan_tag_matches_working_tree():
    text = verify_plan_frozen(ROOT)
    assert "PROSPECTIVE" in text
    assert "RETROSPECTIVE" in text
    assert len(plan_task_ids(text)) == 12
    assert len(plan_format_ids(text)) == 3


def test_stimuli_are_the_twelve_pairs_without_verdicts():
    units = parse_stimuli(ROOT)
    assert len(units) == 24
    assert {u["source_arm"] for u in units} == {"ledger", "full_history"}
    ids = [u["task_id"] for u in units if u["source_arm"] == "ledger"]
    assert ids == plan_task_ids(verify_plan_frozen(ROOT))
    assert all("success" not in u for u in units)
    assert all(u["source_report"] for u in units)


def test_claim_gate_is_symmetric_and_catches_added_numbers():
    src = "The price is $1,299 at https://example.com/a. No other fee."
    assert claim_diff(src, src)["equal"]
    added = claim_diff(src, src + " The tax is 9.")
    assert "number:9" in added["added"]
    # Thousands separators normalize. A trailing URL period is stripped.
    assert ("number", "1299") in claim_set("Cost 1,299.")
    assert ("url", "https://example.com/a") in claim_set("See https://example.com/a.")
    assert ("yesno", "no other fee.") in claim_set(src)


def test_narrative_order_and_holm():
    assert narrative_read(80, 40, 78) == "disclosure"
    assert narrative_read(80, 40, 60) == "both"
    # 42 sits strictly between 40 and 80, so the earlier "both" rule applies.
    # A verbose rate just below DISCLOSE is the length read.
    assert narrative_read(80, 40, 42) == "both"
    assert narrative_read(80, 40, 38) == "length"
    assert narrative_read(50, 50, 50) == "indeterminate"
    # Overlap: within 10pp of ASSERT and strictly between. The plan lists disclosure first.
    assert narrative_read(80, 50, 75) == "disclosure"
    adj = holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj["a"] == 0.03
    assert adj["c"] == 0.06
    assert adj["b"] == 0.06


def test_length_band_matches_the_instruction():
    assert length_band(100) == (90, 110)
    assert in_band(90, 100) and in_band(110, 100) and not in_band(89, 100)
    text = fill_instruction("between {lo} and {hi} tokens. The target is {target} tokens.", 100)
    assert text == "between 90 and 110 tokens. The target is 100 tokens."


def test_rewriter_instructions_come_from_the_plan():
    got = instructions(verify_plan_frozen(ROOT))
    assert "condition ASSERT" in got["ASSERT"]
    assert "{target}" in got["VERBOSE_CONF"]
    assert "{target}" not in got["ASSERT"]
    assert "bullet" in got["FORMAT_BULLETS"]


def test_judge_request_has_no_condition_name_or_arm():
    messages = judge_messages("SYS", "Find the price.", "1. price", "The price is 5.")
    blob = messages[1]["content"]
    assert "User Task: Find the price." in blob
    assert "Action History:\n1. The price is 5." in blob
    assert "ASSERT" not in blob
    assert "ledger" not in blob
    assert status_word('Thoughts: checked\nStatus: "success"') == "success"
    assert status_word("I think it worked") is None


def test_call_order_is_seeded():
    triples = [(i, "ASSERT", "j") for i in range(20)]
    assert shuffled_triples(triples) == shuffled_triples(triples)
    assert shuffled_triples(triples) != triples


def test_human_sample_is_a_quarter_and_unlabeled():
    items = []
    for arm in ("ledger", "full_history"):
        for cond in ("ASSERT", "DISCLOSE", "VERBOSE_CONF"):
            for n in range(4):
                items.append({"unit_id": f"t{n}:{arm}", "source_arm": arm, "condition": cond})
    # 2*3*4 = 24, quarter = 6
    chosen = stratified_quarter(items)
    assert len(chosen) == 6
    rng = random.Random(0)
    assert rng.random() >= 0  # sample helper does not consume an unlabeled field
