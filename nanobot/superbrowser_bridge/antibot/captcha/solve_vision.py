"""Vision-based captcha solving.

Takes a screenshot, feeds it to vision_agent with a captcha-specific
prompt asking for tile indices to click (for grid captchas), then
dispatches the resulting clicks through the t3 manager.

Pattern ported from the existing vision-based grid solve pipeline.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)


async def solve_vision(
    t3manager, session_id: str, info: CaptchaInfo,
    *, vision_agent: Any = None,
) -> dict:
    """Use vision_agent to solve a tile-grid or image-selection captcha.

    Best for: reCAPTCHA v2 image challenges, hCaptcha image challenges,
    custom "select all X" grid captchas.
    """
    if vision_agent is None:
        try:
            from nanobot.vision_agent.client import VisionAgent
            vision_agent = VisionAgent()
        except Exception as exc:
            return {
                "solved": False, "method": "vision",
                "error": f"vision_agent not available: {exc}",
            }

    png = await t3manager.screenshot(session_id)
    b64 = base64.b64encode(png).decode("ascii")
    intent = (
        f"Solve the {info.type} captcha on this page. "
        "Identify the bbox of every tile/image that matches the challenge "
        "question (e.g. 'select all traffic lights'). "
        "Return each matching tile as a separate bbox labeled with its row+col."
    )
    try:
        resp = await vision_agent.analyze(
            screenshot_b64=b64,
            intent=intent,
            url="",
            previous_summary="",
        )
    except Exception as exc:
        return {
            "solved": False, "method": "vision",
            "error": f"vision analyze failed: {type(exc).__name__}: {exc}",
        }

    bboxes = getattr(resp, "bboxes", None) or []
    if not bboxes:
        return {
            "solved": False, "method": "vision",
            "error": "vision returned no tile bboxes",
        }

    iw = getattr(resp, "image_width", 0) or 0
    ih = getattr(resp, "image_height", 0) or 0
    if iw <= 0 or ih <= 0:
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(png))
            iw, ih = img.width, img.height
        except Exception:
            pass

    click_count = 0
    for bb in bboxes:
        try:
            x0, y0, x1, y1 = bb.to_pixels(iw, ih)
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            await t3manager.click_at(session_id, cx, cy, bbox={
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            })
            click_count += 1
        except Exception as exc:
            logger.debug("vision click %s failed: %s", getattr(bb, "label", ""), exc)

    return {
        "solved": click_count > 0,
        "method": "vision",
        "clicks": click_count,
        "note": "Did not verify the captcha cleared — caller should detect again after a brief wait.",
    }
