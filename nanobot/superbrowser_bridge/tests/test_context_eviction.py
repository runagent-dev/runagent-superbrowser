"""Phase 3 context-eviction tests.

Older tool messages should have ``[VISION ...]`` / ``[BRIEF]`` /
``[CHECKLIST]`` / ``[FOCUS_BBOX]`` / ``[ACTION_DELTA]`` blocks stripped
and replaced with a one-line placeholder reconstructed from
step_history. The most recent ``keep_turns`` tool messages stay fully
intact — V_n indices in the next click resolve against the most recent
``[VISION]`` block, so it MUST survive.
"""

from __future__ import annotations

import pytest

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.worker_hook import (
    _evict_old_tool_messages,
    _evict_stale_text,
    _step_one_liner,
)


_SAMPLE_VISION_BLOCK = """Captured screenshot | Page: https://example.com | Title: Example

[VISION  intent=observe  page_type=search-results  cached=false  model=gemini-2.5-flash  dur=842ms]
Summary: Search results page with 12 listings.
Flags: captcha=none  modal=false  error=none  loading=false  login_wall=false
Interactive elements:
  [V1] button "Search"     (200,40 → 280,80)
  [V2] input "Email"       (50,100 → 250,140)
  [V3] link "Sign in"      (300,40 → 380,80)

[BRIEF v=2 done=1/3]
- find headphones (PENDING)
- under 50 USD (DONE)
- sort by rating (PENDING)

[CHECKLIST]
[ ] find headphones
[X] under 50 USD
[ ] sort by rating

[FOCUS_BBOX V2 "Email" match=0.85]
[GUIDANCE: 30 iterations left out of 50.]
"""


def test_evict_strips_vision_through_end():
    out = _evict_stale_text(_SAMPLE_VISION_BLOCK, "[evicted: foo]")
    assert "[V1]" not in out
    assert "[BRIEF" not in out
    assert "[CHECKLIST" not in out
    assert "[FOCUS_BBOX" not in out
    assert "[GUIDANCE" not in out
    # Action summary preserved.
    assert "Captured screenshot" in out
    assert "Page: https://example.com" in out
    assert "[evicted: foo]" in out


def test_evict_passthrough_when_no_evictable_tag():
    plain = "Eval result: 42"
    out = _evict_stale_text(plain, "[evicted: foo]")
    assert out == plain


def test_evict_passthrough_preserves_lowercase_tool_markers():
    """`[click_at_failed:...]` is lowercase → not evictable, must survive."""
    text = (
        "Clicked V3 (450,200) snapped→#submit\n"
        "[click_silent reason=no_dom_change] retry suggested"
    )
    out = _evict_stale_text(text, "[evicted]")
    assert out == text  # No CAPS guidance tag present.


def test_step_one_liner_compact_format():
    step = {
        "tool": "browser_click_at",
        "args": "vision_index=3",
        "result": "Clicked button 'Sign in' at (450, 200)",
    }
    out = _step_one_liner(step)
    assert out.startswith("[evicted: browser_click_at(")
    assert "Sign in" in out


def test_step_one_liner_handles_none():
    out = _step_one_liner(None)
    assert "evicted" in out


def _mk_msg(text: str) -> dict:
    return {"role": "tool", "content": text}


def test_keep_turns_preserves_recent_evicts_old():
    state = BrowserSessionState()
    state.step_history = [
        {"tool": "browser_screenshot", "args": "intent=observe", "result": "Captured 1"},
        {"tool": "browser_click_at", "args": "V_n=3", "result": "Clicked link"},
        {"tool": "browser_screenshot", "args": "intent=verify", "result": "Captured 2"},
        {"tool": "browser_click_at", "args": "V_n=1", "result": "Clicked button"},
        {"tool": "browser_screenshot", "args": "intent=verify", "result": "Captured 3"},
    ]
    messages = [
        {"role": "user", "content": "do task"},
        _mk_msg(_SAMPLE_VISION_BLOCK),
        _mk_msg("Clicked V3"),
        _mk_msg(_SAMPLE_VISION_BLOCK),
        _mk_msg("Clicked V1"),
        _mk_msg(_SAMPLE_VISION_BLOCK),
    ]

    _evict_old_tool_messages(messages, state, keep_turns=3)

    # Most-recent 3 tool msgs (indices 3, 4, 5) intact.
    assert "[V1]" in messages[5]["content"]
    assert "[BRIEF" in messages[5]["content"]
    assert messages[4]["content"] == "Clicked V1"  # No evictable tags.
    assert "[V1]" in messages[3]["content"]
    # Older 2 tool msgs (indices 1, 2) have evictable content stripped.
    assert "[V1]" not in messages[1]["content"]
    assert "[BRIEF" not in messages[1]["content"]
    assert "[evicted" in messages[1]["content"]


def test_eviction_handles_list_content():
    state = BrowserSessionState()
    state.step_history = [
        {"tool": "browser_click_at", "args": "V_n=1", "result": "ok"},
        {"tool": "browser_screenshot", "args": "intent=v", "result": "shot"},
    ]
    list_content = [
        {"type": "text", "text": _SAMPLE_VISION_BLOCK},
        {"type": "text", "text": "trailing free-form note"},
    ]
    messages = [
        {"role": "tool", "content": list_content},
        {"role": "tool", "content": "Latest action"},
    ]

    _evict_old_tool_messages(messages, state, keep_turns=1)

    # The list-content message is older (age=1) → text blocks evicted.
    first_block_text = messages[0]["content"][0]["text"]
    assert "[V1]" not in first_block_text
    assert "[evicted" in first_block_text
    # Most-recent (age=0) preserved.
    assert messages[1]["content"] == "Latest action"


def test_keep_turns_zero_means_no_eviction():
    """Defensive: keep_turns < 1 should be a no-op, not crash."""
    state = BrowserSessionState()
    messages = [
        {"role": "tool", "content": _SAMPLE_VISION_BLOCK},
    ]
    _evict_old_tool_messages(messages, state, keep_turns=0)
    # Function returns early on keep_turns < 1.
    # All content unchanged.
    assert messages[0]["content"] == _SAMPLE_VISION_BLOCK
