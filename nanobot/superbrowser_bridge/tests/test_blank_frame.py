"""Blank-capture handling across the screenshot path.

The bug these guard: a capture taken while a component was re-rendering
(DOM committed, pixels not yet painted) was indistinguishable from a
settled one. Three separate things then went wrong with it, and all
three had to be closed for the fix to hold:

  1. The vision cache key is built from the DOM and never from the
     image, so empty bboxes were stored under the exact key the settled
     page would ask for — a transient paint race promoted into a
     persistent wrong answer.
  2. The screenshot dedup key is (url, DOM content hash), also identical
     before and after paint, so the re-screenshot that would finally
     show the content was refused as a duplicate.
  3. The post-navigation settle flag was consumed by whichever capture
     got there first, usually a background prefetch, leaving the brain's
     own screenshot — which never asked for settle on its own — to read
     the unsettled frame.

Run:
    source venv/bin/activate && \
        PYTHONPATH=nanobot python -m pytest \
        nanobot/superbrowser_bridge/tests/test_blank_frame.py -q
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from superbrowser_bridge.session_tools.state import BrowserSessionState
from superbrowser_bridge.session_tools.tools import screenshot as S


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _payload(*, blank: bool) -> dict:
    return {
        "url": "https://bank.example/calc",
        "elements": "[1]<button>Calculate</button>",
        "screenshot": "QUJD",
        "screenshotBlank": blank,
        "screenshotInk": 0.0001 if blank else 0.42,
        "screenshotAttempts": 3 if blank else 1,
        "scrollSignature": "sig",
        "scrollInfo": {"scrollY": 0},
        "selectorEntries": [],
        "devicePixelRatio": 1.0,
    }


def _run(*, blank: bool, armed: bool = False, monkeypatch: Any) -> tuple[Any, dict]:
    """Execute browser_screenshot against a stubbed /state, vision off."""
    seen: dict = {}

    async def _fake(method: str, url: str, **kw: Any) -> _Resp:
        seen["params"] = kw.get("params")
        seen["timeout"] = kw.get("timeout")
        return _Resp(_payload(blank=blank))

    monkeypatch.setattr(S, "_request_with_backoff", _fake)

    async def _fake_elements(session_id: str, state: Any) -> str:
        return "[1]<button>Calculate</button>"

    monkeypatch.setattr(S, "_fetch_elements", _fake_elements)

    st = BrowserSessionState()
    st.current_url = "https://bank.example/calc"
    # A screenshot with no intervening action is refused outright, so give
    # the session one before exercising the paint path.
    st.actions_since_screenshot = 1
    st._needs_visual_settle = armed
    tool = S.BrowserScreenshotTool(st)
    # Vision off → execute() returns the caption string, which is what we
    # assert on, and no provider is called. The cache-poisoning half of
    # the fix lives in the vision client and is covered there.
    monkeypatch.setenv("VISION_ENABLED", "0")
    out = asyncio.run(tool.execute(session_id="s1"))
    return (out, seen, st)  # type: ignore[return-value]


def _text(out: Any) -> str:
    """Caption text, whether execute() returned a string or image blocks."""
    if isinstance(out, str):
        return out
    return "\n".join(b.get("text", "") for b in out if isinstance(b, dict))


def test_blank_capture_is_announced_to_the_model(monkeypatch) -> None:
    out = _text(_run(blank=True, monkeypatch=monkeypatch)[0])
    assert "BLANK" in out
    # The model must not read an empty page as evidence of absence.
    assert "Do NOT conclude the content is missing" in out


def test_painted_capture_says_nothing_about_paint(monkeypatch) -> None:
    out = _text(_run(blank=False, monkeypatch=monkeypatch)[0])
    assert "[PAINT]" not in out


def _dedup_key(st: BrowserSessionState) -> tuple[str, str]:
    return (
        st._normalize_url("https://bank.example/calc"),
        st.hash_page_content("[1]<button>Calculate</button>", scroll_sig="sig"),
    )


def test_blank_capture_does_not_burn_the_dedup_slot(monkeypatch) -> None:
    _out, _seen, st = _run(blank=True, monkeypatch=monkeypatch)
    assert _dedup_key(st) not in st.screenshotted_keys, (
        "the DOM is identical before and after paint, so recording a blank "
        "capture would refuse the retake that finally shows the content"
    )


def test_painted_capture_does_take_the_dedup_slot(monkeypatch) -> None:
    _out, _seen, st = _run(blank=False, monkeypatch=monkeypatch)
    assert _dedup_key(st) in st.screenshotted_keys, (
        "dedup must still suppress an identical re-screenshot"
    )


def test_armed_settle_is_requested_by_the_brain_facing_capture(monkeypatch) -> None:
    _out, seen, _st = _run(blank=False, armed=True, monkeypatch=monkeypatch)
    assert seen["params"].get("settle") == "true"
    assert seen["timeout"] > 15.0, "a settle needs more room than the plain path"


def test_unarmed_capture_does_not_pay_for_a_settle(monkeypatch) -> None:
    _out, seen, _st = _run(blank=False, armed=False, monkeypatch=monkeypatch)
    assert "settle" not in (seen["params"] or {})


def test_settle_disarms_only_once_a_capture_painted(monkeypatch) -> None:
    _out, _seen, st = _run(blank=False, armed=True, monkeypatch=monkeypatch)
    assert st._needs_visual_settle is False


def test_settle_stays_armed_while_captures_come_back_blank(monkeypatch) -> None:
    _out, _seen, st = _run(blank=True, armed=True, monkeypatch=monkeypatch)
    assert st._needs_visual_settle is True, (
        "a blank capture must leave the next attempt able to settle"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
