"""Tests for the action-delta system.

The brain's biggest blind spot was: "what did my last tool call actually
do to the page?" The action-delta system captures a small page-state
snapshot at the top of every mutating tool, computes a diff against the
post-action state, and renders a one-line `[ACTION_DELTA]` block telling
the brain exactly what changed plus a one-line interpretation hint.

These tests cover the pure helpers — capture, compute, render, interpret
— without exercising live HTTP. The integration with each tool is
verified by smoke-importing the modules in the parent test pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from superbrowser_bridge.session_tools.state import (
    BrowserSessionState,
    PageStateSnapshot,
)


# ----------------------------- helpers ----------------------------------


def _state_with_elements(
    indices: list[int],
    url: str = "https://x.com/p",
    elements_text: str = "",
) -> BrowserSessionState:
    """Build a state with a known fingerprint map + URL."""
    s = BrowserSessionState()
    s.current_url = url
    s.element_fingerprints = {i: f"fp{i}" for i in indices}
    return s


# ------------------------------ capture ---------------------------------


def test_capture_action_snapshot_records_current_state():
    s = _state_with_elements([1, 2, 3], url="https://x.com/")
    s._brain_turn_counter = 5
    snap = s.capture_action_snapshot(target_index=2)
    assert snap.url == "https://x.com/"
    assert snap.elem_count == 3
    assert snap.fingerprint_keys == frozenset({1, 2, 3})
    assert snap.target_index == 2
    assert snap.target_fingerprint == "fp2"
    assert snap.captured_at_turn == 5
    # Should also be stored on state.
    assert s.action_snapshot_pre is snap


def test_capture_clears_extras():
    s = _state_with_elements([1])
    s.action_snapshot_extras = {"stale": "data"}
    s.capture_action_snapshot(target_index=None)
    assert s.action_snapshot_extras == {}


def test_capture_with_no_target_records_none():
    s = _state_with_elements([1, 2])
    snap = s.capture_action_snapshot(target_index=None)
    assert snap.target_index is None
    assert snap.target_fingerprint == ""


# ------------------------------ compute ---------------------------------


def test_compute_delta_url_change():
    s = BrowserSessionState()
    pre = PageStateSnapshot(url="https://x.com/a", elem_count=10)
    post = PageStateSnapshot(url="https://x.com/b", elem_count=10)
    d = s.compute_action_delta(pre, post)
    assert d["url_changed"] is True
    assert d["url_to"] == "https://x.com/b"


def test_compute_delta_elements_added():
    s = BrowserSessionState()
    pre = PageStateSnapshot(elem_count=5, fingerprint_keys=frozenset({1, 2, 3, 4, 5}))
    post = PageStateSnapshot(elem_count=8, fingerprint_keys=frozenset({1, 2, 3, 4, 5, 6, 7, 8}))
    d = s.compute_action_delta(pre, post)
    assert d["elem_delta"] == 3
    assert d["added_indices"] == [6, 7, 8]
    assert d["removed_indices"] == []


def test_compute_delta_elements_removed():
    s = BrowserSessionState()
    pre = PageStateSnapshot(elem_count=5, fingerprint_keys=frozenset({1, 2, 3, 4, 5}))
    post = PageStateSnapshot(elem_count=2, fingerprint_keys=frozenset({1, 2}))
    d = s.compute_action_delta(pre, post)
    assert d["elem_delta"] == -3
    assert d["removed_indices"] == [3, 4, 5]


def test_compute_delta_target_disappeared():
    s = BrowserSessionState()
    pre = PageStateSnapshot(
        elem_count=3,
        fingerprint_keys=frozenset({1, 2, 3}),
        target_index=2,
        target_fingerprint="fp2",
    )
    post = PageStateSnapshot(
        elem_count=2,
        fingerprint_keys=frozenset({1, 3}),
    )
    d = s.compute_action_delta(pre, post)
    assert d["target_disappeared"] is True


def test_compute_delta_target_changed_text():
    s = BrowserSessionState()
    s.element_fingerprints = {2: "newfp"}
    pre = PageStateSnapshot(
        elem_count=3,
        fingerprint_keys=frozenset({1, 2, 3}),
        target_index=2,
        target_fingerprint="oldfp",
    )
    post = PageStateSnapshot(
        elem_count=3,
        fingerprint_keys=frozenset({1, 2, 3}),
    )
    d = s.compute_action_delta(pre, post)
    assert d["target_disappeared"] is False
    assert d["target_changed"] is True


def test_compute_delta_no_change_returns_all_falsy():
    s = BrowserSessionState()
    pre = PageStateSnapshot(
        url="https://x.com/", elem_count=5,
        fingerprint_keys=frozenset({1, 2, 3, 4, 5}),
        dom_hash="abc",
    )
    post = PageStateSnapshot(
        url="https://x.com/", elem_count=5,
        fingerprint_keys=frozenset({1, 2, 3, 4, 5}),
        dom_hash="abc",
    )
    d = s.compute_action_delta(pre, post)
    assert d["url_changed"] is False
    assert d["dom_changed"] is False
    assert d["elem_delta"] == 0
    assert d["target_disappeared"] is False
    assert d["target_changed"] is False


# ----------------------------- interpret --------------------------------


def test_interpret_url_change_says_navigated():
    s = BrowserSessionState()
    delta = {
        "url_changed": True, "url_from": "x", "url_to": "y",
        "title_changed": False, "elem_delta": 0,
        "target_disappeared": False, "target_changed": False,
        "dom_changed": False, "vision_delta": 0,
        "added_indices": [], "added_label_samples": [],
        "target_index": None,
    }
    hint = s._interpret_delta("browser_navigate", delta, {})
    assert "navigated" in hint.lower()
    assert "screenshot" in hint.lower()


def test_interpret_accordion_expand():
    s = BrowserSessionState()
    delta = {
        "url_changed": False, "url_from": "x", "url_to": "x",
        "title_changed": False, "elem_delta": 23,
        "target_disappeared": False, "target_changed": False,
        "dom_changed": True, "vision_delta": 0,
        "added_indices": list(range(10)), "added_label_samples": [],
        "target_index": 47,
    }
    hint = s._interpret_delta("browser_click", delta, {})
    assert "expand" in hint.lower() or "dropdown" in hint.lower()


def test_interpret_no_op_click_says_so():
    s = BrowserSessionState()
    delta = {
        "url_changed": False, "url_from": "x", "url_to": "x",
        "title_changed": False, "elem_delta": 0,
        "target_disappeared": False, "target_changed": False,
        "dom_changed": False, "vision_delta": 0,
        "added_indices": [], "added_label_samples": [],
        "target_index": 47,
    }
    hint = s._interpret_delta("browser_click", delta, {})
    assert "no-op" in hint.lower()
    assert "did not" in hint.lower() or "did NOT" in hint


def test_interpret_type_uses_extras_after():
    s = BrowserSessionState()
    delta = {
        "url_changed": False, "url_from": "x", "url_to": "x",
        "title_changed": False, "elem_delta": 0,
        "target_disappeared": False, "target_changed": False,
        "dom_changed": False, "vision_delta": 0,
        "added_indices": [], "added_label_samples": [],
        "target_index": 33,
    }
    extras = {"before": "", "after": "40", "changed": True}
    hint = s._interpret_delta("browser_type", delta, extras)
    assert "field updated to '40'" in hint


def test_interpret_type_no_change_warns():
    s = BrowserSessionState()
    delta = {
        "url_changed": False, "url_from": "x", "url_to": "x",
        "title_changed": False, "elem_delta": 0,
        "target_disappeared": False, "target_changed": False,
        "dom_changed": False, "vision_delta": 0,
        "added_indices": [], "added_label_samples": [],
        "target_index": 33,
    }
    extras = {"before": "40", "after": "40", "changed": False}
    hint = s._interpret_delta("browser_type", delta, extras)
    assert "do NOT retype" in hint or "advance to the next" in hint


def test_interpret_target_disappeared():
    s = BrowserSessionState()
    delta = {
        "url_changed": False, "url_from": "x", "url_to": "x",
        "title_changed": False, "elem_delta": -1,
        "target_disappeared": True, "target_changed": False,
        "dom_changed": True, "vision_delta": 0,
        "added_indices": [], "added_label_samples": [],
        "target_index": 47,
    }
    hint = s._interpret_delta("browser_click", delta, {})
    assert "consumed" in hint.lower() or "gone" in hint.lower()


# ------------------------------ render ----------------------------------


def test_render_action_delta_contains_marker():
    s = BrowserSessionState()
    pre = PageStateSnapshot(url="x", elem_count=5, fingerprint_keys=frozenset({1, 2, 3, 4, 5}))
    post = PageStateSnapshot(url="x", elem_count=8, fingerprint_keys=frozenset({1, 2, 3, 4, 5, 6, 7, 8}))
    d = s.compute_action_delta(pre, post)
    block = s.render_action_delta("browser_click", "index=47", d)
    assert "[ACTION_DELTA]" in block
    assert "browser_click(index=47)" in block
    assert "+3" in block


def test_render_action_delta_emits_noop_warning_when_nothing_changed():
    """Same elements, same URL → emits the no-op hint so the brain knows
    its action didn't move anything. We deliberately surface this — a
    silent return would let the brain assume "click worked" when the
    page is unchanged."""
    s = BrowserSessionState()
    pre = PageStateSnapshot(elem_count=5, fingerprint_keys=frozenset({1, 2, 3, 4, 5}))
    post = PageStateSnapshot(elem_count=5, fingerprint_keys=frozenset({1, 2, 3, 4, 5}))
    d = s.compute_action_delta(pre, post)
    block = s.render_action_delta("browser_keys", "Tab", d)
    assert "[ACTION_DELTA]" in block
    assert "no-op" in block.lower()


def test_render_action_delta_includes_url_diff():
    s = BrowserSessionState()
    pre = PageStateSnapshot(url="https://x.com/a", elem_count=3, fingerprint_keys=frozenset({1, 2, 3}))
    post = PageStateSnapshot(url="https://x.com/b", elem_count=3, fingerprint_keys=frozenset({1, 2, 3}))
    d = s.compute_action_delta(pre, post)
    block = s.render_action_delta("browser_navigate", "url=…", d)
    assert "URL:" in block
    assert "navigated" in block.lower()


def test_render_action_delta_includes_target_disappeared():
    s = BrowserSessionState()
    pre = PageStateSnapshot(
        elem_count=3, fingerprint_keys=frozenset({1, 2, 3}),
        target_index=2, target_fingerprint="fp2",
    )
    post = PageStateSnapshot(
        elem_count=2, fingerprint_keys=frozenset({1, 3}),
    )
    d = s.compute_action_delta(pre, post)
    block = s.render_action_delta("browser_click", "index=2", d)
    assert "target [2] disappeared" in block


# --------------------------- skip-list -----------------------------------


def test_skip_list_includes_read_only_tools():
    skip = BrowserSessionState._ACTION_DELTA_SKIP_TOOLS
    assert "browser_screenshot" in skip
    assert "browser_get_markdown" in skip
    assert "browser_brief_mark" in skip


# --------------------------- emit (e2e) ----------------------------------


def test_emit_action_delta_returns_empty_when_no_pre():
    s = BrowserSessionState()
    s.action_snapshot_pre = None
    assert s._emit_action_delta({}) == ""


def test_emit_action_delta_returns_empty_for_skipped_tool():
    s = BrowserSessionState()
    s._brain_turn_counter = 5
    s.capture_action_snapshot(target_index=None)
    s.record_step("browser_screenshot", "intent=foo", "ok")
    out = s._emit_action_delta({"url": "https://x.com/", "elements": ""})
    assert out == ""
    # Still clears the snapshot so the next tool starts fresh.
    assert s.action_snapshot_pre is None


def test_emit_action_delta_drops_stale_snapshot():
    s = BrowserSessionState()
    s._brain_turn_counter = 10
    # Snapshot from many turns ago — was likely captured by a refused
    # tool and never cleared.
    s.action_snapshot_pre = PageStateSnapshot(
        captured_at_turn=2, url="x", elem_count=1,
    )
    s.record_step("browser_click", "index=1", "ok")
    out = s._emit_action_delta({"url": "x"})
    assert out == ""
    assert s.action_snapshot_pre is None
