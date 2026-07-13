"""Unit tests for the per-epoch scroll anchor + interacted-control registry
(Phase 0.3 / 0.4 / 3a).

The scroll-anchor pins the scrollY a vision response was numbered at, so the
shared resolver can refuse a bbox click after a scroll (box_2d carries no scroll
offset). These tests cover the schema plumbing + the state-side registry TTL/cap
offline; the live scrollY probe is exercised end-to-end by the run harness.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_scroll_anchor.py
"""

from __future__ import annotations

import sys

from vision_agent.schemas import VisionResponse


def test_with_scroll_anchor_stamps_value() -> None:
    r = VisionResponse(bboxes=[])
    assert r.scroll_y == -1, "default is -1 (no anchor → gate skips)"
    r.with_scroll_anchor(340)
    assert r.scroll_y == 340
    # None leaves the prior value untouched.
    r.with_scroll_anchor(None)
    assert r.scroll_y == 340
    # Floats round.
    r.with_scroll_anchor(12.7)
    assert r.scroll_y == 13
    print("✓ test_with_scroll_anchor_stamps_value")


def test_registry_register_prune_cap() -> None:
    """The interacted-control registry keys by dom_index, prunes by TTL turns,
    and caps its size."""
    from superbrowser_bridge.session_tools.state import BrowserSessionState
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    # Register a stateful control (active_state not None is the signature).
    s.register_click_attempt(
        "click_at(V3)", target_active_state=True, target_dom_index=3)
    idxs = s.prune_interacted_controls()
    assert 3 in idxs, idxs

    # A non-stateful click (active_state None) is NOT registered.
    s.register_click_attempt("click_at(V4)", target_active_state=None,
                             target_dom_index=4)
    assert 4 not in s.prune_interacted_controls()

    # TTL: advance beyond INTERACTED_CONTROL_TTL_TURNS → entry expires.
    s._brain_turn_counter += s.INTERACTED_CONTROL_TTL_TURNS + 1
    assert 3 not in s.prune_interacted_controls(), "should have expired"
    print("✓ test_registry_register_prune_cap")


def test_registry_cap() -> None:
    from superbrowser_bridge.session_tools.state import BrowserSessionState
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    for i in range(s.INTERACTED_CONTROL_MAX + 8):
        s._brain_turn_counter = i  # keep them all "live" within TTL window
        s.register_click_attempt(
            f"click_at(V{i})", target_active_state=True, target_dom_index=i)
    assert len(s.recently_interacted_controls) <= s.INTERACTED_CONTROL_MAX
    print("✓ test_registry_cap")


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)}/{len(fns)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
