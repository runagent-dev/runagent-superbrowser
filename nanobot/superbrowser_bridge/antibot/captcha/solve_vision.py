"""Vision-based captcha solving — T3 HTTP dispatch shim.

The real per-step loop lives in `superbrowser_bridge.session_tools`
(`_solve_captcha_iterative`). This module forwards to it so both entry
points — the Python tool path (`BrowserSolveCaptchaTool._solve_via_vision`)
and the T3 HTTP path (`POST /session/<sid>/captcha/solve` routed through
`_t3_dispatch_from_http` into `antibot.captcha.solve_vision`) — share one
implementation. Keeping a single loop prevents the "click every tile
blindly then verify" bug from recurring in one path while the other is
fixed.

The `t3manager` argument is kept for API compatibility with the existing
dispatch site; the loop itself uses HTTP and the t3 URL shim routes those
calls back into the same manager without a second round-trip.
"""

from __future__ import annotations

import logging
from typing import Any

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)


async def solve_vision(
    t3manager, session_id: str, info: CaptchaInfo,
    *, vision_agent: Any = None,
) -> dict:
    """Forward to the unified iterative captcha loop.

    Returns the same structured dict shape as the loop. On import or
    vision-agent availability failures, returns a solved=False result
    instead of raising — callers (T3 dispatcher, orchestrator) decide
    how to escalate.
    """
    if vision_agent is None:
        try:
            from nanobot.vision_agent import get_vision_agent, vision_agent_enabled
            if not vision_agent_enabled():
                return {
                    "solved": False, "method": "vision",
                    "error": "vision_agent disabled (VISION_ENABLED=0 or no API key)",
                }
            vision_agent = get_vision_agent()
        except Exception as exc:
            return {
                "solved": False, "method": "vision",
                "error": f"vision_agent not available: {exc}",
            }

    try:
        from superbrowser_bridge.session_tools import _solve_captcha_iterative
    except ImportError as exc:
        return {
            "solved": False, "method": "vision",
            "error": f"iterative solver not importable: {exc}",
        }

    return await _solve_captcha_iterative(
        session_id, info, vision_agent,
    )
