"""
Screenshot highlighter: draws dashed bbox + index labels on screenshots so
the vision LLM can ground on element indices instead of guessing pixel coords.

Ported from browser-use/browser_use/browser/python_highlights.py. Pure PIL,
no browser-use runtime dependencies. The server emits element bounds with
backendNodeIds as the stable index; this module paints boxes + labels over
the raw screenshot before it's sent to the LLM.

Graceful degradation: if PIL isn't installed, the overlay step is a no-op
and the raw screenshot is returned unchanged (Pillow is in the project's
stack via transitive deps, so this is an unlikely path).
"""

from __future__ import annotations

import base64
import io
from typing import Iterable, Mapping, Optional, Sequence


# Color palette per element type — matches browser-use so familiar screenshots
# (e.g., past traces) visually compose the same way.
ELEMENT_COLORS: dict[str, tuple[int, int, int]] = {
    "button":   (255, 107, 107),
    "input":    (78, 205, 196),
    "select":   (69, 183, 209),
    "link":     (150, 206, 180),
    "a":        (150, 206, 180),
    "textarea": (255, 140, 66),
    "default":  (221, 160, 221),
}


def _pick_color(tag: str, role: str = "") -> tuple[int, int, int]:
    tag = (tag or "").lower()
    role = (role or "").lower()
    if tag in ELEMENT_COLORS:
        return ELEMENT_COLORS[tag]
    if role in ELEMENT_COLORS:
        return ELEMENT_COLORS[role]
    return ELEMENT_COLORS["default"]


def _dashed_rect(draw, box, color, width: int = 2, dash: int = 4, gap: int = 8) -> None:
    """Draw a dashed rectangle on the given PIL ImageDraw."""
    x0, y0, x1, y1 = box
    # Top edge
    x = x0
    while x < x1:
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        x += dash + gap
    # Bottom edge
    x = x0
    while x < x1:
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
        x += dash + gap
    # Left edge
    y = y0
    while y < y1:
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        y += dash + gap
    # Right edge
    y = y0
    while y < y1:
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)
        y += dash + gap


def _label_position(box, text_size, viewport) -> tuple[int, int]:
    """Place the index label intelligently based on element size."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    tw, th = text_size
    vw, vh = viewport
    # Large elements: inside top-left
    if w > 120 and h > 60:
        return (x0 + 4, y0 + 2)
    # Medium: bottom-right corner
    if w > 60 and h > 30:
        return (max(0, x1 - tw - 4), max(0, y1 - th - 2))
    # Small: above the element so the label doesn't cover content
    y = y0 - th - 2
    if y < 0:
        y = y1 + 2
    x = max(0, min(x0, vw - tw - 2))
    return (x, y)


def build_highlighted_screenshot(
    screenshot_b64: str,
    elements: Sequence[Mapping[str, object]],
    device_pixel_ratio: float = 1.0,
) -> str:
    """Return a new base64 JPEG with bbox overlay drawn on top of the input.

    Args:
        screenshot_b64: raw JPEG/PNG base64 (no data URL prefix).
        elements: iterable of {index, tag, role?, bounds: {x,y,width,height}}.
          `bounds` MUST be in CSS pixels; device_pixel_ratio scales them up to
          device pixels for drawing on the actual screenshot bitmap.
        device_pixel_ratio: from CDP Page.getLayoutMetrics; default 1.

    Returns:
        Base64 JPEG of the annotated screenshot. On any failure (e.g., PIL
        unavailable), returns the input unchanged.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # lazy import
    except Exception:
        return screenshot_b64

    try:
        img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64))).convert("RGB")
    except Exception:
        return screenshot_b64

    draw = ImageDraw.Draw(img)
    font = _load_font()

    vw, vh = img.size
    dpr = device_pixel_ratio or 1.0

    for el in elements:
        bounds = el.get("bounds") or el.get("rect") or {}
        if not isinstance(bounds, Mapping):
            continue
        try:
            x = float(bounds.get("x", 0)) * dpr
            y = float(bounds.get("y", 0)) * dpr
            w = float(bounds.get("width", 0)) * dpr
            h = float(bounds.get("height", 0)) * dpr
        except (TypeError, ValueError):
            continue
        if w < 4 or h < 4:
            continue
        # Clamp to viewport so partial off-screen elements still draw.
        x0 = max(0, int(x))
        y0 = max(0, int(y))
        x1 = min(vw - 1, int(x + w))
        y1 = min(vh - 1, int(y + h))
        if x1 <= x0 or y1 <= y0:
            continue

        color = _pick_color(
            str(el.get("tag", "")),
            str(el.get("role", "")),
        )
        _dashed_rect(draw, (x0, y0, x1, y1), color, width=max(1, int(2 * dpr)))

        index = el.get("index")
        if index is None:
            continue
        label = f"[{index}]"
        try:
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(label) * 7, 12
        lx, ly = _label_position((x0, y0, x1, y1), (tw + 6, th + 4), (vw, vh))
        # Filled background rect behind the label for contrast
        draw.rectangle([lx, ly, lx + tw + 6, ly + th + 4], fill=color)
        draw.text((lx + 3, ly + 2), label, fill=(0, 0, 0), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _load_font():
    """Try a handful of common TrueType fonts, fall back to PIL default."""
    try:
        from PIL import ImageFont
    except Exception:
        return None
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, 14)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None
