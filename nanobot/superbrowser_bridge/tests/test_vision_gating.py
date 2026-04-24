"""Unit tests for the vision-gated execution pipeline.

Verifies the sequencing contract: no mutation tool fires until vision
has observed the current `observation_token`. Exercises the race where
a mutation happens mid-prefetch and the stale response must be
discarded rather than stamped as fresh.

No network calls — VisionAgent is monkey-patched.

Run:
    source venv/bin/activate && \\
        python nanobot/superbrowser_bridge/tests/test_vision_gating.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from typing import Any


def _install_stub_vision_agent(delay_s: float = 0.0, response: Any = None):
    """Install a fake vision_agent package so _schedule_vision_prefetch
    doesn't need a real Gemini key. Returns (module, state_factory)."""
    # Disable real HTTP path — _request_with_backoff is module-level, so
    # we stub it to return a minimal screenshot state response.
    from superbrowser_bridge import session_tools as ST

    class _FakeResp:
        status_code = 200
        headers: dict[str, str] = {}
        def json(self) -> dict[str, Any]:
            # 1x1 white PNG base64 — large enough that image header parsing works.
            b64 = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQ"
                "UBAScY42YAAAAASUVORK5CYII="
            )
            return {
                "screenshot": b64,
                "url": "https://example.test/",
                "elements": "<body></body>",
                "devicePixelRatio": 1.0,
            }

    async def _fake_request(method: str, url: str, **kw: Any) -> _FakeResp:
        return _FakeResp()

    ST._request_with_backoff = _fake_request  # type: ignore[assignment]

    # Also stub _read_image_dims — the PIL call works fine with the 1x1
    # PNG but we want a deterministic value.
    def _fake_dims(b64: str) -> tuple[int, int]:
        return (100, 100)

    ST._read_image_dims = _fake_dims  # type: ignore[assignment]

    # Stub _push_vision_pending and _push_vision_bboxes so tests don't
    # touch the live-viewer socket.
    async def _noop(*a: Any, **k: Any) -> None:
        return None

    ST._push_vision_pending = _noop  # type: ignore[assignment]
    ST._push_vision_bboxes = _noop  # type: ignore[assignment]

    # Build a fake vision_agent module in sys.modules so the dynamic
    # import inside _schedule_vision_prefetch succeeds.
    va = types.ModuleType("vision_agent")

    class _FakeBbox:
        confidence = 0.9
        layer_id = None
        def to_pixels(self, iw: int, ih: int, dpr: float = 1.0) -> tuple[int, int, int, int]:
            return 10, 10, 20, 20

    class _FakeVR:
        summary = "fake vision"
        cached = False
        image_width = 100
        image_height = 100
        dpr = 1.0
        bboxes: list[Any] = []
        def with_image_dims(self, w: int, h: int, dpr: float = 1.0) -> "_FakeVR":
            self.image_width, self.image_height, self.dpr = w, h, dpr
            return self
        def get_bbox(self, idx: int) -> Any:
            return None

    class _FakeAgent:
        async def analyze(self, **kw: Any) -> _FakeVR:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            return response if response is not None else _FakeVR()

    _agent_singleton = _FakeAgent()

    def _get_agent() -> _FakeAgent:
        return _agent_singleton

    def _enabled() -> bool:
        return True

    def _dom_hash_of(elements: str) -> str:
        return "dh"

    va.get_vision_agent = _get_agent
    va.vision_agent_enabled = _enabled
    va.dom_hash_of = _dom_hash_of
    sys.modules["vision_agent"] = va


def _make_state():
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "test-session"
    s.current_url = "https://example.test/"
    return s


def test_advance_token_monotonic() -> None:
    s = _make_state()
    assert s.current_token == 0
    assert s.advance_observation_token("click") == 1
    assert s.advance_observation_token("type") == 2
    assert s.current_token == 2
    assert s.last_token_source == "type"
    print("✓ test_advance_token_monotonic")


def test_vision_is_fresh_requires_token_match() -> None:
    s = _make_state()
    assert not s.vision_is_fresh()  # no response yet
    s._last_vision_response = object()  # pretend something is cached
    s.last_vision_token = -1
    s.current_token = 0
    assert not s.vision_is_fresh()
    s.last_vision_token = 0
    assert s.vision_is_fresh()
    s.advance_observation_token("click")  # → 1
    assert not s.vision_is_fresh()
    print("✓ test_vision_is_fresh_requires_token_match")


def test_require_fresh_waits_past_2s_default() -> None:
    """Prefetch takes 3s; gate must wait, not bail at the old 2s budget."""
    _install_stub_vision_agent(delay_s=3.0)
    from superbrowser_bridge import session_tools as ST

    s = _make_state()
    # Token=0, no vision yet → gate must wait for the prefetch to complete.

    async def run() -> tuple[bool, str]:
        ok, msg = await ST._require_fresh_vision(
            s, s.session_id, max_wait_s=10.0, reason="test",
        )
        return ok, msg

    ok, msg = asyncio.run(run())
    assert ok, f"gate should have waited through 3s prefetch, got: {msg}"
    assert s.vision_is_fresh()
    print("✓ test_require_fresh_waits_past_2s_default")


def test_require_fresh_rejects_response_from_prior_token() -> None:
    """If the token advances mid-prefetch, the arriving response is for
    a stale page and must be discarded rather than stamped as fresh."""
    _install_stub_vision_agent(delay_s=0.3)
    from superbrowser_bridge import session_tools as ST

    s = _make_state()

    async def drive() -> None:
        # Start the prefetch tied to token=0 (inside the loop so
        # asyncio.create_task has a running loop).
        assert s.current_token == 0
        prefetch = ST._schedule_vision_prefetch(s, s.session_id)
        assert prefetch is not None
        # Simulate a mutation (click) happening mid-flight — the token
        # advances while the prefetch is still in the analyze() call.
        await asyncio.sleep(0.05)
        s.advance_observation_token("click")
        # Wait for prefetch to finish; it should silently drop its write.
        try:
            await asyncio.wait_for(asyncio.shield(prefetch), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    asyncio.run(drive())

    assert s.current_token == 1, "click should have advanced token to 1"
    assert s.last_vision_token != 1, (
        "prefetch dispatched at token=0 must not stamp token=1 as observed"
    )
    assert not s.vision_is_fresh()
    print("✓ test_require_fresh_rejects_response_from_prior_token")


def test_require_fresh_superseded_on_concurrent_mutation() -> None:
    """A waiter must abort with [vision_gate_superseded] if the page
    mutates while it's blocked — the LLM's planned action is for a
    page that no longer exists and would land on stale coordinates."""
    _install_stub_vision_agent(delay_s=2.0)
    from superbrowser_bridge import session_tools as ST

    s = _make_state()
    s.advance_observation_token("click")  # token=1, vision not fresh

    async def racer() -> None:
        await asyncio.sleep(0.2)
        s.advance_observation_token("navigate")  # token=2 while we wait

    async def drive() -> tuple[bool, str]:
        r = asyncio.create_task(racer())
        ok, msg = await ST._require_fresh_vision(
            s, s.session_id, max_wait_s=5.0, reason="test_click",
        )
        await r
        return ok, msg

    ok, msg = asyncio.run(drive())
    assert not ok
    assert "vision_gate_superseded" in msg, msg
    print("✓ test_require_fresh_superseded_on_concurrent_mutation")


def test_require_fresh_timeout_returns_structured_error() -> None:
    """When the vision service is unreachable and no fresh pass lands
    before the deadline, the gate returns a [vision_gate_timeout] error
    — mutation must NOT proceed on stale bboxes."""
    # No stub for vision_agent; the import fails and prefetch returns None.
    for name in ("vision_agent",):
        sys.modules.pop(name, None)
    from superbrowser_bridge import session_tools as ST

    s = _make_state()
    s.advance_observation_token("click")

    async def run() -> tuple[bool, str]:
        return await ST._require_fresh_vision(
            s, s.session_id, max_wait_s=0.5, reason="test_click",
        )

    ok, msg = asyncio.run(run())
    assert not ok
    assert "vision_gate_timeout" in msg, msg
    print("✓ test_require_fresh_timeout_returns_structured_error")


def test_default_gate_does_not_invalidate_cached_vision() -> None:
    """After R3.1 the default is OFF: the gate must NOT wipe the cached
    vision response when it already matches the current token. This
    preserves V-index stability — V1 means what the brain planned
    against, not what a fresh re-analysis happens to enumerate first."""
    _install_stub_vision_agent(delay_s=5.0)  # would delay if we actually re-analyzed
    from superbrowser_bridge import session_tools as ST
    from superbrowser_bridge.session_tools import BrowserSessionState
    import os as _os

    _os.environ.pop("VISION_GATE_ALWAYS_REFRESH", None)  # ensure default path
    s = BrowserSessionState()
    s.session_id = "test-session"
    s.current_token = 2
    s.last_vision_token = 2
    sentinel = object()
    s._last_vision_response = sentinel
    assert s.vision_is_fresh()

    async def run() -> tuple[bool, str]:
        return await ST._require_fresh_vision(
            s, s.session_id, max_wait_s=0.5,
        )

    import time as _time
    t0 = _time.monotonic()
    ok, _ = asyncio.run(run())
    elapsed = _time.monotonic() - t0
    assert ok, "gate should pass instantly when vision already fresh"
    assert s._last_vision_response is sentinel, (
        "cached response must be preserved — V-indices depend on it"
    )
    assert elapsed < 0.3, f"gate should not wait, elapsed={elapsed:.2f}s"
    print("✓ test_default_gate_does_not_invalidate_cached_vision")


def test_always_refresh_opt_in_still_works() -> None:
    """Escape hatch: setting VISION_GATE_ALWAYS_REFRESH=1 re-enables
    the pre-action refresh for sites where visible-state drift hurts
    more than V-index stability. Verifies the feature flag is alive."""
    _install_stub_vision_agent(delay_s=0.1)
    from superbrowser_bridge import session_tools as ST
    from superbrowser_bridge.session_tools import BrowserSessionState
    import os as _os

    _os.environ["VISION_GATE_ALWAYS_REFRESH"] = "1"
    try:
        s = BrowserSessionState()
        s.session_id = "test-session"
        s.current_url = "https://example.test/"
        s.current_token = 2
        s.last_vision_token = 2
        s._last_vision_response = object()

        async def run() -> tuple[bool, str]:
            return await ST._require_fresh_vision(
                s, s.session_id, max_wait_s=5.0, reason="opt_in_test",
            )

        ok, _ = asyncio.run(run())
        assert ok
        # When the flag is ON, the response should have been replaced
        # by the fresh prefetch (the _FakeVR stub).
        cls_name = type(s._last_vision_response).__name__ if s._last_vision_response else ""
        assert cls_name != "object", (
            f"with refresh ON, cached response should be replaced; got {cls_name}"
        )
    finally:
        _os.environ.pop("VISION_GATE_ALWAYS_REFRESH", None)
    print("✓ test_always_refresh_opt_in_still_works")


def test_prefetch_busts_session_vision_cache_before_analyze() -> None:
    """Every prefetch must wipe the session's vision cache before
    analyze() runs, so a `dom_hash` collision (React re-render that
    changes visible state without touching DOM structure) can't
    serve bboxes from before the mutation's JS effects propagated."""
    _install_stub_vision_agent()
    import vision_agent as va
    bust_calls: list[str] = []

    agent = va.get_vision_agent()

    class _Cache:
        async def bust_session(self, sid: str) -> int:
            bust_calls.append(sid)
            return 0
        async def get(self, *a: Any, **k: Any) -> Any:
            return None
        async def put(self, *a: Any, **k: Any) -> None:
            return None

    agent._cache = _Cache()  # type: ignore[attr-defined]

    from superbrowser_bridge import session_tools as ST
    s = _make_state()

    async def drive() -> None:
        prefetch = ST._schedule_vision_prefetch(s, s.session_id)
        assert prefetch is not None
        try:
            await asyncio.wait_for(asyncio.shield(prefetch), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    asyncio.run(drive())
    assert s.session_id in bust_calls, (
        f"session cache should have been busted; bust_calls={bust_calls!r}"
    )
    print("✓ test_prefetch_busts_session_vision_cache_before_analyze")


def test_reset_per_session_resets_token() -> None:
    s = _make_state()
    s.advance_observation_token("click")
    s.advance_observation_token("type")
    s.last_vision_token = 2
    s.reset_per_session()
    assert s.current_token == 0
    assert s.last_vision_token == -1
    assert s.last_token_source == "init"
    print("✓ test_reset_per_session_resets_token")


def main() -> int:
    tests = [
        test_advance_token_monotonic,
        test_vision_is_fresh_requires_token_match,
        test_require_fresh_waits_past_2s_default,
        test_require_fresh_rejects_response_from_prior_token,
        test_require_fresh_superseded_on_concurrent_mutation,
        test_require_fresh_timeout_returns_structured_error,
        test_default_gate_does_not_invalidate_cached_vision,
        test_always_refresh_opt_in_still_works,
        test_prefetch_busts_session_vision_cache_before_analyze,
        test_reset_per_session_resets_token,
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
