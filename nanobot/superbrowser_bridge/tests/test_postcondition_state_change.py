"""Unit tests for Arch v4 Phase E: bbox_state_change postcondition.

The action_planner promotes a click postcondition from `dom_mutated` to
`bbox_state_change` ONLY when BOTH signals agree:
  (A) target label includes a stateful keyword (toggle/switch/checkbox/
      radio/filter chip/...)
  (B) bbox.role is a stateful BBoxRole (currently `checkbox`) OR
      bbox.dom_check.role matches a stateful ARIA role (checkbox, radio,
      switch, tab, option, treeitem, menuitemcheckbox, menuitemradio).

Single-signal matches stay on dom_mutated — false positives (verifier
reports state-unchanged on a plain link, click looks failed, wasted
retry) are far worse than false negatives (just behaves like v3).

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_postcondition_state_change
"""

from __future__ import annotations

import sys


def _make_bbox(label: str = "", role: str = "other",
               dom_check: dict | None = None,
               clickable: bool = True,
               role_in_scene: str = "target",
               intent_relevant: bool = True,
               box: list[int] | None = None) -> "BBox":
    from vision_agent.schemas import BBox

    return BBox(
        label=label,
        role=role,
        clickable=clickable,
        confidence=0.9,
        intent_relevant=intent_relevant,
        role_in_scene=role_in_scene,
        box_2d=box or [400, 400, 500, 500],
        dom_check=dom_check,
    )


# ── direct unit tests on _is_stateful_target ──────────────────────


def test_two_signals_both_present_returns_true() -> None:
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="WiFi filter chip", role="checkbox")
    assert _is_stateful_target(bbox, "WiFi filter chip") is True


def test_label_only_returns_false() -> None:
    """Label hints stateful but role is plain — single signal, no
    promotion."""
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="Toggle visibility", role="button")
    assert _is_stateful_target(bbox, "Toggle visibility") is False


def test_role_only_returns_false() -> None:
    """role=checkbox but label has no stateful keyword — single
    signal, no promotion."""
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="Save", role="checkbox")
    assert _is_stateful_target(bbox, "Save") is False


def test_dom_check_role_radio_with_label_signal() -> None:
    """dom_check.role=radio + label hints — promotes."""
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(
        label="under $100 radio",
        role="other",  # vision missed it
        dom_check={"tag": "input", "role": "radio", "text": "$100",
                   "disagree": False},
    )
    assert _is_stateful_target(bbox, "under $100 radio") is True


def test_plain_button_returns_false() -> None:
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="Search", role="button")
    assert _is_stateful_target(bbox, "Search") is False


def test_link_returns_false() -> None:
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="Read more", role="link")
    assert _is_stateful_target(bbox, "Read more") is False


def test_empty_label_returns_false() -> None:
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="", role="checkbox")
    assert _is_stateful_target(bbox, "") is False


def test_filter_keyword_with_checkbox_role() -> None:
    from superbrowser_bridge.action_planner import _is_stateful_target

    bbox = _make_bbox(label="filter: parking", role="checkbox")
    assert _is_stateful_target(bbox, "filter: parking") is True


# ── integration: plan() emits the right postcondition kind ────────


def _make_vision_response_with_main_target(
    label: str, role: str, dom_check: dict | None = None,
) -> "VisionResponse":
    from vision_agent.schemas import (
        VisionResponse, SuggestedAction,
    )

    bbox = _make_bbox(
        label=label, role=role, dom_check=dom_check,
        # box_2d format: [ymin, xmin, ymax, xmax] normalized to [0,1000].
        # 400→500 vertical, 200→300 horizontal — non-degenerate rect.
        box=[400, 200, 500, 300],
    )
    suggested = SuggestedAction(
        action="click",
        priority=1,
        target_bbox_index=0,
        description=f"click {label}",
    )
    # image_width/height are PrivateAttr — set via with_image_dims().
    return VisionResponse(
        summary="page",
        bboxes=[bbox],
        suggested_actions=[suggested],
    ).with_image_dims(1000, 1000)


def test_plan_emits_bbox_state_change_for_stateful() -> None:
    from superbrowser_bridge.action_planner import plan, clear_cache

    clear_cache()
    vresp = _make_vision_response_with_main_target(
        label="WiFi filter chip", role="checkbox",
    )
    queue = plan(
        vresp=vresp, blockers=[], task_instruction="apply filters",
        url="https://example.com/results", recent_steps=[],
    )
    main = [a for a in queue.actions if a.source == "vision_suggestion"]
    assert main, "expected a main action in queue"
    assert main[0].postcondition.kind == "bbox_state_change"
    payload = main[0].postcondition.payload
    assert "widget_px" in payload
    assert len(payload["widget_px"]) == 4


def test_plan_emits_dom_mutated_for_plain_button() -> None:
    from superbrowser_bridge.action_planner import plan, clear_cache

    clear_cache()
    vresp = _make_vision_response_with_main_target(
        label="Search", role="button",
    )
    queue = plan(
        vresp=vresp, blockers=[], task_instruction="search",
        url="https://example.com/", recent_steps=[],
    )
    main = [a for a in queue.actions if a.source == "vision_suggestion"]
    assert main
    assert main[0].postcondition.kind == "dom_mutated"


def test_plan_emits_dom_mutated_when_only_label_stateful() -> None:
    from superbrowser_bridge.action_planner import plan, clear_cache

    clear_cache()
    vresp = _make_vision_response_with_main_target(
        label="Toggle dark mode", role="button",
    )
    queue = plan(
        vresp=vresp, blockers=[], task_instruction="enable dark mode",
        url="https://example.com/settings", recent_steps=[],
    )
    main = [a for a in queue.actions if a.source == "vision_suggestion"]
    assert main
    assert main[0].postcondition.kind == "dom_mutated"


def test_plan_emits_dom_mutated_when_only_role_stateful() -> None:
    from superbrowser_bridge.action_planner import plan, clear_cache

    clear_cache()
    vresp = _make_vision_response_with_main_target(
        label="Save my preferences", role="checkbox",
    )
    queue = plan(
        vresp=vresp, blockers=[], task_instruction="x",
        url="https://example.com/", recent_steps=[],
    )
    main = [a for a in queue.actions if a.source == "vision_suggestion"]
    assert main
    assert main[0].postcondition.kind == "dom_mutated"


def test_plan_uses_dom_check_role_when_vision_role_missed() -> None:
    from superbrowser_bridge.action_planner import plan, clear_cache

    clear_cache()
    vresp = _make_vision_response_with_main_target(
        label="Sort by price toggle",
        role="other",  # vision categorized as "other"
        dom_check={"tag": "button", "role": "switch", "text": "Sort",
                   "disagree": False},
    )
    queue = plan(
        vresp=vresp, blockers=[], task_instruction="sort",
        url="https://example.com/", recent_steps=[],
    )
    main = [a for a in queue.actions if a.source == "vision_suggestion"]
    assert main
    assert main[0].postcondition.kind == "bbox_state_change"


def main() -> int:
    tests = [
        test_two_signals_both_present_returns_true,
        test_label_only_returns_false,
        test_role_only_returns_false,
        test_dom_check_role_radio_with_label_signal,
        test_plain_button_returns_false,
        test_link_returns_false,
        test_empty_label_returns_false,
        test_filter_keyword_with_checkbox_role,
        test_plan_emits_bbox_state_change_for_stateful,
        test_plan_emits_dom_mutated_for_plain_button,
        test_plan_emits_dom_mutated_when_only_label_stateful,
        test_plan_emits_dom_mutated_when_only_role_stateful,
        test_plan_uses_dom_check_role_when_vision_role_missed,
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
