"""Vision preprocessor agent — cheap image-to-text middleman between the
SuperBrowser tools and nanobot's brain.

Public surface:
    get_vision_agent()          -> VisionAgent (lazy singleton)
    vision_agent_enabled()      -> bool   (cheap env check)
    VisionResponse              -> pydantic model returned by analyze()

When VISION_ENABLED is not set to "1", `vision_agent_enabled()` returns
False and callers take the legacy image-block path unchanged.
"""

from __future__ import annotations

import os

from .client import VisionAgent, dom_hash_of, dom_text_hash_of, get_vision_agent
from .schemas import BBox, PageFlags, VisionResponse


_WARNED_MISSING_KEY = False


def vision_agent_enabled() -> bool:
    """True iff the Python vision middleman should intercept screenshots.

    Default: ON when `VISION_API_KEY` (or legacy `VISION_GEMINI_API_KEY`)
    is set. Explicitly disabled by `VISION_ENABLED=0`. Keeps backward-compat
    with `VISION_ENABLED=1` for users who pinned it on.
    """
    explicit = os.environ.get("VISION_ENABLED")
    if explicit == "1":
        return True
    if explicit == "0":
        return False
    has_key = bool(
        os.environ.get("VISION_API_KEY")
        or os.environ.get("VISION_GEMINI_API_KEY")
    )
    if not has_key:
        global _WARNED_MISSING_KEY
        if not _WARNED_MISSING_KEY:
            _WARNED_MISSING_KEY = True
            import sys
            print(
                "[vision_agent] VISION_API_KEY not set — vision_agent disabled. "
                "Set VISION_API_KEY=... (Gemini key) to enable auto-labeling.",
                file=sys.stderr,
            )
        return False
    return True


__all__ = [
    "BBox",
    "PageFlags",
    "VisionAgent",
    "VisionResponse",
    "dom_hash_of",
    "dom_text_hash_of",
    "get_vision_agent",
    "vision_agent_enabled",
]
