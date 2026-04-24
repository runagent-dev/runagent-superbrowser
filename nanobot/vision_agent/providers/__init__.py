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
    # Default bumped from 1500 → 4096: complex pages (seat maps,
    # product listings, checkout grids) routinely serialize to ~3K+
    # chars of JSON. At ~4 chars/token that's ~800 tokens just for
    # payload; headers + `scene.layers` push past the old cap and
    # truncate the response mid-bbox, which then fails JSON parsing.
    # 4096 gives enough headroom while staying below the usual
    # per-model output cap.
    max_tokens = int(os.environ.get("VISION_MAX_TOKENS") or "4096")
    # Bumped default 8000 → 20000: gemini-3-flash-preview frequently
    # exceeds 8s on image-heavy pages (observed 3+ timeouts in a row on
    # Chase's calculator with 100KB screenshots). 20s is well under the
    # agent's iteration budget but gives headroom for tail latency. The
    # compact retry in client.py uses retry_timeout_s (~45s) for extra
    # slack on the second chance.
    timeout_ms = int(os.environ.get("VISION_TIMEOUT_MS") or "20000")

    return GeminiVisionProvider(
        model=model,
        api_key=api_key,
        base_url=base_url_env,
        max_tokens=max_tokens,
        timeout_s=timeout_ms / 1000.0,
    )


def select_fallback_provider() -> VisionProvider | None:
    """Optional secondary provider used when the primary fails twice.

    Gated on VISION_FALLBACK_MODEL being set. Defaults to reusing the
    primary API key and base URL — so the typical config just points
    at a bigger Gemini model (e.g. Pro) as a retry backstop. Returns
    None when no fallback is configured.
    """
    fb_model = (os.environ.get("VISION_FALLBACK_MODEL") or "").strip()
    if not fb_model:
        return None
    fb_key = (
        os.environ.get("VISION_FALLBACK_API_KEY")
        or os.environ.get("VISION_API_KEY")
        or ""
    ).strip()
    if not fb_key:
        return None
    base_url_env = (
        os.environ.get("VISION_FALLBACK_BASE_URL")
        or os.environ.get("VISION_BASE_URL")
        or None
    )
    # Fallback gets more headroom than primary — it only runs when
    # the primary was too truncated or errored, so we pay for
    # completeness.
    fb_max_tokens = int(os.environ.get("VISION_FALLBACK_MAX_TOKENS") or "6144")
    fb_timeout_ms = int(os.environ.get("VISION_FALLBACK_TIMEOUT_MS") or "15000")
    return GeminiVisionProvider(
        model=fb_model,
        api_key=fb_key,
        base_url=base_url_env,
        max_tokens=fb_max_tokens,
        timeout_s=fb_timeout_ms / 1000.0,
    )


__all__ = [
    "GeminiVisionProvider",
    "VisionProvider",
    "select_provider",
    "select_fallback_provider",
]
