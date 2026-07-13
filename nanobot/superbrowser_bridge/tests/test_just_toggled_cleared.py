"""Unit test for stale `just_toggled` clearing on cache reuse (Phase 4.4).

The vision cache returns a shallow copy that shares the SAME bbox objects, so a
`just_toggled` marker stamped two actions ago would re-surface on a cache-hit
and re-fire worker_hook's filter-toggle "re-click to undo" hint on a page where
nothing was toggled this turn. `_apply_just_toggled_marker` now clears every
`just_toggled` FIRST — before any early return — so a reused response can't
carry a stale marker.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_just_toggled_cleared.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from vision_agent.schemas import BBox
from superbrowser_bridge.session_tools.vision_pipeline import (
    _apply_just_toggled_marker,
)


def _resp(bboxes):
    return SimpleNamespace(bboxes=list(bboxes))


def _state(label="", box=None, active=None):
    return SimpleNamespace(
        last_click_target_label=label,
        last_click_target_box_2d=box,
        last_click_target_active_state=active,
    )


def test_stale_marker_cleared_when_no_recorded_click() -> None:
    """A reused bbox carrying just_toggled='off' from a prior pass must be
    cleared when there's no recorded click this turn (prior_active is None)."""
    b = BBox(box_2d=[100, 100, 120, 120], label="Samsung", clickable=True)
    b.just_toggled = "off"
    resp = _resp([b])
    _apply_just_toggled_marker(resp, _state())  # no recorded click
    assert resp.bboxes[0].just_toggled is None, "stale marker not cleared"
    print("✓ test_stale_marker_cleared_when_no_recorded_click")


def test_marker_restamped_on_real_flip() -> None:
    """A genuine flip (recorded active=True, now false) re-stamps 'off' AFTER
    the initial clear."""
    b = BBox(box_2d=[100, 100, 120, 120], label="Samsung", clickable=True)
    b.is_active = False           # now unchecked
    b.just_toggled = "on"         # stale from before
    resp = _resp([b])
    _apply_just_toggled_marker(
        resp, _state(label="Samsung", box=[100, 100, 120, 120], active=True))
    assert resp.bboxes[0].just_toggled == "off", resp.bboxes[0].just_toggled
    print("✓ test_marker_restamped_on_real_flip")


def test_no_flip_leaves_cleared() -> None:
    """Recorded active matches current → no flip, marker stays cleared."""
    b = BBox(box_2d=[100, 100, 120, 120], label="Samsung", clickable=True)
    b.is_active = True
    b.just_toggled = "off"        # stale
    resp = _resp([b])
    _apply_just_toggled_marker(
        resp, _state(label="Samsung", box=[100, 100, 120, 120], active=True))
    assert resp.bboxes[0].just_toggled is None
    print("✓ test_no_flip_leaves_cleared")


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)}/{len(fns)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
