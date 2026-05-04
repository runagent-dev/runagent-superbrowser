"""Unit tests for Arch v4 Phase F: click_at auto-retry once with
fresh bbox.

Covers _find_best_label_match (the fuzzy-matcher) and the retry
plumbing's interaction with the preplan + freshness gates. Full
HTTP-driven retry (call into BrowserClickAtTool.execute → external
/state endpoint → vision) is mocked at the network boundary.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_bbox_auto_retry
"""

from __future__ import annotations

import asyncio
import os
import sys


# ── _find_best_label_match: fuzzy matcher ──────────────────────────


class _FakeBox:
    def __init__(self, label):
        self.label = label


def test_find_match_exact_substring() -> None:
    from superbrowser_bridge.session_tools import _find_best_label_match
    bboxes = [_FakeBox("Cancel"), _FakeBox("Save changes"),
              _FakeBox("WiFi filter chip")]
    # Substring (target ⊆ label).
    assert _find_best_label_match(bboxes, "WiFi") == 3
    # Exact case-insensitive equality counts as substring too.
    assert _find_best_label_match(bboxes, "save changes") == 2


def test_find_match_levenshtein_typo() -> None:
    from superbrowser_bridge.session_tools import _find_best_label_match
    bboxes = [_FakeBox("Search"), _FakeBox("Sort")]
    # "Searh" → "Search" Levenshtein 1 ≤ 2.
    assert _find_best_label_match(bboxes, "Searh") == 1


def test_find_match_token_overlap() -> None:
    from superbrowser_bridge.session_tools import _find_best_label_match
    # Two labels share two of three tokens. Pure token-overlap path.
    bboxes = [
        _FakeBox("Search results page"),
        _FakeBox("Filter results panel"),  # 2/3 token overlap with needle
    ]
    assert _find_best_label_match(bboxes, "filter results dialog") == 2


def test_find_match_separator_normalized_substring() -> None:
    """'Wi-Fi' → 'wifi' should match 'WiFi filter chip' via the
    separator-stripped substring path."""
    from superbrowser_bridge.session_tools import _find_best_label_match
    bboxes = [_FakeBox("Cancel"), _FakeBox("WiFi filter chip")]
    assert _find_best_label_match(bboxes, "Wi-Fi") == 2


def test_find_match_returns_neg_one_when_no_match() -> None:
    from superbrowser_bridge.session_tools import _find_best_label_match
    bboxes = [_FakeBox("Cancel"), _FakeBox("Save")]
    assert _find_best_label_match(bboxes, "WiFi filter chip") == -1


def test_find_match_skips_empty_labels() -> None:
    from superbrowser_bridge.session_tools import _find_best_label_match
    bboxes = [_FakeBox(""), _FakeBox(None), _FakeBox("WiFi")]
    assert _find_best_label_match(bboxes, "WiFi") == 3


def test_find_match_returns_neg_one_on_empty_inputs() -> None:
    from superbrowser_bridge.session_tools import _find_best_label_match
    assert _find_best_label_match([], "WiFi") == -1
    assert _find_best_label_match([_FakeBox("WiFi")], "") == -1


# ── label tokenizer ────────────────────────────────────────────────


def test_label_tokens_strips_separators_and_short_tokens() -> None:
    from superbrowser_bridge.session_tools import _label_tokens
    assert _label_tokens("Wi-Fi filter, chip") == {"filter", "chip"}
    # "wifi" is 4 chars (≥3) so it survives but is dropped from
    # "Wi-Fi" hyphen-split as "wi" + "fi" (each <3) so neither survives.
    assert _label_tokens("WiFi parking") == {"wifi", "parking"}


def test_levenshtein_handles_basics() -> None:
    from superbrowser_bridge.session_tools import _levenshtein
    assert _levenshtein("", "") == 0
    assert _levenshtein("abc", "") == 3
    assert _levenshtein("", "xyz") == 3
    assert _levenshtein("abc", "abc") == 0
    assert _levenshtein("kitten", "sitting") == 3
    assert _levenshtein("dhakka", "dhaka") == 1


# ── gate skip during internal_retry ────────────────────────────────


def test_gates_skip_under_internal_retry_flag() -> None:
    """When _bbox_auto_retry_in_flight is True, both freshness and
    preplan gates yield without consuming state."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    # Set state that would normally trigger refusals: dirty + consumed.
    s.dom_dirty_since_screenshot = True
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0, planned_tool="click_at",
    )
    s.preplan_lock_consumed = True
    # Without the flag, both refuse.
    assert s.must_screenshot_before_state_change("browser_click_at") is not None
    # With the flag, gate yields.
    s._bbox_auto_retry_in_flight = True
    assert s.must_screenshot_before_state_change("browser_click_at") is None


def test_internal_retry_does_not_double_consume_preplan_lock() -> None:
    """A retry covered by an existing preplan should not re-consume
    or refresh the lock."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    # Lock active, NOT consumed yet.
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0, planned_tool="click_at",
    )
    s.preplan_lock_consumed = False
    # First call (the original click): allowed, consumes lock.
    assert s.must_screenshot_before_state_change("browser_click_at") is None
    assert s.preplan_lock_consumed is True
    # Now simulate a retry: gate skips because of the flag, leaving
    # preplan_consecutive_refusals at 0 and lock state untouched.
    refusals_before = s.preplan_consecutive_refusals
    s.dom_dirty_since_screenshot = True  # simulate retry's pre-state
    s._bbox_auto_retry_in_flight = True
    out = s.must_screenshot_before_state_change("browser_click_at")
    assert out is None
    assert s.preplan_consecutive_refusals == refusals_before
    assert s.preplan_lock_consumed is True  # still consumed (no flip)


# ── BBOX_AUTO_RETRY_MAX cap ────────────────────────────────────────


def test_retry_max_cap_returns_none_when_exhausted() -> None:
    """Once the per-session retry budget is hit, _attempt_bbox_auto_
    retry returns None so the original failure path is taken."""
    from superbrowser_bridge.session_tools import (
        BrowserClickAtTool, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s._bbox_auto_retry_attempts = 1  # already used the default budget
    tool = BrowserClickAtTool(s)
    out = asyncio.run(tool._attempt_bbox_auto_retry(
        session_id="sid",
        original_vision_index=3,
        target_label="WiFi",
    ))
    assert out is None


def test_retry_max_zero_disables() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserClickAtTool, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s._bbox_auto_retry_attempts = 0
    os.environ["BBOX_AUTO_RETRY_MAX"] = "0"
    try:
        tool = BrowserClickAtTool(s)
        out = asyncio.run(tool._attempt_bbox_auto_retry(
            session_id="sid",
            original_vision_index=3,
            target_label="WiFi",
        ))
        assert out is None
    finally:
        del os.environ["BBOX_AUTO_RETRY_MAX"]


# ── No-match path returns explicit caption ─────────────────────────


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


def _make_fake_request(payload):
    async def _fake_request(*args, **kw):
        return _FakeResp(payload)
    return _fake_request


class _FakeAgent:
    def __init__(self, vresp):
        self._vresp = vresp
    async def analyze(self, **kw):
        return self._vresp


def test_retry_no_match_caption_when_label_gone() -> None:
    """When fresh vision returns bboxes but none match the label,
    the helper returns [BBOX_AUTO_RETRY_NO_MATCH] (not None) so the
    brain sees an explicit signal to switch tactics."""
    from unittest.mock import patch
    from superbrowser_bridge.session_tools import (
        BrowserClickAtTool, BrowserSessionState,
    )

    class _Vresp:
        bboxes = [_FakeBox("Cancel"), _FakeBox("Save")]

    s = BrowserSessionState()
    s.session_id = "sid"
    tool = BrowserClickAtTool(s)
    with patch(
        "superbrowser_bridge.session_tools._request_with_backoff",
        new=_make_fake_request({"screenshot": "B64", "url": "x"}),
    ), patch(
        "vision_agent.get_vision_agent",
        return_value=_FakeAgent(_Vresp()),
    ), patch(
        "vision_agent.vision_agent_enabled",
        return_value=True,
    ):
        out = asyncio.run(tool._attempt_bbox_auto_retry(
            session_id="sid",
            original_vision_index=3,
            target_label="WiFi filter chip",
        ))
    assert out is not None
    assert "[BBOX_AUTO_RETRY_NO_MATCH" in out
    assert "WiFi filter chip" in out


def test_retry_no_match_caption_when_bboxes_empty() -> None:
    """When fresh vision returns no bboxes at all, helper still
    returns the no-match caption (different remediation hint)."""
    from unittest.mock import patch
    from superbrowser_bridge.session_tools import (
        BrowserClickAtTool, BrowserSessionState,
    )

    class _Empty:
        bboxes = []

    s = BrowserSessionState()
    s.session_id = "sid"
    tool = BrowserClickAtTool(s)
    with patch(
        "superbrowser_bridge.session_tools._request_with_backoff",
        new=_make_fake_request({"screenshot": "B64", "url": "x"}),
    ), patch(
        "vision_agent.get_vision_agent",
        return_value=_FakeAgent(_Empty()),
    ), patch(
        "vision_agent.vision_agent_enabled",
        return_value=True,
    ):
        out = asyncio.run(tool._attempt_bbox_auto_retry(
            session_id="sid",
            original_vision_index=3,
            target_label="WiFi",
        ))
    assert out is not None
    assert "[BBOX_AUTO_RETRY_NO_MATCH" in out


def main() -> int:
    tests = [
        # _find_best_label_match
        test_find_match_exact_substring,
        test_find_match_levenshtein_typo,
        test_find_match_token_overlap,
        test_find_match_separator_normalized_substring,
        test_find_match_returns_neg_one_when_no_match,
        test_find_match_skips_empty_labels,
        test_find_match_returns_neg_one_on_empty_inputs,
        # tokenizer + Levenshtein
        test_label_tokens_strips_separators_and_short_tokens,
        test_levenshtein_handles_basics,
        # gate skip via flag
        test_gates_skip_under_internal_retry_flag,
        test_internal_retry_does_not_double_consume_preplan_lock,
        # max cap
        test_retry_max_cap_returns_none_when_exhausted,
        test_retry_max_zero_disables,
        # no-match path
        test_retry_no_match_caption_when_label_gone,
        test_retry_no_match_caption_when_bboxes_empty,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERR  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
