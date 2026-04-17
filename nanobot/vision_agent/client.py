"""VisionAgent — orchestrates cache + provider + response formatting.

One lazy process-wide singleton. Exposes a single public coroutine:
    analyze(screenshot_b64, intent, session_id, url, dom_hash,
            image_width, image_height) -> VisionResponse

On provider failure or timeout we return a best-effort fallback response
(empty bboxes, summary='vision unavailable'). Callers always get a
VisionResponse back — they should never need to handle exceptions from
here for the request to recover.

Image dimensions are required to denormalize Gemini's box_2d (in [0, 1000]
space) back to CSS pixels. If the caller doesn't supply them, the agent
decodes the base64 screenshot once via PIL to read width/height.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import time
from typing import Optional

from pydantic import ValidationError

from .cache import CacheKey, VisionCache
from .prompts import SYSTEM_PROMPT, build_user_prompt, intent_bucket
from .providers import VisionProvider, select_provider
from .schemas import BBox, PageFlags, VisionResponse


def _log(msg: str) -> None:
    # stderr so it survives stdout buffering and shows in worker logs.
    sys.stderr.write(f"[vision-agent] {msg}\n")


def dom_hash_of(dom_elements: str | None) -> str:
    """8-char SHA-1 prefix of the DOM element listing, used as cache key."""
    if not dom_elements:
        return ""
    return hashlib.sha1(dom_elements.encode("utf-8", errors="ignore")).hexdigest()[:8]


class VisionAgent:
    def __init__(self, provider: VisionProvider, cache: VisionCache) -> None:
        self._provider = provider
        self._cache = cache
        # Per-session record of the bboxes emitted on the PREVIOUS pass.
        # Used to draw Set-of-Marks anchors onto the next screenshot
        # before sending it to the model — the model re-identifies its
        # own labels instead of re-guessing from scratch every round.
        # Keyed by session_id (or "_" for non-session callers).
        self._last_response_bboxes: dict[str, list[BBox]] = {}

    async def analyze(
        self,
        *,
        screenshot_b64: str,
        intent: str,
        session_id: str,
        url: str,
        dom_hash: str,
        previous_summary: str | None = None,
        image_width: int | None = None,
        image_height: int | None = None,
        task_instruction: str | None = None,
    ) -> VisionResponse:
        start = time.monotonic()
        key: CacheKey = (
            session_id or "_",
            url or "_",
            dom_hash or "_",
            intent_bucket(intent),
        )

        # Resolve image dims up front — they're required to denormalize
        # box_2d back to CSS pixels. If the caller didn't pass them, peek
        # at the PNG/JPEG header via PIL (cheap; the image is already in
        # memory as base64).
        if not image_width or not image_height:
            image_width, image_height = _decode_dims(screenshot_b64)

        cached = await self._cache.get(key)
        if cached is not None:
            # Re-attach dims — cache is process-local so they're typically
            # the same as last time, but a viewport resize between calls
            # would invalidate them otherwise. Cached responses skip the
            # SoM overlay entirely (no provider call means no opportunity
            # for Gemini to re-anchor on it).
            _log(
                f"cache HIT  dom_hash={dom_hash or '-'}  "
                f"intent={intent_bucket(intent)}  "
                f"url={(url or '')[:60]}"
            )
            return cached.with_image_dims(image_width, image_height)
        _log(
            f"cache MISS dom_hash={dom_hash or '-'}  "
            f"intent={intent_bucket(intent)}  "
            f"url={(url or '')[:60]}"
        )

        # Set-of-Marks feedback: overlay the bboxes we emitted on the
        # previous pass so Gemini can re-anchor visually instead of
        # re-guessing every element from scratch. Opt-out via
        # VISION_SOM_OVERLAY=0 for A/B testing. Any failure here falls
        # through to the raw screenshot — overlay must never block a
        # vision call.
        if os.environ.get("VISION_SOM_OVERLAY", "1") != "0":
            prev = self._last_response_bboxes.get(session_id or "_") or []
            if prev and image_width > 0 and image_height > 0:
                try:
                    from superbrowser_bridge.highlights import build_som_screenshot
                    screenshot_b64 = build_som_screenshot(
                        screenshot_b64, prev, image_width, image_height,
                    )
                except Exception as exc:
                    _log(f"SoM overlay failed (non-fatal): {exc!r}")

        user_prompt = build_user_prompt(
            intent=intent,
            url=url,
            previous_summary=previous_summary,
            task_instruction=task_instruction,
        )
        try:
            raw = await self._provider.chat_with_image(
                screenshot_b64=screenshot_b64,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:
            _log(f"provider error ({self._provider.name}): {exc!r}")
            return _fallback_response(
                intent=intent,
                duration_ms=int((time.monotonic() - start) * 1000),
                provider=self._provider.name,
                model=self._provider.model,
                reason=f"provider error: {exc}",
            ).with_image_dims(image_width, image_height)

        parsed, parse_error = _parse_response_with_error(raw.text)
        if parsed is None:
            _log(
                f"parse error ({self._provider.name}/{self._provider.model}): "
                f"{parse_error}\n"
                f"  first 800 chars: {raw.text[:800]!r}"
            )
            return _fallback_response(
                intent=intent,
                duration_ms=int((time.monotonic() - start) * 1000),
                provider=self._provider.name,
                model=self._provider.model,
                reason=f"parse: {parse_error}",
            ).with_image_dims(image_width, image_height)

        parsed.intent = intent
        parsed.cached = False
        parsed.duration_ms = int((time.monotonic() - start) * 1000)
        parsed.tokens_used = raw.tokens_used
        parsed.model = raw.model
        parsed.provider = raw.provider
        parsed.with_image_dims(image_width, image_height)

        # Remember this pass's bboxes so the next screenshot from the
        # same session gets a SoM overlay. An empty list is stored
        # deliberately — prevents a stale overlay from lingering after
        # a pass that saw no interactive elements.
        self._last_response_bboxes[session_id or "_"] = list(parsed.bboxes)

        await self._cache.put(key, parsed)
        return parsed


def _decode_dims(screenshot_b64: str) -> tuple[int, int]:
    """Read (width, height) from a base64-encoded image header.

    Returns (0, 0) if PIL is unavailable or the bytes don't decode — the
    consumer falls back to showing normalized box_2d in brain text rather
    than CSS pixels in that case.
    """
    try:
        from PIL import Image  # lazy import; PIL is a transitive dep
        img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
        return int(img.width), int(img.height)
    except Exception as exc:
        _log(f"_decode_dims failed: {exc!r}")
        return 0, 0


def _parse_response(text: str) -> VisionResponse | None:
    """Back-compat wrapper — drops the error message."""
    resp, _err = _parse_response_with_error(text)
    return resp


def _parse_response_with_error(text: str) -> tuple[VisionResponse | None, str]:
    """Best-effort JSON → VisionResponse.

    Returns (response, error_message). On success the error is empty. On
    failure response is None and the error carries enough context to
    debug without having to turn on DEBUG-level logging.
    """
    if not text:
        return None, "empty response text"
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if "\n" in candidate:
            first_line, rest = candidate.split("\n", 1)
            if not first_line.strip().startswith("{"):
                candidate = rest
    start = candidate.find("{")
    if start < 0:
        return None, "no '{' found in response"
    try:
        obj = json.loads(candidate[start:])
    except json.JSONDecodeError:
        end = candidate.rfind("}")
        if end <= start:
            return None, "no closing '}' for JSON object"
        try:
            obj = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            return None, f"json decode: {exc}"

    if not isinstance(obj, dict):
        return None, f"parsed JSON is {type(obj).__name__}, expected object"

    try:
        return VisionResponse.model_validate(obj), ""
    except ValidationError as exc:
        # Log the first 3 validation errors — typically enough to diagnose.
        errs = exc.errors()[:3]
        detail = "; ".join(
            f"{'.'.join(str(x) for x in e.get('loc', []))}: {e.get('msg')}"
            for e in errs
        )
        return None, f"pydantic validation: {detail}"


def _fallback_response(
    *,
    intent: str,
    duration_ms: int,
    provider: str,
    model: str,
    reason: str,
) -> VisionResponse:
    return VisionResponse(
        summary=f"[vision unavailable: {reason}]",
        relevant_text="",
        bboxes=[],
        flags=PageFlags(),
        intent=intent,
        cached=False,
        duration_ms=duration_ms,
        tokens_used=None,
        model=model,
        provider=provider,
    )


# ── Lazy singleton ──────────────────────────────────────────────────────
_instance: Optional[VisionAgent] = None
_instance_lock = asyncio.Lock()


def get_vision_agent() -> VisionAgent:
    """Return the process-wide VisionAgent. Raises if env is misconfigured."""
    global _instance
    if _instance is None:
        _instance = VisionAgent(provider=select_provider(), cache=VisionCache.from_env())
    return _instance


def reset_vision_agent() -> None:
    """For tests: drop the singleton and force re-read of env on next use."""
    global _instance
    _instance = None


__all__ = [
    "VisionAgent",
    "dom_hash_of",
    "get_vision_agent",
    "reset_vision_agent",
]
