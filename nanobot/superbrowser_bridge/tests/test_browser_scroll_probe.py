"""Unit tests: BrowserScrollTool's PROBE caption + telemetry.

When the brain calls `browser_scroll(direction='down', pixels=400,
target_text='Price')`, the TS server returns a `probe` dict (direct DOM
measurement) and an optional `newly_visible` array. These tests pin the
caption format the brain reads — [PROBE target='X' in_viewport=… …] plus
tips and Newly-visible — and confirm that the new probe telemetry keys
(`last_probe_target`, `last_probe_in_viewport`, `last_probe_below_fold`)
flow into `state.scroll_telemetry` so `worker_hook` can read them on the
next turn.

No server required — runs in pure pytest, monkey-patches the HTTP layer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.navigation import BrowserScrollTool


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeHTTPResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _state() -> BrowserSessionState:
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    s.current_url = "https://example.test/"
    return s


def _base_response(**overrides: Any) -> dict[str, Any]:
    """Minimal scroll-endpoint response. Includes `elements` so the
    tool doesn't fall through to the secondary _fetch_elements path."""
    payload: dict[str, Any] = {
        "success": True,
        "elements": "",  # no elements; build_text_only skips the line
        "prevScrollInfo": {"scrollY": 600},
        "scrollInfo": {
            "scrollY": 1000,
            "scrollHeight": 3200,
            "viewportHeight": 900,
        },
        "url": "https://example.test/",
        "title": "Listing",
    }
    payload.update(overrides)
    return payload


async def _run_tool(
    *,
    payload: dict[str, Any],
    target_text: str | None = None,
    pixels: int | None = 400,
    direction: str = "down",
) -> tuple[str, BrowserSessionState]:
    s = _state()
    tool = BrowserScrollTool(s)
    fake = _FakeHTTPResponse(200, payload)
    with patch(
        "superbrowser_bridge.session_tools.tools.navigation._request_with_backoff",
        return_value=fake,
    ), patch(
        "superbrowser_bridge.session_tools.tools.navigation._schedule_vision_prefetch",
        return_value=None,
    ):
        out = await tool.execute(
            session_id="s1",
            direction=direction,
            pixels=pixels,
            target_text=target_text,
        )
    return out, s


@pytest.mark.anyio
async def test_probe_match_in_viewport_renders_flag() -> None:
    payload = _base_response(probe={
        "in_viewport": True,
        "fully_in_viewport": True,
        "anywhere_in_dom": True,
        "below_fold": False,
        "above_fold": False,
        "matched_selector": "button#price",
        "matched_text": "Price",
    })
    out, _s = await _run_tool(payload=payload, target_text="Price")
    assert "[PROBE target='Price' in_viewport=True fully=True" in out
    # In-viewport-true should NOT emit the below_fold tip.
    assert "below the fold" not in out


@pytest.mark.anyio
async def test_probe_below_fold_renders_tip() -> None:
    payload = _base_response(probe={
        "in_viewport": False,
        "fully_in_viewport": False,
        "anywhere_in_dom": True,
        "below_fold": True,
        "above_fold": False,
    })
    out, _s = await _run_tool(payload=payload, target_text="Price")
    assert "below_fold=True" in out
    assert "anywhere_in_dom=true" in out
    assert "target is below the fold" in out
    assert "Do NOT emit a V_n claiming to be 'Price'" in out


@pytest.mark.anyio
async def test_probe_not_in_dom_renders_tip() -> None:
    payload = _base_response(probe={
        "in_viewport": False,
        "fully_in_viewport": False,
        "anywhere_in_dom": False,
        "below_fold": False,
        "above_fold": False,
    })
    out, _s = await _run_tool(payload=payload, target_text="Synonyms")
    assert "[PROBE target='Synonyms'" in out
    assert "not found anywhere in the DOM" in out


@pytest.mark.anyio
async def test_probe_sticky_candidate_renders_flag() -> None:
    payload = _base_response(probe={
        "in_viewport": True,
        "fully_in_viewport": True,
        "anywhere_in_dom": True,
        "below_fold": False,
        "above_fold": False,
        "sticky_candidate": True,
    })
    out, _s = await _run_tool(payload=payload, target_text="Price")
    assert "sticky=true" in out
    assert "position is UNCHANGED" in out


@pytest.mark.anyio
async def test_no_probe_no_caption() -> None:
    payload = _base_response()  # no probe key, no newly_visible
    out, _s = await _run_tool(payload=payload, target_text=None)
    assert "[PROBE" not in out
    assert "Newly visible:" not in out


@pytest.mark.anyio
async def test_newly_visible_caps_at_5() -> None:
    payload = _base_response(newly_visible=["A", "B", "C", "D", "E", "F", "G"])
    out, _s = await _run_tool(payload=payload, target_text=None)
    assert "Newly visible: +A, +B, +C, +D, +E" in out
    assert "+F" not in out
    assert "+G" not in out


@pytest.mark.anyio
async def test_newly_visible_empty_omits_line() -> None:
    payload = _base_response(newly_visible=[])
    out, _s = await _run_tool(payload=payload, target_text=None)
    assert "Newly visible:" not in out


@pytest.mark.anyio
async def test_telemetry_records_probe_keys() -> None:
    payload = _base_response(probe={
        "in_viewport": False,
        "fully_in_viewport": False,
        "anywhere_in_dom": True,
        "below_fold": True,
        "above_fold": False,
    })
    _out, s = await _run_tool(payload=payload, target_text="Price")
    tel = getattr(s, "scroll_telemetry", None) or {}
    assert tel.get("last_probe_target") == "Price"
    assert tel.get("last_probe_in_viewport") is False
    assert tel.get("last_probe_below_fold") is True


@pytest.mark.anyio
async def test_telemetry_skips_probe_keys_when_no_target() -> None:
    payload = _base_response()  # no probe field
    _out, s = await _run_tool(payload=payload, target_text=None)
    tel = getattr(s, "scroll_telemetry", None) or {}
    # `_update_scroll_telemetry` only adds probe keys when `extra` is
    # passed, which only happens when `probe` is present. With no
    # probe, those keys are absent (or None).
    assert tel.get("last_probe_target") is None
    assert tel.get("last_probe_in_viewport") is None


@pytest.mark.anyio
async def test_no_target_text_keeps_newly_visible() -> None:
    payload = _base_response(newly_visible=["Brand", "Sort"])
    out, _s = await _run_tool(payload=payload, target_text=None)
    assert "Newly visible: +Brand, +Sort" in out
    assert "[PROBE" not in out


@pytest.mark.anyio
async def test_probe_caption_appears_before_scroll_state() -> None:
    """[PROBE …] must be in the action prefix so it sits ABOVE the
    [SCROLL_STATE …] line that build_text_only appends. The brain
    reads top-down; PROBE is more authoritative for label questions."""
    payload = _base_response(probe={
        "in_viewport": False,
        "fully_in_viewport": False,
        "anywhere_in_dom": True,
        "below_fold": True,
        "above_fold": False,
    })
    out, _s = await _run_tool(payload=payload, target_text="Price")
    probe_idx = out.find("[PROBE")
    state_idx = out.find("[SCROLL_STATE")
    assert probe_idx >= 0
    assert state_idx >= 0
    assert probe_idx < state_idx, (
        f"PROBE caption must precede SCROLL_STATE; got probe={probe_idx} "
        f"state={state_idx} in:\n{out}"
    )
