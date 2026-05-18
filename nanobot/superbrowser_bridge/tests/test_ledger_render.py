"""Locks in StepOutcome.render_line — restored click determinism.

Background: the memory layer was originally caption-only on render —
when caption existed (almost always) the syntactic anchor (V_n /
index=N stored in args) was hidden. After message-history compaction
the LLM lost the only record of which target it picked, so re-runs
diverged. The render now shows ``caption (args) → result`` so the
anchor survives.

These tests pin the render contract and the label-sanitization helper.
"""

from __future__ import annotations

from superbrowser_bridge.memory.ledger import StepOutcome
from superbrowser_bridge.session_tools._label import clean_label


def test_render_line_caption_and_args() -> None:
    step = StepOutcome(
        tool="browser_click_at",
        args='V2|"Shop"',
        result="/store/",
        caption="clicked Shop nav",
        success=True,
    )
    assert (
        step.render_line()
        == '  ✓ clicked Shop nav (V2|"Shop") → /store/  [browser_click_at]'
    )


def test_render_line_caption_only() -> None:
    step = StepOutcome(
        tool="browser_navigate",
        args="",
        result="title=Wines",
        caption="navigated to product list",
        success=True,
    )
    assert (
        step.render_line()
        == "  ✓ navigated to product list → title=Wines  [browser_navigate]"
    )


def test_render_line_args_only() -> None:
    step = StepOutcome(
        tool="browser_click",
        args="index=5",
        result="/cart/",
        caption="",
        success=True,
    )
    assert step.render_line() == "  ✓ browser_click(index=5) → /cart/"


def test_render_line_neither() -> None:
    step = StepOutcome(
        tool="browser_screenshot",
        args="",
        result="ok",
        caption="",
        success=True,
    )
    assert step.render_line() == "  ✓ browser_screenshot → ok"


def test_render_line_failure_marker() -> None:
    step = StepOutcome(
        tool="browser_click_at",
        args='V5|"Region"',
        result="no_effect",
        caption="clicked Filter chip",
        success=False,
    )
    assert (
        step.render_line()
        == '  ✗ clicked Filter chip (V5|"Region") → no_effect  [browser_click_at]'
    )


def test_clean_label_empty() -> None:
    assert clean_label("") == ""
    assert clean_label(None) == ""


def test_clean_label_quotes_swap() -> None:
    # Inner double quotes flip to single so the outer "..." render
    # pair stays well-formed.
    assert clean_label('Say "hello"') == "Say 'hello'"


def test_clean_label_whitespace_collapse() -> None:
    assert clean_label("  Add\tto\ncart  ") == "Add to cart"


def test_clean_label_truncation() -> None:
    long = "x" * 80
    out = clean_label(long)
    assert len(out) == 60
    assert out.endswith("…")
    assert out[:59] == "x" * 59


def test_clean_label_control_chars_dropped() -> None:
    # \x00, \x07 etc are not whitespace — explicit drop.
    assert clean_label("foo\x00bar\x07baz") == "foobarbaz"
