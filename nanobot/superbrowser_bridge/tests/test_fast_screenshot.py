"""Fast-screenshot mode tests (ABC round C).

`browser_screenshot(fast=true)` returns image + DOM elements
immediately and schedules a background Gemini pass. The sync vision
budget is also env-tunable via SCREENSHOT_VISION_BUDGET_MS.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_fast_screenshot.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from typing import Any


def _install_stub_vision_agent(delay_s: float = 0.0):
    """Install a fake vision_agent that takes `delay_s` to analyze."""
    va = types.ModuleType("vision_agent")

    class _FakeVR:
        summary = "fake vision"
        image_width = 100
        image_height = 100
        dpr = 1.0
        bboxes: list[Any] = []
        scene = None
        flags = types.SimpleNamespace(
            login_required=False, captcha_present=False,
        )
        cached = False
        screenshot_freshness = "fresh"
        relevant_text = ""

        def with_image_dims(self, w, h, dpr=1.0):
            return self

        def as_brain_text(self) -> str:
            return "[vision] fake"

        def get_bbox(self, idx):
            return None

    class _FakeAgent:
        def __init__(self):
            self._cache = types.SimpleNamespace(
                bust_session=lambda sid: _noop_async(),
                get=lambda k: _noop_async(),
                put=lambda k, v: _noop_async(),
            )

        async def analyze(self, **kw):
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            return _FakeVR()

    async def _noop_async(*a, **k):
        return None

    agent = _FakeAgent()
    va.get_vision_agent = lambda: agent
    va.vision_agent_enabled = lambda: True
    va.dom_hash_of = lambda elements: "dh"
    sys.modules["vision_agent"] = va
    return agent


def _fake_transport():
    """Patch _request_with_backoff to return a canned /state."""
    from superbrowser_bridge import session_tools as ST

    class _Resp:
        status_code = 200
        headers: dict = {"content-type": "application/json"}
        text: str = ""
        def raise_for_status(self) -> None:
            return None
        def json(self):
            return {
                "url": "https://example.test/",
                "title": "t",
                "screenshot": (
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A"
                    "AQUBAScY42YAAAAASUVORK5CYII="
                ),
                "elements": "<body></body>",
                "scrollInfo": {"scrollY": 0, "scrollHeight": 500, "viewportHeight": 500},
                "selectorEntries": [],
                "devicePixelRatio": 1.0,
                "fingerprints": {},
                "consoleErrors": [],
                "pendingDialogs": [],
            }

    async def _fake(method, url, **kw):
        return _Resp()

    ST._request_with_backoff = _fake  # type: ignore[assignment]


def test_fast_returns_immediately() -> None:
    """fast=true must return without waiting for the 5s vision call."""
    _install_stub_vision_agent(delay_s=5.0)
    _fake_transport()
    from superbrowser_bridge.session_tools import (
        BrowserScreenshotTool, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.session_id = "t1"
    s.screenshot_budget = 5
    s.actions_since_screenshot = 1  # bypass "reuse previous" guard
    tool = BrowserScreenshotTool(s)
    import time as _time
    t0 = _time.monotonic()
    result = asyncio.run(tool.execute(session_id="t1", fast=True))
    elapsed = _time.monotonic() - t0
    # Must NOT have waited for the 5s agent.analyze().
    assert elapsed < 1.5, f"fast screenshot took too long: {elapsed:.2f}s"
    # Result should carry the fast-mode marker somewhere.
    rendered = "\n".join(
        b.get("text", "") if isinstance(b, dict) else str(b)
        for b in (result if isinstance(result, list) else [{"type": "text", "text": result}])
    )
    assert "[FAST_SCREENSHOT]" in rendered, rendered[:200]
    print(f"✓ test_fast_returns_immediately ({elapsed:.2f}s)")


def test_slow_budget_falls_back_to_image_blocks() -> None:
    """SCREENSHOT_VISION_BUDGET_MS=500 with an 8s vision stub → budget
    expires, we fall through to the image-blocks path (no bboxes) but
    the result still carries the image."""
    _install_stub_vision_agent(delay_s=8.0)
    _fake_transport()
    os.environ["SCREENSHOT_VISION_BUDGET_MS"] = "500"
    try:
        from superbrowser_bridge.session_tools import (
            BrowserScreenshotTool, BrowserSessionState,
        )
        s = BrowserSessionState()
        s.session_id = "t2"
        s.screenshot_budget = 5
        s.actions_since_screenshot = 1
        tool = BrowserScreenshotTool(s)
        import time as _time
        t0 = _time.monotonic()
        result = asyncio.run(tool.execute(session_id="t2", fast=False))
        elapsed = _time.monotonic() - t0
        # Must timeout at ~0.5s, not wait 8s.
        assert elapsed < 2.0, f"budget not honored: {elapsed:.2f}s"
    finally:
        os.environ.pop("SCREENSHOT_VISION_BUDGET_MS", None)
    print(f"✓ test_slow_budget_falls_back_to_image_blocks ({elapsed:.2f}s)")


def test_default_budget_completes_vision_pass() -> None:
    """With a fast stub (50ms) and default 15s budget, the vision pass
    completes and the result contains the rich brain_text."""
    _install_stub_vision_agent(delay_s=0.05)
    _fake_transport()
    os.environ.pop("SCREENSHOT_VISION_BUDGET_MS", None)
    from superbrowser_bridge.session_tools import (
        BrowserScreenshotTool, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.session_id = "t3"
    s.screenshot_budget = 5
    s.actions_since_screenshot = 1
    tool = BrowserScreenshotTool(s)
    result = asyncio.run(tool.execute(session_id="t3", fast=False))
    rendered = "\n".join(
        b.get("text", "") if isinstance(b, dict) else str(b)
        for b in (result if isinstance(result, list) else [{"type": "text", "text": result}])
    )
    # Sync path reached — brain_text from the stub VisionResponse is present
    assert "[vision] fake" in rendered, rendered[:200]
    # fast-mode marker should NOT be present — we took the full path
    assert "[FAST_SCREENSHOT]" not in rendered, rendered[:200]
    print("✓ test_default_budget_completes_vision_pass")


def main() -> int:
    tests = [
        test_fast_returns_immediately,
        test_slow_budget_falls_back_to_image_blocks,
        test_default_budget_completes_vision_pass,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
