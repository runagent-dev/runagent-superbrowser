"""Vision prefetch + brain-text emission pipeline.

Owns the asynchronous vision_agent.analyze() loop that fires after every
mutating tool. Mutation tools call `_schedule_vision_prefetch` on success;
the next screenshot/click finds the bboxes already cached.

Functions in this module read+write `BrowserSessionState` fields
(`_last_vision_response`, `_pending_vision_task`, etc.) but never import
the state class — `state` is taken as a parameter and only string-typed
in annotations (`from __future__ import annotations`).
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any

import httpx

from .http_client import SUPERBROWSER_URL, _request_with_backoff


def _read_image_dims(b64: str) -> tuple[int, int]:
    """Decode (width, height) from a base64-encoded screenshot.

    Used to denormalize Gemini's box_2d coords (in [0, 1000] space)
    against the actual screenshot dimensions before any click is
    dispatched. Returns (0, 0) if PIL isn't available or the bytes
    don't decode — the vision agent then falls back to showing
    normalized coords in brain text.
    """
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        return int(img.width), int(img.height)
    except Exception:
        return 0, 0


async def _push_vision_bboxes(
    session_id: str,
    resp: Any,
    *,
    url: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """POST denormalized bboxes to the SuperBrowser server so live
    viewers can flash them on the screencast overlay.

    Fire-and-forget; never raises into the caller. Brain-text indices
    (`V_n`) come from the same ranking `as_brain_text()` uses so the
    overlay's labels match what the brain sees.

    Extra payload fields (all optional):
      - `url`          — the URL the screenshot was captured on. The UI
                          uses this to drop bboxes whose URL no longer
                          matches the current screencast frame.
      - `freshness`    — `fresh|uncertain|stale` from the vision model.
                          The UI dims the overlay when != "fresh".
      - `latencyMs`    — vision-agent round-trip. Surfaces in the UI as
                          debug info.
    """
    if not session_id:
        return
    iw, ih = getattr(resp, "image_width", 0), getattr(resp, "image_height", 0)
    if iw <= 0 or ih <= 0:
        return
    # Mirror the rank order of as_brain_text() so the overlay's V_n
    # labels line up with what the brain sees in tool output.
    ordered = sorted(
        getattr(resp, "bboxes", []),
        key=lambda b: (
            0 if getattr(b, "intent_relevant", False) else 1,
            0 if getattr(b, "clickable", False) else 1,
            -getattr(b, "confidence", 0.0),
        ),
    )
    payload_bboxes: list[dict[str, Any]] = []
    for i, b in enumerate(ordered, start=1):
        try:
            x0, y0, x1, y1 = b.to_pixels(iw, ih)
        except Exception:
            continue
        payload_bboxes.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "label": getattr(b, "label", "")[:40],
            "role": getattr(b, "role", "other"),
            "clickable": bool(getattr(b, "clickable", False)),
            "intent_relevant": bool(getattr(b, "intent_relevant", False)),
            "index": i,
        })
    freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
    payload: dict[str, Any] = {
        "bboxes": payload_bboxes,
        "imageWidth": iw,
        "imageHeight": ih,
        "url": url or "",
        "freshness": freshness,
    }
    if latency_ms is not None:
        payload["latencyMs"] = int(latency_ms)
    # For T3 sessions, fan out to the local Python event bus so the T3
    # viewer at :3101 can paint the same overlays T1 shows. The TS
    # server POST still runs (404s cleanly for t3-* session IDs) so
    # the non-T3 path stays byte-identical.
    if session_id.startswith("t3-"):
        try:
            from superbrowser_bridge.antibot import t3_event_bus as _bus
            _bus.default().emit_vision_bboxes(
                session_id, payload_bboxes, iw, ih,
                url=url or "",
                freshness=freshness,
                latency_ms=latency_ms,
            )
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/vision-bboxes",
                json=payload,
            )
    except Exception:
        # Best-effort — overlay is debug visualization, not load-bearing.
        pass


async def _push_vision_pending(session_id: str) -> None:
    """Tell live viewers a vision pass is in flight. The UI renders a
    transient "vision updating…" indicator; without it the overlay
    silently lags the action by one Gemini round-trip.

    Fire-and-forget. Never raises into the caller.
    """
    if not session_id:
        return
    payload = {"dispatchedAt": int(time.time() * 1000)}
    if session_id.startswith("t3-"):
        try:
            from superbrowser_bridge.antibot import t3_event_bus as _bus
            _bus.default().emit_vision_pending(session_id)
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/vision-pending",
                json=payload,
            )
    except Exception:
        pass


def _schedule_vision_prefetch(
    state: "BrowserSessionState", session_id: str,
) -> "asyncio.Task[Any] | None":
    """Fire a background vision_agent.analyze() so the next
    `browser_screenshot` call finds cached bboxes instead of waiting 3-8s
    for Gemini.

    Returns the spawned task so callers can optionally wait for it with
    a budget via `_await_vision_prefetch`. Errors are swallowed inside
    `_run()`; the caller receives `None` when vision is disabled, the
    session is missing, or task creation failed.

    Called from the success path of mutating tools (click, type, scroll,
    navigate). Uses the same cache key as the sync path so the real
    screenshot call hits cache.
    """
    try:
        from vision_agent import (  # type: ignore[import-not-found]
            dom_hash_of,
            get_vision_agent,
            vision_agent_enabled,
        )
        try:
            from vision_agent import (  # type: ignore[import-not-found]
                dom_text_hash_of,
            )
        except ImportError:
            dom_text_hash_of = None  # type: ignore[assignment]
    except ImportError:
        return None
    if not vision_agent_enabled() or get_vision_agent is None:
        return None
    if not session_id:
        return None

    # Announce pending vision so the UI can show a "vision updating…"
    # indicator. Fire-and-forget; failure must not block the prefetch.
    try:
        asyncio.create_task(_push_vision_pending(session_id))
    except Exception:
        pass

    async def _run() -> "Any":
        try:
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true", "bounds": "true"},
                timeout=15.0,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            b64 = data.get("screenshot")
            if not b64:
                return None
            agent = get_vision_agent()
            img_w, img_h = _read_image_dims(b64)
            elements = data.get("elements", "")
            dh = dom_hash_of(elements) if dom_hash_of else ""
            # Phase 1.2: viewport-aware secondary cache-key signal so
            # the same page at different scroll positions doesn't reuse
            # bboxes captured for the previous viewport.
            dth = ""
            if dom_text_hash_of is not None:
                try:
                    dth = dom_text_hash_of(
                        elements,
                        scroll_info=data.get("scrollInfo"),
                    )
                except Exception:
                    dth = ""
            dispatched = time.monotonic()
            # DPR: when the viewport runs at deviceScaleFactor > 1 the
            # screenshot is physical-pixel sized. Pass it through so
            # click dispatch can divide to land in CSS pixel space.
            try:
                dpr_val = float(data.get("devicePixelRatio") or 1.0)
            except (TypeError, ValueError):
                dpr_val = 1.0
            resp = await agent.analyze(
                screenshot_b64=b64,
                intent=state._last_intent or "observe page",
                session_id=session_id,
                url=data.get("url", "") or state.current_url,
                dom_hash=dh,
                dom_text_hash=dth,
                previous_summary=state._last_vision_summary or None,
                image_width=img_w,
                image_height=img_h,
                task_instruction=state.task_instruction or None,
            )
            resp.with_image_dims(img_w, img_h, dpr=dpr_val)
            state._last_vision_response = resp
            state._last_vision_summary = resp.summary
            state._last_vision_ts = time.time()
            state._last_vision_url = (data.get("url", "") or state.current_url or "")
            state._last_dom_hash = dh or state._last_dom_hash
            state.vision_calls += 1
            # Push the fresh bboxes to live viewers immediately —
            # without this, overlay only updates on the next
            # screenshot tool call, so the user sees bboxes lag by
            # one full action cycle. Fire-and-forget, non-fatal.
            try:
                latency_ms = int((time.monotonic() - dispatched) * 1000)
                await _push_vision_bboxes(
                    session_id, resp,
                    url=state._last_vision_url,
                    latency_ms=latency_ms,
                )
            except Exception:
                pass
            return resp
        except Exception as exc:
            print(f"  [vision prefetch failed: {exc}]")
            return None

    try:
        new_task = asyncio.create_task(_run())
    except Exception:
        return None
    # Phase 1.1: store the task on state so the NEXT mutating tool call
    # can wait for it via ensure_vision_synced(). Cancel any prior
    # in-flight prefetch — only one is meaningful at a time, and a
    # never-awaited older task is just wasted Gemini latency. Best
    # effort; if cancellation is too late, the older task will write
    # into _last_vision_response then the newer task overwrites, so
    # correctness is preserved.
    prev = state._pending_vision_task
    if prev is not None and not prev.done():
        try:
            prev.cancel()
        except Exception:
            pass
    state._pending_vision_task = new_task
    return new_task


async def _append_fresh_vision(
    task: "asyncio.Task[Any] | None",
    result: str,
    *,
    budget_ms: int | None = None,
    expected_label: str | None = None,
    pre_url: str | None = None,
    pre_dom_hash: str | None = None,
    state: "BrowserSessionState | None" = None,
) -> str:
    """Wait for the prefetched vision pass (up to the budget) and
    append a one-line brain-facing hint to `result` when it arrives.

    The hint lets the planner reason on the post-action screen state
    in the SAME tool response rather than waiting for the next
    screenshot call. If the vision pass didn't finish in time, the
    task keeps running in the background (shielded) and the overlay
    will update on the next push.

    Phase 3.3: when `expected_label` + `pre_url` + `pre_dom_hash` are
    supplied (the click_at tool fills these in), compare the post-click
    vision pass against them. If the label is STILL visible AND the
    URL/DOM didn't change, the click missed — surface
    `[click_missed:label_still_visible]` so the brain stops assuming
    success after a no-op click on a `pointer-events:none` overlay or
    a covered element. This converts a class of silent failures into
    explicit signals.
    """
    resp = await _await_vision_prefetch(task, budget_ms=budget_ms)
    if resp is None:
        return result
    summary = (getattr(resp, "summary", "") or "").strip()
    note_parts: list[str] = []
    if summary:
        note_parts.append(summary[:240])
        freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
        if freshness != "fresh":
            note_parts[-1] = f"{note_parts[-1]} [freshness={freshness}]"
    # Phase 3.3 click-hit verification.
    if expected_label and state is not None:
        try:
            label_lower = expected_label.strip().lower()
            relevant = (getattr(resp, "relevant_text", "") or "").lower()
            current_url = (state.current_url or "")
            same_url = (
                pre_url is not None
                and pre_url == current_url
            )
            same_dom = (
                pre_dom_hash is not None
                and pre_dom_hash == (state._last_dom_hash or "")
            )
            if (
                label_lower
                and label_lower in relevant
                and same_url
                and same_dom
            ):
                # Record the cursor failure so the script-lockout gate
                # counts this as a tried-and-failed cursor strategy.
                try:
                    state.record_cursor_failure(
                        strategy="click_at",
                        target=expected_label[:80],
                        reason="label_still_visible (no URL/DOM delta)",
                    )
                except Exception:
                    pass
                miss_note = (
                    f"[click_missed:label_still_visible expected="
                    f"{expected_label[:40]!r}] The clicked target is "
                    f"still visible on the page and neither the URL "
                    f"nor DOM hash changed — the click likely landed "
                    f"on a covered or pointer-events:none surface. Re-"
                    f"observe vision (pick a fresh V_n) before trying "
                    f"again with a different strategy."
                )
                note_parts.append(miss_note)
        except Exception:
            pass
    if not note_parts:
        return result
    sep = "" if result.endswith("\n") else "\n"
    return f"{result}{sep}[vision] {' | '.join(note_parts)}"


async def _await_vision_required(
    task: "asyncio.Task[Any] | None",
    timeout_ms: int | None = None,
) -> "Any":
    """Phase 1.1 hard sync. Block until `task` resolves or `timeout_ms`
    elapses. Default timeout is VISION_HARD_SYNC_TIMEOUT_MS (8000ms).

    Unlike `_await_vision_prefetch`, this is intended to be called from
    the START of a mutating tool to guarantee fresh state — not from the
    END to opportunistically attach a hint. On timeout the task is left
    running (shielded), but the caller is responsible for surfacing
    that timeout to the brain so it can retry rather than dispatch on
    cached vision.
    """
    if task is None:
        return None
    if task.done():
        try:
            return task.result()
        except Exception:
            return None
    if timeout_ms is None:
        try:
            timeout_ms = int(
                os.environ.get("VISION_HARD_SYNC_TIMEOUT_MS") or "8000"
            )
        except ValueError:
            timeout_ms = 8000
    if timeout_ms <= 0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


async def _await_vision_prefetch(
    task: "asyncio.Task[Any] | None",
    budget_ms: int | None = None,
) -> "Any":
    """Wait up to `budget_ms` for a prefetch task to complete.

    Returns the VisionResponse when the task finishes in time, otherwise
    None. On timeout the task is left running (shielded) so the
    background cache write + UI push still happen. Budget defaults to
    VISION_AWAIT_BUDGET_MS env var (fallback 2000 ms); 0 disables the
    wait and returns immediately.
    """
    if task is None:
        return None
    if budget_ms is None:
        try:
            budget_ms = int(
                os.environ.get("VISION_AWAIT_BUDGET_MS") or "2000"
            )
        except ValueError:
            budget_ms = 2000
    if budget_ms <= 0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=budget_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None
