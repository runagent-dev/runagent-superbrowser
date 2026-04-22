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
from .providers import VisionProvider, select_fallback_provider, select_provider
from .schemas import BBox, DiffInfo, PageFlags, SceneGraph, SceneLayer, VisionResponse


def _log(msg: str) -> None:
    # stderr so it survives stdout buffering and shows in worker logs.
    sys.stderr.write(f"[vision-agent] {msg}\n")


def dom_hash_of(dom_elements: str | None) -> str:
    """SHA-256 of the DOM element listing, used as cache key.

    Uses the full 64-char hex to avoid cache-key collisions on pages
    where two different DOM snapshots (e.g. a dismissed modal replaced
    by a similarly-structured banner) happen to share an 8-char prefix.
    A collision causes stale bboxes to be served, which manifests as
    the vision agent "hallucinating" targets that aren't really on the
    current screen.
    """
    if not dom_elements:
        return ""
    return hashlib.sha256(dom_elements.encode("utf-8", errors="ignore")).hexdigest()


class VisionAgent:
    # Suppress SoM overlay when the previous pass is older than this.
    # A cold overlay from a minute ago more often misleads Gemini
    # (elements moved / page changed) than helps it re-anchor.
    _SOM_STALE_AFTER_S: float = 10.0

    def __init__(
        self,
        provider: VisionProvider,
        cache: VisionCache,
        *,
        fallback_provider: VisionProvider | None = None,
    ) -> None:
        self._provider = provider
        # Optional secondary provider (typically a larger model) used
        # when the primary fails twice. None by default; set via
        # VISION_FALLBACK_MODEL env.
        self._fallback_provider = fallback_provider
        self._cache = cache
        # Per-session record of the bboxes emitted on the PREVIOUS pass.
        # Used to draw Set-of-Marks anchors onto the next screenshot
        # before sending it to the model — the model re-identifies its
        # own labels instead of re-guessing from scratch every round.
        # Keyed by session_id (or "_" for non-session callers).
        self._last_response_bboxes: dict[str, list[BBox]] = {}
        # Timestamp (monotonic) of the last stored bboxes per session.
        # Used to drop SoM overlay when the anchor is too old to trust.
        self._last_response_ts: dict[str, float] = {}
        # URL the previous pass was taken on. If the current pass is on
        # a different URL the layout is almost certainly different, so
        # we skip SoM overlay to avoid anchoring Gemini on dead coords.
        self._last_response_url: dict[str, str] = {}
        # Previous-pass active_blocker_layer_id per session. Used to
        # compute DiffInfo.modal_state (opened / closed / same) without
        # re-running the model.
        self._last_blocker_layer: dict[str, str] = {}
        # Page-type-aware model tiering (P3.14). After the first pass
        # classifies the page, subsequent calls on the same (session,
        # url) can skip directly to the fallback provider for complex
        # types that routinely truncate on the flash model.
        self._last_page_type: dict[tuple[str, str], str] = {}

    # Page types that benefit from the larger/slower fallback model.
    # Adding more types here trades latency for bbox completeness —
    # these are the pages where flash-tier Gemini consistently
    # truncates or under-emits on complex layouts.
    _COMPLEX_PAGE_TYPES: frozenset[str] = frozenset({
        "map_or_booking", "search_results", "checkout_form",
        "product_listing",
    })

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
        cursor_trail: list[tuple[int, int]] | None = None,
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
        #
        # Suppressed when:
        #   - intent is solve_captcha_step (tiles re-render between steps)
        #   - previous pass is older than _SOM_STALE_AFTER_S (anchor too
        #     cold to trust; layout probably shifted)
        #   - URL changed since the previous pass (different page → old
        #     bboxes would point at dead coordinates)
        _bucket = intent_bucket(intent)
        sid_key = session_id or "_"
        prev_ts = self._last_response_ts.get(sid_key, 0.0)
        prev_url = self._last_response_url.get(sid_key, "")
        _som_prev_stale = (
            prev_ts > 0.0
            and (time.monotonic() - prev_ts) > self._SOM_STALE_AFTER_S
        )
        _som_url_changed = bool(prev_url) and (prev_url != (url or ""))
        if (
            os.environ.get("VISION_SOM_OVERLAY", "1") != "0"
            and _bucket != "solve_captcha_step"
            and not _som_prev_stale
            and not _som_url_changed
        ):
            prev = self._last_response_bboxes.get(sid_key) or []
            if prev and image_width > 0 and image_height > 0:
                try:
                    from superbrowser_bridge.highlights import build_som_screenshot
                    screenshot_b64 = build_som_screenshot(
                        screenshot_b64, prev, image_width, image_height,
                    )
                except Exception as exc:
                    _log(f"SoM overlay failed (non-fatal): {exc!r}")
        elif _som_prev_stale or _som_url_changed:
            # Clear the stale anchor so future passes don't keep
            # re-evaluating the same ageing bboxes.
            self._last_response_bboxes.pop(sid_key, None)
            self._last_response_ts.pop(sid_key, None)
            self._last_response_url.pop(sid_key, None)

        # Cursor trail overlay — only for captcha step-mode. Draws numbered
        # dots where earlier step-mode clicks landed so Gemini can reason
        # about "I already clicked that tile" from the image itself in
        # addition to the structured `Previous click` hint in the prompt.
        if (
            _bucket == "solve_captcha_step"
            and cursor_trail
            and image_width > 0
            and image_height > 0
        ):
            try:
                from superbrowser_bridge.highlights import build_som_screenshot
                screenshot_b64 = build_som_screenshot(
                    screenshot_b64, [], image_width, image_height,
                    cursor_trail=list(cursor_trail),
                )
            except Exception as exc:
                _log(f"cursor trail overlay failed (non-fatal): {exc!r}")

        async def _one_shot(
            provider: VisionProvider, compact: bool,
        ) -> tuple[Any, str, str]:
            """Returns (parsed_or_None, raw_text, error_reason)."""
            prompt = build_user_prompt(
                intent=intent,
                url=url,
                previous_summary=previous_summary,
                task_instruction=task_instruction,
                compact=compact,
            )
            try:
                raw_ = await provider.chat_with_image(
                    screenshot_b64=screenshot_b64,
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prompt,
                )
            except Exception as exc:  # noqa: BLE001
                return None, "", f"provider error: {exc}"
            parsed_, err_ = _parse_response_with_error(raw_.text)
            if parsed_ is None:
                return None, raw_.text, f"parse: {err_}"
            return (parsed_, raw_), "", ""

        # Page-type-aware tier-up: if the previous pass classified this
        # (session, url) as a complex type, go straight to the larger
        # fallback model on the first attempt. Saves one round-trip of
        # "flash tries, truncates, compact retries, then fallback" on
        # pages we already know are heavy.
        tier_key = (session_id or "_", url or "_")
        last_type = self._last_page_type.get(tier_key, "")
        use_fallback_first = bool(
            self._fallback_provider is not None
            and last_type in self._COMPLEX_PAGE_TYPES
            and os.environ.get("VISION_TIER_UP", "1") != "0"
        )
        first_provider = (
            self._fallback_provider
            if use_fallback_first and self._fallback_provider is not None
            else self._provider
        )

        # First attempt — primary OR fallback depending on tier-up decision.
        first, first_text, first_err = await _one_shot(first_provider, compact=False)
        if first is not None:
            parsed, raw = first
        else:
            # Second attempt — primary provider, compact prompt (trims
            # bboxes so the response fits on heavy pages).
            _log(
                f"first-attempt {first_err} ({self._provider.name}/"
                f"{self._provider.model}); retrying in compact mode. "
                f"first 400 chars: {first_text[:400]!r}"
            )
            second, _second_text, second_err = await _one_shot(
                self._provider, compact=True,
            )
            if second is not None:
                parsed, raw = second
            elif self._fallback_provider is not None:
                # Third attempt — FALLBACK provider. Only runs when the
                # primary failed twice AND a fallback is configured via
                # VISION_FALLBACK_MODEL. Gives pages with truncation or
                # transient primary-model issues one more chance.
                _log(
                    f"compact retry also failed ({second_err}); "
                    f"trying fallback provider "
                    f"({self._fallback_provider.name}/"
                    f"{self._fallback_provider.model})"
                )
                third, _third_text, third_err = await _one_shot(
                    self._fallback_provider, compact=False,
                )
                if third is not None:
                    parsed, raw = third
                else:
                    _log(f"fallback also failed ({third_err}); empty response.")
                    return _fallback_response(
                        intent=intent,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        provider=self._provider.name,
                        model=self._provider.model,
                        reason=(
                            f"{first_err} | retry: {second_err} | "
                            f"fallback: {third_err}"
                        ),
                    ).with_image_dims(image_width, image_height)
            else:
                _log(
                    f"compact retry also failed ({second_err}); "
                    "no fallback configured, returning empty response."
                )
                return _fallback_response(
                    intent=intent,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    provider=self._provider.name,
                    model=self._provider.model,
                    reason=f"{first_err} | retry: {second_err}",
                ).with_image_dims(image_width, image_height)

        parsed.intent = intent
        parsed.cached = False
        parsed.duration_ms = int((time.monotonic() - start) * 1000)
        parsed.tokens_used = raw.tokens_used
        parsed.model = raw.model
        parsed.provider = raw.provider
        parsed.with_image_dims(image_width, image_height)
        # Server-side bbox cap (P3.11). Gemini occasionally returns 50+
        # bboxes despite the prompt asking for 10-25. Trim to top-N so
        # the brain's selectorMap and overlay UI don't drown in noise.
        # Ranking mirrors as_brain_text(): intent_relevant first, then
        # clickable, then confidence.
        try:
            max_bboxes = int(os.environ.get("VISION_MAX_BBOXES") or "25")
        except ValueError:
            max_bboxes = 25
        if max_bboxes > 0 and len(parsed.bboxes) > max_bboxes:
            parsed.bboxes = sorted(
                parsed.bboxes,
                key=lambda b: (
                    0 if getattr(b, "intent_relevant", False) else 1,
                    0 if getattr(b, "clickable", False) else 1,
                    -float(getattr(b, "confidence", 0.0) or 0.0),
                ),
            )[:max_bboxes]
        # Older Gemini outputs (and model families without scene training)
        # omit `scene` entirely. The planner assumes a non-null scene, so
        # synthesize a degenerate one here from flags + label heuristics.
        _derive_scene_if_missing(parsed, task_instruction)

        # Remember this pass's bboxes so the next screenshot from the
        # same session gets a SoM overlay. An empty list is stored
        # deliberately — prevents a stale overlay from lingering after
        # a pass that saw no interactive elements. For step-mode captcha
        # solving we do NOT remember, since the next step will re-render
        # and stale bboxes would mislead the overlay.
        if _bucket != "solve_captcha_step":
            # Compute structured diff BEFORE overwriting last-response
            # state, so the diff compares previous-pass bboxes against
            # this-pass bboxes.
            prev_labels = {
                (getattr(b, "label", "") or "").strip().lower()
                for b in (self._last_response_bboxes.get(sid_key) or [])
                if getattr(b, "label", "")
            }
            curr_labels = {
                (getattr(b, "label", "") or "").strip().lower()
                for b in (parsed.bboxes or [])
                if getattr(b, "label", "")
            }
            added = sorted(curr_labels - prev_labels)
            removed = sorted(prev_labels - curr_labels)
            prev_url_for_diff = self._last_response_url.get(sid_key, "")
            url_changed = bool(
                prev_url_for_diff
                and (url or "") != prev_url_for_diff
            )
            # Detect modal/blocker layer transitions from the scene graph.
            prev_blocker = self._last_blocker_layer.get(sid_key, "")
            curr_scene = getattr(parsed, "scene", None)
            curr_blocker = (
                getattr(curr_scene, "active_blocker_layer_id", None) or ""
                if curr_scene is not None else ""
            )
            if curr_blocker and not prev_blocker:
                modal_state = "opened"
            elif prev_blocker and not curr_blocker:
                modal_state = "closed"
            else:
                modal_state = "same"
            # Only attach diff when we had a previous pass to compare
            # against. None on the very first pass for a session so the
            # planner can tell "first observation" apart from "nothing
            # changed".
            if prev_labels or prev_url_for_diff:
                parsed.diff_from_previous = DiffInfo(
                    bboxes_added=added[:20],
                    bboxes_removed=removed[:20],
                    url_changed=url_changed,
                    modal_state=modal_state,  # type: ignore[arg-type]
                )
            self._last_blocker_layer[sid_key] = curr_blocker
            # Remember page_type for tier-up next time on this URL.
            try:
                self._last_page_type[tier_key] = getattr(
                    parsed, "page_type", "",
                ) or ""
            except Exception:
                pass

            # Only remember bboxes when the screenshot was fresh. A stale
            # or partial pass would poison the next SoM overlay.
            if parsed.screenshot_freshness == "fresh":
                self._last_response_bboxes[sid_key] = list(parsed.bboxes)
                self._last_response_ts[sid_key] = time.monotonic()
                self._last_response_url[sid_key] = url or ""
            else:
                self._last_response_bboxes.pop(sid_key, None)
                self._last_response_ts.pop(sid_key, None)
                self._last_response_url.pop(sid_key, None)

            # Skip the cache for step-mode: each step needs a fresh pass,
            # and a coincidental key collision (same session, same url,
            # same dom_hash, same intent bucket) could return the prior
            # step's response to the current one. Non-step intents cache
            # as before so hot pages stay fast.
            #
            # Also skip when the model self-reported the screenshot as
            # non-fresh — caching a stale/uncertain pass would re-serve
            # those bboxes on the next call, which is exactly the
            # hallucination we're guarding against.
            if parsed.screenshot_freshness == "fresh":
                await self._cache.put(key, parsed)
            else:
                _log(
                    f"cache PUT skipped (freshness={parsed.screenshot_freshness})  "
                    f"url={(url or '')[:60]}"
                )
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


# Label regexes used by the degenerate scene derivation below. Kept at
# module scope so we compile them once.
_DISMISS_LABEL_RE = None
_REJECT_LABEL_RE = None


def _get_label_res():
    import re
    global _DISMISS_LABEL_RE, _REJECT_LABEL_RE
    if _DISMISS_LABEL_RE is None:
        _DISMISS_LABEL_RE = re.compile(
            r"accept|agree|consent|got it|ok(ay)?|close|no thanks|not now|"
            r"continue|allow|i understand|dismiss|×|✕|x$",
            re.IGNORECASE,
        )
        _REJECT_LABEL_RE = re.compile(
            r"reject|decline|deny|no$|opt[- ]?out",
            re.IGNORECASE,
        )
    return _DISMISS_LABEL_RE, _REJECT_LABEL_RE


def _derive_scene_if_missing(
    resp: VisionResponse,
    task_instruction: str | None,
) -> None:
    """Fill `resp.scene` when Gemini didn't emit one.

    Keeps the planner contract simple: downstream code can assume
    `resp.scene is not None`, so it never has to branch on "did vision
    give us a scene or not". The derived scene is deliberately minimal
    — one layer for the content, plus a single modal layer when a
    blocking signal is present — but enough to drive the dismiss-first
    planner logic on pages where the model skipped the field.

    Also back-fills `role_in_scene` on bboxes whose label regex-matches
    a dismiss/reject pattern, so those pages can still be classified as
    blockers by the planner even when the model left every bbox as
    role_in_scene="unknown".
    """
    dismiss_re, _ = _get_label_res()

    # Backfill role_in_scene on unknown bboxes using label regex. Only
    # flip to "blocker" when the page clearly has an overlay — otherwise
    # a button labelled "OK" on a perfectly normal form would wrongly
    # look like a blocker. We gate on flags.modal_open / login_wall /
    # captcha_present OR the presence of an error banner.
    page_looks_blocked = bool(
        resp.flags.modal_open
        or resp.flags.login_wall
        or resp.flags.captcha_present
        or resp.flags.error_banner
    )
    if page_looks_blocked:
        for b in resp.bboxes:
            if b.role_in_scene == "unknown" and dismiss_re.search(b.label or ""):
                b.role_in_scene = "blocker"

    # Also backfill target when a bbox label substring-matches the task.
    # Mirrors the cheap "typed text is already in task" shortcut used by
    # type_verify.py — same hazards (overly aggressive match on short
    # tasks) so we require label length >= 3 and a direct containment.
    if task_instruction:
        task_lc = task_instruction.lower()
        for b in resp.bboxes:
            if b.role_in_scene != "unknown":
                continue
            lbl = (b.label or "").strip().lower()
            if len(lbl) >= 3 and lbl in task_lc and b.clickable:
                b.role_in_scene = "target"

    if resp.scene is not None and resp.scene.layers:
        return

    layers: list[SceneLayer] = []
    active_id: Optional[str] = None

    # Decide whether there's a blocking layer based on flags + derived
    # blocker bboxes. Keep this cheap — no JS evaluation, no DOM probe.
    has_blocker_bbox = any(b.role_in_scene == "blocker" for b in resp.bboxes)
    if page_looks_blocked or has_blocker_bbox:
        # Pick a dismiss_hint: prefer the bbox we marked blocker, else
        # the first captcha_widget bbox, else "Dismiss".
        hint = None
        for b in resp.bboxes:
            if b.role_in_scene == "blocker" and b.label:
                hint = b.label
                break
        if hint is None and resp.flags.captcha_widget_bbox and resp.flags.captcha_widget_bbox.label:
            hint = resp.flags.captcha_widget_bbox.label
        layers.append(SceneLayer(
            id="L0_modal",
            kind="modal" if resp.flags.modal_open or resp.flags.login_wall or resp.flags.captcha_present else "banner",
            blocks_interaction_below=True,
            dismiss_hint=hint or "Dismiss",
        ))
        active_id = "L0_modal"
        # Assign layer_id to matching bboxes so the brain-facing scene
        # block has something to point to.
        for b in resp.bboxes:
            if b.role_in_scene == "blocker" and not b.layer_id:
                b.layer_id = "L0_modal"

    content_id = f"L{len(layers)}_content"
    layers.append(SceneLayer(
        id=content_id,
        kind="content",
        blocks_interaction_below=False,
    ))
    for b in resp.bboxes:
        if not b.layer_id:
            b.layer_id = content_id

    resp.scene = SceneGraph(layers=layers, active_blocker_layer_id=active_id)


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
        _instance = VisionAgent(
            provider=select_provider(),
            cache=VisionCache.from_env(),
            fallback_provider=select_fallback_provider(),
        )
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
