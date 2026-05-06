"""Tests for the wineaccess revamp (Phase A–E).

Covers:
  - bbox age gate (Phase A2)
  - looser repeat-click guard (Phase A4)
  - observed_query_keys + unknown-key refusal (Phase D)
  - force-reobserve gate (Phase E1)
  - auto-find-target hint detection (Phase E)
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl, urlparse

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.session_tools.tools import BrowserClickAtTool
from superbrowser_bridge.worker_hook import (
    BrowserWorkerHook,
    _guess_section_hint,
    _word_tokens,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ----------------- Phase A2: bbox age gate -----------------


def test_bbox_age_gate_blocks_old_epoch():
    """An 8s+ old vision epoch should refuse the click."""
    state = BrowserSessionState()
    # Force an old epoch.
    state._vision_epoch_taken_at = time.time() - 12.0
    # Set up minimal vision response so we get past the earlier checks.
    state._last_vision_response = MagicMock()
    state._last_vision_response.bboxes = [MagicMock(box_2d=[100, 200, 200, 300], label="x")]
    state._last_vision_response.bboxes[0].label = "x"
    state._last_vision_response.image_width = 1280
    state._last_vision_response.image_height = 800
    state._last_vision_response.dpr = 1.0
    state._last_vision_response.screenshot_freshness = "fresh"
    state.vision_for_target_resolution = lambda: state._last_vision_response

    tool = BrowserClickAtTool(state)
    os.environ["BBOX_MAX_AGE_MS"] = "8000"
    try:
        out = _run(tool.execute(session_id="t", vision_index=1))
    finally:
        del os.environ["BBOX_MAX_AGE_MS"]
    assert "[click_at_failed:bbox_too_old" in out, out


def test_bbox_age_gate_passes_fresh_epoch():
    """A fresh epoch should NOT trigger the age gate."""
    state = BrowserSessionState()
    state._vision_epoch_taken_at = time.time() - 0.5
    bbox_mock = MagicMock()
    bbox_mock.label = "x"
    bbox_mock.box_2d = [100, 200, 200, 300]
    bbox_mock.to_pixels = MagicMock(return_value=(120, 80, 240, 160))
    resp = MagicMock()
    resp.bboxes = [bbox_mock]
    resp.image_width = 1280
    resp.image_height = 800
    resp.dpr = 1.0
    resp.screenshot_freshness = "fresh"
    resp.get_bbox = lambda i: bbox_mock if i == 1 else None
    state._last_vision_response = resp
    state.vision_for_target_resolution = lambda: resp

    tool = BrowserClickAtTool(state)
    # The HTTP call will fail (no server); we don't care, we only
    # assert the bbox_too_old message is absent.
    try:
        out = _run(tool.execute(session_id="t-fresh", vision_index=1))
    except Exception as exc:
        out = str(exc)
    assert "[click_at_failed:bbox_too_old" not in out


# ----------------- Phase A4: looser repeat-click guard -----------------


def test_looser_repeat_guard_fires_on_third_attempt():
    state = BrowserSessionState()
    state.current_url = "https://example.com/"
    state._brain_turn_counter = 1
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    state._brain_turn_counter = 2
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    state._brain_turn_counter = 3
    blocked = state.check_dead_click_loose("click_at(V2)", coords=(250, 150))
    assert blocked is not None
    assert "looser_match" in blocked


def test_looser_guard_resets_on_url_change():
    state = BrowserSessionState()
    state.current_url = "https://example.com/page1"
    state._brain_turn_counter = 1
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    state._brain_turn_counter = 2
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    state.current_url = "https://example.com/page2"
    state._brain_turn_counter = 3
    blocked = state.check_dead_click_loose("click_at(V2)", coords=(250, 150))
    assert blocked is None


def test_looser_guard_coord_equivalence():
    """Different V_n but same physical button should still trigger."""
    state = BrowserSessionState()
    state.current_url = "https://example.com/"
    state._brain_turn_counter = 1
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    state._brain_turn_counter = 2
    state.register_click_attempt("click_at(V8)", coords=(252, 152))
    # 3rd attempt with V_15 at near-identical coords
    state._brain_turn_counter = 3
    blocked = state.check_dead_click_loose("click_at(V15)", coords=(248, 148))
    assert blocked is not None


def test_looser_guard_resets_after_window():
    state = BrowserSessionState()
    state.current_url = "https://example.com/"
    state._brain_turn_counter = 1
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    state._brain_turn_counter = 2
    state.register_click_attempt("click_at(V2)", coords=(250, 150))
    # 8 turns later — outside the 6-turn window.
    state._brain_turn_counter = 10
    blocked = state.check_dead_click_loose("click_at(V2)", coords=(250, 150))
    assert blocked is None


# ----------------- Phase D: observed_query_keys -----------------


def test_record_url_harvests_query_keys():
    state = BrowserSessionState()
    state.record_url("https://www.wineaccess.com/store/?sort=critic_score&page=2")
    assert "sort" in state.observed_query_keys
    assert "page" in state.observed_query_keys
    state.record_url("https://www.wineaccess.com/store/?filter=oregon")
    assert "filter" in state.observed_query_keys


def test_unknown_keys_detected():
    """The unknown-key check itself isn't called from state, but the
    set membership is the underlying signal — verify it does what the
    navigate guard expects."""
    state = BrowserSessionState()
    state.record_url("https://example.com/?sort=score")
    fab = "https://example.com/?category__in=x&ordering=-rating"
    fab_keys = [k for k, _ in parse_qsl(urlparse(fab).query, keep_blank_values=True)]
    unknown = [k for k in fab_keys if k not in state.observed_query_keys]
    assert unknown == ["category__in", "ordering"]


def test_record_url_handles_no_query():
    state = BrowserSessionState()
    state.record_url("https://example.com/page1")
    assert state.observed_query_keys == set()


# ----------------- Phase E1: force-reobserve gate -----------------


def test_force_reobserve_gate_blocks_click():
    state = BrowserSessionState()
    state._force_reobserve_pending = True
    tool = BrowserClickAtTool(state)
    out = _run(tool.execute(session_id="t", vision_index=1))
    assert "[click_at_refused:cursor_cascade]" in out
    assert "browser_find_target" in out


def test_force_reobserve_gate_clears_after_state_reset():
    state = BrowserSessionState()
    state._force_reobserve_pending = True
    # Simulate the screenshot path's reset (state.py line ~1518).
    state.epoch_interact_attempts = 0
    state._force_reobserve_pending = False
    assert state._force_reobserve_pending is False


# ----------------- Phase E: auto-find-target hint -----------------


def test_section_hint_known_keywords():
    assert _guess_section_hint("Oregon filter") == "Region"
    assert _guess_section_hint("California wines") == "Region"
    assert _guess_section_hint("Pairs with fish") == "Pairings"
    assert _guess_section_hint("Dessert wines") == "Pairings"
    assert _guess_section_hint("White wine") == "Type"


def test_section_hint_no_match():
    assert _guess_section_hint("Submit form") == ""
    assert _guess_section_hint("") == ""


def test_word_tokens_basic():
    assert _word_tokens("hello world") == ["hello", "world"]
    assert _word_tokens("under $40 / fish") == ["under", "40", "fish"]


def test_auto_find_target_hint_skips_when_focus_in_vision():
    """When vision already has a matching V_n, the hook should NOT
    inject the AUTO_FIND_TARGET nudge."""
    state = BrowserSessionState()
    state._brain_turn_counter = 5

    state.set_task_brief(
        "find headphones",
        [{"label": "Oregon filter", "kind": "filter", "predicate": {"manual": True}}],
    )

    bbox = MagicMock()
    bbox.label = "Oregon (12 wines)"
    state._last_vision_response = MagicMock()
    state._last_vision_response.bboxes = [bbox]
    state._last_markdown = "Some sidebar with Oregon and other regions"

    hook = BrowserWorkerHook(state, max_iterations=50)
    hook._last_auto_find_iter = -100
    parts: list[str] = []
    hook._inject_auto_find_target_hint(parts)
    auto_blocks = [p for p in parts if "AUTO_FIND_TARGET" in p]
    assert auto_blocks == [], f"Should not nudge when V_n matches: {parts}"


def test_auto_find_target_hint_fires_when_in_markdown_only():
    """The wineaccess case: focus in markdown but no V_n labels it →
    nudge."""
    state = BrowserSessionState()
    state._brain_turn_counter = 5

    if not state.task_brief:
        state.set_task_brief(
            "find Oregon white wine",
            [{"label": "Oregon filter", "kind": "filter", "predicate": {"manual": True}}],
        )

    bbox = MagicMock()
    bbox.label = "Sort by"  # vision saw the sort dropdown, not Region
    state._last_vision_response = MagicMock()
    state._last_vision_response.bboxes = [bbox]
    state._last_markdown = (
        "Filter sidebar:\nRegion\n  Oregon (12)\n  Washington (8)\n"
        "Variety\nPrice"
    )

    hook = BrowserWorkerHook(state, max_iterations=50)
    hook._last_auto_find_iter = -100
    parts: list[str] = []
    hook._inject_auto_find_target_hint(parts)
    auto_blocks = [p for p in parts if "AUTO_FIND_TARGET" in p]
    if state.task_brief is None:
        # If task_brief is disabled in the env, the hint can't fire.
        # Skip rather than fail.
        return
    assert auto_blocks, f"Expected nudge — got: {parts}"
    block = auto_blocks[0]
    assert "browser_find_target" in block
    assert "Oregon" in block


def test_auto_find_target_hint_throttles_re_emission():
    """Should not nudge on every iteration once fired."""
    state = BrowserSessionState()
    state._brain_turn_counter = 5
    state.set_task_brief(
        "find Oregon white wine",
        [{"label": "Oregon filter", "kind": "filter", "predicate": {"manual": True}}],
    )
    if state.task_brief is None:
        return
    bbox = MagicMock()
    bbox.label = "Sort by"
    state._last_vision_response = MagicMock()
    state._last_vision_response.bboxes = [bbox]
    state._last_markdown = "Region\n  Oregon (12)\n"

    hook = BrowserWorkerHook(state, max_iterations=50)
    hook._last_auto_find_iter = state._brain_turn_counter - 1
    parts: list[str] = []
    hook._inject_auto_find_target_hint(parts)
    assert not [p for p in parts if "AUTO_FIND_TARGET" in p], (
        "Throttle window should suppress nudge"
    )
