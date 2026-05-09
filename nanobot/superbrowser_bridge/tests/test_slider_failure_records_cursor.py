"""Unit test: slider-tool failures populate the cursor-failure ledger.

Before the patch, slider tools never called state.record_cursor_failure().
Result: when slider attempts failed, the run_script lockout
(scripting.py:165-198) saw zero cursor-strategy failures, and the agent
got stuck — slider→fail→run_script→blocked, with no path forward.

This test drives each slider tool through every failure path with the
HTTP layer monkey-patched to fake errors, then asserts that
state.cursor_failure_strategies grew by the expected strategy keys.

No server required — runs in pure pytest.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.slider import (
    BrowserSetSliderTool,
    BrowserSetSliderAtTool,
    BrowserListSliderHandlesTool,
    BrowserDragSliderUntilTool,
)


# Anyio plugin runs each @pytest.mark.anyio test on every backend by
# default; pin to asyncio so we don't require trio installed.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeHTTPResponse:
    """Minimal stand-in for httpx.Response for the slider tools."""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None,
                 text_body: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text_body or "fake error"

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_state() -> BrowserSessionState:
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    s.current_url = "https://example.test/"
    return s


@pytest.mark.anyio
async def test_set_slider_http_error_records_strategy() -> None:
    s = _make_state()
    tool = BrowserSetSliderTool(s)
    fake_resp = _FakeHTTPResponse(500, {"error": "boom"}, "boom")
    with patch(
        "superbrowser_bridge.session_tools.tools.slider._request_with_backoff",
        return_value=fake_resp,
    ):
        out = await tool.execute(
            session_id="s1",
            selector="input[name=monthly]",
            value_json="100",
        )
    assert "[set_slider_failed]" in out
    assert "slider_set" in s.cursor_failure_strategies, (
        f"expected 'slider_set' in ledger after HTTP failure; got {s.cursor_failure_strategies!r}"
    )


@pytest.mark.anyio
async def test_set_slider_unresolved_records_strategy() -> None:
    s = _make_state()
    tool = BrowserSetSliderTool(s)
    fake_resp = _FakeHTTPResponse(200, {
        "outcome": {"strategy": "unresolved", "error": "selector not found"},
        "url": "https://example.test/",
    })
    with patch(
        "superbrowser_bridge.session_tools.tools.slider._request_with_backoff",
        return_value=fake_resp,
    ):
        out = await tool.execute(
            session_id="s1",
            selector="#missing",
            value_json="10",
        )
    assert "[set_slider_failed]" in out
    assert "slider_set" in s.cursor_failure_strategies


@pytest.mark.anyio
async def test_set_slider_at_no_vision_records_strategy() -> None:
    s = _make_state()
    tool = BrowserSetSliderAtTool(s)
    # vision_for_target_resolution returns None when no vision cached.
    out = await tool.execute(session_id="s1", vision_index=1, value=300.0)
    assert "[set_slider_at_failed:no_vision]" in out
    assert "slider_set_at" in s.cursor_failure_strategies


@pytest.mark.anyio
async def test_list_slider_handles_empty_records_strategy() -> None:
    s = _make_state()
    tool = BrowserListSliderHandlesTool(s)
    fake_resp = _FakeHTTPResponse(200, {"handles": []})
    with patch(
        "superbrowser_bridge.session_tools.tools.slider._request_with_backoff",
        return_value=fake_resp,
    ):
        out = await tool.execute(session_id="s1")
    assert "[list_slider_handles:empty]" in out
    assert "slider_list" in s.cursor_failure_strategies


@pytest.mark.anyio
async def test_drag_slider_until_no_handle_records_strategy() -> None:
    s = _make_state()
    tool = BrowserDragSliderUntilTool(s)
    # No vision_index, no handle_bbox_json, no label_hint → resolver fails.
    # Bypass feedback gate and vision-sync gate via stubs.
    async def _no_gate(_name: str) -> str | None:
        return None
    async def _ok_sync(*, reason: str = "") -> str | None:  # noqa: ARG001
        return None
    s.ensure_vision_synced = _ok_sync  # type: ignore[assignment]
    with patch(
        "superbrowser_bridge.session_tools.tools.slider._feedback_gate",
        side_effect=_no_gate,
    ):
        out = await tool.execute(session_id="s1", target_value=300.0)
    assert "[drag_slider_until_failed:no_handle]" in out
    assert "slider_drag_until" in s.cursor_failure_strategies


@pytest.mark.anyio
async def test_two_distinct_failures_unlock_run_script_threshold() -> None:
    """End-to-end: two different slider failures push the lockout count
    to ≥ 2, the threshold scripting.py uses to unblock mutating JS. This
    is the core behavior change — slider failures now count."""
    s = _make_state()
    set_tool = BrowserSetSliderTool(s)
    list_tool = BrowserListSliderHandlesTool(s)
    fail_resp_set = _FakeHTTPResponse(200, {
        "outcome": {"strategy": "unresolved", "error": "no match"},
        "url": "https://example.test/",
    })
    fail_resp_list = _FakeHTTPResponse(200, {"handles": []})
    with patch(
        "superbrowser_bridge.session_tools.tools.slider._request_with_backoff",
        side_effect=[fail_resp_set, fail_resp_list],
    ):
        await set_tool.execute(session_id="s1", selector="#a", value_json="1")
        await list_tool.execute(session_id="s1")
    distinct = len(s.cursor_failure_strategies)
    assert distinct >= 2, (
        f"expected ≥ 2 distinct strategies after 2 different slider failures; "
        f"got {distinct}: {s.cursor_failure_strategies!r}"
    )
    assert {"slider_set", "slider_list"}.issubset(s.cursor_failure_strategies)
