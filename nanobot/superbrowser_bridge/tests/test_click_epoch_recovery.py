"""Unit test: `browser_click_at(vision_index=...)` auto-recovers from an
invalidated vision epoch instead of returning [click_at_failed:epoch_invalidated].

Before the fix: when a previous click set mutation_delta >
MUTATION_DIRTY_THRESHOLD, `_vision_epoch_response` was reset to None
and any subsequent `vision_index` click hit a hard gate that forced
the brain to call `browser_screenshot` manually.

After the fix: the tool refreshes the vision epoch internally (via
_schedule_vision_prefetch + freeze_vision_epoch) and retries the
resolution. If V_n still doesn't resolve, the original error returns —
otherwise execution falls through to the normal dispatch path.

This test exercises both branches: recovered (fresh response contains
V_n) and unrecovered (fresh response missing V_n).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.click import (
    BrowserClickAtTool,
    _attempt_epoch_recovery,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_state_with_invalidated_epoch() -> BrowserSessionState:
    """Build a state where the epoch was previously frozen and then
    invalidated by a high-mutation click."""
    s = BrowserSessionState()
    s._brain_turn_counter = 5
    s.current_url = "https://example.test/"
    # Simulate "epoch was frozen at some point" — id > 0 means an
    # epoch was taken; `_vision_epoch_response = None` means it was
    # invalidated.
    s._vision_epoch_id = 3
    s._vision_epoch_response = None
    s._last_vision_response = None
    s.screenshot_budget = 4  # Recovery must NOT decrement this.
    return s


def _fake_vision_response_with_bbox(index: int):
    """Build a stub vision response whose `get_bbox(n)` returns a fake
    bbox iff `n == index`. Mirrors the minimum surface area the click
    path reads off the response object."""
    bbox = SimpleNamespace(
        x0=100.0, y0=100.0, x1=200.0, y1=150.0,
        label="Submit", confidence=0.95,
        role_in_scene="target", intent_relevant=True,
        clickable=True, is_active=False,
        to_pixels=lambda iw, ih, dpr=1.0: (100, 100, 200, 150),
    )
    resp = SimpleNamespace(
        bboxes=[bbox],
        screenshot_freshness="fresh",
        summary="ok",
        image_width=1280,
        image_height=720,
        dpr=1.0,
        get_bbox=lambda n: bbox if n == index else None,
    )
    return resp, bbox


# ---------------- _attempt_epoch_recovery -----------------------

def _make_fake_task(state: BrowserSessionState, resp) -> "asyncio.Task":
    """Return an awaitable that, when awaited, stamps the fake response
    onto state._last_vision_response (mirroring what the real prefetch
    `_run()` does) and returns the response."""
    import asyncio

    async def _run():
        state._last_vision_response = resp
        return resp

    return asyncio.ensure_future(_run())


@pytest.mark.anyio
async def test_recovery_succeeds_when_vn_present() -> None:
    s = _make_state_with_invalidated_epoch()
    resp, _bbox = _fake_vision_response_with_bbox(2)
    fake_task = _make_fake_task(s, resp)

    with patch(
        "superbrowser_bridge.session_tools.tools.click._schedule_vision_prefetch",
        return_value=fake_task,
    ):
        ok = await _attempt_epoch_recovery(s, "sess1", 2)

    assert ok is True
    # Recovery re-froze the epoch.
    assert s._vision_epoch_response is resp
    # Budget unaffected.
    assert s.screenshot_budget == 4


@pytest.mark.anyio
async def test_recovery_returns_false_when_vn_missing() -> None:
    s = _make_state_with_invalidated_epoch()
    resp, _bbox = _fake_vision_response_with_bbox(2)
    fake_task = _make_fake_task(s, resp)

    with patch(
        "superbrowser_bridge.session_tools.tools.click._schedule_vision_prefetch",
        return_value=fake_task,
    ):
        # V_5 isn't in the fresh response (which only has V_2).
        ok = await _attempt_epoch_recovery(s, "sess1", 5)

    assert ok is False


@pytest.mark.anyio
async def test_recovery_returns_false_when_prefetch_disabled() -> None:
    s = _make_state_with_invalidated_epoch()

    with patch(
        "superbrowser_bridge.session_tools.tools.click._schedule_vision_prefetch",
        return_value=None,
    ):
        ok = await _attempt_epoch_recovery(s, "sess1", 2)

    assert ok is False
    # Budget still untouched.
    assert s.screenshot_budget == 4


# ---------------- end-to-end BrowserClickAtTool ----------------

@pytest.mark.anyio
async def test_click_at_returns_error_when_recovery_fails() -> None:
    """When recovery can't resolve V_n, the existing error message
    must still surface so the brain knows to re-screenshot."""
    s = _make_state_with_invalidated_epoch()
    tool = BrowserClickAtTool(s)

    async def _no_op(*_a, **_kw):
        return None

    with patch.object(s, "ensure_vision_synced", return_value=None), \
         patch(
            "superbrowser_bridge.session_tools.tools.click._schedule_vision_prefetch",
            return_value=None,
        ):
        out = await tool.execute(session_id="sess1", vision_index=4)

    assert "[click_at_failed:epoch_invalidated]" in out
    # Budget still untouched after the failed recovery.
    assert s.screenshot_budget == 4
