"""Unit tests for Subgoal.precondition + check_precondition.

No external services required. Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_precondition.py
"""

from __future__ import annotations

import sys

from superbrowser_bridge.perception_fusion import FusedPerception
from superbrowser_bridge.task_graph import (
    Precondition,
    Subgoal,
    _build_graph_from_parsed,
)


class _FakeBBox:
    def __init__(self, *, label, box=(0, 0, 10, 10), layer_id=None):
        self.label = label
        self.box_2d = list(box)
        self.role = "button"
        self.confidence = 0.9
        self.clickable = True
        self.intent_relevant = True
        self.role_in_scene = "target"
        self.layer_id = layer_id


def test_precondition_from_dict_tolerates_missing() -> None:
    assert Precondition.from_dict(None) is None
    # Empty label and no regex → collapses to None.
    assert Precondition.from_dict({}) is None
    # Label provided → dataclass.
    pc = Precondition.from_dict({"element_label": "Submit"})
    assert pc is not None and pc.element_label == "Submit"
    assert pc.required is True
    print("✓ test_precondition_from_dict_tolerates_missing")


def test_subgoal_check_precondition_match_via_label() -> None:
    sg = Subgoal(
        id="g1", description="submit the form", status="active",
        precondition=Precondition(element_label="Submit"),
    )
    fused = FusedPerception.build(
        vision_bboxes=[_FakeBBox(label="Submit form")],
        dom_entries=[],
        observation_token=1,
    )
    res = sg.check_precondition(fused)
    assert res.satisfied is True
    assert res.reason == "label_match"
    print("✓ test_subgoal_check_precondition_match_via_label")


def test_subgoal_check_precondition_miss() -> None:
    sg = Subgoal(
        id="g1", description="submit the form", status="active",
        precondition=Precondition(element_label="Submit"),
    )
    fused = FusedPerception.build(
        vision_bboxes=[_FakeBBox(label="Cancel")],
        dom_entries=[], observation_token=1,
    )
    res = sg.check_precondition(fused)
    assert res.satisfied is False
    assert res.required_action == "re_perceive"
    print("✓ test_subgoal_check_precondition_miss")


def test_subgoal_without_precondition_always_satisfied() -> None:
    sg = Subgoal(id="g1", description="navigate", status="active")
    fused = FusedPerception.build(
        vision_bboxes=[], dom_entries=[], observation_token=1,
    )
    res = sg.check_precondition(fused)
    assert res.satisfied is True
    assert res.reason == "no_precondition"
    print("✓ test_subgoal_without_precondition_always_satisfied")


def test_decompose_parse_picks_up_precondition() -> None:
    parsed = [
        {
            "id": "g1",
            "description": "search for X",
            "precondition": {
                "element_label": "Search",
                "role_hint": "button",
            },
            "expected_signals": [
                {"kind": "url_contains", "payload": {"text": "/results"}},
            ],
        },
    ]
    graph = _build_graph_from_parsed(parsed)
    sg = graph.subgoals["g1"]
    assert sg.precondition is not None
    assert sg.precondition.element_label == "Search"
    assert sg.precondition.role_hint == "button"
    print("✓ test_decompose_parse_picks_up_precondition")


def test_subgoal_round_trip_with_precondition() -> None:
    sg = Subgoal(
        id="g1", description="Click submit", status="active",
        precondition=Precondition(element_label="Submit", role_hint="button"),
    )
    d = sg.to_dict()
    assert d["precondition"]["element_label"] == "Submit"
    sg2 = Subgoal.from_dict(d)
    assert sg2.precondition is not None
    assert sg2.precondition.element_label == "Submit"
    assert sg2.precondition.role_hint == "button"
    print("✓ test_subgoal_round_trip_with_precondition")


def main() -> int:
    tests = [
        test_precondition_from_dict_tolerates_missing,
        test_subgoal_check_precondition_match_via_label,
        test_subgoal_check_precondition_miss,
        test_subgoal_without_precondition_always_satisfied,
        test_decompose_parse_picks_up_precondition,
        test_subgoal_round_trip_with_precondition,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
