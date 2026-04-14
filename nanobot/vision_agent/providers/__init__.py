"""Vision provider selection.

Reads env to decide which concrete provider to instantiate:

    VISION_PROVIDER   = openai | openrouter | gemini   (default: openai)
    VISION_MODEL      = model identifier accepted by that provider
    VISION_API_KEY    = API key for that provider
    VISION_BASE_URL   = optional override (otherwise provider default)
    VISION_MAX_TOKENS = response cap (default 1500)
    VISION_TIMEOUT_MS = hard timeout per call (default 8000)

Raises RuntimeError with a clear message when VISION_API_KEY is missing.
"""

from __future__ import annotations

import os

from .base import VisionProvider
from .gemini_provider import GeminiVisionProvider
from .openai_provider import OpenAIVisionProvider
from .openrouter_provider import OpenRouterVisionProvider


_PROVIDERS: dict[str, type[VisionProvider]] = {
    "openai": OpenAIVisionProvider,
    "openrouter": OpenRouterVisionProvider,
    "gemini": GeminiVisionProvider,
}


def select_provider() -> VisionProvider:
    kind = (os.environ.get("VISION_PROVIDER") or "openai").strip().lower()
    if kind not in _PROVIDERS:
        raise RuntimeError(
            f"VISION_PROVIDER={kind!r} is not recognized. "
            f"Expected one of: {', '.join(_PROVIDERS)}."
        )

    api_key = os.environ.get("VISION_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "VISION_API_KEY is not set. The vision preprocessor needs its "
            "own key (kept separate from the nanobot brain) to avoid "
            "routing image tokens through an expensive reasoning model."
        )

    model = (os.environ.get("VISION_MODEL") or "").strip()
    if not model:
        raise RuntimeError(
            "VISION_MODEL is not set. Pick a cheap vision model like "
            "'gpt-4o-mini', 'openai/gpt-4o-mini' (for openrouter), or "
            "'gemini-2.0-flash-exp'."
        )

    base_url_env = os.environ.get("VISION_BASE_URL") or None
    max_tokens = int(os.environ.get("VISION_MAX_TOKENS") or "1500")
    timeout_ms = int(os.environ.get("VISION_TIMEOUT_MS") or "8000")

    cls = _PROVIDERS[kind]
    return cls(
        model=model,
        api_key=api_key,
        base_url=base_url_env,
        max_tokens=max_tokens,
        timeout_s=timeout_ms / 1000.0,
    )


__all__ = [
    "GeminiVisionProvider",
    "OpenAIVisionProvider",
    "OpenRouterVisionProvider",
    "VisionProvider",
    "select_provider",
]
