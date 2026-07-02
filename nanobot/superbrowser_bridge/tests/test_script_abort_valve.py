"""Hard-abort valve for a pathological browser_eval / browser_run_script loop.

A weak model can ignore the `[script_warning]` advisory and loop on read-only
scripts (no cursor progress) until the 900s wall-clock, draining the whole token
budget for a guaranteed failure (model-split eval: 86 iters / ~2.9M tokens →
timeout). `_consec_script_abort_threshold` bounds that: at N consecutive script
calls the tool raises `WorkerMustExitError`, which delegation.py turns into a
clean failure. The abort fires BEFORE the network POST, so this test needs no
live server.

Run:
    source venv/bin/activate && PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_script_abort_valve.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

from superbrowser_bridge.session_tools.effects import WorkerMustExitError
from superbrowser_bridge.session_tools.tools.scripting import (
    BrowserEvalTool,
    _consec_script_abort_threshold,
)


def _stub_state(consecutive: int) -> SimpleNamespace:
    return SimpleNamespace(
        actions_since_screenshot=0,
        consecutive_click_calls=0,
        consecutive_script_calls=consecutive,
        log_activity=lambda *a, **k: None,
        record_step=lambda *a, **k: None,
    )


def test_threshold_env_reader() -> None:
    os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"] = "5"
    try:
        assert _consec_script_abort_threshold() == 5
    finally:
        del os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"]
    os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"] = "0"
    try:
        assert _consec_script_abort_threshold() == 0  # disabled
    finally:
        del os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"]
    os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"] = "notanint"
    try:
        assert _consec_script_abort_threshold() == 16  # falls back to default
    finally:
        del os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"]
    print("✓ test_threshold_env_reader")


def test_aborts_at_threshold() -> None:
    """At the Nth consecutive script call the tool raises before any network I/O."""
    os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"] = "5"
    try:
        stub = _stub_state(consecutive=4)  # this call increments to 5 == threshold
        tool = BrowserEvalTool(stub)
        raised = False
        try:
            asyncio.run(tool.execute(session_id="s", script="1+1"))
        except WorkerMustExitError:
            raised = True
        assert raised, "expected WorkerMustExitError at the threshold"
        assert stub.consecutive_script_calls == 5
    finally:
        del os.environ["SUPERBROWSER_MAX_CONSEC_SCRIPTS"]
    print("✓ test_aborts_at_threshold")


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
