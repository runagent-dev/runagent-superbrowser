"""Unit tests for stateful form-control bbox injection (v6).

The repro: the vision model omits pre-checked checkboxes/radios/switches from
its bbox list, so the brain has no `V_n` to un-check a site-preselected
default. `_inject_stateful_control_bboxes` uses the DOM scan's selectorEntries
(which carry exact bounds + live checked/selected state) as a deterministic
safety net — injecting a bbox when vision missed the control and refreshing
`is_active` in place when it didn't. `_enrich_bboxes_with_dom_metadata` is
also extended to read native `checked`/`selected` (not just aria-*).

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_stateful_control_inject.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from vision_agent.schemas import BBox
from superbrowser_bridge.session_tools.vision_pipeline import (
    _control_kind,
    _entry_is_active,
    _resolve_control_label,
    _enrich_bboxes_with_dom_metadata,
    _inject_stateful_control_bboxes,
)

# Square 1000x1000 image at dpr=1 makes box_2d values equal pixel values, so
# an entry at bounds (x,y,w,h) round-trips to box_2d [y, x, y+h, x+w].
IW = IH = 1000
DPR = 1.0


def _entry(index, tag, attrs, x, y, w, h, text=""):
    return {
        "index": index,
        "tagName": tag,
        "attributes": dict(attrs),
        "text": text,
        "bounds": {"x": x, "y": y, "width": w, "height": h},
    }


def _bbox(y, x, y1, x1, **kw):
    return BBox(box_2d=[y, x, y1, x1], **kw)


def _resp(bboxes):
    return SimpleNamespace(bboxes=list(bboxes))


def _checkbox_entry(active, index=3, x=100, y=100, w=20, h=20, text=""):
    return _entry(
        index, "input",
        {"type": "checkbox", "checked": "true" if active else "false"},
        x, y, w, h, text=text,
    )


def test_control_kind() -> None:
    assert _control_kind(_checkbox_entry(True)) == "checkbox"
    assert _control_kind(
        _entry(1, "input", {"type": "radio"}, 0, 0, 10, 10)) == "radio"
    assert _control_kind(
        _entry(1, "div", {"role": "switch"}, 0, 0, 10, 10)) == "switch"
    assert _control_kind(
        _entry(1, "div", {"role": "menuitemcheckbox"}, 0, 0, 10, 10)
    ) == "checkbox"
    # Plain text input / non-control → None.
    assert _control_kind(_entry(1, "input", {"type": "text"}, 0, 0, 10, 10)) is None
    assert _control_kind(_entry(1, "a", {}, 0, 0, 10, 10)) is None
    print("✓ test_control_kind")


def test_entry_is_active() -> None:
    assert _entry_is_active({"checked": "true"}) is True
    assert _entry_is_active({"checked": "false"}) is False
    assert _entry_is_active({"selected": "true"}) is True
    assert _entry_is_active({"aria-checked": "true"}) is True
    assert _entry_is_active({"aria-current": "page"}) is True
    assert _entry_is_active({}) is False
    print("✓ test_entry_is_active")


def test_inject_omitted_checkbox() -> None:
    """(a) Vision emitted nothing here → inject 1, is_active from `checked`."""
    resp = _resp([])
    entries = [_checkbox_entry(active=True, index=7)]
    n = _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 1, n
    assert len(resp.bboxes) == 1
    b = resp.bboxes[0]
    assert b.role == "checkbox"
    assert b.is_active is True
    assert b.clickable is True
    assert b.dom_index == 7
    assert b.box_2d == [100, 100, 120, 120], b.box_2d
    print("✓ test_inject_omitted_checkbox")


def test_wide_parent_still_injects_tight() -> None:
    """(b) A WIDE row that only *contains* the input (IoU<0.5) must NOT
    suppress injecting the tight, state-bearing box."""
    wide = _bbox(100, 80, 120, 300, label="Add insurance $9.99", clickable=True)
    resp = _resp([wide])
    entries = [_checkbox_entry(active=True, x=100, y=100, w=20, h=20)]
    n = _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 1, n
    assert len(resp.bboxes) == 2
    injected = resp.bboxes[-1]
    assert injected.is_active is True
    assert injected.box_2d == [100, 100, 120, 120]
    print("✓ test_wide_parent_still_injects_tight")


def test_vision_tight_box_refreshed_not_duplicated() -> None:
    """(c) Vision already emitted a tight box (IoU>=0.5) → 0 injected, its
    is_active is refreshed in place."""
    tight = _bbox(100, 100, 120, 120, label="Add insurance", clickable=True)
    assert tight.is_active is False
    resp = _resp([tight])
    entries = [_checkbox_entry(active=True, index=9)]
    n = _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    assert len(resp.bboxes) == 1, "must not duplicate"
    assert resp.bboxes[0].is_active is True, "refreshed in place"
    assert resp.bboxes[0].dom_index == 9
    assert n == 1
    print("✓ test_vision_tight_box_refreshed_not_duplicated")


def test_idempotent_across_calls() -> None:
    """(d) Running twice on the SAME resp never appends a duplicate."""
    resp = _resp([])
    entries = [_checkbox_entry(active=True)]
    _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    assert len(resp.bboxes) == 1, len(resp.bboxes)
    print("✓ test_idempotent_across_calls")


def test_cache_reuse_flip_updates_state() -> None:
    """(e) After an uncheck the cached resp is reused (dom_hash unchanged);
    re-running with the entry now checked=false must flip is_active."""
    resp = _resp([])
    _inject_stateful_control_bboxes(
        resp, [_checkbox_entry(active=True)], IW, IH, DPR, None)
    assert resp.bboxes[0].is_active is True
    # Simulate the user un-checking: same geometry, checked now false.
    _inject_stateful_control_bboxes(
        resp, [_checkbox_entry(active=False)], IW, IH, DPR, None)
    assert len(resp.bboxes) == 1, "still no duplicate"
    assert resp.bboxes[0].is_active is False, "flip surfaced without new vision"
    print("✓ test_cache_reuse_flip_updates_state")


def test_radio_injected_with_role() -> None:
    """(f) A pre-selected radio injects with role='radio' + active."""
    resp = _resp([])
    entries = [_entry(2, "input", {"type": "radio", "checked": "true"},
                      50, 50, 16, 16)]
    n = _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 1
    assert resp.bboxes[0].role == "radio"
    assert resp.bboxes[0].is_active is True
    print("✓ test_radio_injected_with_role")


def test_env_flag_off_is_noop() -> None:
    """(g) BBOX_STATEFUL_CONTROL_INJECT=0 → byte-identical no-op."""
    import os
    resp = _resp([])
    os.environ["BBOX_STATEFUL_CONTROL_INJECT"] = "0"
    try:
        n = _inject_stateful_control_bboxes(
            resp, [_checkbox_entry(active=True)], IW, IH, DPR, None)
    finally:
        del os.environ["BBOX_STATEFUL_CONTROL_INJECT"]
    assert n == 0 and len(resp.bboxes) == 0
    print("✓ test_env_flag_off_is_noop")


def test_offscreen_and_degenerate_skipped() -> None:
    """(h) Off-image centre or zero-size rect → skipped."""
    resp = _resp([])
    off = _entry(1, "input", {"type": "checkbox", "checked": "true"},
                 5000, 5000, 20, 20)   # centre way off-image
    degenerate = _entry(2, "input", {"type": "checkbox", "checked": "true"},
                        10, 10, 0, 0)   # zero size
    n = _inject_stateful_control_bboxes(
        resp, [off, degenerate], IW, IH, DPR, None)
    assert n == 0 and len(resp.bboxes) == 0, (n, len(resp.bboxes))
    print("✓ test_offscreen_and_degenerate_skipped")


def test_label_from_sibling_label_for_id() -> None:
    """(i) A bare checkbox's label resolves from a <label for=id> sibling."""
    cb = _entry(4, "input", {"type": "checkbox", "checked": "true", "id": "ins"},
                100, 100, 20, 20, text="")
    lbl = _entry(5, "label", {"for": "ins"}, 130, 100, 200, 20,
                 text="Add trip insurance")
    resp = _resp([])
    _inject_stateful_control_bboxes(resp, [cb, lbl], IW, IH, DPR, None)
    injected = next(b for b in resp.bboxes if b.role == "checkbox")
    assert injected.label == "Add trip insurance", injected.label
    print("✓ test_label_from_sibling_label_for_id")


def test_unselected_option_not_injected() -> None:
    """A non-selected <option> is vision's job — don't inject it."""
    resp = _resp([])
    opt = _entry(1, "option", {"selected": "false"}, 10, 10, 100, 20, text="Std")
    n = _inject_stateful_control_bboxes(resp, [opt], IW, IH, DPR, None)
    assert n == 0 and len(resp.bboxes) == 0
    print("✓ test_unselected_option_not_injected")


def test_enrichment_reads_native_checked() -> None:
    """Extended enrichment: a matched entry with checked='true' → is_active."""
    bb = _bbox(100, 100, 120, 120, label="Add insurance", clickable=True)
    resp = _resp([bb])
    entry = _checkbox_entry(active=True, index=3, text="Add insurance")
    touched = _enrich_bboxes_with_dom_metadata(
        resp, [entry], IW, IH, DPR, None)
    assert touched >= 1
    assert resp.bboxes[0].is_active is True, "native checked → is_active"
    assert resp.bboxes[0].dom_index == 3
    print("✓ test_enrichment_reads_native_checked")


def test_inactive_not_injected_by_default() -> None:
    """Active-gate: an un-checked checkbox vision missed is NOT injected — that
    is vision's job. Only pre-checked controls are the DOM safety net."""
    resp = _resp([])
    n = _inject_stateful_control_bboxes(
        resp, [_checkbox_entry(active=False, index=3)], IW, IH, DPR, None)
    assert n == 0 and len(resp.bboxes) == 0, (n, len(resp.bboxes))
    print("✓ test_inactive_not_injected_by_default")


def test_inactive_injected_when_flag_set() -> None:
    """BBOX_STATEFUL_INJECT_INACTIVE=1 restores injecting un-checked controls."""
    import os
    resp = _resp([])
    os.environ["BBOX_STATEFUL_INJECT_INACTIVE"] = "1"
    try:
        n = _inject_stateful_control_bboxes(
            resp, [_checkbox_entry(active=False, index=3)], IW, IH, DPR, None)
    finally:
        del os.environ["BBOX_STATEFUL_INJECT_INACTIVE"]
    assert n == 1 and len(resp.bboxes) == 1
    assert resp.bboxes[0].is_active is False
    print("✓ test_inactive_injected_when_flag_set")


def test_cap_limits_new_injections() -> None:
    """BBOX_STATEFUL_CONTROL_MAX caps the number of NEW injected bboxes."""
    import os
    resp = _resp([])
    entries = [
        _checkbox_entry(active=True, index=i, x=10 + i * 40, y=10, w=20, h=20)
        for i in range(5)
    ]
    os.environ["BBOX_STATEFUL_CONTROL_MAX"] = "2"
    try:
        n = _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    finally:
        del os.environ["BBOX_STATEFUL_CONTROL_MAX"]
    assert n == 2 and len(resp.bboxes) == 2, (n, len(resp.bboxes))
    print("✓ test_cap_limits_new_injections")


def test_cap_prioritizes_task_relevant() -> None:
    """Under a tight cap, a task-keyword-matching control is kept over noise."""
    import os
    resp = _resp([])
    noise = _checkbox_entry(active=True, index=1, x=10, y=10, text="Newsletter")
    target = _checkbox_entry(
        active=True, index=2, x=200, y=10, text="Wheelchair accessible")
    os.environ["BBOX_STATEFUL_CONTROL_MAX"] = "1"
    try:
        _inject_stateful_control_bboxes(
            resp, [noise, target], IW, IH, DPR,
            "Find a wheelchair accessible vehicle")
    finally:
        del os.environ["BBOX_STATEFUL_CONTROL_MAX"]
    assert len(resp.bboxes) == 1
    assert "wheelchair" in resp.bboxes[0].label.lower(), resp.bboxes[0].label
    assert resp.bboxes[0].intent_relevant is True
    print("✓ test_cap_prioritizes_task_relevant")


def test_bare_controls_get_distinct_labels() -> None:
    """Two label-less checkboxes must not collapse to identical 'checkbox'
    labels (which confuses the brain + label-keyed detectors) — they are
    disambiguated by DOM index."""
    resp = _resp([])
    entries = [
        _checkbox_entry(active=True, index=7, x=10, y=10),
        _checkbox_entry(active=True, index=12, x=100, y=10),
    ]
    _inject_stateful_control_bboxes(resp, entries, IW, IH, DPR, None)
    labels = {b.label for b in resp.bboxes}
    assert len(labels) == 2, labels
    assert all("#" in lbl for lbl in labels), labels
    print("✓ test_bare_controls_get_distinct_labels")


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
