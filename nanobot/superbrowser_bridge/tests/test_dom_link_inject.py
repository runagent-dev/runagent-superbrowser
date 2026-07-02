"""Unit tests for DOM link/button bbox injection.

The repro: the brain-facing bbox list is Gemini-only, so a link that is
obvious in the DOM (`<a href>`, `<button>`, role=link) but culled by the
vision model's conservatism / bbox caps simply does not exist for the
brain — it misses "obvious" links a human sees instantly.
`_inject_dom_link_bboxes` generalizes the proven stateful-control
injection: DOM selectorEntries with no matching vision bbox (IoU + label
dedup) are injected as low-confidence `src=dom` boxes, ranked by task
relevance so nav chrome can't flood the list.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_dom_link_inject.py
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from vision_agent.schemas import BBox, VisionResponse
from superbrowser_bridge.session_tools.vision_pipeline import (
    _inject_dom_link_bboxes,
    _link_kind,
    _link_label,
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


def _link_entry(text="Season tickets", index=5, x=200, y=400, w=140, h=24,
                tag="a", attrs=None):
    return _entry(index, tag, attrs or {}, x, y, w, h, text=text)


def _bbox(y, x, y1, x1, **kw):
    return BBox(box_2d=[y, x, y1, x1], **kw)


def _resp(bboxes):
    return SimpleNamespace(bboxes=list(bboxes))


def test_link_kind() -> None:
    assert _link_kind(_entry(1, "a", {}, 0, 0, 10, 10)) == "link"
    assert _link_kind(_entry(1, "button", {}, 0, 0, 10, 10)) == "button"
    assert _link_kind(_entry(1, "div", {"role": "link"}, 0, 0, 10, 10)) == "link"
    assert _link_kind(_entry(1, "div", {"role": "button"}, 0, 0, 10, 10)) == "button"
    # tab/menuitem map to 'button' — canonical BBox roles would coerce
    # them to non-actionable 'other' otherwise.
    assert _link_kind(_entry(1, "div", {"role": "tab"}, 0, 0, 10, 10)) == "button"
    assert _link_kind(_entry(1, "li", {"role": "menuitem"}, 0, 0, 10, 10)) == "button"
    # Non-links → None.
    assert _link_kind(_entry(1, "input", {"type": "checkbox"}, 0, 0, 10, 10)) is None
    assert _link_kind(_entry(1, "div", {}, 0, 0, 10, 10)) is None
    print("✓ test_link_kind")


def test_link_label() -> None:
    assert _link_label(_link_entry(text="Deals")) == "Deals"
    assert _link_label(
        _entry(1, "a", {"aria-label": "Open cart"}, 0, 0, 10, 10)) == "Open cart"
    assert _link_label(
        _entry(1, "a", {"title": "Homepage"}, 0, 0, 10, 10)) == "Homepage"
    # No text, no aria-label/title/value → empty → candidate is skipped.
    assert _link_label(_entry(1, "a", {}, 0, 0, 10, 10)) == ""
    print("✓ test_link_label")


def test_inject_omitted_link() -> None:
    """Vision emitted nothing for the link → inject with DOM-exact box."""
    resp = _resp([])
    entries = [_link_entry(text="Season tickets", index=5, x=200, y=400, w=140, h=24)]
    n = _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 1, n
    b = resp.bboxes[0]
    assert b.role == "link"
    assert b.clickable is True
    assert b.label == "Season tickets"
    assert b.dom_index == 5
    assert b.source == "dom"
    assert abs(b.confidence - 0.55) < 1e-9
    assert b.box_2d == [400, 200, 424, 340], b.box_2d
    print("✓ test_inject_omitted_link")


def test_iou_dedup_suppresses_duplicate() -> None:
    """Vision already boxed the link tightly → no injection."""
    seen = _bbox(400, 200, 424, 340, label="Season tickets", clickable=True, role="link")
    resp = _resp([seen])
    entries = [_link_entry(text="Season tickets", x=200, y=400, w=140, h=24)]
    n = _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 0, n
    assert len(resp.bboxes) == 1
    print("✓ test_iou_dedup_suppresses_duplicate")


def test_label_dedup_catches_loose_geometry() -> None:
    """Vision boxed the same link with LOOSE geometry (padded nav item →
    IoU < 0.4). The label arm must still suppress the double-inject."""
    loose = _bbox(380, 150, 460, 480, label="Season Tickets", clickable=True, role="link")
    resp = _resp([loose])
    entries = [_link_entry(text="season  tickets", x=200, y=400, w=140, h=24)]
    n = _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 0, n
    print("✓ test_label_dedup_catches_loose_geometry")


def test_idempotent_on_cached_response() -> None:
    """Cached VisionResponse objects are aliased across steps — running
    the injector twice must not append duplicates."""
    resp = _resp([])
    entries = [_link_entry(text="Deals", index=2)]
    assert _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None) == 1
    assert _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None) == 0
    assert len(resp.bboxes) == 1
    print("✓ test_idempotent_on_cached_response")


def test_skips_unlabeled_disabled_offscreen_tiny_and_huge() -> None:
    resp = _resp([])
    entries = [
        # No label at all → skip.
        _entry(1, "a", {}, 10, 10, 100, 20),
        # Disabled → skip.
        _entry(2, "button", {"disabled": ""}, 10, 40, 100, 20, text="Buy"),
        _entry(3, "a", {"aria-disabled": "true"}, 10, 70, 100, 20, text="Buy 2"),
        # Centre off-image → skip.
        _link_entry(text="Below fold", index=4, x=10, y=1500, w=100, h=20),
        # Tiny (sub-4px) → skip.
        _entry(5, "a", {}, 10, 100, 2, 2, text="dot"),
        # Card-container anchor spanning >35% of the viewport → skip.
        _entry(6, "a", {}, 0, 0, 700, 600, text="Whole card"),
    ]
    n = _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 0, [b.label for b in resp.bboxes]
    print("✓ test_skips_unlabeled_disabled_offscreen_tiny_and_huge")


def test_cap_and_task_relevant_ranking() -> None:
    """With more candidates than the cap, task-relevant labels survive and
    header/footer chrome sinks."""
    resp = _resp([])
    entries = []
    # 12 generic mid-page links…
    for i in range(12):
        entries.append(_link_entry(text=f"Generic {i}", index=i, x=100, y=200 + i * 30))
    # …one task-relevant link…
    entries.append(_link_entry(text="Wheelchair accessible seating", index=90, x=100, y=700))
    # …and one header-chrome link (top 10% band).
    entries.append(_link_entry(text="About us", index=91, x=100, y=20))
    os.environ["BBOX_DOM_LINK_MAX"] = "3"
    try:
        n = _inject_dom_link_bboxes(
            resp, entries, IW, IH, DPR, "Find wheelchair accessible seating")
    finally:
        del os.environ["BBOX_DOM_LINK_MAX"]
    assert n == 3, n
    labels = [b.label for b in resp.bboxes]
    assert labels[0] == "Wheelchair accessible seating", labels
    assert resp.bboxes[0].intent_relevant is True
    # Chrome-band link must not have displaced mid-page candidates.
    assert "About us" not in labels, labels
    print("✓ test_cap_and_task_relevant_ranking")


def test_kill_switch() -> None:
    resp = _resp([])
    entries = [_link_entry()]
    os.environ["BBOX_DOM_LINK_INJECT"] = "0"
    try:
        n = _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None)
    finally:
        del os.environ["BBOX_DOM_LINK_INJECT"]
    assert n == 0
    assert resp.bboxes == []
    print("✓ test_kill_switch")


def test_brain_text_renders_src_dom() -> None:
    """End-to-end through the real VisionResponse renderer: injected
    boxes appear as [V_n] lines with a src=dom provenance extra."""
    resp = VisionResponse(bboxes=[], summary="ticket page")
    resp.with_image_dims(IW, IH, dpr=DPR)
    n = _inject_dom_link_bboxes(
        resp, [_link_entry(text="Season tickets")], IW, IH, DPR, None)
    assert n == 1
    text = resp.as_brain_text()
    assert "'Season tickets'" in text, text
    assert "src=dom" in text, text
    print("✓ test_brain_text_renders_src_dom")


def test_intra_pass_dedup() -> None:
    """Two entries with identical labels / overlapping rects must not both
    land in one pass."""
    resp = _resp([])
    entries = [
        _link_entry(text="Deals", index=1, x=100, y=100),
        _link_entry(text="Deals", index=2, x=600, y=600),          # same label
        _link_entry(text="Hot deals", index=3, x=102, y=102),      # overlaps #1
    ]
    n = _inject_dom_link_bboxes(resp, entries, IW, IH, DPR, None)
    assert n == 1, [b.label for b in resp.bboxes]
    print("✓ test_intra_pass_dedup")


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
