"""Vision provider selection.

Three providers are supported, all routed through the openai SDK with a
per-provider base URL:

    gemini      → Google's OpenAI-compat endpoint. PREFERRED — its native
                  `box_2d` training gives the best click accuracy, which is
                  what this layer depends on.
    openai      → api.openai.com (the SDK default).
    openrouter  → openrouter.ai (lets you reach gemini/openai/others with
                  one key).

Accuracy note: gemini emits tight `box_2d` ([ymin,xmin,ymax,xmax] in
[0,1000]) because it's trained for spatial grounding. openai models follow
the same prompt format but their coordinate guesses drift more, so small
buttons can be missed. Use openai when gemini is unavailable (e.g. a
blocked Google project) or as a fallback, and prefer gemini for accuracy.

Env:
    VISION_PROVIDER   = gemini | openai | openrouter   (default: gemini)
    VISION_MODEL      = model id for that provider, e.g.
                          gemini     → 'gemini-2.5-flash'
                          openai     → 'gpt-4o'  (vision + json_object)
                          openrouter → 'google/gemini-2.5-flash'
    VISION_API_KEY    = API key for the chosen provider (kept separate
                        from the nanobot brain key)
    VISION_BASE_URL   = override the provider's default endpoint (rarely
                        needed)
    VISION_MAX_TOKENS = response cap (default 4096)
    VISION_TIMEOUT_MS = hard timeout per call (default 20000)

Fallback (optional secondary, used when the primary fails twice):
    VISION_FALLBACK_MODEL    = enables the fallback when set
    VISION_FALLBACK_PROVIDER = provider kind for the fallback
                               (default: same as VISION_PROVIDER)
    VISION_FALLBACK_API_KEY  = key for the fallback (default: VISION_API_KEY)
    VISION_FALLBACK_BASE_URL, VISION_FALLBACK_MAX_TOKENS,
    VISION_FALLBACK_TIMEOUT_MS
"""

from __future__ import annotations

import os
from typing import Optional

from .base import VisionProvider
from .gemini_provider import GeminiVisionProvider
from .openai_provider import OpenAIVisionProvider

# Providers we know how to build. All share the openai SDK; only the base
# URL (and gemini's thinking_config extra_body) differ.
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"gemini", "openai", "openrouter"})

# Baked-in default endpoint for openrouter so callers only need to set
# provider + model + key. openai uses the SDK default (api.openai.com),
# and gemini bakes its own compat URL inside GeminiVisionProvider.
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _build_provider(
    kind: str,
    *,
    model: str,
    api_key: str,
    base_url: Optional[str],
    max_tokens: int,
    timeout_s: float,
) -> VisionProvider:
    """Construct the concrete provider for `kind`.

    `kind` is assumed to already be validated against
    `_SUPPORTED_PROVIDERS` by the caller.
    """
    if kind == "gemini":
        return GeminiVisionProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,  # None → gemini compat URL inside the class
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
    if kind == "openrouter":
        return OpenAIVisionProvider(
            model=model,
            api_key=api_key,
            base_url=base_url or _OPENROUTER_BASE_URL,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
    # openai → SDK default endpoint unless explicitly overridden.
    return OpenAIVisionProvider(
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


def select_provider() -> VisionProvider:
    kind = (os.environ.get("VISION_PROVIDER") or "gemini").strip().lower()
    if kind not in _SUPPORTED_PROVIDERS:
        raise RuntimeError(
            f"VISION_PROVIDER={kind!r} is not supported. Choose one of "
            f"{sorted(_SUPPORTED_PROVIDERS)}. They all route through the "
            "openai SDK; gemini's native box_2d output gives the best click "
            "accuracy, so prefer it when available."
        )

    api_key = os.environ.get("VISION_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "VISION_API_KEY is not set. The vision preprocessor needs its "
            "own API key (kept separate from the nanobot brain) so image "
            "tokens don't route through an expensive reasoning model."
        )

    model = (os.environ.get("VISION_MODEL") or "").strip()
    if not model:
        raise RuntimeError(
            "VISION_MODEL is not set. Pick a vision model for the chosen "
            "provider — e.g. gemini='gemini-2.5-flash', openai='gpt-4o', "
            "openrouter='google/gemini-2.5-flash'."
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
    # Bumped default 8000 → 20000: flash-grade models frequently exceed
    # 8s on image-heavy pages (observed 3+ timeouts in a row on Chase's
    # calculator with 100KB screenshots). 20s is well under the agent's
    # iteration budget but gives headroom for tail latency. The compact
    # retry in client.py uses retry_timeout_s (~45s) for extra slack on
    # the second chance.
    timeout_ms = int(os.environ.get("VISION_TIMEOUT_MS") or "20000")

    return _build_provider(
        kind,
        model=model,
        api_key=api_key,
        base_url=base_url_env,
        max_tokens=max_tokens,
        timeout_s=timeout_ms / 1000.0,
    )


def select_fallback_provider() -> VisionProvider | None:
    """Optional secondary provider used when the primary fails twice.

    Gated on VISION_FALLBACK_MODEL being set. By default the fallback
    reuses the primary's provider kind, API key, and base URL — so the
    typical config just points at a bigger model of the SAME provider
    (e.g. gemini-2.5-pro as a retry backstop). Cross-provider fallback is
    possible too: set VISION_FALLBACK_PROVIDER (and usually
    VISION_FALLBACK_API_KEY) to back gemini up with openai, etc. Returns
    None when no fallback is configured.
    """
    fb_model = (os.environ.get("VISION_FALLBACK_MODEL") or "").strip()
    if not fb_model:
        return None
    # Fallback provider kind defaults to the primary's, so a gemini setup
    # falls back to another gemini model with the same key. Override to
    # cross providers.
    fb_kind = (
        os.environ.get("VISION_FALLBACK_PROVIDER")
        or os.environ.get("VISION_PROVIDER")
        or "gemini"
    ).strip().lower()
    if fb_kind not in _SUPPORTED_PROVIDERS:
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
    return _build_provider(
        fb_kind,
        model=fb_model,
        api_key=fb_key,
        base_url=base_url_env,
        max_tokens=fb_max_tokens,
        timeout_s=fb_timeout_ms / 1000.0,
    )


__all__ = [
    "GeminiVisionProvider",
    "OpenAIVisionProvider",
    "VisionProvider",
    "select_provider",
    "select_fallback_provider",
]
