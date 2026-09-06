"""Umbrella ablation switches used by the research harness.

Each resolver returns today's production value unless an ablation env var
is set, so callers can replace a scattered ``os.environ.get`` with one call
and every arm stays reproducible from its env alone.

``ABLATE_VISION_REUSE=1`` — "no perception reuse" (eval E5): the agent must
re-ground before every interactive action, i.e. no background prefetch, no
vision-response cache, the vision epoch expires after any mutating turn, and
the ``[CACHED VISION …]`` piggyback never fires. The individual knobs
(``VISION_ASYNC_PREFETCH``, ``VISION_CACHE_TTL_SEC``, ``VISION_MAX_AGE_TURNS``,
``FRESH_VISION_SECONDS``) still work on their own.

``ABLATE_CLICK_LADDER=1`` — "first click path only" (eval E7): no js/keyboard
escalation after a silent primary click (the TypeScript selector cascade is
gated separately by ``SUPERBROWSER_CLICK_TIERS=tier1`` because the server
reads it at startup).
"""
from __future__ import annotations

import os

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def vision_reuse_ablated() -> bool:
    return os.environ.get("ABLATE_VISION_REUSE", "").lower() in _TRUE


def click_ladder_ablated() -> bool:
    return os.environ.get("ABLATE_CLICK_LADDER", "").lower() in _TRUE


def async_prefetch_enabled() -> bool:
    if vision_reuse_ablated():
        return False
    return os.environ.get("VISION_ASYNC_PREFETCH", "1") not in _FALSE


def vision_cache_ttl_s(default: float = 60.0) -> float:
    if vision_reuse_ablated():
        return 0.0
    raw = os.environ.get("VISION_CACHE_TTL_SEC")
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def vision_max_age_turns(default: int = 1) -> int:
    if vision_reuse_ablated():
        return 0
    raw = os.environ.get("VISION_MAX_AGE_TURNS")
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def fresh_vision_seconds(default: float = 10.0) -> float:
    if vision_reuse_ablated():
        return 0.0
    raw = os.environ.get("FRESH_VISION_SECONDS")
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def click_ladder_auto_enabled() -> bool:
    if click_ladder_ablated():
        return False
    return os.environ.get("CLICK_LADDER_AUTO", "1") != "0"
