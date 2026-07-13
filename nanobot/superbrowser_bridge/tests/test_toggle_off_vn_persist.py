"""Unit tests for uncheck-persistence + indeterminate state (Phase 3a / 3b).

The bug: after the brain toggles a control OFF, a fresh vision pass omits it
(inactive) and the plain injection skips it (only ACTIVE controls are injected),
so its clickable `V_n` vanishes and SOUL's "confirm active=false / re-check"
loop breaks. Fix: `_inject_stateful_control_bboxes` also injects an inactive
control when its dom_index is in `interacted_dom_indices` (a control the brain
touched this session), widened to same-`name` radio siblings. Plus: tri-state
controls now surface `is_mixed` (active=mixed).

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_toggle_off_vn_persist.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from vision_agent.schemas import BBox
from superbrowser_bridge.session_tools.vision_pipeline import (
    _entry_is_mixed,
    _inject_stateful_control_bboxes,
)

IW = IH = 1000
DPR = 1.0


def _entry(index, tag, attrs, x, y, w, h, text=""):
    return {
        "index": index, "tagName": tag, "attributes": dict(attrs),
        "text": text, "bounds": {"x": x, "y": y, "width": w, "height": h},
    }


def _resp(bboxes):
    return SimpleNamespace(bboxes=list(bboxes))


def _checkbox(active, index, x=100, y=100, w=20, h=20, **attrs):
    a = {"type": "checkbox", "checked": "true" if active else "false"}
    a.update(attrs)
    return _entry(index, "input", a, x, y, w, h)


def test_inactive_not_injected_by_default() -> None:
    """Baseline: an inactive checkbox vision omitted is NOT injected."""
    resp = _resp([])
    n = _inject_stateful_control_bboxes(
        resp, [_checkbox(active=False, index=5)], IW, IH, DPR, None)
    assert n == 0 and len(resp.bboxes) == 0, (n, resp.bboxes)
    print("✓ test_inactive_not_injected_by_default")


def test_interacted_inactive_is_injected() -> None:
    """A just-toggled-OFF control (dom_index in the interacted set) DOES get a
    clickable V_n even though it's inactive."""
    resp = _resp([])
    n = _inject_stateful_control_bboxes(
        resp, [_checkbox(active=False, index=5)], IW, IH, DPR, None,
        interacted_dom_indices={5},
    )
    assert n == 1 and len(resp.bboxes) == 1, (n, resp.bboxes)
    b = resp.bboxes[0]
    assert b.is_active is False and b.clickable is True and b.dom_index == 5
    print("✓ test_interacted_inactive_is_injected")


def test_radio_group_widening_by_name() -> None:
    """Toggling one radio keeps its inactive same-name siblings clickable too —
    so 'deselect a radio by picking a sibling' has a V_n to click."""
    resp = _resp([])
    entries = [
        _checkbox(active=False, index=1, x=100, y=100, name="color"),  # touched
        _checkbox(active=False, index=2, x=100, y=140, name="color"),  # sibling
        _checkbox(active=False, index=3, x=100, y=180, name="size"),   # other grp
    ]
    # Make them radios.
    for e in entries:
        e["attributes"]["type"] = "radio"
    _inject_stateful_control_bboxes(
        resp, entries, IW, IH, DPR, None, interacted_dom_indices={1})
    injected_idx = {b.dom_index for b in resp.bboxes}
    assert 1 in injected_idx and 2 in injected_idx, injected_idx
    assert 3 not in injected_idx, "different-name radio must NOT be widened in"
    print("✓ test_radio_group_widening_by_name")


def test_entry_is_mixed() -> None:
    assert _entry_is_mixed({"indeterminate": "true"}) is True
    assert _entry_is_mixed({"aria-checked": "mixed"}) is True
    assert _entry_is_mixed({"checked": "true"}) is False
    assert _entry_is_mixed({}) is False
    print("✓ test_entry_is_mixed")


def test_mixed_flag_on_injected_and_render() -> None:
    resp = _resp([])
    e = _checkbox(active=False, index=8)
    e["attributes"]["indeterminate"] = "true"
    _inject_stateful_control_bboxes(
        resp, [e], IW, IH, DPR, None, interacted_dom_indices={8})
    assert resp.bboxes and resp.bboxes[0].is_mixed is True
    # Brain text renders active=mixed (not active=true).
    from vision_agent.schemas import VisionResponse
    vr = VisionResponse(bboxes=[resp.bboxes[0]])
    txt = vr.as_brain_text()
    assert "active=mixed" in txt, txt
    print("✓ test_mixed_flag_on_injected_and_render")


def test_refresh_in_place_sets_mixed() -> None:
    """A vision-emitted box gets is_mixed refreshed in place from the entry."""
    b = BBox(box_2d=[100, 100, 120, 120], label="Select all", clickable=True)
    resp = _resp([b])
    e = _checkbox(active=False, index=4)
    e["attributes"]["aria-checked"] = "mixed"
    _inject_stateful_control_bboxes(resp, [e], IW, IH, DPR, None)
    assert resp.bboxes[0].is_mixed is True
    print("✓ test_refresh_in_place_sets_mixed")


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)}/{len(fns)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
