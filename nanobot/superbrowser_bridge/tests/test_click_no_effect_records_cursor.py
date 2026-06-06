"""Unit test: a silent (no_effect) cursor click feeds the cursor-failure
ledger.

Before the patch, a click that produced zero DOM/url/focus delta
(`mutation_delta=0`) was tagged `[no_effect:...]` for the brain but NEVER
recorded in state.cursor_failure_strategies / cursor_failure_records.
That's the petfinder deadlock: the run_script escape hatch counts ledger
records (see test_run_script_release.py), so silent clicks could never
open it.

This drives browser_click to the no_effect path with the HTTP + vision
collaborators stubbed, and asserts the ledger grows by exactly one record.

Coverage note: browser_click_at uses the IDENTICAL `if _tagged_no_effect:
record_cursor_failure(strategy="click_at", ...)` pattern (click.py), so
this test covers the recording mechanism for both. The only click_at-
specific extra is the `if not _expected_label` dedup guard that defers to
vision_pipeline's label_still_visible record — a non-load-bearing
refinement (worst case if it regressed: the hatch opens marginally
earlier on click_at-with-label loops). Driving the full click_at execute
would require a complete VisionResponse with get_bbox/scene/layered bbox
attributes (nanobot.vision_agent isn't importable from the test path), so
it isn't exercised here.

No server required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.click import BrowserClickTool

_CLICK = "superbrowser_bridge.session_tools.tools.click"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeHTTPResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {"content-type": "application/json"}
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_state() -> BrowserSessionState:
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    s.current_url = "https://example.test/search/"
    return s


async def _fake_append(_task: Any, text: str, *args: Any, **kwargs: Any) -> str:
    return text


def _no_effect_response() -> _FakeHTTPResponse:
    """A 200 click response that resolved to an element but moved nothing —
    the silent no_effect shape (snapped=False keeps the click-ladder off
    since there are no resolved coords)."""
    return _FakeHTTPResponse(200, {
        "effect": {"mutation_delta": 0, "url_changed": False, "focused_changed": False},
        "snap": {"snapped": False},
        "url": "https://example.test/search/",
    })


def _click_patches() -> list[Any]:
    """Collaborator stubs so a click reaches the no_effect block without
    touching the network or the vision pipeline. The vision-cache bust
    inside the no_effect block does `from nanobot.vision_agent.client
    import get_vision_agent`, which isn't importable from the test path —
    its own try/except swallows that, so no patch is needed for it."""
    return [
        patch(f"{_CLICK}._request_with_backoff", return_value=_no_effect_response()),
        patch(f"{_CLICK}._fetch_elements", return_value=""),
        patch(f"{_CLICK}._schedule_vision_prefetch", return_value=None),
        patch(f"{_CLICK}._append_fresh_vision", new=_fake_append),
        patch(f"{_CLICK}.lookup_postcondition", return_value=None),
        patch(f"{_CLICK}.run_click_with_ladder", return_value=""),
    ]


@pytest.mark.anyio
async def test_browser_click_no_effect_records_cursor_failure() -> None:
    s = _make_state()
    tool = BrowserClickTool(s)
    cms = _click_patches()
    for cm in cms:
        cm.start()
    try:
        out = await tool.execute(session_id="s1", index=3)
    finally:
        for cm in cms:
            cm.stop()
    assert "[no_effect:browser_click]" in out
    assert "click" in s.cursor_failure_strategies
    recs = [r for r in s.cursor_failure_records if r["strategy"] == "click"]
    assert len(recs) == 1, f"expected exactly one click record, got {s.cursor_failure_records!r}"
    assert recs[0]["reason"] == "no_effect"


@pytest.mark.anyio
async def test_browser_click_with_effect_records_nothing() -> None:
    """Control: a click that DID move the page must not record a failure —
    guards against the ledger filling up on successful clicks (which would
    open the run_script hatch spuriously)."""
    s = _make_state()
    tool = BrowserClickTool(s)
    cms = [
        patch(f"{_CLICK}._request_with_backoff", return_value=_FakeHTTPResponse(200, {
            "effect": {"mutation_delta": 5, "url_changed": False, "focused_changed": True},
            "snap": {"snapped": True, "x": 150, "y": 120},
            "url": "https://example.test/search/",
        })),
        patch(f"{_CLICK}._fetch_elements", return_value=""),
        patch(f"{_CLICK}._schedule_vision_prefetch", return_value=None),
        patch(f"{_CLICK}._append_fresh_vision", new=_fake_append),
        patch(f"{_CLICK}.lookup_postcondition", return_value=None),
        patch(f"{_CLICK}.run_click_with_ladder", return_value=""),
    ]
    for cm in cms:
        cm.start()
    try:
        out = await tool.execute(session_id="s1", index=4)
    finally:
        for cm in cms:
            cm.stop()
    assert "[no_effect:" not in out
    assert "click" not in s.cursor_failure_strategies
    assert s.cursor_failure_records == []
