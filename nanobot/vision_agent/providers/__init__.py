"""Vision provider selection.

Gemini is the only supported provider — its native `box_2d` training
gives the click-accuracy this layer depends on. OpenAI / OpenRouter were
removed when we standardized on Gemini's normalized [0, 1000] bounding
box format; their pixel-coordinate guesses drifted enough to miss small
buttons.

Env:
    VISION_PROVIDER   = gemini   (only accepted value; defaults to gemini)
    VISION_MODEL      = native Gemini model id (e.g. 'gemini-2.0-flash-exp',
                        'gemini-2.5-flash')
    VISION_API_KEY    = Google API key for the gemini compat endpoint
    VISION_BASE_URL   = override for the gemini OpenAI-compat URL (rarely
                        needed)
    VISION_MAX_TOKENS = response cap (default 1500)
    VISION_TIMEOUT_MS = hard timeout per call (default 8000)
"""

from __future__ import annotations

import os

from .base import VisionProvider
from .gemini_provider import GeminiVisionProvider


def select_provider() -> VisionProvider:
    kind = (os.environ.get("VISION_PROVIDER") or "gemini").strip().lower()
    if kind != "gemini":
        raise RuntimeError(
            f"VISION_PROVIDER={kind!r} is not supported. The vision agent "
            "uses Gemini's normalized box_2d output for click accuracy; "
            "set VISION_PROVIDER=gemini."
        )

    api_key = os.environ.get("VISION_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "VISION_API_KEY is not set. The vision preprocessor needs a "
            "Google API key (kept separate from the nanobot brain) to "
            "avoid routing image tokens through an expensive reasoning "
            "model."
        )

    model = (os.environ.get("VISION_MODEL") or "").strip()
    if not model:
        raise RuntimeError(
            "VISION_MODEL is not set. Pick a Gemini vision model — "
            "'gemini-2.0-flash-exp', 'gemini-2.5-flash', or "
            "'gemini-3-flash-preview' are good defaults."
        )

    base_url_env = os.environ.get("VISION_BASE_URL") or None
    max_tokens = int(os.environ.get("VISION_MAX_TOKENS") or "1500")
    timeout_ms = int(os.environ.get("VISION_TIMEOUT_MS") or "8000")

    return GeminiVisionProvider(
        model=model,
        api_key=api_key,
        base_url=base_url_env,
        max_tokens=max_tokens,
        timeout_s=timeout_ms / 1000.0,
    )


__all__ = [
    "GeminiVisionProvider",
    "VisionProvider",
    "select_provider",
]
