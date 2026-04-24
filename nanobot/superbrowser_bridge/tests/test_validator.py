"""Unit tests for validator.validate — the mandatory propose/validate/fire gate.

No external services required. Mocks `_require_fresh_vision` so the
freshness path stays offline; everything else (fusion, scoring, blocker
gate, precondition check, intent synthesis) runs for real.

Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_validator.py
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from superbrowser_bridge import validator as validator_mod
from superbrowser_bridge.task_graph import (
    Precondition,
    Subgoal,
    TaskGraph,
)


# ---------------------------------------------------------------- helpers


def _install_fresh_vision(monkey_ok: bool, message: str = "") -> None:
    """Swap `_require_fresh_vision` with a predictable stub for the test run."""
    from superbrowser_bridge import session_tools

    async def _stub(state, session_id, *, reason="", force_refresh=False):
        return monkey_ok, message

    session_tools._require_fresh_vision = _stub  # type: ignore[attr-defined]


class _FakeBBox:
    def __init__(
        self, *, label: str, box: tuple[int, int, int, int],
        role: str = "button", confidence: float = 0.8,
        layer_id: str | None = None,
    ) -> None:
        self.label = label
        self.box_2d = list(box)
        self.role = role
        self.confidence = confidence
        self.clickable = True
        self.intent_relevant = True
        self.role_in_scene = "target"
        self.layer_id = layer_id

    def to_pixels(self, image_w: int, image_h: int, *, dpr: float = 1.0):
        ymin, xmin, ymax, xmax = self.box_2d
        scale_w = image_w / max(dpr, 1e-6)
        scale_h = image_h / max(dpr, 1e-6)
        x0 = int(xmin / 1000.0 * scale_w)
        y0 = int(ymin / 1000.0 * scale_h)
        x1 = int(xmax / 1000.0 * scale_w)
        y1 = int(ymax / 1000.0 * scale_h)
        return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)

    def center_pixels(self, image_w: int, image_h: int, *, dpr: float = 1.0):
        x0, y0, x1, y1 = self.to_pixels(image_w, image_h, dpr=dpr)
        return ((x0 + x1) // 2, (y0 + y1) // 2)


class _FakeScene:
    def __init__(self, active_blocker_layer_id: str | None = None, layers=()):
        self.active_blocker_layer_id = active_blocker_layer_id
        self.layers = list(layers)


class _FakeVisionResp:
    def __init__(self, bboxes, *, scene=None):
        self.bboxes = bboxes
        self.scene = scene
        self.image_width = 1000
        self.image_height = 1000
        self.dpr = 1.0

    def with_image_dims(self, w, h, dpr=None):
        self.image_width = w
        self.image_height = h
        return self


def _mk_state(
    *, vision_resp=None, dom_entries=None, task_graph=None, current_token=1,
) -> SimpleNamespace:
    return SimpleNamespace(
        _last_vision_response=vision_resp,
        last_selector_entries=dom_entries or [],
        task_graph=task_graph,
        current_token=current_token,
        validator_stats={},
    )


def _mk_dom_entry(**kw):
    return {
        "index": kw.get("index", 1),
        "xpath": kw.get("xpath", "/html/body/button[1]"),
        "tagName": kw.get("tag", "button"),
        "attributes": {"role": kw.get("role", "button")},
        "text": kw.get("text", ""),
        "bounds": {
            "x": kw.get("x", 0), "y": kw.get("y", 0),
            "width": kw.get("w", 100), "height": kw.get("h", 40),
            "vw": 1000, "vh": 1000,
        },
        "regionTag": kw.get("region", "main"),
    }


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------- tests


def test_stale_vision_rejects() -> None:
    _install_fresh_vision(False, "[vision_gate_timeout]")
    state = _mk_state(vision_resp=None)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="Submit",
        ),
    ))
    assert not result.ok
    assert result.reason == "stale_vision"
    assert result.required_action == "re_perceive"
    assert state.validator_stats["rejected_stale"] == 1
    print("✓ test_stale_vision_rejects")


def test_ok_path_with_valid_intent() -> None:
    _install_fresh_vision(True)
    vr = _FakeVisionResp([_FakeBBox(label="Submit form", box=(50, 50, 100, 200))])
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="Submit",
        ),
    ))
    assert result.ok, (result.reason, result.caption)
    assert result.action is not None
    assert result.action.resolved.label == "Submit form"
    assert result.action.resolved.click_point_px is not None
    assert state.validator_stats["ok"] == 1
    print("✓ test_ok_path_with_valid_intent")


def test_label_mismatch_rejects() -> None:
    _install_fresh_vision(True)
    vr = _FakeVisionResp([_FakeBBox(label="Cancel", box=(0, 0, 50, 100))])
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="Submit form",
        ),
    ))
    assert not result.ok
    assert result.reason == "label_mismatch"
    assert "label_mismatch" in result.caption
    assert state.validator_stats["rejected_label"] == 1
    print("✓ test_label_mismatch_rejects")


def test_intent_synthesized_from_label_when_missing() -> None:
    """Back-compat: legacy `browser_click_at(V2)` without intent must
    still work — validator synthesizes intent from the bbox label."""
    _install_fresh_vision(True)
    vr = _FakeVisionResp([_FakeBBox(label="Accept cookies", box=(0, 0, 50, 200))])
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(tool="click_at", bbox_index=1, intent=""),
    ))
    assert result.ok, (result.reason, result.caption)
    assert state.validator_stats.get("intent_synthesized", 0) == 1
    print("✓ test_intent_synthesized_from_label_when_missing")


def test_blocker_layer_rejects_click_on_content() -> None:
    """Scene has an active_blocker_layer_id and the target sits in a
    different layer. The unified blocker-unaddressed gate catches this
    (it fires before the older layer-id-mismatch gate) — the legacy
    `rejected_blocker` counter still increments only on layer-mismatch
    cases where the target label *does* match the dismiss hint."""
    _install_fresh_vision(True)
    bbox_content = _FakeBBox(
        label="Main action", box=(300, 300, 400, 500),
        layer_id="L1_content",
    )
    scene = _FakeScene(
        active_blocker_layer_id="L0_modal",
        layers=[SimpleNamespace(id="L0_modal", dismiss_hint="Close")],
    )
    vr = _FakeVisionResp([bbox_content], scene=scene)
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="Main action",
        ),
    ))
    assert not result.ok
    assert result.reason == "blocker_unaddressed", result.reason
    assert "Close" in result.caption
    assert state.validator_stats["rejected_blocker_unaddressed"] == 1
    print("✓ test_blocker_layer_rejects_click_on_content")


def test_precondition_miss_triggers_re_perceive() -> None:
    _install_fresh_vision(True)
    # Vision sees "Cancel" but the subgoal's precondition wants "Submit".
    vr = _FakeVisionResp([_FakeBBox(label="Cancel", box=(0, 0, 50, 100))])
    subgoal = Subgoal(
        id="g1", description="Submit form", status="active",
        precondition=Precondition(element_label="Submit"),
    )
    graph = TaskGraph(subgoals={"g1": subgoal}, active_id="g1")
    state = _mk_state(vision_resp=vr, task_graph=graph)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="Cancel",
            subgoal_id="g1",
        ),
    ))
    assert not result.ok
    assert result.reason == "precondition_not_satisfied"
    assert result.required_action == "re_perceive"
    assert "coverage_miss" in result.caption
    assert state.validator_stats["rejected_precondition"] == 1
    print("✓ test_precondition_miss_triggers_re_perceive")


def test_dom_fusion_recovers_when_vision_missed() -> None:
    """Tools-section recovery: vision didn't emit a bbox for 'Export'
    but DOM has it + the subgoal precondition wants it."""
    _install_fresh_vision(True)
    vr = _FakeVisionResp([_FakeBBox(label="Unrelated", box=(0, 0, 50, 100))])
    dom = [_mk_dom_entry(
        index=9, text="Export data", x=800, y=50, w=120, h=30,
        region="toolbar",
    )]
    subgoal = Subgoal(
        id="g1", description="Export the data", status="active",
        precondition=Precondition(element_label="Export data"),
    )
    graph = TaskGraph(subgoals={"g1": subgoal}, active_id="g1")
    state = _mk_state(vision_resp=vr, dom_entries=dom, task_graph=graph)
    # Brain asks to click by intent (no bbox_index). Validator should
    # resolve via DOM fusion path.
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", intent="Export data", subgoal_id="g1",
        ),
    ))
    assert result.ok, (result.reason, result.caption)
    assert result.action.resolved.source == "dom", result.action.resolved.source
    assert result.action.resolved.xpath is not None
    assert state.validator_stats.get("dom_recovery_hits", 0) == 1
    print("✓ test_dom_fusion_recovers_when_vision_missed")


def test_bad_vision_index_rejects() -> None:
    _install_fresh_vision(True)
    vr = _FakeVisionResp([_FakeBBox(label="Only", box=(0, 0, 50, 100))])
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=99, intent="anything",
        ),
    ))
    assert not result.ok
    assert result.reason == "bad_vision_index"
    assert result.required_action == "re_perceive"
    print("✓ test_bad_vision_index_rejects")


def test_raw_coord_click_skips_label_gate() -> None:
    """Raw (x,y) clicks have no label — only freshness, precondition,
    and blocker gates apply. A lack of `intent` must NOT reject."""
    _install_fresh_vision(True)
    vr = _FakeVisionResp([])  # No bboxes — raw coord click against blank vision.
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", raw_x=123.0, raw_y=456.0,
        ),
    ))
    assert result.ok, (result.reason, result.caption)
    assert result.action.resolved.source == "raw_coord"
    assert result.action.resolved.click_point_px == (123, 456)
    print("✓ test_raw_coord_click_skips_label_gate")


def test_blocker_unaddressed_rejects_wrong_target() -> None:
    """Vision has set page_type=error_page and one of the bboxes has a
    dismiss verb label — brain tries to click a non-dismiss element.
    Validator must reject with `blocker_unaddressed`."""
    _install_fresh_vision(True)
    content = _FakeBBox(label="SpotHero unavailable", box=(0, 0, 40, 500))
    continue_btn = _FakeBBox(label="Continue Anyway", box=(50, 200, 100, 400))
    vr = _FakeVisionResp([content, continue_btn])
    vr.page_type = "error_page"
    vr.flags = SimpleNamespace(
        modal_open=False, login_wall=False, error_banner=None,
        captcha_present=False,
    )
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="site header",
        ),
    ))
    assert not result.ok
    assert result.reason == "blocker_unaddressed"
    assert "Continue Anyway" in result.caption
    assert state.validator_stats["rejected_blocker_unaddressed"] == 1
    print("✓ test_blocker_unaddressed_rejects_wrong_target")


def test_blocker_unaddressed_passes_when_target_matches_dismiss() -> None:
    """Same page state as above, but the brain correctly targets the
    dismiss element — validator lets it through."""
    _install_fresh_vision(True)
    content = _FakeBBox(label="SpotHero unavailable", box=(0, 0, 40, 500))
    continue_btn = _FakeBBox(label="Continue Anyway", box=(50, 200, 100, 400))
    vr = _FakeVisionResp([content, continue_btn])
    vr.page_type = "error_page"
    vr.flags = SimpleNamespace(
        modal_open=False, login_wall=False, error_banner=None,
        captcha_present=False,
    )
    state = _mk_state(vision_resp=vr)
    # Continue Anyway ranks second by insertion order in the fake resp,
    # but `resolve_by_bbox_index(2)` walks vision_bbox elements.
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=2, intent="Continue Anyway",
        ),
    ))
    assert result.ok, (result.reason, result.caption)
    assert state.validator_stats.get("rejected_blocker_unaddressed", 0) == 0
    print("✓ test_blocker_unaddressed_passes_when_target_matches_dismiss")


def test_blocker_unaddressed_gate_does_not_apply_to_raw_coords() -> None:
    """Raw-coord clicks have no label to compare — gate must not fire."""
    _install_fresh_vision(True)
    continue_btn = _FakeBBox(label="Continue Anyway", box=(50, 200, 100, 400))
    vr = _FakeVisionResp([continue_btn])
    vr.page_type = "error_page"
    vr.flags = SimpleNamespace(
        modal_open=False, login_wall=False, error_banner=None,
        captcha_present=False,
    )
    state = _mk_state(vision_resp=vr)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", raw_x=300.0, raw_y=75.0,
        ),
    ))
    assert result.ok, (result.reason, result.caption)
    print("✓ test_blocker_unaddressed_gate_does_not_apply_to_raw_coords")


def test_validated_action_carries_observation_token() -> None:
    _install_fresh_vision(True)
    vr = _FakeVisionResp([_FakeBBox(label="Submit", box=(0, 0, 50, 100))])
    state = _mk_state(vision_resp=vr, current_token=42)
    result = _run(validator_mod.validate(
        state, "s1",
        validator_mod.ProposedAction(
            tool="click_at", bbox_index=1, intent="Submit",
        ),
    ))
    assert result.ok
    assert result.action.observation_token == 42
    assert result.action.validator_version == validator_mod.VALIDATOR_VERSION
    print("✓ test_validated_action_carries_observation_token")


def main() -> int:
    tests = [
        test_stale_vision_rejects,
        test_ok_path_with_valid_intent,
        test_label_mismatch_rejects,
        test_intent_synthesized_from_label_when_missing,
        test_blocker_layer_rejects_click_on_content,
        test_precondition_miss_triggers_re_perceive,
        test_dom_fusion_recovers_when_vision_missed,
        test_bad_vision_index_rejects,
        test_raw_coord_click_skips_label_gate,
        test_blocker_unaddressed_rejects_wrong_target,
        test_blocker_unaddressed_passes_when_target_matches_dismiss,
        test_blocker_unaddressed_gate_does_not_apply_to_raw_coords,
        test_validated_action_carries_observation_token,
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
