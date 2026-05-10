"""Unit test: BrowserClickSelectorTool rejects React-dynamic IDs.

Brain sometimes captures radix/headlessui/`useId()` IDs from a
screenshot and re-uses them after the page re-renders. The ID has
rotated by then, so the selector silently fails or hits the wrong
element — and the brain learns nothing because the failure mode is
indistinguishable from "valid selector, element not present".

The guard short-circuits this: matched selectors are rejected upfront
with an advisory routing the brain to `browser_click_at(vision_index=...)`.
No HTTP dispatch happens, so this test doesn't need a server.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.click import (
    BrowserClickSelectorTool,
    _DYNAMIC_ID_RE,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_state() -> BrowserSessionState:
    s = BrowserSessionState()
    s._brain_turn_counter = 0
    s.current_url = "https://example.test/"
    return s


# ---------------- regex unit cases ----------------------------------

@pytest.mark.parametrize(
    "selector",
    [
        "#:r13:",
        "#radix-:r13:",
        "#radix-\\:r13\\:",
        "button#radix-:r13:",
        ".modal #radix-:r0:",
        "#headlessui-button-32",
        "#headlessui-menu-items-4",
        "#__id_12345",
    ],
)
def test_dynamic_id_pattern_matches(selector: str) -> None:
    assert _DYNAMIC_ID_RE.search(selector), (
        f"expected dynamic-ID match for {selector!r}"
    )


@pytest.mark.parametrize(
    "selector",
    [
        "#email",
        "#submit",
        "#radix-trigger-main",
        "#email-input-12",
        "button:hover",
        "a:not([disabled])",
        '[data-id=":r13:"]',
        ".square-54",
        "[data-testid=submit]",
    ],
)
def test_dynamic_id_pattern_skips_stable(selector: str) -> None:
    assert not _DYNAMIC_ID_RE.search(selector), (
        f"did NOT expect dynamic-ID match for {selector!r}"
    )


# ---------------- end-to-end tool execute --------------------------

@pytest.mark.anyio
async def test_click_selector_rejects_dynamic_id() -> None:
    s = _make_state()
    tool = BrowserClickSelectorTool(s)

    # If the guard fires correctly, _request_with_backoff is never called.
    # Patch it to a sentinel so we can assert "not called" by raising on
    # invocation.
    async def _fail_if_called(*_a, **_kw):
        raise AssertionError(
            "HTTP dispatch happened despite dynamic-ID guard"
        )

    with patch(
        "superbrowser_bridge.session_tools.tools.click._request_with_backoff",
        side_effect=_fail_if_called,
    ), patch.object(s, "ensure_vision_synced", return_value=None):
        out = await tool.execute(
            session_id="s1",
            selector="button#radix-:r13:",
        )

    assert out.startswith("[click_selector_rejected:dynamic_id]"), out
    assert "browser_click_at(vision_index=" in out
    assert "click_selector" in s.cursor_failure_strategies
    assert s._pending_undo_entry is None, (
        "rejection ran before begin_click_record — no undo entry should exist"
    )


@pytest.mark.anyio
async def test_click_selector_allows_stable_id() -> None:
    s = _make_state()
    tool = BrowserClickSelectorTool(s)

    class _FakeResp:
        status_code = 200
        def json(self):
            return {
                "clicked": {"x": 100, "y": 200},
                "url": "https://example.test/",
                "elements": "",
                "fingerprints": {},
            }

    async def _fake_dispatch(*_a, **_kw):
        return _FakeResp()

    with patch(
        "superbrowser_bridge.session_tools.tools.click._request_with_backoff",
        side_effect=_fake_dispatch,
    ), patch.object(s, "ensure_vision_synced", return_value=None), \
       patch.object(s, "advance_observation_token"), \
       patch(
        "superbrowser_bridge.session_tools.tools.click._schedule_vision_prefetch",
        return_value=None,
    ), patch(
        "superbrowser_bridge.session_tools.tools.click._append_fresh_vision",
        side_effect=lambda _t, caption, **_kw: caption,
    ):
        out = await tool.execute(
            session_id="s1",
            selector="#email",
        )

    assert not out.startswith("[click_selector_rejected"), out
    assert "click_selector" not in s.cursor_failure_strategies
