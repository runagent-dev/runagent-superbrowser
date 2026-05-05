"""Tests for the inter-action wallclock sleep.

User asked for "slow slow" pacing — a real wallclock pause before each
mutating tool dispatches. This (a) lets page state settle from the
prior action (lazy loads, debounced filter reflows), (b) gives the
brain effective extra thinking time, (c) provides time for the
auto-vision-on-delta force-refresh to complete before the brain reads
the response.
"""

from __future__ import annotations

import asyncio
import os
import time

from superbrowser_bridge.session_tools import BrowserSessionState


def _run(coro):
    # Match test_type_verify's pattern to avoid polluting the global
    # event loop. asyncio.run() closes the loop on exit which breaks
    # subsequent tests that use asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_inter_action_pause_default_sleeps():
    """Default INTER_ACTION_DELAY_MS_DEFAULT is 1500ms; we override to
    a small value so the test runs fast."""
    s = BrowserSessionState()
    os.environ["INTER_ACTION_DELAY_MS"] = "120"
    try:
        start = time.monotonic()
        _run(s.inter_action_pause())
        elapsed_ms = (time.monotonic() - start) * 1000
        # Allow scheduling jitter — assert at least most of the budget
        # was spent sleeping.
        assert elapsed_ms >= 100, f"slept {elapsed_ms:.0f}ms; expected ≥100"
    finally:
        del os.environ["INTER_ACTION_DELAY_MS"]


def test_inter_action_pause_zero_is_noop():
    s = BrowserSessionState()
    os.environ["INTER_ACTION_DELAY_MS"] = "0"
    try:
        start = time.monotonic()
        _run(s.inter_action_pause())
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50, f"slept {elapsed_ms:.0f}ms; expected ~0"
    finally:
        del os.environ["INTER_ACTION_DELAY_MS"]


def test_inter_action_pause_invalid_env_falls_back_to_default():
    """Bad INTER_ACTION_DELAY_MS values shouldn't crash — the helper
    catches ValueError and falls back to the class default."""
    s = BrowserSessionState()
    os.environ["INTER_ACTION_DELAY_MS"] = "not-a-number"
    try:
        # Default is 1500ms but we don't want to sleep that long in
        # tests — instead patch the class default temporarily.
        original = BrowserSessionState.INTER_ACTION_DELAY_MS_DEFAULT
        BrowserSessionState.INTER_ACTION_DELAY_MS_DEFAULT = 80
        try:
            start = time.monotonic()
            _run(s.inter_action_pause())
            elapsed_ms = (time.monotonic() - start) * 1000
            assert elapsed_ms >= 70, f"slept {elapsed_ms:.0f}ms; expected ≥70"
        finally:
            BrowserSessionState.INTER_ACTION_DELAY_MS_DEFAULT = original
    finally:
        del os.environ["INTER_ACTION_DELAY_MS"]


def test_default_constant_is_at_least_one_second():
    """Sanity: the default should give the brain real thinking time + the
    auto-vision force-refresh window."""
    assert BrowserSessionState.INTER_ACTION_DELAY_MS_DEFAULT >= 1000
