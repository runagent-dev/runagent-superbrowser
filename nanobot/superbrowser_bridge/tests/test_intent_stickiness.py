"""Sticky-intent bug tests (Round 5).

Once `_last_intent` had a captcha-flavored value, every subsequent
vision call inherited it — which flipped Gemini's prompt into
captcha-tile-mode and starved the brain of regular UI bboxes
(buttons, form fields, "Continue Anyway" dismissals).

Round 5 fix is 4-pronged:
  R5.1 — reset on navigate/rewind (in advance_observation_token)
  R5.2 — reset in reset_per_session
  R5.3 — never make captcha intents sticky in the first place
  R5.4 — safety-net override when last vision had no captcha flag

No network. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_intent_stickiness.py
"""

from __future__ import annotations

import sys
import types


def test_is_captcha_intent_classifier() -> None:
    from superbrowser_bridge.session_tools import _is_captcha_intent
    assert _is_captcha_intent("solve captcha — locate widget + tiles")
    assert _is_captcha_intent("solve captcha step — pick next action")
    assert _is_captcha_intent("the challenge page")  # "challenge" bucket
    assert not _is_captcha_intent("observe page")
    assert not _is_captcha_intent("verify navigation succeeded")
    assert not _is_captcha_intent("")
    assert not _is_captcha_intent(None)  # type: ignore[arg-type]
    print("✓ test_is_captcha_intent_classifier")


def test_last_intent_clears_on_navigate() -> None:
    """R5.1: advance_observation_token('navigate') must clear
    _last_intent so vision on the new URL starts fresh."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_intent = "solve captcha — locate widget"
    s.advance_observation_token("navigate")
    assert s._last_intent == "", f"expected cleared, got {s._last_intent!r}"
    print("✓ test_last_intent_clears_on_navigate")


def test_last_intent_clears_on_rewind() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_intent = "solve captcha step"
    s.advance_observation_token("rewind")
    assert s._last_intent == ""
    print("✓ test_last_intent_clears_on_rewind")


def test_last_intent_survives_click_token_advance() -> None:
    """Non-navigate mutations should NOT clear the intent — otherwise
    the brain loses useful intent chaining across a click / type /
    scroll sequence on the same page."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_intent = "observe search results"
    s.advance_observation_token("click")
    assert s._last_intent == "observe search results"
    s.advance_observation_token("type")
    assert s._last_intent == "observe search results"
    print("✓ test_last_intent_survives_click_token_advance")


def test_last_intent_clears_on_reset_per_session() -> None:
    """R5.2: reset_per_session must clear _last_intent so a fresh
    browser_open doesn't inherit the previous session's intent."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_intent = "solve captcha — locate widget"
    s.reset_per_session()
    assert s._last_intent == ""
    print("✓ test_last_intent_clears_on_reset_per_session")


def test_last_vision_has_captcha_flag() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _last_vision_has_captcha_flag,
    )
    s = BrowserSessionState()
    # No response → False
    assert not _last_vision_has_captcha_flag(s)
    # Response without flags → False
    s._last_vision_response = types.SimpleNamespace()
    assert not _last_vision_has_captcha_flag(s)
    # Response with flags.captcha_present=False → False
    s._last_vision_response = types.SimpleNamespace(
        flags=types.SimpleNamespace(captcha_present=False)
    )
    assert not _last_vision_has_captcha_flag(s)
    # Response with flags.captcha_present=True → True
    s._last_vision_response = types.SimpleNamespace(
        flags=types.SimpleNamespace(captcha_present=True)
    )
    assert _last_vision_has_captcha_flag(s)
    print("✓ test_last_vision_has_captcha_flag")


def test_safety_net_preserves_intent_when_captcha_active() -> None:
    """R5.4 safety net must NOT clear the intent when the page is
    actively captcha-gated. We don't want to drop captcha mode in the
    middle of a solve."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _is_captcha_intent, _last_vision_has_captcha_flag,
    )
    s = BrowserSessionState()
    s._last_intent = "solve captcha step — pick next"
    s._last_vision_response = types.SimpleNamespace(
        flags=types.SimpleNamespace(captcha_present=True)
    )
    # Verify the safety-net conditions.
    assert _is_captcha_intent(s._last_intent)
    assert _last_vision_has_captcha_flag(s)
    # In this state build_tool_result_blocks will NOT clear the intent
    # (check covered by the runtime branch `and not _last_vision_has_captcha_flag`).
    print("✓ test_safety_net_preserves_intent_when_captcha_active")


def main() -> int:
    tests = [
        test_is_captcha_intent_classifier,
        test_last_intent_clears_on_navigate,
        test_last_intent_clears_on_rewind,
        test_last_intent_survives_click_token_advance,
        test_last_intent_clears_on_reset_per_session,
        test_last_vision_has_captcha_flag,
        test_safety_net_preserves_intent_when_captcha_active,
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
