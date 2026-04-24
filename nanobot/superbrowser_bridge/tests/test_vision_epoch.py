"""Vision epoch freeze tests (Round 3).

The epoch is the snapshot of `_last_vision_response` captured when a
brain-facing tool reply is emitted. V-index resolvers read from the
frozen epoch so a prefetch that overwrites `_last_vision_response`
AFTER the brain has planned can't retarget the click.

No network. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_vision_epoch.py
"""

from __future__ import annotations

import asyncio
import sys


def test_freeze_captures_current_response() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    sentinel_old = object()
    s._last_vision_response = sentinel_old
    s._last_vision_url = "https://example.test/"
    s.freeze_vision_epoch()
    assert s._vision_epoch_response is sentinel_old
    assert s._vision_epoch_url == "https://example.test/"
    print("✓ test_freeze_captures_current_response")


def test_epoch_survives_last_vision_overwrite() -> None:
    """This is the core regression test: a background prefetch
    overwriting _last_vision_response must NOT change the brain-facing
    epoch the next mutation resolves V-indices against."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    frozen = object()
    s._last_vision_response = frozen
    s.freeze_vision_epoch()
    # Simulate a background prefetch landing with a different response.
    s._last_vision_response = object()
    # Epoch must still point at the frozen one.
    assert s._vision_epoch_response is frozen
    assert s.vision_for_target_resolution() is frozen
    print("✓ test_epoch_survives_last_vision_overwrite")


def test_vision_for_target_resolution_falls_back() -> None:
    """Before any freeze (fresh session, never emitted a vision-carrying
    tool reply), V-index readers must fall back to _last_vision_response."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    assert s._vision_epoch_response is None
    latest = object()
    s._last_vision_response = latest
    assert s.vision_for_target_resolution() is latest
    print("✓ test_vision_for_target_resolution_falls_back")


def test_navigate_clears_epoch() -> None:
    """advance_observation_token('navigate') must clear the epoch — the
    previous brain-facing snapshot belongs to a different page."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_vision_response = object()
    s.freeze_vision_epoch()
    assert s._vision_epoch_response is not None
    s.advance_observation_token("navigate")
    assert s._vision_epoch_response is None
    assert s._vision_epoch_url == ""
    print("✓ test_navigate_clears_epoch")


def test_rewind_clears_epoch() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_vision_response = object()
    s.freeze_vision_epoch()
    s.advance_observation_token("rewind")
    assert s._vision_epoch_response is None
    print("✓ test_rewind_clears_epoch")


def test_click_does_not_clear_epoch() -> None:
    """Lighter mutations (click/type/scroll) keep the epoch alive so
    in-flight tool calls can still resolve their own V-index. The NEXT
    tool reply will freeze a new epoch once its own vision lands."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_vision_response = object()
    s.freeze_vision_epoch()
    frozen = s._vision_epoch_response
    s.advance_observation_token("click")
    assert s._vision_epoch_response is frozen
    s.advance_observation_token("type")
    assert s._vision_epoch_response is frozen
    print("✓ test_click_does_not_clear_epoch")


def test_reset_per_session_clears_epoch() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_vision_response = object()
    s.freeze_vision_epoch()
    assert s._vision_epoch_id >= 1
    s.reset_per_session()
    assert s._vision_epoch_response is None
    assert s._vision_epoch_url == ""
    assert s._vision_epoch_id == 0
    print("✓ test_reset_per_session_clears_epoch")


def main() -> int:
    tests = [
        test_freeze_captures_current_response,
        test_epoch_survives_last_vision_overwrite,
        test_vision_for_target_resolution_falls_back,
        test_navigate_clears_epoch,
        test_rewind_clears_epoch,
        test_click_does_not_clear_epoch,
        test_reset_per_session_clears_epoch,
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
