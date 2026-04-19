"""Crop the captcha widget region out of a full-page screenshot."""

from __future__ import annotations

import base64
import io
import logging
from typing import Optional

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)


async def widget_screenshot(
    t3manager, session_id: str, info: Optional[CaptchaInfo] = None,
) -> dict:
    """Return a base64 JPEG cropped to the captcha widget bbox.

    If `info` isn't provided, detects first. If no bbox is found, falls
    back to the full-page screenshot so callers still get something.
    """
    if info is None:
        from .detect import detect
        info = await detect(t3manager, session_id)

    png = await t3manager.screenshot(session_id)
    if not info.present or not info.widget_bbox:
        return {
            "image_base64": base64.b64encode(png).decode("ascii"),
            "cropped": False,
            "bbox": None,
            "captcha_type": info.type,
        }

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        x0, y0, x1, y1 = [max(0, int(v)) for v in info.widget_bbox]
        # Pad 8px so we don't cut off the handle/edges.
        x0 = max(0, x0 - 8)
        y0 = max(0, y0 - 8)
        x1 = min(img.width, x1 + 8)
        y1 = min(img.height, y1 + 8)
        crop = img.crop((x0, y0, x1, y1))
        buf = io.BytesIO()
        crop.convert("RGB").save(buf, format="JPEG", quality=85)
        return {
            "image_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "cropped": True,
            "bbox": [x0, y0, x1, y1],
            "captcha_type": info.type,
        }
    except Exception as exc:
        logger.debug("crop failed: %s", exc)
        return {
            "image_base64": base64.b64encode(png).decode("ascii"),
            "cropped": False,
            "bbox": info.widget_bbox,
            "captcha_type": info.type,
            "error": str(exc)[:120],
        }
