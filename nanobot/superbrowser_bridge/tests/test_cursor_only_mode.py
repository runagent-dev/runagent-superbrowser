"""Tests for CURSOR_ONLY_MODE — refusal of DOM-index `browser_type`
when the orchestrator decomposed a multi-condition query.

The DOM-index `browser_click` and CSS-selector `browser_click_selector`
were removed in favor of vision-bbox-only `browser_click_at`, so the
former CURSOR_ONLY_MODE block on BrowserClickTool is gone. The
remaining DOM-index tool that warrants this refusal is `browser_type`:
when task_brief is set, DOM indices drift and keystrokes can land on an
adjacent non-input, so the brain is forced through
`browser_type_at(vision_index=V_n)`.
"""

from __future__ import annotations

import asyncio
import os

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.session_tools.tools.cursor import BrowserTypeTool
from superbrowser_bridge.task_brief import TaskBrief


def _state_with_brief() -> BrowserSessionState:
    s = BrowserSessionState()
    s.session_id = "session-test"
    s.task_brief = TaskBrief("q", [
        {"label": "White wine type", "kind": "filter", "predicate": {"manual": True}},
    ])
    return s


def _state_without_brief() -> BrowserSessionState:
    s = BrowserSessionState()
    s.session_id = "session-test"
    return s


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_browser_type_refused_with_task_brief():
    s = _state_with_brief()
    tool = BrowserTypeTool(s)
    out = _run(tool.execute(session_id="session-test", index=33, text="40"))
    assert isinstance(out, str)
    assert "[CURSOR_ONLY_MODE]" in out
    assert "browser_type_at(vision_index=V_n" in out
    assert "'40'" in out


def test_cursor_only_mode_off_via_env_allows_call():
    """CURSOR_ONLY_MODE=0 disables the refusal globally — used for
    legacy single-condition workflows that need DOM-index typing."""
    s = _state_with_brief()
    tool = BrowserTypeTool(s)
    os.environ["CURSOR_ONLY_MODE"] = "0"
    try:
        try:
            out = _run(tool.execute(session_id="session-test", index=33, text="40"))
        except Exception:
            return  # expected: HTTP backend unavailable in test
        assert "[CURSOR_ONLY_MODE]" not in out
    finally:
        del os.environ["CURSOR_ONLY_MODE"]


def test_no_refusal_when_no_task_brief():
    """Single-condition mode (no task_brief) keeps the index path so
    legacy callers still work."""
    s = _state_without_brief()
    tool = BrowserTypeTool(s)
    try:
        out = _run(tool.execute(session_id="session-test", index=33, text="40"))
    except Exception:
        return  # HTTP backend unavailable in test
    assert "[CURSOR_ONLY_MODE]" not in out


def test_refusal_records_step():
    """The refusal should be visible in step_history so the brief's
    attempt ledger sees it."""
    s = _state_with_brief()
    tool = BrowserTypeTool(s)
    _run(tool.execute(session_id="session-test", index=33, text="40"))
    assert s.step_history
    last = s.step_history[-1]
    assert last["tool"] == "browser_type"
    assert "CURSOR_ONLY_MODE" in last["result"]
