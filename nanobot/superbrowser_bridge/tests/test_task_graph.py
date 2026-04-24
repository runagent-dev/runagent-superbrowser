"""Unit tests for the subconscious task graph.

No external services required. Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_task_graph.py
"""

from __future__ import annotations

import sys

from superbrowser_bridge.task_graph import (
    Signal,
    Subgoal,
    TaskGraph,
    decompose_task,
    evaluate_signal,
    trivial_graph,
    updater_check,
)


def _g123() -> TaskGraph:
    return TaskGraph(
        subgoals={
            "g1": Subgoal(
                id="g1", description="search",
                expected_signals=[Signal(kind="url_contains", payload={"text": "/results"})],
                status="active",
            ),
            "g2": Subgoal(
                id="g2", description="filter",
                expected_signals=[Signal(kind="element_visible", payload={"text": "Sort by"})],
            ),
            "g3": Subgoal(
                id="g3", description="click",
                expected_signals=[Signal(kind="url_contains", payload={"text": "/product/"})],
            ),
        },
        active_id="g1",
    )


def test_trivial_graph_renders_plan() -> None:
    g = trivial_graph("find headphones")
    assert g.total == 1
    assert g.current() is not None
    text = g.to_brain_text()
    assert "[PLAN]" in text
    assert "find headphones" in text
    print("✓ test_trivial_graph_renders_plan")


def test_signal_url_contains_match_and_miss() -> None:
    sig = Signal(kind="url_contains", payload={"text": "/results"})
    assert evaluate_signal(sig, url="https://x.com/results?q=foo") is True
    assert evaluate_signal(sig, url="https://x.com/home") is False
    print("✓ test_signal_url_contains_match_and_miss")


def test_signal_url_matches_regex() -> None:
    sig = Signal(kind="url_matches", payload={"pattern": r"/product/\d+$"})
    assert evaluate_signal(sig, url="https://x.com/product/42") is True
    assert evaluate_signal(sig, url="https://x.com/product/42/reviews") is False
    print("✓ test_signal_url_matches_regex")


def test_signal_element_visible_via_dom_text() -> None:
    sig = Signal(kind="element_visible", payload={"text": "Sort by"})
    dom_text = "[1] btn 'Filter'\n[2] btn 'Sort by Price'\n[3] link 'Help'"
    assert evaluate_signal(sig, dom_elements_text=dom_text) is True
    assert evaluate_signal(sig, dom_elements_text="[1] btn 'Cancel'") is False
    print("✓ test_signal_element_visible_via_dom_text")


def test_signal_scroll_at_bottom_uses_telemetry() -> None:
    sig = Signal(kind="scroll_at_bottom", payload={})
    assert evaluate_signal(sig, scroll_telemetry={"reached_bottom": True}) is True
    assert evaluate_signal(sig, scroll_telemetry={"reached_bottom": False}) is False
    assert evaluate_signal(sig) is False  # no telemetry → not fired
    print("✓ test_signal_scroll_at_bottom_uses_telemetry")


def test_updater_advances_on_signal() -> None:
    g = _g123()
    new_id, reason = updater_check(g, url="https://x.com/results?q=foo")
    assert reason.startswith("signal fired"), reason
    # Active had no transitions → next_id is None → advance() falls through
    # to insertion-order successor.
    assert new_id is None
    g.advance(new_id, reason)
    assert g.active_id == "g2"
    assert g.subgoals["g1"].status == "done"
    assert g.subgoals["g2"].status == "active"
    print("✓ test_updater_advances_on_signal")


def test_updater_skips_ahead() -> None:
    g = _g123()
    # URL has /product/ in it — that's g3's signal, not g1's.
    new_id, reason = updater_check(g, url="https://x.com/product/42")
    assert new_id == "g3", new_id
    assert reason.startswith("skipped ahead"), reason
    g.advance(new_id, reason)
    assert g.active_id == "g3"
    assert g.subgoals["g1"].status == "done"
    print("✓ test_updater_skips_ahead")


def test_updater_stale_threshold() -> None:
    g = _g123()
    # No signal fires — stale once we exceed the threshold.
    _, reason = updater_check(
        g, url="https://x.com/home",
        actions_on_active=10, stale_action_threshold=6,
    )
    assert reason.startswith("stale:"), reason
    print("✓ test_updater_stale_threshold")


def test_updater_no_change_when_under_threshold() -> None:
    g = _g123()
    nid, reason = updater_check(
        g, url="https://x.com/home",
        actions_on_active=2, stale_action_threshold=6,
    )
    assert nid == "g1"
    assert reason == "", reason
    print("✓ test_updater_no_change_when_under_threshold")


def test_round_trip_serialization() -> None:
    g = _g123()
    g.advance(None, "test transition")
    g2 = TaskGraph.from_dict(g.to_dict())
    assert g2.active_id == g.active_id
    assert g2.total == g.total
    assert g2.subgoals["g1"].status == "done"
    assert len(g2.history) == len(g.history)
    print("✓ test_round_trip_serialization")


def test_decompose_falls_back_when_no_api_key() -> None:
    # No VISION_API_KEY in test env → should return trivial 1-node graph.
    import os
    saved = os.environ.pop("VISION_API_KEY", None)
    try:
        g = decompose_task("find cheapest hotel in Paris", "https://booking.com")
        assert g.total >= 1
        assert g.current() is not None
    finally:
        if saved is not None:
            os.environ["VISION_API_KEY"] = saved
    print("✓ test_decompose_falls_back_when_no_api_key")


def main() -> int:
    tests = [
        test_trivial_graph_renders_plan,
        test_signal_url_contains_match_and_miss,
        test_signal_url_matches_regex,
        test_signal_element_visible_via_dom_text,
        test_signal_scroll_at_bottom_uses_telemetry,
        test_updater_advances_on_signal,
        test_updater_skips_ahead,
        test_updater_stale_threshold,
        test_updater_no_change_when_under_threshold,
        test_round_trip_serialization,
        test_decompose_falls_back_when_no_api_key,
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
