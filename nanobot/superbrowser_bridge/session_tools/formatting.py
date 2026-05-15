"""DOM-element fetch + state-caption formatting.

Routines that turn the TS server's `/state` response into the strings
the brain reads after every tool. Pulled out of the giant tool file so
the click/type/navigate paths can share one canonical caption format.
"""

from __future__ import annotations

from typing import Any

from .http_client import SUPERBROWSER_URL, _request_with_backoff


def _vision_alternatives_hint(
    state: "BrowserSessionState",
    *,
    exclude_index: int | None = None,
    limit: int = 3,
) -> str:
    """Build a short "try these instead" line from the last vision pass.

    Used on click refusals (low confidence, timeout, blocker, stale
    index) so the brain has concrete alternative targets instead of
    reflexively retrying a neighbour [index]. Returns the empty string
    when there's no usable vision data.
    """
    resp = getattr(state, "_last_vision_response", None)
    if resp is None:
        return ""
    freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
    if freshness == "stale":
        return ""
    bboxes = list(getattr(resp, "bboxes", []) or [])
    if not bboxes:
        return ""
    ranked = sorted(
        enumerate(bboxes, start=1),
        key=lambda pair: (
            0 if getattr(pair[1], "intent_relevant", False) else 1,
            0 if getattr(pair[1], "clickable", False) else 1,
            -float(getattr(pair[1], "confidence", 0.0) or 0.0),
        ),
    )
    picks: list[str] = []
    for i, b in ranked:
        if exclude_index is not None and i == exclude_index:
            continue
        label = (getattr(b, "label", "") or getattr(b, "role", "") or "").strip()
        conf = float(getattr(b, "confidence", 0.0) or 0.0)
        picks.append(f"V{i} (conf {conf:.2f} '{label[:30]}')")
        if len(picks) >= limit:
            break
    if not picks:
        return ""
    tag = "" if freshness == "fresh" else f" [vision {freshness}]"
    return "Higher-value targets from recent vision" + tag + ": " + ", ".join(picks) + "."


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


def _count_elements(data: dict, state: "BrowserSessionState | None" = None) -> int:
    """Best-effort interactive-element count for the canonical state block.

    Prefers state.element_fingerprints (authoritative, populated by
    _fetch_elements). Falls back to counting non-empty lines in the
    elements text - one element per line in the TS bridge format.
    """
    if state is not None:
        fps = getattr(state, "element_fingerprints", None)
        if isinstance(fps, dict) and fps:
            return len(fps)
    elements = data.get("elements")
    if not elements:
        return 0
    if isinstance(elements, list):
        return len(elements)
    if isinstance(elements, str):
        return sum(1 for line in elements.splitlines() if line.strip())
    return 0


def _format_state(data: dict, state: "BrowserSessionState | None" = None) -> str:
    """Canonical per-iteration state caption.

    Six-line contract:
        [SESSION_STATE session_id=... url=... title="..." step=N]
        URL: ...
        Title: ...
        Scroll: y/h (vh)
        Elements: N interactive (call browser_list_elements to inspect)
        [Notices: console_errors=N pending_dialogs=N]

    The element dump that used to live here was the dominant per-turn
    cost - 50+ lines of "[V_n] role 'label'" entries injected after
    every tool call. It now lives behind browser_list_elements which
    the worker invokes on demand. Console errors and pending dialogs
    collapse to a single "Notices" line; full payloads are still
    available via the elements tool when needed.
    """
    parts: list[str] = []
    session_id = data.get("sessionId") or (state.session_id if state else "")
    url = data.get("url") or ""
    title = (data.get("title") or "").replace('"', "'")[:80]
    step = state.step_counter if state else 0
    if session_id or url:
        parts.append(
            f'[SESSION_STATE session_id={session_id or "?"} '
            f'url={url or "?"} title="{title}" step={step}]'
        )
    if url:
        parts.append(f"URL: {url}")
    if data.get("title"):
        parts.append(f"Title: {data['title']}")
    if data.get("scrollInfo"):
        si = data["scrollInfo"]
        parts.append(
            f"Scroll: {si.get('scrollY', 0)}/{si.get('scrollHeight', 0)} "
            f"(viewport: {si.get('viewportHeight', 0)})"
        )

    elem_count = _count_elements(data, state)
    if elem_count:
        parts.append(
            f"Elements: {elem_count} interactive "
            "(call browser_list_elements(session_id) to inspect)"
        )

    notices: list[str] = []
    console_errors = data.get("consoleErrors")
    if console_errors:
        n = len(console_errors) if isinstance(console_errors, (list, dict, str)) else 1
        notices.append(f"console_errors={n}")
    pending_dialogs = data.get("pendingDialogs")
    if pending_dialogs:
        n = len(pending_dialogs) if isinstance(pending_dialogs, (list, dict, str)) else 1
        notices.append(f"pending_dialogs={n}")
    if notices:
        parts.append(f"[Notices: {' '.join(notices)}]")

    return "\n".join(parts)
