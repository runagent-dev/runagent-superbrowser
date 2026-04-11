"""
Action loop detection for the browser worker agent.

Ported from browser-use (browser_use/agent/views.py) and adapted for
the superbrowser tool interface. Detects repeated actions and page
stagnation, generating escalating nudge messages for the LLM.

This is a soft detection system — it generates guidance messages but
never blocks actions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PageFingerprint:
    """Lightweight fingerprint of the browser page state."""

    url: str
    element_count: int
    text_hash: str  # First 16 chars of SHA-256 of DOM text

    @staticmethod
    def from_page_state(url: str, dom_text: str, element_count: int) -> PageFingerprint:
        text_hash = hashlib.sha256(
            dom_text.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        return PageFingerprint(url=url, element_count=element_count, text_hash=text_hash)


def _normalize_action(tool_name: str, params: dict) -> str:
    """Normalize action parameters for similarity hashing.

    Maps superbrowser tool names to canonical action types and
    normalizes parameters so similar actions produce identical hashes.
    """
    # Map superbrowser tool names to canonical types
    name_map = {
        "browser_click": "click",
        "browser_click_at": "click_at",
        "browser_type": "input",
        "browser_navigate": "navigate",
        "browser_scroll": "scroll",
        "browser_run_script": "script",
        "browser_eval": "eval",
        "browser_keys": "keys",
        "browser_select": "select",
    }
    action = name_map.get(tool_name, tool_name)

    if action == "click":
        index = params.get("index", "")
        return f"click|{index}"

    if action == "click_at":
        x, y = params.get("x", ""), params.get("y", "")
        return f"click_at|{x}|{y}"

    if action == "input":
        index = params.get("index", "")
        text = str(params.get("text", "")).strip().lower()
        return f"input|{index}|{text}"

    if action == "navigate":
        url = str(params.get("url", ""))
        return f"navigate|{url}"

    if action == "scroll":
        direction = params.get("direction", "down")
        return f"scroll|{direction}"

    if action == "script":
        # Hash script content — similar scripts produce similar hashes
        script = str(params.get("script", ""))
        # Normalize whitespace for comparison
        script_norm = re.sub(r"\s+", " ", script.strip().lower())
        script_hash = hashlib.sha256(script_norm.encode()).hexdigest()[:12]
        return f"script|{script_hash}"

    if action == "eval":
        script = str(params.get("script", ""))
        script_norm = re.sub(r"\s+", " ", script.strip().lower())
        script_hash = hashlib.sha256(script_norm.encode()).hexdigest()[:12]
        return f"eval|{script_hash}"

    # Default: hash by action name + sorted params
    filtered = {k: v for k, v in sorted(params.items()) if v is not None}
    return f"{action}|{json.dumps(filtered, sort_keys=True, default=str)}"


def _compute_hash(tool_name: str, params: dict) -> str:
    """Compute a stable hash string for an action."""
    normalized = _normalize_action(tool_name, params)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


@dataclass
class ActionLoopDetector:
    """Tracks action repetition and page stagnation to detect behavioral loops.

    Rolling window of recent action hashes with escalating nudge messages
    at 5, 8, and 12+ repetitions. Also tracks page fingerprints for
    stagnation detection (same page state across multiple actions).
    """

    window_size: int = 20
    recent_action_hashes: list[str] = field(default_factory=list)
    recent_page_fingerprints: list[PageFingerprint] = field(default_factory=list)

    # Current repetition state
    max_repetition_count: int = 0
    most_repeated_hash: str | None = None
    consecutive_stagnant_pages: int = 0

    def record_action(self, tool_name: str, params: dict) -> None:
        """Record an action and update repetition statistics."""
        h = _compute_hash(tool_name, params)
        self.recent_action_hashes.append(h)
        if len(self.recent_action_hashes) > self.window_size:
            self.recent_action_hashes = self.recent_action_hashes[-self.window_size:]
        self._update_stats()

    def record_page_state(self, url: str, elements_text: str, element_count: int) -> None:
        """Record the current page fingerprint and update stagnation count."""
        fp = PageFingerprint.from_page_state(url, elements_text, element_count)
        if self.recent_page_fingerprints and self.recent_page_fingerprints[-1] == fp:
            self.consecutive_stagnant_pages += 1
        else:
            self.consecutive_stagnant_pages = 0
        self.recent_page_fingerprints.append(fp)
        if len(self.recent_page_fingerprints) > 5:
            self.recent_page_fingerprints = self.recent_page_fingerprints[-5:]

    def _update_stats(self) -> None:
        """Recompute max_repetition_count from the current window."""
        if not self.recent_action_hashes:
            self.max_repetition_count = 0
            self.most_repeated_hash = None
            return
        counts: dict[str, int] = {}
        for h in self.recent_action_hashes:
            counts[h] = counts.get(h, 0) + 1
        self.most_repeated_hash = max(counts, key=lambda k: counts[k])
        self.max_repetition_count = counts[self.most_repeated_hash]

    def detect_loop(self) -> str | None:
        """Return an escalating nudge message if a loop is detected, or None."""
        messages: list[str] = []

        # Action repetition nudges (escalating)
        if self.max_repetition_count >= 12:
            messages.append(
                f"CRITICAL: You have repeated the same action {self.max_repetition_count} times. "
                "STOP repeating. Extract whatever data you have NOW using "
                "browser_get_markdown and return your results immediately."
            )
        elif self.max_repetition_count >= 8:
            messages.append(
                f"STOP: You have repeated the same action {self.max_repetition_count} times. "
                "This approach is not working. Use browser_run_script with "
                "completely different selectors or a different strategy."
            )
        elif self.max_repetition_count >= 5:
            messages.append(
                f"You have repeated a similar action {self.max_repetition_count} times. "
                "Try a different approach — use browser_eval to inspect the DOM, "
                "then write a new browser_run_script with different selectors."
            )

        # Page stagnation nudge
        if self.consecutive_stagnant_pages >= 5:
            messages.append(
                f"The page has not changed across {self.consecutive_stagnant_pages} "
                "consecutive actions. Your actions may not be having any effect. "
                "Try browser_eval to check what's on the page, or use "
                "browser_get_markdown to extract content."
            )

        if messages:
            return "\n".join(messages)
        return None

    @property
    def is_looping(self) -> bool:
        """True if any loop signal has been detected."""
        return self.max_repetition_count >= 5 or self.consecutive_stagnant_pages >= 5
