"""Cloudflare Managed Challenge interstitial solver.

For pages that serve the "Performing security verification" / "Just a
moment" interstitial instead of a concrete captcha widget. The fix is
not to click anything — it's to let CF's JS finish scoring the session
and stamp a `cf_clearance` cookie. The existing humanized wait loop in
`T3SessionManager._wait_for_cf_clear` already handles the mechanics;
this module wraps it so `BrowserSolveCaptchaTool` can call it through
the same tool surface the agent uses for other captcha types.

Distinct from `solve_token` (Turnstile widget with a site_key) and
`solve_vision` (grid/click challenges) — here the entire page IS the
challenge and no user interaction is required, just time + humanization.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)


async def solve_cf_interstitial(
    t3manager,
    session_id: str,
    info: CaptchaInfo,
    *,
    vision_agent: Any = None,  # unused; kept for solver signature parity
    timeout_s: float | None = None,
) -> dict:
    """Wait for a Cloudflare Managed Challenge to auto-clear.

    Reuses `T3SessionManager._wait_for_cf_clear` — which humanizes
    mouse/wheel during the poll — so the behaviour is identical to the
    navigate-time wait the existing `_goto_with_warmup` does. Giving
    the solver a longer default timeout (60s vs navigate's 30s) makes
    the tool call meaningfully different from "just wait longer in
    navigate": by the time the agent reaches this strategy, CF has
    already had 30s and failed, so another 60s with fresh humanization
    is our second bite.
    """
    wait_s = timeout_s if timeout_s is not None else float(
        os.environ.get("T3_CF_SOLVER_WAIT_S") or 60.0,
    )
    start = time.monotonic()
    try:
        s = t3manager._get(session_id)  # type: ignore[attr-defined]
    except KeyError:
        return {
            "solved": False, "method": "cf_wait",
            "error": f"session {session_id} not found",
        }

    origin = s.page.url
    # Extract the domain for learning + proxy-tier calls.
    try:
        from urllib.parse import urlparse as _urlparse
        domain = (_urlparse(origin).hostname or "").lower().replace("www.", "")
    except Exception:
        domain = ""

    result = await t3manager._wait_for_cf_clear(
        session_id, timeout_s=wait_s, origin_url=origin,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    solved = bool(result.get("cleared"))
    payload: dict[str, Any] = {
        "solved": solved,
        "method": "cf_wait",
        "subMethod": "interstitial_auto_pass",
        "durationMs": duration_ms,
        "iterations": result.get("iterations", 0),
        "cookies_landed": result.get("cookies_landed", []),
        "final_url": result.get("final_url", ""),
        "final_title": result.get("final_title", ""),
    }
    if solved:
        # Per-domain learning: reset the consecutive-failure streak.
        # `needs_headful` stays sticky if it was already set.
        try:
            from superbrowser_bridge import routing as _routing
            if domain:
                _routing.record_cf_success(domain)
        except Exception:
            pass
        return payload

    payload["block_class"] = "cloudflare"
    payload["error"] = (
        result.get("error")
        or f"interstitial_not_cleared_after_{int(wait_s)}s"
    )

    # Per-domain learning: bump the CF failure streak. After 2 in a row,
    # `needs_headful=True` is sticky on this domain.
    streak = 0
    try:
        from superbrowser_bridge import routing as _routing
        if domain:
            streak = _routing.record_cf_failure(domain)
    except Exception:
        pass

    # Proxy-tier auto-demote on CF block. Next session on this domain
    # picks up a residential proxy from PROXY_POOL_RESIDENTIAL (if set).
    # No-op when the residential pool is unconfigured.
    try:
        from superbrowser_bridge.antibot import proxy_tiers as _tiers
        if domain:
            _tiers.default().demote(domain)
    except Exception:
        pass

    # Escalation hints surfaced to the caller / agent.
    hints: list[str] = []
    if not result.get("cookies_landed"):
        hints.append(
            "no challenge-clearance cookie persisted — set "
            "SUPERBROWSER_COOKIE_JAR=1 and pre-solve the domain in a "
            "manual session to bootstrap cf_clearance"
        )
    hints.append("set PROXY_POOL_RESIDENTIAL for residential-IP retry")
    hints.append(
        "set SUPERBROWSER_ALLOW_HEADFUL=1 (+xvfb) and restart worker "
        "to force headful Chromium"
        + (" — REQUIRED on this domain" if streak >= 2 else "")
    )
    payload["escalation_hints"] = hints
    payload["cf_failure_streak"] = streak
    return payload
