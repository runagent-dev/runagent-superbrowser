"""
Action-repetition + DOM-stagnation loop detector.

Replaces the previous ad-hoc consecutive_click counter with a generic
approach adapted from browser-use agent/service.py loop detection:

  - Action hash = (tool_name, sorted(args_json)). If the same hash fires 3+
    times within the last 5 iterations, the agent is stuck. Read-only /
    polling tools are exempt — repeatedly calling them is legitimate waiting
    for async content, not a loop.
  - DOM/URL fingerprint. If URL + a hash of normalized page content stays
    identical for ≥ STAGNATION_LIMIT consecutive iterations, the agent
    isn't making progress.

Both detectors emit escalating guidance strings the hook layer injects into
the LLM conversation. Escalations suggest a concrete recovery action
(different selector, wait, reload, switch tool) — NOT surrender — because
asking the agent to "call done() with partial results" produces
confabulated answers on sites that just need more patience.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
from typing import Deque


REPEAT_WINDOW = 5           # look at the last N iterations
REPEAT_THRESHOLD = 3        # same hash this many times within window
STAGNATION_LIMIT = 6        # consecutive unchanged (url, fingerprint) rounds

# Read-only / polling tools. Repeatedly calling these is how the agent waits
# for JS-heavy pages to render — not a loop. Exempt from action-hash detection.
READONLY_TOOLS = frozenset({
    "browser_get_markdown",
    "browser_get_state",
    "browser_eval",
    "browser_wait_for",
    "browser_screenshot",
    "browser_detect_captcha",
})


@dataclass
class LoopDetector:
    """Per-worker loop detection state. One instance per BrowserWorkerHook."""

    # --- action repetition ---
    _action_hashes: Deque[str] = field(default_factory=lambda: deque(maxlen=REPEAT_WINDOW))
    _action_nudge_count: int = 0

    # --- page-state stagnation ---
    _last_fingerprint: str = ""
    _last_url: str = ""
    _stagnation_count: int = 0
    _stagnation_nudge_count: int = 0

    # --- hashing helpers -------------------------------------------------

    @staticmethod
    def hash_action(tool_name: str, args: dict | None) -> str:
        """Stable hash of (tool_name, canonical_args)."""
        try:
            canon = json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            canon = str(args)
        return hashlib.sha1(f"{tool_name}:{canon}".encode()).hexdigest()[:16]

    @staticmethod
    def hash_page(url: str, content_text: str) -> str:
        """Fingerprint for DOM-stagnation check.

        Uses normalized whitespace and up to 4000 chars so scrolling / async
        content loads show up as different fingerprints. The old 500-char
        snippet matched too often on long results pages where the top of the
        document never changes between scrolls.
        """
        normalized = " ".join((content_text or "").split())[:4000]
        return hashlib.sha1(f"{url}|{normalized}".encode("utf-8", errors="ignore")).hexdigest()[:16]

    # --- detection -------------------------------------------------------

    def record_action(self, tool_name: str, args: dict | None) -> str | None:
        """Record one tool call. Returns a guidance string if a loop is detected."""
        # Polling read-only tools repeatedly is legitimate. Skip them entirely.
        if tool_name in READONLY_TOOLS:
            return None

        h = self.hash_action(tool_name, args)
        self._action_hashes.append(h)
        repeats = sum(1 for x in self._action_hashes if x == h)
        if repeats < REPEAT_THRESHOLD:
            return None

        self._action_nudge_count += 1
        if self._action_nudge_count == 1:
            return (
                f"[LOOP: you have called {tool_name}({str(args)[:80]}) "
                f"{repeats} times in a row. Change tactic — try a different "
                "selector, a different tool, or browser_wait_for the expected content.]"
            )
        if self._action_nudge_count == 2:
            return (
                "[LOOP-ESCALATE: same action repeated again. Try a DIFFERENT "
                "approach: a new selector, browser_wait_for for expected content, "
                "reload via browser_navigate to the same URL, or combine multiple "
                "steps in a single browser_run_script. Do NOT fabricate data.]"
            )
        return (
            "[LOOP-CRITICAL: action has repeated many times. Switch strategy — "
            "try a different tool or selector entirely. Only return done(success=False) "
            "if you have verified the data is genuinely unreachable (not just slow).]"
        )

    def record_page_state(self, url: str, content_text: str) -> str | None:
        """Record page state. Returns a guidance string if stagnation detected."""
        fp = self.hash_page(url, content_text)
        if fp == self._last_fingerprint and url == self._last_url:
            self._stagnation_count += 1
        else:
            self._stagnation_count = 1
            self._last_fingerprint = fp
            self._last_url = url
        if self._stagnation_count < STAGNATION_LIMIT:
            return None

        self._stagnation_nudge_count += 1
        if self._stagnation_nudge_count == 1:
            return (
                f"[STAGNATION: the page has not changed in {self._stagnation_count} "
                "iterations. Either your action had no effect or you are on the "
                "wrong page. Try: (1) browser_eval to check DOM mutation, "
                "(2) browser_wait_for for the expected content, or "
                "(3) navigate elsewhere if this page won't yield the answer.]"
            )
        return (
            "[STAGNATION-ESCALATE: page still unchanged. Try a CONCRETE recovery: "
            "browser_wait_for for dynamic content, reload via browser_navigate, "
            "navigate to a different URL, or use browser_run_script to interact. "
            "Do NOT fabricate data. If this page genuinely cannot yield the answer, "
            "navigate elsewhere or return done(success=False) with an honest reason.]"
        )

    def reset_action_nudge(self) -> None:
        """Called when the agent actually varies its action; resets soft counter."""
        self._action_nudge_count = 0
