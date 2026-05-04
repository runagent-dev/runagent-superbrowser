"""Unit tests for the post-action verifier.

Covers the pure-python helpers that translate a VisionResponse with
verify_action intent into the verifier output shape, plus the
dense-scene neighbor-counter used to gate auto-fire.

No network, no real vision call.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_action_verifier
"""

from __future__ import annotations

import sys


def test_count_nearby_bboxes_radius() -> None:
    from superbrowser_bridge.action_verifier import (
        count_nearby_bboxes, DENSE_SCENE_NEIGHBOR_RADIUS_PX,
    )

    class _BB:
        def __init__(self, box):
            self.box_2d = box

    # Image is 1000x1000. Target at (500, 500).
    image_w, image_h = 1000, 1000
    target = (500, 500)
    # 3 bboxes near the target (centers within 80px), 2 far.
    bboxes = [
        # Centers at (500, 500), (550, 500), (450, 550) — close
        _BB([490, 490, 510, 510]),  # center ~ (500, 500)
        _BB([540, 490, 560, 510]),  # center ~ (500, 550)
        _BB([440, 540, 460, 560]),  # center ~ (550, 450)
        # Far
        _BB([10, 10, 30, 30]),
        _BB([970, 970, 990, 990]),
    ]
    n = count_nearby_bboxes(target, bboxes, image_w, image_h)
    assert n == 3, f"expected 3, got {n}"


def test_count_nearby_bboxes_handles_empty() -> None:
    from superbrowser_bridge.action_verifier import count_nearby_bboxes

    assert count_nearby_bboxes((0, 0), [], 100, 100) == 0


def test_build_verifier_result_succeeded() -> None:
    """When PageState.last_action_verdict is `succeeded`, the verifier
    returns recommendation=continue."""
    from superbrowser_bridge.action_verifier import build_verifier_result
    from vision_agent.schemas import (
        VisionResponse, PageState, LastActionVerdict,
    )

    resp = VisionResponse(
        page_state=PageState(
            last_action_verdict=LastActionVerdict(
                verdict="succeeded",
                evidence="modal closed and toast appeared",
                delta_summary="dialog dismissed",
            ),
        ),
    )
    out = build_verifier_result(resp, expected="modal should close")
    assert out["action_outcome"] == "succeeded"
    assert out["recommendation"] == "continue"
    # element_state_delta prefers delta_summary; evidence carries the
    # full quote.
    assert out["element_state_delta"] == "dialog dismissed"
    assert "modal closed" in out["evidence"]


def test_build_verifier_result_failed_recommends_undo() -> None:
    from superbrowser_bridge.action_verifier import build_verifier_result
    from vision_agent.schemas import (
        VisionResponse, PageState, LastActionVerdict,
    )

    resp = VisionResponse(
        page_state=PageState(
            last_action_verdict=LastActionVerdict(
                verdict="failed",
                evidence="page is unchanged",
            ),
        ),
    )
    out = build_verifier_result(resp, expected="filter applied")
    assert out["action_outcome"] == "failed"
    assert out["recommendation"] == "undo"


def test_build_verifier_result_uncertain_retries() -> None:
    from superbrowser_bridge.action_verifier import build_verifier_result
    from vision_agent.schemas import (
        VisionResponse, PageState, LastActionVerdict,
    )

    resp = VisionResponse(
        page_state=PageState(
            last_action_verdict=LastActionVerdict(
                verdict="uncertain",
            ),
        ),
    )
    out = build_verifier_result(resp, expected="??")
    assert out["recommendation"] == "retry"


def test_build_verifier_result_handles_no_response() -> None:
    from superbrowser_bridge.action_verifier import build_verifier_result

    out = build_verifier_result(None, expected="anything")
    assert out["action_outcome"] == "uncertain"
    assert out["recommendation"] == "retry"


def test_render_verifier_text_includes_undo_guidance() -> None:
    from superbrowser_bridge.action_verifier import render_verifier_text

    text = render_verifier_text({
        "action_outcome": "failed",
        "recommendation": "undo",
        "element_state_delta": "no change",
        "expected": "modal closes",
    })
    assert "[VERIFY]" in text
    assert "undo" in text.lower()
    assert "navigate(back)" in text.lower() or "navigate" in text.lower()


def main() -> int:
    tests = [
        test_count_nearby_bboxes_radius,
        test_count_nearby_bboxes_handles_empty,
        test_build_verifier_result_succeeded,
        test_build_verifier_result_failed_recommends_undo,
        test_build_verifier_result_uncertain_retries,
        test_build_verifier_result_handles_no_response,
        test_render_verifier_text_includes_undo_guidance,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERR  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
