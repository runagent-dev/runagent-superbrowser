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

from .client import VisionAgent, dom_hash_of, get_vision_agent
from .schemas import BBox, PageFlags, VisionResponse


def vision_agent_enabled() -> bool:
    """True iff the Python vision middleman should intercept screenshots."""
    return os.environ.get("VISION_ENABLED") == "1"


__all__ = [
    "BBox",
    "PageFlags",
    "VisionAgent",
    "VisionResponse",
    "dom_hash_of",
    "get_vision_agent",
    "vision_agent_enabled",
]
