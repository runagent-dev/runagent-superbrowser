"""Module-level constants and shared in-memory state for the
orchestrator-tools package.

The delegation registry (``_DELEGATION_ATTEMPTS``) is a single
in-process dict keyed by ``"<domain>::<task-hash>"``. It enforces the
outer-loop circuit breaker — at most ``_DELEGATION_MAX_ATTEMPTS``
delegations against the same task on the same domain inside a
``_DELEGATION_WINDOW_SEC`` window. Stays at module scope so the
orchestrator's two tool calls (delegate + delegate-after-fallback)
both see the same counter.
"""

from __future__ import annotations

import re as _re
from pathlib import Path

# Outer-loop circuit breaker.
#
# Keyed by (domain, sha1(instructions)[:12]) so the same task on the same
# domain is what gets counted — an orchestrator legitimately delegating two
# different browser tasks against the same site is fine.
#
# Each value is (attempt_count, first_seen_ts, last_worker_result). Entries
# older than one hour are treated as stale and reset on next touch; the
# orchestrator may have recovered and the user may be retrying something
# intentional. The result text is kept so a 2nd-delegation gate can inspect
# it for structured-data markers and refuse re-delegation when the previous
# run actually answered the question.
_DELEGATION_ATTEMPTS: dict[str, tuple[int, float, str]] = {}
_DELEGATION_MAX_ATTEMPTS = 2
_DELEGATION_WINDOW_SEC = 60 * 60


# Phase 4: result-quality detector. A worker result is "substantive" when it
# contains markers of verified live data — concrete prices, named addresses,
# specific time stamps, or boolean flags from the page. The orchestrator's
# default reflex on a hedged "Unable to complete this truthfully" phrasing
# is to re-delegate, even when the worker returned 1500+ chars of verified
# findings; that re-delegation throws away progress and starts a fresh
# session that usually fares worse. This signal lets us intercept that
# reflex.
_SUBSTANTIVE_PRICE_RE = _re.compile(r"\$\s?\d+(?:[.,]\d+)?")
_SUBSTANTIVE_KEYWORDS = (
    "in & out", "in&out", "in and out", "in-and-out",
    "garage", "verified", "found", "options",
)


# Where the browser worker workspace lives (relative to this file)
_BASE = Path(__file__).resolve().parent.parent.parent
BROWSER_WORKSPACE = str(_BASE / "workspace_browser")
