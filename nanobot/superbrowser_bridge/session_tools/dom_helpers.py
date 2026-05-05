"""DOM-side helpers — element fetching, bounds resolution, label
look-up, and brain-facing state formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .constants import SUPERBROWSER_URL
from .http_client import _request_with_backoff

if TYPE_CHECKING:
    from .state import BrowserSessionState  # noqa: F401

def _rect_iou(a: dict, b_box) -> float:
    """IoU between a DOM rect dict ({x,y,w,h} in CSS px) and a vision bbox.

    Vision bboxes use a normalised [ymin, xmin, ymax, xmax] in 0–1000
    space (the Gemini convention) plus the vision response's
    image_width / image_height for the unscaling. To avoid threading
    those through every call site, we accept the bbox object directly
    and use its ``to_pixels`` helper if present; otherwise we fall back
    to direct numeric coordinates assumed already in CSS px.

    Returns 0.0 on any error or zero-area input.
    """
    try:
        if hasattr(b_box, "box_2d") and len(b_box.box_2d) == 4:
            ymin, xmin, ymax, xmax = b_box.box_2d
            # Vision uses 0–1000 normalised. We need the image w/h to
            # unscale. The caller passes the parent vision_response's
            # image_width / image_height as attributes on the bbox via
            # ``_attached_iw / _attached_ih`` set up below.
            iw = getattr(b_box, "_attached_iw", None)
            ih = getattr(b_box, "_attached_ih", None)
            dpr = getattr(b_box, "_attached_dpr", 1.0) or 1.0
            if not iw or not ih:
                return 0.0
            x0 = (xmin / 1000.0) * iw / dpr
            y0 = (ymin / 1000.0) * ih / dpr
            x1 = (xmax / 1000.0) * iw / dpr
            y1 = (ymax / 1000.0) * ih / dpr
        else:
            return 0.0
    except Exception:
        return 0.0
    ax0 = float(a.get("x", 0))
    ay0 = float(a.get("y", 0))
    ax1 = ax0 + float(a.get("w", 0))
    ay1 = ay0 + float(a.get("h", 0))
    if ax1 <= ax0 or ay1 <= ay0 or x1 <= x0 or y1 <= y0:
        return 0.0
    ix0 = max(ax0, x0)
    iy0 = max(ay0, y0)
    ix1 = min(ax1, x1)
    iy1 = min(ay1, y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a_area = (ax1 - ax0) * (ay1 - ay0)
    b_area = (x1 - x0) * (y1 - y0)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0

async def _fetch_elements_with_bounds(
    session_id: str, state: "BrowserSessionState"
) -> bool:
    """Fetch /state?bounds=true and populate state.elements_bounds.

    The TS server toggles ``selectorEntries`` (with element rects) on
    the ``bounds`` query param — not ``includeBounds`` (verified in
    src/server/http.ts:651 ``req.query.bounds === 'true'``). Mismatched
    param name silently drops the response so the crosscheck never
    sees rects; explicit prints below make a future regression visible.

    Best-effort — returns True on success, False on any failure.
    """
    try:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "false", "bounds": "true"},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"[click_crosscheck] fetch_bounds failed: {exc}")
        return False
    entries = data.get("selectorEntries") or []
    bounds_map: dict[int, dict] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            idx = int(e.get("index"))
        except (TypeError, ValueError):
            continue
        b = e.get("bounds") or {}
        if not isinstance(b, dict):
            continue
        # Normalise to {x, y, w, h}. The TS scraper emits {x, y, width, height}
        # (DOMRect-shaped).
        if all(k in b for k in ("x", "y", "width", "height")):
            bounds_map[idx] = {
                "bounds": {
                    "x": float(b["x"]),
                    "y": float(b["y"]),
                    "w": float(b["width"]),
                    "h": float(b["height"]),
                },
                "text": (e.get("text") or "")[:120],
            }
        elif all(k in b for k in ("x", "y", "w", "h")):
            bounds_map[idx] = {
                "bounds": {
                    "x": float(b["x"]),
                    "y": float(b["y"]),
                    "w": float(b["w"]),
                    "h": float(b["h"]),
                },
                "text": (e.get("text") or "")[:120],
            }
    if bounds_map:
        state.elements_bounds = bounds_map
        return True
    print(
        f"[click_crosscheck] /state returned no selectorEntries with bounds "
        f"(entries={len(entries)}). Crosscheck will skip for this click."
    )
    return False

def _resolve_v_by_label(
    state: "BrowserSessionState", original_label: str, min_score: float = 0.3,
) -> int | None:
    """Find the V_n in the current vision response whose label best
    matches `original_label`. Returns None when no match scores above
    `min_score`.

    Used by click_at after a forced re-screenshot — vision response
    order can shift, so re-resolving by label is more stable than
    trusting the original index points to the same element.
    """
    if not original_label:
        return None
    vr = getattr(state, "_last_vision_response", None)
    if vr is None:
        return None
    bboxes = list(getattr(vr, "bboxes", []) or [])
    if not bboxes:
        return None
    try:
        from .task_brief import _label_match_score
    except ImportError:
        from superbrowser_bridge.task_brief import _label_match_score  # type: ignore[import-not-found]
    best_idx: int | None = None
    best_score = 0.0
    for v_index, bb in enumerate(bboxes, start=1):
        label = (getattr(bb, "label", "") or "").strip()
        if not label:
            continue
        score = _label_match_score(original_label, label)
        if score > best_score:
            best_score = score
            best_idx = v_index
    if best_score < min_score:
        return None
    return best_idx

async def _fetch_elements(session_id: str, state: "BrowserSessionState | None" = None) -> str:
    """Fetch current interactive elements without vision (cheap, no screenshot).

    This is the key BrowserOS pattern: every action gets a fresh element snapshot
    so the agent always knows what's on the page without wasting a screenshot.

    If `state` is passed, we ALSO update `state.element_fingerprints` with
    the fresh per-index fingerprint map. Click/type tools then send the
    cached fingerprint as `expected_fingerprint` so the TS side can reject
    stale-index clicks (DOM shifted between state-fetch and click).
    """
    try:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "false"},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        if state is not None:
            fps = data.get("fingerprints") or {}
            if isinstance(fps, dict):
                # JSON keys come back as strings; coerce to int for direct index lookup.
                state.element_fingerprints = {int(k): v for k, v in fps.items() if isinstance(v, str)}
            # Phase 1.2: propagate the URL the TS bridge actually sees.
            # Form submits / history.pushState / JS redirects don't go
            # through browser_navigate so state.current_url would otherwise
            # stay stuck on the URL we last typed into navigate. Updating
            # here lets `vision_for_target_resolution` correctly invalidate
            # the epoch when the page changed under us.
            actual_url = data.get("url") or ""
            if actual_url and actual_url != state.current_url:
                state.record_url(actual_url)
        return data.get("elements", "")
    except Exception:
        return ""

def _build_network_block_message(
    status_code: int, url: str, block_class: str = "",
) -> str:
    """Structured message when a page returns 4xx/5xx — tells the worker to
    stop immediately rather than trying interactions on a blocked shell.

    This is distinct from CAPTCHA: CAPTCHA returns 200 + a challenge page.
    A 403/429/503 means the bot-detection edge refused to serve content at
    all, so no amount of clicking will help. The right move is to exit the
    worker via done(success=False) so the orchestrator can route to the
    search worker or escalate (proxy, TLS fingerprinting, etc.).

    EXCEPTION: Cloudflare Managed Challenge masquerades as a 403 "Just a
    moment" page — the challenge can often auto-pass given enough time +
    humanized interaction. When `block_class='cloudflare'`, route the
    agent to `browser_solve_captcha(method='auto')` first; only fall
    back to `done(success=False)` if the solver can't clear it.
    """
    if (block_class or "").lower() == "cloudflare":
        return (
            f"\n\n[CF_INTERSTITIAL status={status_code} url={url} "
            f"block_class=cloudflare]\n"
            f"Cloudflare Managed Challenge ('Just a moment...') detected. "
            f"This is NOT a permanent block — CF is scoring the session "
            f"and may auto-clear given more time + humanization.\n"
            f"ACTION: call browser_solve_captcha(method='auto') — the "
            f"dedicated CF waiter (up to 60s of humanized polling) is "
            f"wired to handle this. If the solver returns solved=false "
            f"with block_class=cloudflare, THEN call "
            f"done(success=False, final_answer='CF_INTERSTITIAL_STUCK: {url}') "
            f"so the orchestrator can escalate to residential proxy / "
            f"headful mode / search."
        )
    reason_hint = {
        401: "Authentication required — this page needs a logged-in session.",
        403: "Forbidden — site's bot detection refused at the network layer. No page interaction will help.",
        404: "Page not found at this URL.",
        429: "Rate-limited — the site throttled our requests. Different IP may help.",
        451: "Blocked for legal reasons (geographic restriction likely).",
        503: "Service unavailable — could be bot detection (Cloudflare/Akamai) or real outage.",
    }.get(status_code, "Server returned an error status — page content is not usable.")
    return (
        f"\n\n[NETWORK_BLOCKED status={status_code} url={url}]\n"
        f"{reason_hint}\n"
        f"ACTION: do NOT attempt further interactions. Call "
        f"done(success=False, final_answer='NETWORK_BLOCKED: HTTP {status_code} at {url}') "
        f"so the orchestrator can escalate (try a different approach, search worker, or request proxy)."
    )

def _format_state(data: dict, state: "BrowserSessionState | None" = None) -> str:
    parts: list[str] = []
    # Leading structured marker that survives tool-result truncation. Even
    # when maxToolResultChars slices the trailing base64 image apart, these
    # first ~120 characters stay intact, so the worker's LLM can always see
    # "the tool succeeded; a session is open" and won't fire a redundant
    # browser_open.
    session_id = data.get("sessionId") or (state.session_id if state else "")
    url = data.get("url") or ""
    title = (data.get("title") or "").replace('"', "'")[:80]
    step = state.step_counter if state else 0
    if session_id or url:
        parts.append(
            f'[SESSION_STATE session_id={session_id or "?"} '
            f'url={url or "?"} title="{title}" step={step}]'
        )
    if data.get("url"):
        parts.append(f"URL: {data['url']}")
    if data.get("title"):
        parts.append(f"Title: {data['title']}")
    if data.get("scrollInfo"):
        si = data["scrollInfo"]
        parts.append(f"Scroll: {si.get('scrollY', 0)}/{si.get('scrollHeight', 0)} (viewport: {si.get('viewportHeight', 0)})")
    if data.get("elements"):
        parts.append(f"\nInteractive elements:\n{data['elements']}")
    if data.get("consoleErrors"):
        parts.append(f"\nConsole errors: {data['consoleErrors']}")
    if data.get("pendingDialogs"):
        parts.append(f"\nPending dialogs: {data['pendingDialogs']}")
    return "\n".join(parts)
