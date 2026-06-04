"""Unit tests: the heavy-page run_script guard releases after cursor
tools have demonstrably failed.

Background: on heavy pages (search_results, product_listing, …) and
hard bot-detection domains, BrowserRunScriptTool used to block a
mutating script UNCONDITIONALLY — deadlocking the worker when cursor
tools also silently no-op'd (the petfinder case). The guard now consults
the cursor-failure ledger via state.cursor_failures_released(): it lifts
the lockout once ≥3 distinct OR ≥5 total cursor failures have accrued
within a recent turn window, while still blocking on a fresh page so the
cursor-first contract is preserved.

These tests cover both the pure predicate and the end-to-end gate
decision (HTTP layer mocked — no server required).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.scripting import BrowserRunScriptTool


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeHTTPResponse:
    """Minimal httpx.Response stand-in for the script endpoint."""

    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_state(*, page_type: str = "search_results", url: str = "https://example.test/search/") -> BrowserSessionState:
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    s.current_url = url
    s._last_vision_response = SimpleNamespace(page_type=page_type, bboxes=[])
    return s


def _add_failures(s: BrowserSessionState, strategies: list[str], *, at_turn: int) -> None:
    """Record cursor failures stamped at `at_turn` (record_cursor_failure
    uses the current _brain_turn_counter)."""
    s._brain_turn_counter = at_turn
    for i, strat in enumerate(strategies):
        s.record_cursor_failure(strategy=strat, target=f"t{i}", reason="no_effect")


# --------------------------------------------------------------------------
# Pure predicate: state.cursor_failures_released
# --------------------------------------------------------------------------

def test_released_empty_ledger() -> None:
    s = _make_state()
    released, distinct, total = s.cursor_failures_released()
    assert released is False and distinct == 0 and total == 0


def test_released_three_distinct() -> None:
    s = _make_state()
    _add_failures(s, ["click_at", "click", "click_selector"], at_turn=5)
    released, distinct, total = s.cursor_failures_released()
    assert released is True and distinct == 3 and total == 3


def test_released_five_total_same_strategy() -> None:
    """The petfinder shape: same tool (click_at) hammered with no effect.
    distinct stays at 1 but the 5-total safety net opens the hatch."""
    s = _make_state()
    _add_failures(s, ["click_at"] * 5, at_turn=5)
    released, distinct, total = s.cursor_failures_released()
    assert released is True and distinct == 1 and total == 5


def test_not_released_two_distinct() -> None:
    s = _make_state()
    _add_failures(s, ["click_at", "click"], at_turn=5)
    released, distinct, total = s.cursor_failures_released()
    assert released is False and distinct == 2 and total == 2


def test_not_released_when_failures_aged_out() -> None:
    """Old failures fall outside the window and don't keep the hatch open."""
    s = _make_state()
    _add_failures(s, ["click_at"] * 6, at_turn=0)
    s._brain_turn_counter = 100  # far past the default window of 10
    released, distinct, total = s.cursor_failures_released()
    assert released is False and distinct == 0 and total == 0


def test_released_respects_custom_thresholds() -> None:
    s = _make_state()
    _add_failures(s, ["click_at", "click"], at_turn=3)
    # Lower min_distinct to 2 → now releases.
    released, _, _ = s.cursor_failures_released(min_distinct=2)
    assert released is True


# --------------------------------------------------------------------------
# End-to-end gate: BrowserRunScriptTool.execute
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_blocks_on_fresh_heavy_page() -> None:
    """No cursor failures yet → guard still blocks, and counters are NOT
    incremented (proves the early-return path)."""
    s = _make_state(page_type="search_results")
    tool = BrowserRunScriptTool(s)
    out = await tool.execute(session_id="s1", script="x()", mutates=True)
    assert out.startswith("[run_script_blocked:bot_detection_risk]")
    assert s.consecutive_script_calls == 0
    assert s.recent_run_script_outcomes == []


@pytest.mark.anyio
async def test_releases_after_three_distinct_failures() -> None:
    s = _make_state(page_type="search_results")
    _add_failures(s, ["click_at", "click", "click_selector"], at_turn=4)
    tool = BrowserRunScriptTool(s)
    fake = _FakeHTTPResponse(200, {"success": True, "result": "ok", "logs": [], "duration": 7})
    with patch(
        "superbrowser_bridge.session_tools.tools.scripting._request_with_backoff",
        return_value=fake,
    ), patch(
        "superbrowser_bridge.session_tools.tools.scripting._fetch_elements",
        return_value="",
    ):
        out = await tool.execute(session_id="s1", script="document.title", mutates=True)
    assert out.startswith("[run_script_released:cursor_first_exhausted]")
    assert "browser_navigate" in out
    # Fall-through ran: the script executed and counters advanced.
    assert s.consecutive_script_calls == 1
    assert s.recent_run_script_outcomes == [True]
    assert "Result: ok" in out


@pytest.mark.anyio
async def test_releases_after_five_total_failures() -> None:
    """The petfinder safety net: 5 same-strategy no-effect clicks release."""
    s = _make_state(page_type="search_results")
    _add_failures(s, ["click_at"] * 5, at_turn=6)
    tool = BrowserRunScriptTool(s)
    fake = _FakeHTTPResponse(200, {"success": True, "result": "done", "logs": [], "duration": 3})
    with patch(
        "superbrowser_bridge.session_tools.tools.scripting._request_with_backoff",
        return_value=fake,
    ), patch(
        "superbrowser_bridge.session_tools.tools.scripting._fetch_elements",
        return_value="",
    ):
        out = await tool.execute(session_id="s1", script="document.title", mutates=True)
    assert out.startswith("[run_script_released:cursor_first_exhausted]")


@pytest.mark.anyio
async def test_still_blocks_when_failures_aged_out() -> None:
    s = _make_state(page_type="search_results")
    _add_failures(s, ["click_at"] * 6, at_turn=0)
    s._brain_turn_counter = 100
    tool = BrowserRunScriptTool(s)
    out = await tool.execute(session_id="s1", script="x()", mutates=True)
    assert out.startswith("[run_script_blocked:bot_detection_risk]")


@pytest.mark.anyio
async def test_hard_domain_releases_with_stronger_warning() -> None:
    """Hard bot-detection domains release too (per decision), but carry a
    blunter navigate-first warning."""
    s = _make_state(page_type="other", url="https://www.zillow.com/homes/for_sale/")
    _add_failures(s, ["click_at", "click", "click_selector"], at_turn=4)
    tool = BrowserRunScriptTool(s)
    fake = _FakeHTTPResponse(200, {"success": True, "result": "ok", "logs": [], "duration": 5})
    with patch(
        "superbrowser_bridge.session_tools.tools.scripting._request_with_backoff",
        return_value=fake,
    ), patch(
        "superbrowser_bridge.session_tools.tools.scripting._fetch_elements",
        return_value="",
    ):
        out = await tool.execute(session_id="s1", script="document.title", mutates=True)
    assert out.startswith("[run_script_released:cursor_first_exhausted]")
    assert "high-detection list" in out


@pytest.mark.anyio
async def test_guard_disabled_env_executes_regardless() -> None:
    s = _make_state(page_type="search_results")
    tool = BrowserRunScriptTool(s)
    fake = _FakeHTTPResponse(200, {"success": True, "result": "ok", "logs": [], "duration": 5})
    with patch.dict(os.environ, {"RUN_SCRIPT_HEAVY_PAGE_GUARD": "0"}), patch(
        "superbrowser_bridge.session_tools.tools.scripting._request_with_backoff",
        return_value=fake,
    ), patch(
        "superbrowser_bridge.session_tools.tools.scripting._fetch_elements",
        return_value="",
    ):
        out = await tool.execute(session_id="s1", script="document.title", mutates=True)
    assert "[run_script_blocked" not in out
    assert "[run_script_released" not in out
    assert "Result: ok" in out
