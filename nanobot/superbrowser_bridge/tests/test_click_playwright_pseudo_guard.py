"""Unit test: BrowserClickSelectorTool rejects Playwright/jQuery pseudo-selectors.

Brain learned `:has-text("X")`, `:contains("X")`, `text=X`, `:visible`, etc.
from Playwright/jQuery training data and emits them as if they were CSS.
`document.querySelector` throws SyntaxError on these, the TS getRects path
swallows the error, and the brain sees "selector not found or zero-size" —
indistinguishable from a missing element. Brain then wastes 5+ turns on
browser_eval / markdown lookups before falling through to raw coords.

The guard rejects upfront with an explicit advisory routing the brain to
`browser_click_at(vision_index=...)` for text-based clicks.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools.click import (
    BrowserClickSelectorTool,
    _PLAYWRIGHT_PSEUDO_RE,
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
        'button.tile-checkbox:has-text("Open-Box")',
        'div:contains("Submit")',
        'a:visible',
        'input:hidden',
        'li:eq(3)',
        'tr:first',
        'tr:last',
        'tr:odd',
        'tr:even',
        ':button',
        ':input',
        ':checkbox',
        ':radio',
        ':submit',
        ':selected',
        'text="Submit"',
        'role=button[name="Submit"]',
        'xpath=//button[@id="submit"]',
        'div >> text=Submit',
        'button >> role=menu',
    ],
)
def test_playwright_pseudo_matches(selector: str) -> None:
    assert _PLAYWRIGHT_PSEUDO_RE.search(selector), (
        f"expected Playwright-pseudo match for {selector!r}"
    )


@pytest.mark.parametrize(
    "selector",
    [
        # Standard CSS pseudos that look superficially similar.
        "button:hover",
        "input:focus",
        "a:not([disabled])",
        "li:nth-child(3)",
        "li:nth-of-type(2)",
        "div:has(> p)",  # standard `:has(...)` (CSS Level 4)
        "div:is(.foo, .bar)",
        "div:where(.foo, .bar)",
        "tr:first-child",
        "tr:last-child",
        "p:first-of-type",
        "button:enabled",  # actually valid CSS (UI state)
        "button:disabled",
        # Real CSS selectors with no pseudos.
        ".square-54",
        "#email",
        "[data-testid=submit]",
        "button.tile-checkbox",
        # Attribute selectors with text= inside (not a Playwright engine,
        # just a value containing text=).
        '[data-eq="text="]',
    ],
)
def test_playwright_pseudo_skips_standard_css(selector: str) -> None:
    assert not _PLAYWRIGHT_PSEUDO_RE.search(selector), (
        f"did NOT expect Playwright match for {selector!r}"
    )


# ---------------- end-to-end tool execute ---------------------------

@pytest.mark.anyio
async def test_click_selector_rejects_has_text() -> None:
    """The exact failure mode from the log:
    browser_click_selector('button.tile-checkbox:has-text("Open-Box")')
    should reject upfront, not dispatch."""
    s = _make_state()
    tool = BrowserClickSelectorTool(s)

    async def _fail_if_called(*_a, **_kw):
        raise AssertionError(
            "HTTP dispatch happened despite Playwright-pseudo guard"
        )

    with patch(
        "superbrowser_bridge.session_tools.tools.click._request_with_backoff",
        side_effect=_fail_if_called,
    ), patch.object(s, "ensure_vision_synced", return_value=None):
        out = await tool.execute(
            session_id="s1",
            selector='button.tile-checkbox:has-text("Open-Box")',
        )

    assert out.startswith("[click_selector_rejected:playwright_pseudo]"), out
    assert "browser_click_at(vision_index=" in out
    assert "click_selector" in s.cursor_failure_strategies


@pytest.mark.anyio
async def test_click_selector_rejects_contains() -> None:
    s = _make_state()
    tool = BrowserClickSelectorTool(s)

    async def _fail_if_called(*_a, **_kw):
        raise AssertionError("dispatched despite guard")

    with patch(
        "superbrowser_bridge.session_tools.tools.click._request_with_backoff",
        side_effect=_fail_if_called,
    ), patch.object(s, "ensure_vision_synced", return_value=None):
        out = await tool.execute(
            session_id="s1",
            selector='div:contains("Login")',
        )

    assert out.startswith("[click_selector_rejected:playwright_pseudo]"), out


@pytest.mark.anyio
async def test_click_selector_allows_standard_pseudos() -> None:
    """Real CSS pseudos like `:hover`, `:not()`, `:nth-child()`,
    `:enabled`, `:disabled`, `:has()` must NOT be rejected."""
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
            selector="button:not([disabled]):nth-child(3)",
        )

    assert not out.startswith("[click_selector_rejected"), out
