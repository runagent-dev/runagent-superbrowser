"""Unit tests for perception_fusion — the DOM ↔ vision merge layer.

No external services required. Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_perception_fusion.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from superbrowser_bridge.perception_fusion import (
    FusedPerception,
    Rect,
    build_fused_perception,
    detect_active_blocker,
)


def _mk_vision_bbox(
    *, label: str, box: tuple[int, int, int, int],
    role: str = "button", confidence: float = 0.9,
    layer_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        label=label,
        box_2d=list(box),
        role=role,
        confidence=confidence,
        clickable=True,
        intent_relevant=True,
        role_in_scene="target",
        layer_id=layer_id,
    )


def _mk_dom_entry(
    *, index: int, text: str, x: int, y: int, w: int, h: int,
    vw: int = 1000, vh: int = 1000, role: str = "button",
    tag: str = "button", xpath: str | None = None,
    region_tag: str = "main",
) -> dict:
    return {
        "index": index,
        "xpath": xpath or f"/html/body/button[{index}]",
        "tagName": tag,
        "attributes": {"role": role},
        "text": text,
        "bounds": {
            "x": x, "y": y, "width": w, "height": h, "vw": vw, "vh": vh,
        },
        "regionTag": region_tag,
    }


def test_rect_iou_basics() -> None:
    a = Rect(ymin=0, xmin=0, ymax=100, xmax=100)
    b = Rect(ymin=50, xmin=50, ymax=150, xmax=150)
    iou = a.iou(b)
    # Intersection 50x50=2500, union 100*100*2 - 2500 = 17500.
    assert abs(iou - 2500 / 17500) < 1e-4, iou
    # Disjoint
    c = Rect(ymin=500, xmin=500, ymax=600, xmax=600)
    assert a.iou(c) == 0.0
    # Empty rect
    assert Rect().iou(a) == 0.0
    print("✓ test_rect_iou_basics")


def test_fuses_vision_with_matching_dom() -> None:
    # Vision sees a Submit button somewhere in the top-right.
    vbbox = _mk_vision_bbox(
        label="Submit", box=(50, 800, 100, 950),  # ymin, xmin, ymax, xmax (0-1000)
    )
    # DOM has the same button at viewport 800,50 → 950,100 with vw=1000,vh=1000.
    dom = _mk_dom_entry(
        index=3, text="Submit", x=800, y=50, w=150, h=50,
    )
    fused = FusedPerception.build(
        vision_bboxes=[vbbox], dom_entries=[dom], observation_token=7,
    )
    assert fused.fused_count == 1, fused
    elem = fused.elements[0]
    assert elem.source == "fused", elem.source
    assert elem.label == "Submit"
    assert elem.xpath is not None
    assert elem.dom_index == 3
    print("✓ test_fuses_vision_with_matching_dom")


def test_orphan_vision_kept_as_vision_source() -> None:
    vbbox = _mk_vision_bbox(label="Cancel", box=(50, 50, 100, 200))
    # DOM entries miss the element vision saw — still vision-only.
    dom = _mk_dom_entry(index=1, text="Login", x=10, y=500, w=80, h=30)
    fused = FusedPerception.build(
        vision_bboxes=[vbbox], dom_entries=[dom], observation_token=1,
    )
    assert fused.vision_only_count == 1
    elem = [e for e in fused.elements if e.source == "vision"][0]
    assert elem.label == "Cancel"
    assert elem.xpath is None
    print("✓ test_orphan_vision_kept_as_vision_source")


def test_dom_orphan_recovers_on_intent_match() -> None:
    # Vision emits one bbox, but the subgoal needs "Export" which vision
    # culled (sidebar/toolbar). DOM has it — should recover via intent.
    vbbox = _mk_vision_bbox(label="Main CTA", box=(400, 400, 500, 600))
    export_entry = _mk_dom_entry(
        index=9, text="Export data", x=900, y=50, w=80, h=28,
        region_tag="toolbar",
    )
    other_entry = _mk_dom_entry(
        index=2, text="Settings", x=50, y=50, w=80, h=28,
    )
    fused = FusedPerception.build(
        vision_bboxes=[vbbox], dom_entries=[export_entry, other_entry],
        observation_token=3,
        intent_labels=["Export data"],  # active subgoal's precondition
    )
    # Vision orphan kept + Export recovered from DOM = 2 elements.
    sources = sorted(e.source for e in fused.elements)
    assert sources == ["dom", "vision"], sources
    dom_elem = [e for e in fused.elements if e.source == "dom"][0]
    assert dom_elem.label == "Export data"
    assert dom_elem.xpath == "/html/body/button[9]"
    # Second DOM entry ("Settings") has no intent overlap — not surfaced.
    assert all(e.label != "Settings" for e in fused.elements)
    print("✓ test_dom_orphan_recovers_on_intent_match")


def test_resolve_by_label_picks_best_match() -> None:
    v1 = _mk_vision_bbox(label="Cancel subscription", box=(10, 10, 60, 200))
    v2 = _mk_vision_bbox(label="Submit form", box=(100, 10, 150, 200))
    fused = FusedPerception.build(
        vision_bboxes=[v1, v2], dom_entries=[], observation_token=1,
    )
    elem = fused.resolve_by_label("Submit")
    assert elem is not None and elem.label == "Submit form", elem
    elem_no = fused.resolve_by_label("Settings menu")
    assert elem_no is None
    print("✓ test_resolve_by_label_picks_best_match")


def test_resolve_by_bbox_index_preserves_vision_order() -> None:
    v1 = _mk_vision_bbox(label="First", box=(0, 0, 50, 100))
    v2 = _mk_vision_bbox(label="Second", box=(60, 0, 110, 100))
    v3 = _mk_vision_bbox(label="Third", box=(120, 0, 170, 100))
    fused = FusedPerception.build(
        vision_bboxes=[v1, v2, v3], dom_entries=[], observation_token=1,
    )
    assert fused.resolve_by_bbox_index(1).label == "First"
    assert fused.resolve_by_bbox_index(2).label == "Second"
    assert fused.resolve_by_bbox_index(3).label == "Third"
    assert fused.resolve_by_bbox_index(4) is None
    print("✓ test_resolve_by_bbox_index_preserves_vision_order")


def test_build_fused_perception_wrapper() -> None:
    vr = SimpleNamespace(bboxes=[
        _mk_vision_bbox(label="A", box=(0, 0, 50, 50)),
    ])
    fp = build_fused_perception(
        vision_response=vr,
        dom_entries=[],
        observation_token=5,
    )
    assert fp.observation_token == 5
    assert len(fp.elements) == 1
    print("✓ test_build_fused_perception_wrapper")


def test_dom_without_vw_vh_does_not_crash() -> None:
    # Older TS payloads omitted vw/vh — fusion should skip geometric
    # match but still surface via pure-DOM fallback when intent hits.
    entry = {
        "index": 1,
        "xpath": "/html/body/button[1]",
        "tagName": "button",
        "attributes": {},
        "text": "Export data",
        "bounds": {"x": 10, "y": 10, "width": 100, "height": 30},
    }
    fp = FusedPerception.build(
        vision_bboxes=[], dom_entries=[entry], observation_token=1,
        intent_labels=["Export data"],
    )
    assert len(fp.elements) == 1 and fp.elements[0].source == "dom"
    print("✓ test_dom_without_vw_vh_does_not_crash")


def _mk_vresp(
    *, bboxes=(), scene=None, flags=None, page_type="other",
) -> SimpleNamespace:
    return SimpleNamespace(
        bboxes=list(bboxes),
        scene=scene,
        flags=flags if flags is not None else SimpleNamespace(
            modal_open=False, login_wall=False, error_banner=None,
            captcha_present=False,
        ),
        page_type=page_type,
    )


def test_detect_blocker_via_scene_layer() -> None:
    layer = SimpleNamespace(id="L0_modal", dismiss_hint="Accept all")
    scene = SimpleNamespace(active_blocker_layer_id="L0_modal", layers=[layer])
    vr = _mk_vresp(
        bboxes=[_mk_vision_bbox(label="Accept all", box=(10, 10, 60, 200))],
        scene=scene,
    )
    info = detect_active_blocker(vr)
    assert info is not None
    assert info.source == "scene"
    assert info.dismiss_hint == "Accept all"
    assert info.layer_id == "L0_modal"
    print("✓ test_detect_blocker_via_scene_layer")


def test_detect_blocker_via_flags_modal_open() -> None:
    flags = SimpleNamespace(
        modal_open=True, login_wall=False, error_banner=None,
        captcha_present=False,
    )
    vr = _mk_vresp(
        bboxes=[
            _mk_vision_bbox(label="Close", box=(10, 10, 60, 200)),
            _mk_vision_bbox(label="Subscribe", box=(70, 10, 120, 200)),
        ],
        flags=flags,
    )
    info = detect_active_blocker(vr)
    assert info is not None
    assert info.source == "flags"
    assert info.dismiss_hint == "Close"
    assert "modal_open" in info.reason
    print("✓ test_detect_blocker_via_flags_modal_open")


def test_detect_blocker_via_page_type_error() -> None:
    vr = _mk_vresp(
        bboxes=[
            _mk_vision_bbox(label="SpotHero is not available", box=(0, 0, 100, 500), role="text_block"),
            _mk_vision_bbox(label="Continue Anyway", box=(120, 300, 160, 500)),
        ],
        page_type="error_page",
    )
    info = detect_active_blocker(vr)
    assert info is not None
    assert info.source == "page_type"
    assert info.dismiss_hint == "Continue Anyway"
    assert "error_page" in info.reason
    print("✓ test_detect_blocker_via_page_type_error")


def test_detect_blocker_via_sparse_heuristic() -> None:
    # 2 bboxes, one with a dismiss verb — vision didn't set flags or
    # page_type but the shape screams "this is a wall".
    vr = _mk_vresp(
        bboxes=[
            _mk_vision_bbox(label="Something went wrong", box=(0, 0, 50, 500), role="text_block"),
            _mk_vision_bbox(label="Proceed", box=(60, 200, 120, 400)),
        ],
    )
    info = detect_active_blocker(vr)
    assert info is not None
    assert info.source == "sparse_heuristic"
    assert info.dismiss_hint == "Proceed"
    print("✓ test_detect_blocker_via_sparse_heuristic")


def test_detect_blocker_returns_none_on_normal_page() -> None:
    # 10 bboxes of regular content, no flags, no page_type blocker.
    vr = _mk_vresp(
        bboxes=[
            _mk_vision_bbox(label=f"Item {i}", box=(i*10, 0, i*10 + 10, 100))
            for i in range(10)
        ],
        page_type="search_results",
    )
    assert detect_active_blocker(vr) is None
    print("✓ test_detect_blocker_returns_none_on_normal_page")


def test_detect_blocker_handles_none_vision() -> None:
    assert detect_active_blocker(None) is None
    print("✓ test_detect_blocker_handles_none_vision")


def main() -> int:
    tests = [
        test_rect_iou_basics,
        test_fuses_vision_with_matching_dom,
        test_orphan_vision_kept_as_vision_source,
        test_dom_orphan_recovers_on_intent_match,
        test_resolve_by_label_picks_best_match,
        test_resolve_by_bbox_index_preserves_vision_order,
        test_build_fused_perception_wrapper,
        test_dom_without_vw_vh_does_not_crash,
        test_detect_blocker_via_scene_layer,
        test_detect_blocker_via_flags_modal_open,
        test_detect_blocker_via_page_type_error,
        test_detect_blocker_via_sparse_heuristic,
        test_detect_blocker_returns_none_on_normal_page,
        test_detect_blocker_handles_none_vision,
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
