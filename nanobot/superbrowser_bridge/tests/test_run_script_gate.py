"""Phase 4 hard-gate tests for browser_run_script.

The gate refuses run_script execution until the brain has attempted at
least RUN_SCRIPT_MIN_CURSOR_ATTEMPTS (default 3) cursor interactions
on the current vision epoch. A fresh screenshot resets the counter.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.session_tools.tools import BrowserRunScriptTool


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_fresh_epoch_blocks_run_script():
    os.environ["RUN_SCRIPT_EPOCH_GATE"] = "1"
    try:
        state = BrowserSessionState()
        assert state.epoch_interact_attempts == 0

        tool = BrowserRunScriptTool(state)
        out = _run(
            tool.execute(
                session_id="test-1",
                script="document.querySelectorAll('a').length",
            )
        )
        assert "[run_script_blocked:epoch_cursor_quota]" in out
        assert "0 of 3" in out
        assert "browser_click_selector" in out
    finally:
        del os.environ["RUN_SCRIPT_EPOCH_GATE"]


def test_one_cursor_attempt_still_blocks():
    """One attempt is no longer enough — the new threshold is 3."""
    os.environ["RUN_SCRIPT_EPOCH_GATE"] = "1"
    try:
        state = BrowserSessionState()
        state.epoch_interact_attempts = 1

        tool = BrowserRunScriptTool(state)
        out = _run(
            tool.execute(
                session_id="test-1cursor",
                script="document.title",
            )
        )
        assert "[run_script_blocked:epoch_cursor_quota]" in out
        assert "1 of 3" in out
    finally:
        del os.environ["RUN_SCRIPT_EPOCH_GATE"]


def test_two_cursor_attempts_still_block():
    os.environ["RUN_SCRIPT_EPOCH_GATE"] = "1"
    try:
        state = BrowserSessionState()
        state.epoch_interact_attempts = 2

        tool = BrowserRunScriptTool(state)
        out = _run(
            tool.execute(
                session_id="test-2cursors",
                script="document.title",
            )
        )
        assert "[run_script_blocked:epoch_cursor_quota]" in out
        assert "2 of 3" in out
    finally:
        del os.environ["RUN_SCRIPT_EPOCH_GATE"]


def test_three_cursor_attempts_clears_gate():
    os.environ["RUN_SCRIPT_EPOCH_GATE"] = "1"
    try:
        state = BrowserSessionState()
        state.epoch_interact_attempts = 3

        tool = BrowserRunScriptTool(state)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "result": 7,
            "logs": [],
            "duration": 12,
        }
        mock_resp.raise_for_status = lambda: None
        mock_resp.status_code = 200

        async def _fake_request(method, url, **kw):
            return mock_resp

        with patch(
            "superbrowser_bridge.session_tools.tools.eval._request_with_backoff",
            new=AsyncMock(side_effect=_fake_request),
        ), patch(
            "superbrowser_bridge.session_tools.tools.eval._fetch_elements",
            new=AsyncMock(return_value=""),
        ):
            out = _run(
                tool.execute(
                    session_id="test-3cursors",
                    script="document.querySelectorAll('a').length",
                )
            )
        assert "[run_script_blocked:epoch_cursor_quota]" not in out
    finally:
        del os.environ["RUN_SCRIPT_EPOCH_GATE"]


def test_threshold_env_override():
    """RUN_SCRIPT_MIN_CURSOR_ATTEMPTS lowers/raises the bar."""
    os.environ["RUN_SCRIPT_EPOCH_GATE"] = "1"
    os.environ["RUN_SCRIPT_MIN_CURSOR_ATTEMPTS"] = "1"
    try:
        state = BrowserSessionState()
        state.epoch_interact_attempts = 1

        tool = BrowserRunScriptTool(state)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "result": 0,
            "logs": [],
            "duration": 5,
        }
        mock_resp.raise_for_status = lambda: None
        mock_resp.status_code = 200

        async def _fake_request(method, url, **kw):
            return mock_resp

        with patch(
            "superbrowser_bridge.session_tools.tools.eval._request_with_backoff",
            new=AsyncMock(side_effect=_fake_request),
        ), patch(
            "superbrowser_bridge.session_tools.tools.eval._fetch_elements",
            new=AsyncMock(return_value=""),
        ):
            out = _run(
                tool.execute(
                    session_id="test-override",
                    script="document.title",
                )
            )
        # With threshold lowered to 1, 1 attempt should clear it.
        assert "[run_script_blocked:epoch_cursor_quota]" not in out
    finally:
        del os.environ["RUN_SCRIPT_EPOCH_GATE"]
        del os.environ["RUN_SCRIPT_MIN_CURSOR_ATTEMPTS"]


def test_screenshot_path_resets_gate_field():
    """The state-level reset (state.py:1518 / 1600) zeroes the gate.
    Test that the field is plumbed correctly: writes survive, resets
    via direct assignment work, and reset_per_session zeros it."""
    state = BrowserSessionState()
    state.epoch_interact_attempts = 5
    assert state.epoch_interact_attempts == 5
    state.reset_per_session()
    assert state.epoch_interact_attempts == 0


def test_gate_disabled_by_env_passes_through():
    os.environ["RUN_SCRIPT_EPOCH_GATE"] = "0"
    try:
        state = BrowserSessionState()
        assert state.epoch_interact_attempts == 0

        tool = BrowserRunScriptTool(state)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "result": 0,
            "logs": [],
            "duration": 5,
        }
        mock_resp.raise_for_status = lambda: None
        mock_resp.status_code = 200

        async def _fake_request(method, url, **kw):
            return mock_resp

        with patch(
            "superbrowser_bridge.session_tools.tools.eval._request_with_backoff",
            new=AsyncMock(side_effect=_fake_request),
        ), patch(
            "superbrowser_bridge.session_tools.tools.eval._fetch_elements",
            new=AsyncMock(return_value=""),
        ):
            out = _run(
                tool.execute(
                    session_id="test-disabled",
                    script="document.title",
                )
            )
        assert "[run_script_blocked:epoch_cursor_quota]" not in out
    finally:
        del os.environ["RUN_SCRIPT_EPOCH_GATE"]
