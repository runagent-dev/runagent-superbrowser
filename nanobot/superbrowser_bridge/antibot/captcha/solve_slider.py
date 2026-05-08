"""Slider captcha solver — bezier-interpolated drag.

For the "drag puzzle" class (Tencent, Geetest, DataDome slide-to-verify).
Pattern: identify the slider handle + target gap via vision_agent or via
the detected widget bbox, then drag the handle with human-like motion.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)


def _bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    steps: int = 30,
    jitter_px: float = 3.0,
) -> list[tuple[float, float]]:
    """Quadratic bezier path with a mid-point offset + small noise per step.

    Mimics a real human hand drag — not a straight line, slight overshoot near
    the end, small side-to-side wobble. Avoids the linear-drag tell that
    DataDome/Geetest fingerprint.
    """
    sx, sy = start
    ex, ey = end
    # Control point offset above/below the line.
    dx = ex - sx
    mid_x = sx + dx * 0.5
    mid_y = sy + (random.uniform(-1.5, 1.5) * 0.1 * abs(dx))
    path: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out.
        t2 = t * t * (3 - 2 * t)
        # Quadratic bezier.
        x = (1 - t2) ** 2 * sx + 2 * (1 - t2) * t2 * mid_x + t2 ** 2 * ex
        y = (1 - t2) ** 2 * sy + 2 * (1 - t2) * t2 * mid_y + t2 ** 2 * ey
        x += random.uniform(-jitter_px, jitter_px)
        y += random.uniform(-jitter_px, jitter_px)
        path.append((x, y))
    # Small overshoot + correction at the end.
    path.append((ex + random.uniform(2, 6), ey))
    path.append((ex, ey))
    return path


async def solve_slider(
    t3manager, session_id: str, info: CaptchaInfo, *, vision_agent: Any = None,
) -> dict:
    """Locate slider handle + gap, drag the handle into the gap."""
    bbox = info.widget_bbox
    if not bbox or len(bbox) != 4:
        return {
            "solved": False, "method": "slider",
            "error": "no widget_bbox to drive the drag from",
        }

    # Heuristic: the slider handle is in the leftmost portion of the widget;
    # the target gap is to the right. Try to find them precisely via DOM first.
    handle_cx: float
    handle_cy: float
    target_x: float
    try:
        coords = await t3manager.evaluate(
            session_id,
            """(bbox) => {
              const [x0, y0, x1, y1] = bbox;
              const els = document.elementsFromPoint((x0+x1)/2, (y0+y1)/2);
              // Find the sliding handle: look for a child whose
              // className contains 'slider' or 'handle'.
              for (const el of els) {
                const c = ((el.className||'') + '').toLowerCase();
                if (c.includes('handle') || c.includes('slider') || c.includes('knob')) {
                  const r = el.getBoundingClientRect();
                  return {hx: r.left + r.width/2, hy: r.top + r.height/2,
                          tx: x1 - r.width/2};
                }
              }
              return null;
            }""",
            bbox,
        )
        if coords and isinstance(coords, dict):
            handle_cx = float(coords["hx"])
            handle_cy = float(coords["hy"])
            target_x = float(coords["tx"])
        else:
            raise ValueError("no handle via DOM")
    except Exception:
        # Fallback: assume handle at left end, target near right end.
        x0, y0, x1, y1 = bbox
        handle_cx = x0 + 15
        handle_cy = (y0 + y1) / 2
        target_x = x1 - 15

    path = _bezier_path(
        (handle_cx, handle_cy),
        (target_x, handle_cy + random.uniform(-2, 2)),
        steps=30,
    )

    # Dispatch the drag through patchright.
    s = t3manager._sessions.get(session_id)  # type: ignore[attr-defined]
    if s is None:
        return {"solved": False, "method": "slider", "error": "session not found"}
    page = s.page
    try:
        await page.mouse.move(handle_cx, handle_cy)
        await asyncio.sleep(0.05)
        await page.mouse.down()
        for (x, y) in path:
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.008, 0.02))
        await asyncio.sleep(0.08)
        await page.mouse.up()
    except Exception as exc:
        return {
            "solved": False, "method": "slider",
            "error": f"drag failed: {type(exc).__name__}: {exc}",
        }

    # Allow the site a beat to validate, then re-detect.
    await asyncio.sleep(1.2)
    from .detect import detect
    verify = await detect(t3manager, session_id)
    solved = not verify.present
    return {
        "solved": solved, "method": "slider",
        "error": "" if solved else f"post-drag still detected: {verify.type}",
    }
