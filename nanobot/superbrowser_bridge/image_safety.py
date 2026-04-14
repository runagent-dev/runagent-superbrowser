"""
Python image-safety mirror. Used by session_tools.build_image_blocks so
screenshots built on the Python side (with highlight overlays painted on)
get the same 2MB / 1568px cap as screenshots captured in TypeScript.

The rationale is identical to image-safety.ts:
  - vision cost scales with pixel count, so cap dimensions at 1568
  - Gemini's OpenAI-compat endpoint rejects payloads >~1.5MB silently
  - highlight overlays can 2-3x a raw screenshot's size; without this
    clamp, overlays are what tip us over the 400 threshold
"""

from __future__ import annotations

import io
from typing import Literal, Tuple

from PIL import Image

MAX_SIDE_PX = 1568
MAX_BYTES = 2_000_000
START_QUALITY = 70
MIN_QUALITY = 35


def sanitize_image_bytes(
    data: bytes,
    max_bytes: int = MAX_BYTES,
) -> Tuple[bytes, Literal["image/jpeg"]]:
    """Re-encode an image to JPEG within the byte/dimension budget.

    Strips EXIF/ICC/color profile as a side effect of opening and re-saving
    without preserving metadata.

    Strategy mirrors sanitizeImageBuffer in TS: decode → downscale to
    MAX_SIDE_PX if larger → JPEG q70 → iteratively drop quality by 10
    until under max_bytes. If still over at MIN_QUALITY, halve dimensions
    once and retry from q70.
    """
    # Open via BytesIO so we can re-encode without touching disk.
    with Image.open(io.BytesIO(data)) as img:
        # Apply EXIF rotation then convert to RGB (JPEG has no alpha).
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode != "RGB":
            img = img.convert("RGB")

        width, height = img.size
        longest = max(width, height)
        scale = MAX_SIDE_PX / longest if longest > MAX_SIDE_PX else 1.0

        for round_idx in range(2):
            target_w = max(1, int(round(width * scale)))
            target_h = max(1, int(round(height * scale)))
            resized = img if (target_w, target_h) == img.size else img.resize(
                (target_w, target_h), Image.LANCZOS
            )

            quality = START_QUALITY
            buf = io.BytesIO()
            # optimize=True makes Pillow do a second pass to shrink Huffman
            # tables; typically trims 5-10% at no visible quality cost.
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            while buf.tell() > max_bytes and quality > MIN_QUALITY:
                quality -= 10
                buf = io.BytesIO()
                resized.save(buf, format="JPEG", quality=quality, optimize=True)

            if buf.tell() <= max_bytes or round_idx == 1:
                return buf.getvalue(), "image/jpeg"

            # Still over budget at MIN_QUALITY — halve dims once and retry.
            scale = scale * 0.5

        # Shouldn't be reachable; loop returns on round 1.
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=MIN_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"


def sanitize_image_b64(
    b64_str: str,
    max_bytes: int = MAX_BYTES,
) -> str:
    """Base64-in / base64-out wrapper. Also strips whitespace/newlines —
    Gemini's OpenAI-compat layer rejects line-wrapped base64 in data URIs.
    """
    import base64

    # Strip optional data URI prefix then all whitespace.
    cleaned = b64_str
    if cleaned.startswith("data:"):
        comma = cleaned.find(",")
        if comma != -1:
            cleaned = cleaned[comma + 1 :]
    cleaned = "".join(cleaned.split())

    raw = base64.b64decode(cleaned)
    out_bytes, _ = sanitize_image_bytes(raw, max_bytes=max_bytes)
    return base64.b64encode(out_bytes).decode("ascii")
