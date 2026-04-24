"""Unit tests for browser_rewind_to_checkpoint.

Verifies the tool's contract:
  * Fails cleanly when no checkpoint has been recorded.
  * Advances the observation token on success.
  * Busts the vision cache + clears element fingerprints.
  * Posts a navigate request to the TS server with the checkpoint URL.

No network calls — HTTP transport is monkey-patched.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_rewind_tool.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any


class _FakeResp:
    def __init__(self, body: Any, status: int = 200):
        self._body = body
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        import json as _json
        self.text = _json.dumps(body) if not isinstance(body, str) else body

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_http_recorder():
    """Record all HTTP calls the tool makes so tests can assert on them."""
    from superbrowser_bridge import session_tools as ST
    calls: list[dict[str, Any]] = []

    async def _fake_request(method: str, url: str, **kw: Any) -> _FakeResp:
        calls.append({"method": method, "url": url, "kw": kw})
        if url.endswith("/navigate"):
            return _FakeResp({
                "url": kw.get("json", {}).get("url"),
                "title": "checkpoint page",
                "status": 200,
            })
        # /state endpoint used by the post-rewind prefetch
        return _FakeResp({})

    ST._request_with_backoff = _fake_request  # type: ignore[assignment]
    return calls


def _install_vision_stub():
    from superbrowser_bridge import session_tools as ST

    # Neutralize the prefetch's own HTTP path and image sniffing so the
    # test doesn't need a live Gemini.
    async def _noop(*a: Any, **k: Any) -> None:
        return None

    ST._push_vision_pending = _noop  # type: ignore[assignment]
    ST._push_vision_bboxes = _noop   # type: ignore[assignment]

    # Make the prefetch a no-op — vision_agent module absent.
    sys.modules.pop("vision_agent", None)

    # Install a stub vision_agent whose `_cache.bust_session` tracks calls.
    class _Cache:
        def __init__(self) -> None:
            self.busted: list[str] = []

        async def bust_session(self, sid: str) -> int:
            self.busted.append(sid)
            return 0

        async def get(self, *a: Any, **k: Any) -> Any:
            return None

        async def put(self, *a: Any, **k: Any) -> None:
            return None

    class _Agent:
        def __init__(self) -> None:
            self._cache = _Cache()

        async def analyze(self, **kw: Any) -> None:
            return None

    agent = _Agent()

    mod = types.ModuleType("vision_agent")
    mod.get_vision_agent = lambda: agent  # type: ignore[attr-defined]
    mod.vision_agent_enabled = lambda: True  # type: ignore[attr-defined]
    mod.dom_hash_of = lambda elements: "dh"  # type: ignore[attr-defined]
    sys.modules["vision_agent"] = mod
    return agent


def _make_state(checkpoint: str = ""):
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "test-session"
    s.current_url = "https://example.test/stuck"
    if checkpoint:
        s.best_checkpoint_url = checkpoint
    s.element_fingerprints[1] = "fp1"
    s.element_fingerprints[2] = "fp2"
    s._last_vision_response = object()
    s._last_vision_ts = 12345.0
    s._last_vision_url = "https://example.test/stuck"
    s.last_vision_token = 5
    s.current_token = 5
    return s


def test_rewind_fails_without_checkpoint() -> None:
    from superbrowser_bridge.session_tools import BrowserRewindToCheckpointTool
    s = _make_state()
    tool = BrowserRewindToCheckpointTool(s)
    out = asyncio.run(tool.execute(session_id="test-session"))
    assert isinstance(out, str)
    assert "rewind_failed:no_checkpoint" in out
    # No side effects.
    assert s.current_token == 5
    assert s.element_fingerprints  # untouched
    print("✓ test_rewind_fails_without_checkpoint")


def test_rewind_advances_token_and_clears_state() -> None:
    calls = _install_http_recorder()
    agent = _install_vision_stub()
    from superbrowser_bridge.session_tools import BrowserRewindToCheckpointTool
    s = _make_state(checkpoint="https://example.test/home")
    tool = BrowserRewindToCheckpointTool(s)
    _ = asyncio.run(tool.execute(session_id="test-session"))

    # Token bumped with source=rewind.
    assert s.current_token == 6, f"expected current_token=6, got {s.current_token}"
    assert s.last_token_source == "rewind"
    # Fingerprints cleared.
    assert s.element_fingerprints == {}
    # Vision state wiped — the gate must NOT report fresh.
    assert s._last_vision_response is None
    assert not s.vision_is_fresh()
    # Cache busted for this session.
    assert "test-session" in agent._cache.busted
    # Navigate request fired at the checkpoint URL.
    navigate_calls = [c for c in calls if c["url"].endswith("/navigate")]
    assert len(navigate_calls) == 1
    assert navigate_calls[0]["kw"]["json"]["url"] == "https://example.test/home"
    # Step history records the rewind.
    assert s.step_history, "step_history should have the rewind entry"
    assert s.step_history[-1]["tool"] == "browser_rewind_to_checkpoint"
    print("✓ test_rewind_advances_token_and_clears_state")


def test_rewind_reports_http_error() -> None:
    from superbrowser_bridge import session_tools as ST

    async def _bad_request(method: str, url: str, **kw: Any) -> _FakeResp:
        return _FakeResp({"error": "navigation refused"}, status=500)

    ST._request_with_backoff = _bad_request  # type: ignore[assignment]
    _install_vision_stub()
    from superbrowser_bridge.session_tools import BrowserRewindToCheckpointTool
    s = _make_state(checkpoint="https://example.test/home")
    tool = BrowserRewindToCheckpointTool(s)
    out = asyncio.run(tool.execute(session_id="test-session"))
    assert "rewind_failed:http_500" in out
    assert "navigation refused" in out
    # Token still advanced — the commit point is before the HTTP call,
    # which matches the "any mutation invalidates the gate" contract.
    assert s.current_token == 6
    print("✓ test_rewind_reports_http_error")


def main() -> int:
    tests = [
        test_rewind_fails_without_checkpoint,
        test_rewind_advances_token_and_clears_state,
        test_rewind_reports_http_error,
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
