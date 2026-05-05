"""Tests for CURSOR_ONLY_MODE — refusal of DOM-index click/type when
the orchestrator decomposed a multi-condition query.

The trace pattern this targets: brain reflexively reaches for
browser_click([N]) and browser_type([N]) even though DOM indices drift
and don't fire humanized cursor events. With task_brief set, both
tools refuse and direct the brain to browser_click_at(V_n) /
browser_type_at(V_n). browser_click_selector remains allowed as a
second-tier fallback for stable CSS hooks.
"""

from __future__ import annotations

import asyncio
import os

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.session_tools.tools.cursor import (
    BrowserClickTool,
    BrowserTypeTool,
)
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
    # Match test_type_verify's pattern — asyncio.run() closes the loop
    # and breaks subsequent tests in the same process that rely on
    # asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_browser_click_refused_with_task_brief():
    s = _state_with_brief()
    tool = BrowserClickTool(s)
    out = _run(tool.execute(session_id="session-test", index=47))
    assert isinstance(out, str)
    assert "[CURSOR_ONLY_MODE]" in out
    assert "browser_click_at(vision_index=V_n)" in out
    assert "browser_click_selector" in out


def test_browser_type_refused_with_task_brief():
    s = _state_with_brief()
    tool = BrowserTypeTool(s)
    out = _run(tool.execute(session_id="session-test", index=33, text="40"))
    assert isinstance(out, str)
    assert "[CURSOR_ONLY_MODE]" in out
    assert "browser_type_at(vision_index=V_n" in out
    # Original text must appear in the directive so the brain doesn't
    # have to remember what it was about to type.
    assert "'40'" in out


def test_cursor_only_mode_off_via_env_allows_call():
    """CURSOR_ONLY_MODE=0 disables the refusal globally — used for
    tests / legacy single-condition workflows that need DOM clicks."""
    s = _state_with_brief()
    tool = BrowserClickTool(s)
    os.environ["CURSOR_ONLY_MODE"] = "0"
    try:
        # Without HTTP backend the click will eventually raise — we
        # just need to confirm the early CURSOR_ONLY_MODE refusal is
        # NOT what stopped it. Check that the early-return string
        # shape isn't returned.
        try:
            out = _run(tool.execute(session_id="session-test", index=47))
        except Exception:
            return  # expected: HTTP backend unavailable in test
        assert "[CURSOR_ONLY_MODE]" not in out
    finally:
        del os.environ["CURSOR_ONLY_MODE"]


def test_no_refusal_when_no_task_brief():
    """Single-condition mode (no task_brief) keeps the index path so
    legacy callers still work."""
    s = _state_without_brief()
    tool = BrowserClickTool(s)
    try:
        out = _run(tool.execute(session_id="session-test", index=47))
    except Exception:
        return  # HTTP backend unavailable in test
    assert "[CURSOR_ONLY_MODE]" not in out


def test_refusal_records_step():
    """The refusal should be visible in step_history so the brief's
    attempt ledger sees it."""
    s = _state_with_brief()
    tool = BrowserClickTool(s)
    _run(tool.execute(session_id="session-test", index=47))
    assert s.step_history
    last = s.step_history[-1]
    assert last["tool"] == "browser_click"
    assert "CURSOR_ONLY_MODE" in last["result"]
