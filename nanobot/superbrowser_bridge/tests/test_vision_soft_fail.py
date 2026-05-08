"""Vision schema soft-fail tests.

The repro: Gemini emits `scene.layers[0].bbox` as a dict in a shape the
original `_coerce_box` validator rejected. Before P1 both the first
attempt AND the compact retry would fail, `_fallback_response()` would
return empty bboxes, and the brain would plan against no labels.

After P1:
  1. `_coerce_box` accepts dict shapes Gemini actually emits (ymin/xmin
     and y0/x0) plus nested `{box_2d: [...]}`.
  2. `_parse_response_with_error` has a soft-fail ladder — on
     ValidationError it strips the bad paths and retries; as a last
     resort it returns a `bboxes`-only response with scene=None.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_vision_soft_fail.py
"""

from __future__ import annotations

import json
import sys


def test_coerce_box_accepts_ymin_xmin_dict() -> None:
    from vision_agent.schemas import BBox
    b = BBox(label="x", box_2d={"ymin": 10, "xmin": 20, "ymax": 40, "xmax": 60})
    assert b.box_2d == [10, 20, 40, 60]
    print("✓ test_coerce_box_accepts_ymin_xmin_dict")


def test_coerce_box_accepts_y0_x0_dict() -> None:
    from vision_agent.schemas import BBox
    b = BBox(label="x", box_2d={"y0": 5, "x0": 15, "y1": 25, "x1": 35})
    assert b.box_2d == [5, 15, 25, 35]
    print("✓ test_coerce_box_accepts_y0_x0_dict")


def test_coerce_box_accepts_nested_box_2d() -> None:
    from vision_agent.schemas import BBox
    b = BBox(label="x", box_2d={"box_2d": [1, 2, 3, 4]})
    assert b.box_2d == [1, 2, 3, 4]
    print("✓ test_coerce_box_accepts_nested_box_2d")


def test_coerce_box_softfails_unknown_dict() -> None:
    """An unrecognizable dict should zero-out the box, not sink the
    whole response. Regression guard for the live SpotHero failure."""
    from vision_agent.schemas import BBox
    b = BBox(label="x", box_2d={"mystery": "field"})
    assert b.box_2d == [0, 0, 0, 0]
    print("✓ test_coerce_box_softfails_unknown_dict")


def test_parse_preserves_bboxes_when_scene_layers_bad() -> None:
    """The real failure from the SpotHero run. scene.layers[].bbox had a
    shape the original validator rejected; both parse attempts failed
    and the brain got zero bboxes. After P1 we must keep the bboxes
    and drop only the malformed scene leaf."""
    from vision_agent.client import _parse_response_with_error
    raw = json.dumps({
        "summary": "SpotHero with autocomplete open",
        "relevant_text": "San Francisco Museum of Modern Art",
        "bboxes": [
            {"label": "Search", "box_2d": [100, 200, 150, 400], "clickable": True},
            {"label": "Button", "box_2d": [500, 200, 550, 400], "clickable": True},
            {"label": "Suggestion", "box_2d": [300, 200, 340, 380], "clickable": True},
        ],
        # This is the shape that killed us in production.
        "scene": {
            "layers": [
                {"id": "layer1", "bbox": "not a dict at all", "role": "main"},
            ],
        },
        "flags": {"captcha_present": False},
    })
    resp, err = _parse_response_with_error(raw)
    assert resp is not None, f"soft-fail should recover, got err={err!r}"
    assert err == "", f"expected empty err on recovery, got {err!r}"
    assert len(resp.bboxes) == 3, (
        f"bboxes must be preserved across scene soft-fail, got {len(resp.bboxes)}"
    )
    print("✓ test_parse_preserves_bboxes_when_scene_layers_bad")


def test_parse_last_resort_when_strip_also_fails() -> None:
    """Even if the stripping retry can't coerce the response, we should
    manually build a bboxes-only VisionResponse rather than returning
    None + error. The brain's primary need is bbox labels."""
    from vision_agent.client import _parse_response_with_error
    # Top-level field mismatched in a way that stripping the leaf
    # probably won't fully fix; we still want bboxes preserved.
    raw = json.dumps({
        "summary": "fallback path",
        "bboxes": [{"label": "OK", "box_2d": [0, 0, 100, 100], "clickable": True}],
        "flags": "this should be a dict",
    })
    resp, _err = _parse_response_with_error(raw)
    # Either the strip retry worked OR the last-resort path kicked in.
    # In both cases we must have bboxes.
    assert resp is not None
    assert len(resp.bboxes) == 1
    assert resp.bboxes[0].label == "OK"
    print("✓ test_parse_last_resort_when_strip_also_fails")


def main() -> int:
    tests = [
        test_coerce_box_accepts_ymin_xmin_dict,
        test_coerce_box_accepts_y0_x0_dict,
        test_coerce_box_accepts_nested_box_2d,
        test_coerce_box_softfails_unknown_dict,
        test_parse_preserves_bboxes_when_scene_layers_bad,
        test_parse_last_resort_when_strip_also_fails,
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
