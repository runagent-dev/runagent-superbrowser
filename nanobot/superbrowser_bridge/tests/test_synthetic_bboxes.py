"""Unit coverage for the synthetic V_n / DOM-scan pipeline.

The pipeline closes the visibility gap where vision (Gemini) misses
small dynamic items (autocomplete suggestions, calendar cells, modal
CTAs) and forces the brain to fall back to `browser_eval` /
`browser_run_script`. These tests verify the integration points:

  * `BrowserSessionState.inject_synthetic_bboxes` assigns reserved-
    range V_n (>=1000) and stores side-channel meta.
  * `vision_for_target_resolution()` returns a `_MergedVisionResponse`
    when synthetics are active; the wrapper resolves synthetic V_n.
  * `_click_pending_screenshot_block` bypasses synthetic V_n so the
    brain can click them immediately, without re-screenshotting.
  * `_is_enumeration_script` catches the exact `querySelectorAll(...)
    .map/forEach` pattern the brain reaches for to walk a dropdown.
  * `BrowserEvalTool` hard-blocks enumeration scripts when synthetics
    are active and falls through (advisory) when none are.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from vision_agent.schemas import BBox, VisionResponse
from superbrowser_bridge.session_tools.state import (
    BrowserSessionState,
    _MergedVisionResponse,
)
from superbrowser_bridge.session_tools.tools.click import (
    _click_pending_screenshot_block,
)
from superbrowser_bridge.session_tools.tools.scripting import (
    BrowserEvalTool,
    _is_enumeration_script,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_vision_response(n_bboxes: int = 3) -> VisionResponse:
    """Build a minimal VisionResponse with n dummy bboxes."""
    bboxes = [
        BBox(
            label=f"vision_{i}",
            box_2d=[10 * i, 10 * i, 10 * i + 20, 10 * i + 20],
            clickable=True,
            role="button",
            confidence=0.6,
            intent_relevant=False,
            role_in_scene="content",
        )
        for i in range(n_bboxes)
    ]
    resp = VisionResponse(
        intent="test",
        page_type="other",
        bboxes=bboxes,
    )
    # Image dims so to_pixels() works in case the scanner falls back to
    # them when resolving synthetic geometry.
    resp._image_width = 800
    resp._image_height = 600
    return resp


def _make_synth_bbox(label: str) -> BBox:
    """Build a plausible synthetic-style bbox (intent-relevant target)."""
    return BBox(
        label=label,
        box_2d=[100, 100, 200, 200],
        clickable=True,
        role="option",
        confidence=0.85,
        intent_relevant=True,
        role_in_scene="target",
    )


def test_inject_assigns_reserved_range_v_n_and_stores_meta() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    bboxes = [_make_synth_bbox("alpha"), _make_synth_bbox("beta")]
    v_indices = s.inject_synthetic_bboxes(
        bboxes,
        scan_kind="autocomplete",
        anchor_v=3,
        ttl_turns=3,
    )
    assert len(v_indices) == 2
    # Reserved range (>= 1000) so no collision with vision V_n.
    assert all(v >= 1001 for v in v_indices)
    assert v_indices[1] == v_indices[0] + 1
    # Side-channel meta populated for each entry.
    for v in v_indices:
        meta = s._synthetic_meta_by_v[v]
        assert meta["origin"] == "dom_scan"
        assert meta["scan_kind"] == "autocomplete"
        assert meta["scan_anchor_v"] == 3
        assert meta["expires_at_turn"] == s._brain_turn_counter + 3


def test_inject_no_op_when_bboxes_empty() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    assert s.inject_synthetic_bboxes([], scan_kind="autocomplete") == []
    assert not s._synthetic_bboxes_by_v


def test_is_synthetic_v_truthy_only_for_injected() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    [v_n] = s.inject_synthetic_bboxes(
        [_make_synth_bbox("only")],
        scan_kind="autocomplete",
    )
    assert s.is_synthetic_v(v_n)
    # Vision V_n (1..3 here) must not be flagged synthetic.
    assert not s.is_synthetic_v(1)
    assert not s.is_synthetic_v(2)
    assert not s.is_synthetic_v(9999)


def test_merged_response_resolves_synthetic_and_delegates_to_base() -> None:
    s = BrowserSessionState()
    base = _make_vision_response(n_bboxes=3)
    s._last_vision_response = base
    bb = _make_synth_bbox("synth_label")
    [v_synth] = s.inject_synthetic_bboxes(
        [bb], scan_kind="autocomplete",
    )
    resolved = s.vision_for_target_resolution()
    # No epoch frozen, so the wrapper is built off _last_vision_response.
    assert isinstance(resolved, _MergedVisionResponse)
    # Synthetic V_n resolves to the injected bbox.
    assert resolved.get_bbox(v_synth) is bb
    # Vision V_n still goes through the base ranker.
    vision_bbox = resolved.get_bbox(1)
    assert vision_bbox is not None
    assert vision_bbox.label.startswith("vision_")


def test_merged_response_skipped_when_no_synthetics() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    # No injection → plain response (no wrapper overhead).
    resolved = s.vision_for_target_resolution()
    assert resolved is s._last_vision_response


def test_consume_synthetic_removes_and_clears_pending_when_last() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.last_type_had_suggestions = True
    s.last_type_anchor_label = "V3"
    bboxes = [_make_synth_bbox("a"), _make_synth_bbox("b")]
    v_indices = s.inject_synthetic_bboxes(
        bboxes, scan_kind="autocomplete",
    )
    # Consume first — pending still armed because one synthetic remains.
    assert s.consume_synthetic_v(v_indices[0])
    assert s.last_type_had_suggestions is True
    # Consume last — pending guard auto-releases.
    assert s.consume_synthetic_v(v_indices[1])
    assert s.last_type_had_suggestions is False
    assert s.last_type_anchor_label == ""
    # No-op on a stale V_n.
    assert not s.consume_synthetic_v(v_indices[0])


def test_expire_drops_entries_past_ttl() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s._brain_turn_counter = 5
    v_now = s.inject_synthetic_bboxes(
        [_make_synth_bbox("fresh")],
        scan_kind="autocomplete",
        ttl_turns=3,
    )[0]
    # Same turn, nothing expired.
    assert s.expire_stale_synthetic_bboxes() == 0
    # Advance past the TTL → entry expires.
    s._brain_turn_counter = 5 + 3 + 1
    assert s.expire_stale_synthetic_bboxes() == 1
    assert v_now not in s._synthetic_bboxes_by_v


def test_clear_synthetic_drops_everything() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("x"), _make_synth_bbox("y")],
        scan_kind="autocomplete",
    )
    assert s.clear_synthetic_bboxes(reason="test") == 2
    assert not s._synthetic_bboxes_by_v
    assert not s._synthetic_meta_by_v


def test_pending_screenshot_block_bypasses_synthetic_v() -> None:
    s = BrowserSessionState()
    s.last_type_had_suggestions = True
    s.last_type_anchor_label = "V3"
    s.last_type_at = 100.0
    s.last_screenshot_at = 50.0  # screenshot OLDER than type — guard armed
    s._last_vision_response = _make_vision_response()
    [v_synth] = s.inject_synthetic_bboxes(
        [_make_synth_bbox("pick_me")],
        scan_kind="autocomplete",
    )
    # Vision V_n → guard fires (must screenshot first).
    blocked_vision = _click_pending_screenshot_block(s, vision_index=1)
    assert blocked_vision is not None and "[click_pending_screenshot]" in blocked_vision
    # Synthetic V_n → guard bypassed.
    blocked_synth = _click_pending_screenshot_block(s, vision_index=v_synth)
    assert blocked_synth is None


def test_pending_screenshot_caption_mentions_synthetic_when_available() -> None:
    s = BrowserSessionState()
    s.last_type_had_suggestions = True
    s.last_type_anchor_label = "V3"
    s.last_type_at = 100.0
    s.last_screenshot_at = 50.0
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("alpha")], scan_kind="autocomplete",
    )
    # When the brain (incorrectly) targets a non-synthetic V_n, the
    # refusal message points it at the synthetic V_n instead of just
    # nagging "screenshot first".
    blocked = _click_pending_screenshot_block(s, vision_index=1)
    assert blocked is not None
    assert "[click_pending_screenshot]" in blocked
    assert "alpha" in blocked
    assert "click_at" in blocked


def test_enumeration_pattern_detector_matches_spothero_case() -> None:
    """Exact JS string from the user's spothero log must trigger the
    enumeration anti-pattern detector."""
    spothero = (
        'return Array.from(document.querySelectorAll(\'ul[role="listbox"] '
        "li')).map((el, i) => ({index: i, text: el.innerText}))"
    )
    assert _is_enumeration_script(spothero)


def test_enumeration_pattern_detector_lets_inspection_through() -> None:
    """Routine inspection must NOT trip the detector — false positives
    would break the cold-start inspection workflow."""
    assert not _is_enumeration_script("document.title")
    assert not _is_enumeration_script("document.activeElement.tagName")
    assert not _is_enumeration_script("window.scrollY")
    assert not _is_enumeration_script(
        "document.querySelectorAll('input').length"
    )


@pytest.mark.anyio
async def test_eval_blocked_when_synthetic_active() -> None:
    """When synthetic V_n exist and the brain runs ANY eval (the
    enumeration case is a strict subset of all-eval blocking), the tool
    must hard-block and surface the synthetic V_n list. With the
    moderate-scope guard, ANY eval is blocked while synthetic is active
    — the enumeration-specific block is only a fallback for the case
    when synthetic isn't active."""
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("San Francisco MOMA")],
        scan_kind="autocomplete",
    )
    tool = BrowserEvalTool(s)
    spothero = (
        "return Array.from(document.querySelectorAll('ul[role=\"listbox\"] "
        "li')).map((el, i) => ({index: i, text: el.innerText}))"
    )
    out = await tool.execute(session_id="sess1", script=spothero)
    assert "[eval_blocked:synthetic_v_active]" in out
    assert "San Francisco MOMA" in out
    assert "browser_click_at" in out


@pytest.mark.anyio
async def test_eval_blocked_for_any_script_when_synthetic_active() -> None:
    """The synthetic-V_n window blocks ALL eval scripts, not just
    enumeration patterns. Every eval ages the synthetic bbox; force
    the brain to click_at the synthetic V_n instead."""
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("Dropdown item")], scan_kind="autocomplete",
    )
    tool = BrowserEvalTool(s)
    # Even an innocent inspection eval is blocked while synthetic is active.
    out = await tool.execute(session_id="sess1", script="document.title")
    assert "[eval_blocked:synthetic_v_active]" in out
    assert "Dropdown item" in out
    assert "browser_click_at" in out


@pytest.mark.anyio
async def test_run_script_blocked_when_synthetic_active() -> None:
    """run_script (mutating or not) is blocked while synthetic V_n
    are active. Mirrors the eval block."""
    from superbrowser_bridge.session_tools.tools.scripting import (
        BrowserRunScriptTool,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("Suggest")], scan_kind="autocomplete",
    )
    tool = BrowserRunScriptTool(s)
    out = await tool.execute(
        session_id="sess1", script="return document.body.innerText",
    )
    assert "[run_script_blocked:synthetic_v_active]" in out
    assert "Suggest" in out


@pytest.mark.anyio
async def test_eval_advisory_when_no_synthetic() -> None:
    """No synthetic V_n active → enumeration eval IS allowed (the brain
    may be doing legitimate cold-start inspection). Tool must complete
    normally, not block."""
    s = BrowserSessionState()
    tool = BrowserEvalTool(s)

    class _FakeResp:
        status_code = 200
        def raise_for_status(self) -> None:
            return None
        def json(self) -> dict:
            return {"result": ["x", "y", "z"]}

    async def _fake_dispatch(*_a, **_kw):
        return _FakeResp()

    with patch(
        "superbrowser_bridge.session_tools.tools.scripting._request_with_backoff",
        side_effect=_fake_dispatch,
    ):
        out = await tool.execute(
            session_id="sess1",
            script=(
                'document.querySelectorAll("li").forEach(x => x.click())'
            ),
        )
    assert "[eval_blocked:enumeration_pattern]" not in out


def test_synthetic_v_summary_renders_active_entries() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("First"), _make_synth_bbox("Second")],
        scan_kind="autocomplete",
    )
    summary = s.synthetic_v_summary()
    assert "First" in summary
    assert "Second" in summary
    assert "kind=autocomplete" in summary
    assert "V1001" in summary


def test_synthetic_v_summary_empty_when_no_entries() -> None:
    s = BrowserSessionState()
    assert s.synthetic_v_summary() == ""


@pytest.mark.anyio
async def test_screenshot_redirects_when_fresh_synthetic_v_active() -> None:
    """The 'let me verify first' detour into screenshot must be refused
    when the brain has fresh synthetic V_n to click. Otherwise the
    cursor-first ladder unravels: the brain screenshots, gets vision
    that may not see the dropdown, and falls back to eval/run_script."""
    from superbrowser_bridge.session_tools.tools.screenshot import (
        BrowserScreenshotTool,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s._brain_turn_counter = 5
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("Suggestion A")], scan_kind="autocomplete",
    )
    tool = BrowserScreenshotTool(s)
    out = await tool.execute(session_id="sess1")
    assert "[screenshot_blocked:synthetic_v_fresh]" in out
    assert "Suggestion A" in out
    assert "browser_click_at" in out


@pytest.mark.anyio
async def test_screenshot_allowed_when_synthetic_v_is_stale() -> None:
    """If the synthetic V_n was injected several turns ago, the brain
    is allowed to screenshot — staleness means the page may have
    moved on and a fresh vision pass is the right call."""
    from unittest.mock import patch
    from superbrowser_bridge.session_tools.tools.screenshot import (
        BrowserScreenshotTool,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s._brain_turn_counter = 5
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("Old suggestion")], scan_kind="autocomplete",
    )
    # Advance several turns so the synthetic is no longer "fresh".
    s._brain_turn_counter = 5 + 5

    tool = BrowserScreenshotTool(s)
    # Patch the should_allow_screenshot gate so we just see whether the
    # synthetic-fresh redirect fires. We don't care about the rest of
    # the screenshot flow here.
    with patch.object(s, "should_allow_screenshot", return_value=(False, "[budget exhausted]")):
        out = await tool.execute(session_id="sess1")
    assert "[screenshot_blocked:synthetic_v_fresh]" not in out


def test_reset_per_session_clears_synthetic_state() -> None:
    s = BrowserSessionState()
    s._last_vision_response = _make_vision_response()
    s.inject_synthetic_bboxes(
        [_make_synth_bbox("a")], scan_kind="autocomplete",
    )
    assert s._synthetic_bboxes_by_v
    s.reset_per_session()
    assert not s._synthetic_bboxes_by_v
    assert not s._synthetic_meta_by_v
    assert s._next_synthetic_v == 1000
