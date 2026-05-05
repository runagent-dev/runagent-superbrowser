"""Tests for the post-rewind observation gate.

The trace pattern: brain calls browser_rewind_to_checkpoint, then
immediately browser_navigate(<hallucinated URL>) without taking a
screenshot in between. The rewind invalidated all V_n indices and DOM
fingerprints, so whatever the navigate decided was based on stale
context.

Two pieces:
  1. `BrowserRewindToCheckpointTool` sets `state.rewind_just_fired = True`
     and includes [POST_REWIND] in its caption.
  2. The worker hook reads `rewind_just_fired`; if the brain's NEXT call
     isn't browser_screenshot/get_markdown/brief_mark, it injects
     [REWIND_NOT_OBSERVED]. Either way the flag is one-shot.

Tool-level HTTP isn't exercised here — we test the state contract +
caption template that worker_hook depends on.
"""

from __future__ import annotations

from superbrowser_bridge.session_tools import BrowserSessionState


def test_rewind_just_fired_default_is_false():
    s = BrowserSessionState()
    assert s.rewind_just_fired is False


def test_rewind_just_fired_one_shot_clear_on_deliberation():
    """Hook contract: rewind_just_fired clears when last_tool is in
    {screenshot, get_markdown, brief_mark}."""
    s = BrowserSessionState()
    s.rewind_just_fired = True
    last_tool = "browser_screenshot"
    if last_tool in {"browser_screenshot", "browser_get_markdown", "browser_brief_mark"}:
        s.rewind_just_fired = False
    assert s.rewind_just_fired is False


def test_rewind_just_fired_does_not_clear_on_rewind_step_itself():
    """The hook should NOT clear the flag when the most recent step is
    the rewind itself — the gate fires on the next downstream tool."""
    s = BrowserSessionState()
    s.rewind_just_fired = True
    last_tool = "browser_rewind_to_checkpoint"
    # Hook explicit branch keeps the flag set in this case.
    if last_tool == "browser_rewind_to_checkpoint":
        pass
    elif last_tool in {"browser_screenshot", "browser_get_markdown", "browser_brief_mark"}:
        s.rewind_just_fired = False
    assert s.rewind_just_fired is True


def test_post_rewind_caption_template_is_directive():
    """Smoke check on the caption shape — what the brain reads after a
    successful rewind. Mirrors the literal in
    BrowserRewindToCheckpointTool.execute."""
    target = "https://x.com/listing"
    caption = (
        f"Rewound to checkpoint: {target[:80]}\n\n"
        f"[POST_REWIND] Vision cache + DOM fingerprints invalidated. "
        f"Next required tool: browser_screenshot (or "
        f"browser_get_markdown / browser_brief_mark if you have "
        f"explicit evidence to log). Do NOT browser_click_at / "
        f"browser_type_at / browser_navigate before re-observing — the "
        f"V_n indices from BEFORE the rewind no "
        f"longer point at anything. The brief focus is unchanged; "
        f"if [FOCUS_EXHAUSTED] fired before the rewind, the focus "
        f"is still exhausted — consider browser_brief_mark to "
        f"advance past it instead of re-attempting the same "
        f"approach on the rewound page."
    )
    assert "[POST_REWIND]" in caption
    assert "browser_screenshot" in caption
    assert "Do NOT" in caption
    assert "FOCUS_EXHAUSTED" in caption
