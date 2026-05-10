"""Unit test: `browser_click_at(vision_index=...)` HARD-rejects on an
invalidated vision epoch and forces the brain to re-screenshot.

An earlier draft of this fix attempted an in-tool "recovery" that
refreshed the vision response and re-resolved V_n against it. That was
wrong: V_n is a POSITIONAL index into a ranked bbox list, and the
ranking shifts when the page mutates. After recovery, V_5 in the new
response is a different element than V_5 in the old response — so the
recovered click landed on the wrong button silently, causing the brain
to chase phantom state changes for several turns before giving up.

The correct behaviour is: hard-reject, return an explicit error, let
the brain call `browser_screenshot` to see the new bbox numbering, and
re-pick V_n on that fresh snapshot. The extra turn is the right cost.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.click import (
    BrowserClickAtTool,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_state_with_invalidated_epoch() -> BrowserSessionState:
    s = BrowserSessionState()
    s._brain_turn_counter = 5
    s.current_url = "https://example.test/"
    s._vision_epoch_id = 3
    s._vision_epoch_response = None
    s._last_vision_response = None
    s.screenshot_budget = 4
    return s


@pytest.mark.anyio
async def test_click_at_rejects_on_invalidated_epoch() -> None:
    """Invalidated epoch must produce the explicit error string so the
    brain knows to re-screenshot. Critically, the tool MUST NOT
    silently dispatch a click against the live vision response — V_n
    in the new ranking points to a different element."""
    s = _make_state_with_invalidated_epoch()
    tool = BrowserClickAtTool(s)

    async def _fail_if_dispatched(*_a, **_kw):
        raise AssertionError(
            "Click dispatched despite invalidated epoch — would have "
            "clicked the wrong element. The hard-reject was bypassed."
        )

    with patch.object(s, "ensure_vision_synced", return_value=None), \
         patch(
            "superbrowser_bridge.session_tools.tools.click._request_with_backoff",
            side_effect=_fail_if_dispatched,
        ):
        out = await tool.execute(session_id="sess1", vision_index=4)

    assert "[click_at_failed:epoch_invalidated]" in out
    assert "browser_screenshot" in out, (
        "error message must instruct the brain to re-screenshot"
    )
    # Screenshot budget untouched — no implicit screenshot fired.
    assert s.screenshot_budget == 4


@pytest.mark.anyio
async def test_click_at_raw_coords_unaffected_by_invalidated_epoch() -> None:
    """The hard-reject is scoped to vision_index. Raw `(x,y)` coords
    are direct viewport addresses and don't depend on the ranking, so
    they must pass through the gate normally."""
    s = _make_state_with_invalidated_epoch()
    tool = BrowserClickAtTool(s)

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = ""
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "clicked": {"x": 100, "y": 200},
                "url": "https://example.test/",
                "elements": "",
                "fingerprints": {},
                "snapped": True,
            }

    async def _fake_dispatch(*_a, **_kw):
        return _FakeResp()

    with patch.object(s, "ensure_vision_synced", return_value=None), \
         patch.object(s, "advance_observation_token"), \
         patch(
            "superbrowser_bridge.session_tools.tools.click._request_with_backoff",
            side_effect=_fake_dispatch,
        ), patch(
            "superbrowser_bridge.session_tools.tools.click._schedule_vision_prefetch",
            return_value=None,
        ), patch(
            "superbrowser_bridge.session_tools.tools.click._append_fresh_vision",
            side_effect=lambda _t, caption, **_kw: caption,
        ):
        out = await tool.execute(session_id="sess1", x=100.0, y=200.0)

    assert "[click_at_failed:epoch_invalidated]" not in str(out)
