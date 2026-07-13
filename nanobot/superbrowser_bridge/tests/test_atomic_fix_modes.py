"""Unit tests for the parameterized atomic text-JS template (P0.1 / Phase 1).

`render_atomic_text_js` gained `mode` (replace | append | delete_tail) + `count`
so the field's final value is computed from the live value INSIDE one JS tick
(race-free), and now emits `method` / `is_editable` / `editor` / `mode`. These
tests assert the rendered JS is well-formed and carries the mode-specific logic;
they do NOT need a browser (pure string checks + optional node --check).

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_atomic_fix_modes.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile

from superbrowser_bridge.session_tools import render_atomic_text_js


PLACEHOLDERS = ("__TARGET_X__", "__TARGET_Y__", "__TARGET_TEXT__",
                "__MODE__", "__COUNT__")


def test_no_unfilled_placeholders() -> None:
    for mode in ("replace", "append", "delete_tail"):
        js = render_atomic_text_js(10.0, 20.0, "hi", mode=mode, count=2)
        for ph in PLACEHOLDERS:
            assert ph not in js, f"{ph} left unfilled in mode={mode}"
    print("✓ test_no_unfilled_placeholders")


def test_mode_and_count_literals() -> None:
    js = render_atomic_text_js(1.0, 2.0, "x", mode="delete_tail", count=3)
    assert '"delete_tail"' in js, "mode literal missing"
    assert "const _count = 3;" in js, "count literal missing"
    # append mode wires before + text; delete_tail wires slice.
    assert "before + _rawText" in js, "append computation missing"
    assert "before.slice(0" in js, "delete_tail computation missing"
    print("✓ test_mode_and_count_literals")


def test_text_json_escaped() -> None:
    # A value with quotes / newlines must be JSON-encoded, not break the JS.
    js = render_atomic_text_js(1.0, 2.0, 'a"b\nc', mode="replace")
    assert 'const _rawText = "a\\"b\\nc";' in js, js[js.index("_rawText"):][:60]
    print("✓ test_text_json_escaped")


def test_returns_new_fields() -> None:
    js = render_atomic_text_js(1.0, 2.0, "x")
    for field in ("is_editable", "editor", "method", "mode"):
        assert field in js, f"return field {field} missing"
    # Rich-text branch uses execCommand (Phase 2a).
    assert "execCommand('insertText'" in js
    assert "execCommand('delete'" in js
    # Caret parked at end for a follow-up keys.
    assert "setSelectionRange(target.length, target.length)" in js
    # React tracker reset preserved.
    assert "_valueTracker" in js and "setValue('')" in js
    print("✓ test_returns_new_fields")


def test_clear_to_empty_render() -> None:
    # text="" in replace mode is the canonical clear.
    js = render_atomic_text_js(1.0, 2.0, "", mode="replace")
    assert 'const _rawText = "";' in js
    assert '"replace"' in js
    print("✓ test_clear_to_empty_render")


def test_node_syntax_when_available() -> None:
    node = shutil.which("node")
    if not node:
        print("• test_node_syntax_when_available (SKIP: node not found)")
        return
    for mode, text, count in [("replace", "foo", 0), ("append", "x", 0),
                              ("delete_tail", "", 4)]:
        js = render_atomic_text_js(5.0, 6.0, text, mode=mode, count=count)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write("void " + js + ";")
            path = f.name
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"mode={mode}: {r.stderr[:200]}"
    print("✓ test_node_syntax_when_available")


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n{len(fns)}/{len(fns)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
