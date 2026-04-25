"""
Low-level session-based browser tools for nanobot.

State is encapsulated in BrowserSessionState — not module globals.
This allows multiple Nanobot instances (e.g., orchestrator + browser worker)
to have isolated state in the same process.
"""

from __future__ import annotations

import asyncio
import json
import time
import os
import base64
from datetime import datetime
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

SUPERBROWSER_URL = "http://localhost:3100"
SCREENSHOT_DIR = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", "/tmp/superbrowser/screenshots")


# --- Tier 3 (patchright) HTTP-shaped shim -----------------------------------
#
# Sessions whose IDs start with `t3-` are served by the in-process
# `T3SessionManager` instead of the TS server. Intercepting here means all
# 21 existing tools work unchanged — they still call _request_with_backoff;
# this helper just peels off t3-routed URLs and returns a response-shaped
# object the tool code can call .json() / .status_code / .text on.

class _T3Response:
    """Minimal httpx.Response stand-in for t3 dispatches."""

    def __init__(self, payload: Any, status_code: int = 200, content: bytes = b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content or (payload if isinstance(payload, bytes) else b"")
        self.headers: dict[str, str] = {}
        self.text = ""
        if isinstance(payload, bytes):
            # Binary response (screenshot). Advertise as JPEG so callers
            # that key off content-type treat it correctly.
            self.headers["content-type"] = "image/jpeg"
        elif isinstance(payload, str):
            self.text = payload
            self.headers["content-type"] = "text/plain"
        elif isinstance(payload, (dict, list)):
            try:
                self.text = json.dumps(payload)
                self.headers["content-type"] = "application/json"
            except Exception:
                self.text = ""

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"t3 {self.status_code}", request=None, response=None,  # type: ignore[arg-type]
            )


def _is_t3_url(url: str) -> bool:
    return "/session/t3-" in url


async def _t3_dispatch_from_http(
    method: str, url: str, *, json_body: dict[str, Any] | None,
) -> _T3Response:
    """Parse the t3-routed URL + body and call T3SessionManager."""
    from superbrowser_bridge.antibot import interactive_session as _t3

    # Split path. Expected forms:
    #   /session/t3-<uuid>             -> DELETE (close)
    #   /session/t3-<uuid>/<verb>      -> GET or POST
    path = url.split(SUPERBROWSER_URL, 1)[-1].split("?", 1)[0]
    parts = [p for p in path.split("/") if p]
    # parts: ["session", "t3-<uuid>", "<verb>?"]
    if len(parts) < 2 or not parts[1].startswith("t3-"):
        return _T3Response({"error": "bad t3 url"}, status_code=400)
    sid = parts[1]
    verb = parts[2] if len(parts) >= 3 else None
    body = dict(json_body or {})

    mgr = _t3.default()
    try:
        if verb is None:
            # DELETE /session/<sid>
            if method.upper() == "DELETE":
                res = await mgr.close(sid)
                return _T3Response(res)
            return _T3Response({"error": "no verb"}, status_code=400)

        if verb == "navigate":
            url_to = body.get("url", "")
            data = await mgr.navigate(sid, url_to, timeout_s=body.get("timeout_s", 45.0))
            return _T3Response(data)

        if verb == "state":
            data = await mgr.state(sid, use_vision=bool(body.get("vision", False)))
            return _T3Response(data)

        if verb == "screenshot":
            png = await mgr.screenshot(sid)
            return _T3Response(png, content=png)

        if verb == "markdown":
            md = await mgr.get_markdown(sid)
            return _T3Response({"content": md})

        if verb == "click":
            if "bbox" in body or ("x" in body and "y" in body):
                x = float(body.get("x", body.get("bbox", {}).get("x0", 0)))
                y = float(body.get("y", body.get("bbox", {}).get("y0", 0)))
                bbox = body.get("bbox")
                expected_label = body.get("expected_label") or body.get("label")
                strategy = body.get("strategy") or "primary"
                data = await mgr.click_at(
                    sid, x, y, bbox=bbox,
                    strategy=str(strategy).lower(),
                    expected_label=(
                        str(expected_label).strip()
                        if expected_label else None
                    ),
                )
            else:
                data = await mgr.click(sid, int(body["index"]))
            return _T3Response(data)

        if verb == "type":
            data = await mgr.type(
                sid,
                int(body["index"]),
                body.get("text", ""),
                clear=bool(body.get("clear", True)),
            )
            return _T3Response(data)

        if verb == "type-at" or verb == "type_at":
            data = await mgr.type_at(
                sid,
                float(body.get("x", 0)),
                float(body.get("y", 0)),
                body.get("text", ""),
                clear=bool(body.get("clear", True)),
                target_label=str(body.get("label", "")),
            )
            return _T3Response(data)

        if verb == "fix-text-at" or verb == "fix_text_at":
            data = await mgr.fix_text_at(
                sid,
                float(body.get("x", 0)),
                float(body.get("y", 0)),
                body.get("text", ""),
                target_label=str(body.get("label", "")),
            )
            return _T3Response(data)

        if verb == "keys":
            # BrowserKeysTool sends `keys` as either a string ("Enter",
            # "ArrowDown", or a chord like "Control+A") OR as a list of
            # such strings. NEVER pass a bare string through `list()` —
            # that splits it into individual characters and presses each
            # one, turning `browser_keys("Enter")` into typing the
            # letters E-n-t-e-r into the focused input.
            raw_keys = body.get("keys", [])
            if isinstance(raw_keys, str):
                keys_list = [raw_keys]
            elif isinstance(raw_keys, (list, tuple)):
                keys_list = [str(k) for k in raw_keys]
            else:
                keys_list = []
            data = await mgr.keys(sid, keys_list)
            return _T3Response(data)

        if verb == "scroll":
            data = await mgr.scroll(
                sid,
                direction=body.get("direction"),
                percent=body.get("percent"),
            )
            return _T3Response(data)

        if verb == "drag":
            data = await mgr.drag(
                sid,
                float(body.get("startX", 0)), float(body.get("startY", 0)),
                float(body.get("endX", 0)), float(body.get("endY", 0)),
                steps=int(body.get("steps", 20)),
            )
            return _T3Response(data)

        if verb == "select":
            data = await mgr.select(sid, int(body["index"]), body.get("value", ""))
            return _T3Response(data)

        if verb == "evaluate":
            data = await mgr.evaluate(sid, body.get("script", ""))
            return _T3Response({"result": data})

        if verb == "script":
            data = await mgr.run_script(sid, body.get("code", ""))
            return _T3Response(data)

        if verb == "wait-for" or verb == "wait_for":
            data = await mgr.wait_for(
                sid,
                selector=body.get("selector"),
                timeout_s=float(body.get("timeout", 10.0)),
            )
            return _T3Response(data)

        # Captcha verbs — map URL shapes like /captcha/detect, /captcha/solve,
        # /captcha/screenshot to the antibot.captcha module.
        if verb == "captcha":
            sub = parts[3] if len(parts) >= 4 else ""
            from superbrowser_bridge.antibot import captcha as _cap
            if sub == "detect":
                info = await _cap.detect(mgr, sid)
                return _T3Response({
                    "captcha": {
                        "present": info.present,
                        "type": info.type,
                        "site_key": info.site_key,
                        "widget_bbox": info.widget_bbox,
                        "widget_selector": info.widget_selector,
                        "frame_url": info.frame_url,
                        "notes": info.notes,
                    },
                })
            if sub == "screenshot":
                info = await _cap.detect(mgr, sid)
                data = await _cap.widget_screenshot(mgr, sid, info)
                return _T3Response(data)
            if sub == "solve":
                info = await _cap.detect(mgr, sid)
                if not info.present:
                    return _T3Response({
                        "solved": True, "method": "none",
                        "note": "no captcha detected at solve time",
                    })
                method = (body.get("method") or "auto").lower()
                if method in ("auto", "token") and info.type in (
                    "recaptcha-v2", "hcaptcha", "turnstile",
                ):
                    res = await _cap.solve_token(mgr, sid, info)
                    if res.get("solved"):
                        return _T3Response(res)
                if method in ("auto", "slider") and info.type == "slider":
                    res = await _cap.solve_slider(mgr, sid, info)
                    if res.get("solved") or method == "slider":
                        return _T3Response(res)
                if (
                    method in ("auto", "cf_wait")
                    and info.type == "cf_interstitial"
                ):
                    # Whole-page CF interstitial — wait for auto-pass.
                    res = await _cap.solve_cf_interstitial(mgr, sid, info)
                    if res.get("solved") or method == "cf_wait":
                        return _T3Response(res)
                if method in ("auto", "vision"):
                    res = await _cap.solve_vision(mgr, sid, info)
                    return _T3Response(res)
                return _T3Response({
                    "solved": False, "method": method,
                    "error": f"no applicable strategy for {info.type}",
                })

        # Verbs that don't yet have a t3 equivalent (dialog, human-input).
        return _T3Response(
            {"error": f"t3 verb '{verb}' not yet implemented"},
            status_code=501,
        )
    except KeyError as exc:
        return _T3Response({"error": f"session {exc} not found"}, status_code=404)
    except Exception as exc:
        # Log the full traceback to both stdout and a dedicated log file
        # so operators can diagnose T3 crashes without having to grep
        # through asyncio noise. The file path is intentionally stable
        # so repeated failures accumulate for post-mortem inspection.
        import traceback as _tb
        tb_str = _tb.format_exc()
        _err_msg = (
            f"[t3 dispatch] verb={verb!r} sid={sid!r} "
            f"{type(exc).__name__}: {exc}"
        )
        print(_err_msg)
        print(tb_str)
        try:
            log_path = os.environ.get("T3_ERROR_LOG") or "/tmp/superbrowser/t3_errors.log"
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as _lf:
                from datetime import datetime as _dt
                _lf.write(
                    f"\n--- {_dt.utcnow().isoformat()} ---\n"
                    f"{_err_msg}\n"
                    f"body: {json.dumps(body, default=str)[:500]}\n"
                    f"{tb_str}\n"
                )
        except Exception:
            pass
        return _T3Response(
            {
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "verb": verb,
                "traceback_head": tb_str.splitlines()[-3:] if tb_str else [],
            },
            status_code=500,
        )


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


async def _request_with_backoff(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> httpx.Response:
    """POST/GET with jittered backoff on transient errors.

    Retries on 429 (rate-limited) and 503 (service overloaded) with
    delays roughly [1s, 2s, 4s] + small jitter. Honors the server's
    Retry-After header when present.

    This exists because a single run of nanobot fires 200-500 tool calls
    against our TS server — without backoff, a brief burst hitting the
    per-IP rate limiter would surface as a hard 429 that the LLM mis-
    classifies as a permanent outage and refuses to retry.
    """
    # Intercept t3 session URLs — route to the in-process patchright manager
    # instead of the TS server.
    if _is_t3_url(url):
        return await _t3_dispatch_from_http(method, url, json_body=json)

    import random
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                if method.upper() == "GET":
                    r = await client.get(url, params=params)
                elif method.upper() == "DELETE":
                    r = await client.delete(url)
                else:
                    r = await client.post(url, json=json)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                if attempt == max_retries:
                    raise
                delay = (2 ** attempt) + random.uniform(0, 0.5)
                print(f"  [net retry {attempt + 1}/{max_retries}] {type(e).__name__}: waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            # Retryable status codes. Honor Retry-After if present.
            if r.status_code in (429, 503):
                if attempt == max_retries:
                    return r  # caller sees the 429 after all retries
                retry_after = r.headers.get("Retry-After")
                try:
                    retry_after_s = float(retry_after) if retry_after else None
                except ValueError:
                    retry_after_s = None
                delay = retry_after_s if retry_after_s is not None else (2 ** attempt) + random.uniform(0, 0.5)
                # Cap at 10s — Retry-After from a confused server could otherwise block the run.
                delay = min(10.0, delay)
                print(f"  [429 retry {attempt + 1}/{max_retries}] waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            return r
    # Unreachable: loop either returns or raises.
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry loop exited without return")


async def _fetch_feedback_state() -> dict[str, Any]:
    """Read the TS-side FeedbackBus snapshot over HTTP.

    Non-fatal on any failure — returns {} so callers fall through to the
    normal dispatch path (caller stays the same when the signal is down).
    """
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/feedback")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


# --- Atomic field-correction JS (tier-agnostic) -------------------------------
# Runs inside /evaluate on either t1 (TS server) or t3 (patchright) to do the
# full probe-write-verify cycle in a single synchronous tick. No intermediate
# empty state where a framework re-render could race. Placeholders
# __TARGET_X__ / __TARGET_Y__ / __TARGET_TEXT__ get string-replaced by the
# Python caller with JSON-literal values.
_ATOMIC_FIX_TEXT_JS = """
(() => {
  const x = __TARGET_X__, y = __TARGET_Y__, target = __TARGET_TEXT__;
  const el = document.elementFromPoint(x, y);
  if (!el) return {ok: false, reason: 'no_element'};
  const tag = el.tagName.toLowerCase();
  const isInput = tag === 'input' || tag === 'textarea';
  const isEditable = !!el.isContentEditable;
  if (!isInput && !isEditable) {
    return {ok: false, reason: 'not_input', tag};
  }
  const attrLabel = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
  const attrName = el.name || '';
  const attrAutocomplete = el.getAttribute('autocomplete') || '';
  const attrInputType = isInput ? (el.getAttribute('type') || 'text').toLowerCase() : '';
  if (isInput) {
    if (['file','checkbox','radio','hidden','submit','button',
         'image','reset','range','color'].includes(attrInputType)) {
      return {ok: false, reason: 'non_text_input', tag, input_type: attrInputType};
    }
  }
  const before = isInput ? (el.value || '') : (el.innerText || '');
  if (before === target) {
    return {ok: true, before, after: target, changed: false, tag,
            label: attrLabel, name: attrName, autocomplete: attrAutocomplete,
            input_type: attrInputType};
  }
  try { el.focus(); } catch (_) {}
  try {
    if (isInput) {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
                                       : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) {
        desc.set.call(el, target);
        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: target}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      } else {
        el.value = target;
      }
    } else if (isEditable) {
      el.innerText = target;
      el.dispatchEvent(new InputEvent('input', {bubbles: true}));
    }
  } catch (e) {
    return {ok: false, reason: 'exception', error: String(e).slice(0, 120), before, tag};
  }
  const after = isInput ? (el.value || '') : (el.innerText || '');
  return {ok: after === target, before, after, changed: before !== after, tag,
          label: attrLabel, name: attrName, autocomplete: attrAutocomplete,
          input_type: attrInputType};
})()
"""


def _diff_text(a: str, b: str) -> str:
    """Human-readable diff summary for a → b text change."""
    if a == b:
        return "no change"
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    suf = 0
    while (suf < len(a) - p and suf < len(b) - p
           and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]):
        suf += 1
    old_mid = a[p:len(a) - suf]
    new_mid = b[p:len(b) - suf]
    if not old_mid and new_mid:
        return f"inserted {new_mid!r} at position {p}"
    if old_mid and not new_mid:
        return f"removed {old_mid!r} at position {p}"
    return f"replaced {old_mid!r} with {new_mid!r} at position {p}"


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
        return asyncio.create_task(_run())
    except Exception:
        return None


async def _append_fresh_vision(
    task: "asyncio.Task[Any] | None",
    result: str,
    *,
    budget_ms: int | None = None,
) -> str:
    """Wait for the prefetched vision pass (up to the budget) and
    append a one-line brain-facing hint to `result` when it arrives.

    The hint lets the planner reason on the post-action screen state
    in the SAME tool response rather than waiting for the next
    screenshot call. If the vision pass didn't finish in time, the
    task keeps running in the background (shielded) and the overlay
    will update on the next push.
    """
    resp = await _await_vision_prefetch(task, budget_ms=budget_ms)
    if resp is None:
        return result
    summary = (getattr(resp, "summary", "") or "").strip()
    if not summary:
        return result
    note = summary[:240]
    freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
    if freshness != "fresh":
        note = f"{note} [freshness={freshness}]"
    sep = "" if result.endswith("\n") else "\n"
    return f"{result}{sep}[vision] {note}"


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


async def _feedback_gate(tool_name: str) -> str | None:
    """Return a deferred-result string when another subsystem owns the
    browser right now (active captcha solve). None means `proceed`.

    Used at the top of mutating tools (click/type/scroll/navigate) to
    keep nanobot from racing the captcha solver — if the gate fires,
    nanobot gets an observation saying "captcha active, retry after 2s"
    and yields instead of firing a click that lands on a solved-then-
    reloaded page.
    """
    state = await _fetch_feedback_state()
    if state.get("captchaActive"):
        strategy = state.get("captchaStrategy") or "unknown"
        msg = (
            f"[feedback] {tool_name} deferred: captcha solve in progress "
            f"(strategy={strategy}). Retry after ~2000ms; do not issue "
            f"more actions until you see the captcha_done signal."
        )
        print(f"  {msg}")
        return msg
    return None

# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.
BLOCKED_BROWSER_OPEN_HARD_STOP = 3

def _maybe_no_effect_prefix(
    data: Any, tool_name: str, base_caption: str,
    *, session_state: "BrowserSessionState | None" = None,
) -> str:
    """Wrap a mutation-tool caption with a `[no_effect:...]` header when
    the TS bridge reports zero url/DOM/focus delta. The base caption is
    preserved so vision prefetch, cached bboxes and elements text still
    reach the brain — the prefix is what the brain AND the worker hook
    read as a hard failure signal.

    Also records the failure against the per-domain tactic registry via
    `routing.record_tactic_failure` and (on effect) decays any prior
    penalty via `routing.decay_tactic_success`. The penalty data is what
    the next worker's delegation prompt reads to pre-select a better
    tactic on sites that systematically reject a given tool.
    """
    had_effect, reason = _classify_effect(data, tool_name)
    # Tactic-penalty bookkeeping — resolve domain from session state.
    domain = ""
    if session_state is not None:
        try:
            from urllib.parse import urlparse
            url = session_state.current_url or ""
            if url:
                host = (urlparse(url).hostname or "").lower()
                domain = host[4:] if host.startswith("www.") else host
        except Exception:
            domain = ""
    try:
        from superbrowser_bridge.routing import (
            record_tactic_failure, decay_tactic_success,
        )
        if domain:
            if had_effect:
                decay_tactic_success(domain, tool_name)
            else:
                record_tactic_failure(domain, tool_name)
    except Exception:
        pass

    if had_effect:
        # Positive reinforcement — when a cursor tool successfully
        # moves the page state, tag it so the brain's next turn sees
        # "this tactic worked" and stays on the cursor track instead
        # of pivoting to scripts. Also resets the script-usage
        # counter so a cursor-success cleanly breaks any recent
        # script streak.
        if tool_name in _CURSOR_TOOL_NAMES:
            if session_state is not None:
                try:
                    session_state.consecutive_script_calls = 0
                except Exception:
                    pass
            return f"[cursor_success:{tool_name}] {base_caption}"
        return base_caption
    hint = (
        f"[no_effect:{tool_name}] {reason}. The tool dispatched but the "
        f"page didn't respond — no DOM mutation, no URL change, no focus "
        f"change. Do NOT retry the same tool with the same target; try "
        f"ONE OF (in this preference order): "
        f"(a) **browser_screenshot** first — the page may have changed "
        f"under you; re-observe and click the fresh [V_n]; "
        f"(b) **browser_semantic_click(target='<label>')** — atomic "
        f"fresh vision + dispatch, works across React apps; "
        f"(c) browser_click_selector(<css>) — pixel-exact if the target "
        f"has a stable CSS hook; "
        f"(d) browser_rewind_to_checkpoint if the page appears frozen. "
        f"Do NOT synthesize clicks via browser_run_script — JS clicks are "
        f"isTrusted=false and bot-detected; the sandbox will reject them."
    )
    return f"{hint}\n{base_caption}"


# Names of the "cursor-based" interaction tools — click, type, drag
# etc. Used by `_maybe_no_effect_prefix` to tag successful actions
# with `[cursor_success:...]` as positive reinforcement. Excludes
# scripts/eval (which don't move the cursor) and observation tools
# like screenshot/get_markdown.
_CURSOR_TOOL_NAMES = frozenset({
    "browser_click",
    "browser_click_at",
    "browser_click_selector",
    "browser_type",
    "browser_type_at",
    "browser_fix_text_at",
    "browser_keys",
    "browser_drag",
    "browser_drag_slider_until",
    "browser_select",
    "browser_semantic_click",
    "browser_semantic_type",
})


def _is_captcha_intent(intent: str) -> bool:
    """True when `intent`'s bucket is a captcha bucket — these intents
    switch the vision prompt into captcha-tile mode, so they should
    NEVER become sticky (would poison subsequent non-captcha tools).
    Falls back to a substring match when intent_bucket is unavailable."""
    if not intent:
        return False
    try:
        from vision_agent.prompts import intent_bucket as _bucket
        return _bucket(intent) in ("solve_captcha", "solve_captcha_step")
    except Exception:
        s = intent.lower()
        return "captcha" in s or "challenge" in s


def _last_vision_has_captcha_flag(state: "BrowserSessionState") -> bool:
    """True when the last vision response's flags.captcha_present is
    True. Used to decide whether a sticky captcha intent is still
    relevant — if the current page isn't flagged as a captcha, the
    intent is stale and should be dropped."""
    resp = getattr(state, "_last_vision_response", None)
    if resp is None:
        return False
    flags = getattr(resp, "flags", None)
    if flags is None:
        return False
    return bool(getattr(flags, "captcha_present", False))


def _maybe_script_usage_warning(state: "BrowserSessionState") -> str:
    """Return a `[script_warning] ...` string when the brain is
    over-using `browser_run_script` even though cursor alternatives
    are visible, else empty string.

    Trigger: 3+ consecutive script calls, AND the last vision pass
    emitted at least one clickable bbox. The warning lists the top 3
    labels so the brain has concrete semantic targets to reach for.
    """
    try:
        count = int(state.consecutive_script_calls or 0)
    except Exception:
        count = 0
    if count < 3:
        return ""
    resp = getattr(state, "_last_vision_response", None)
    bboxes = getattr(resp, "bboxes", None) if resp is not None else None
    if not bboxes:
        return ""
    labels: list[str] = []
    for b in bboxes[:20]:
        if not getattr(b, "clickable", False):
            continue
        lbl = (getattr(b, "label", "") or "").strip()
        if lbl and lbl not in labels:
            labels.append(lbl)
        if len(labels) >= 3:
            break
    if not labels:
        return ""
    rendered = ", ".join(f"'{lbl}'" for lbl in labels)
    return (
        f"[script_warning] {count} consecutive run_script calls. "
        f"Vision shows clickable bboxes ({rendered}, ...) — these can "
        f"be clicked atomically via `browser_semantic_click(target='<label>')` "
        f"without the WAF-block risk scripts carry. Reserve scripts "
        f"for actions no cursor tool can express."
    )


def _classify_effect(
    data: Any, tool_name: str,
) -> tuple[bool, str]:
    """Inspect a mutation tool's HTTP response for the TS `effect` field.

    Returns `(had_effect, no_effect_reason)`:
      * `had_effect=True, ""` when the TS bridge reports any of
        url_changed / mutation_delta > 0 / focused_changed.
      * `had_effect=False, <human_reason>` when all three are zero —
        the caller prefixes `[no_effect:<tool>] …` onto its return so
        the brain and the worker hook can distinguish "the tool fired
        but nothing happened" from a real success.
      * `had_effect=True, ""` when the `effect` field is missing —
        preserves legacy behavior against an older TS bridge that
        hasn't shipped the effect snapshot yet.

    Used by click / type / keys / drag / type_at / click_at / drag_slider
    at the moment they've got the HTTP response back but haven't built
    the brain-facing caption yet.
    """
    if not isinstance(data, dict):
        return True, ""
    effect = data.get("effect")
    if not isinstance(effect, dict):
        return True, ""  # TS side too old OR path didn't capture effect
    url_changed = bool(effect.get("url_changed"))
    try:
        mutation_delta = int(effect.get("mutation_delta") or 0)
    except (TypeError, ValueError):
        mutation_delta = 0
    focused_changed = bool(effect.get("focused_changed"))
    if url_changed or mutation_delta > 0 or focused_changed:
        return True, ""
    return False, (
        f"{tool_name}: url unchanged, DOM unchanged "
        f"(mutation_delta=0), focus unchanged"
    )


# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.



class WorkerMustExitError(RuntimeError):
    """Raised from a tool when the worker must terminate immediately.

    Bubbles up through nanobot's tool runner. Carries a reason string the
    orchestrator can surface to the user so the failure mode is observable
    (vs. a silent iteration drain).
    """


_CAPTCHA_KEYWORDS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "verify you are human", "prove you are not a robot", "slider puzzle",
    "click all images", "select all", "drag the", "i'm not a robot",
)
_HARD_DOMAINS = (
    "apartments.com", "zillow.com", "ticketmaster.com", "nytimes.com",
    "linkedin.com", "instagram.com", "facebook.com",
)
# Hash length used to dedupe screenshots when page content changes on same URL.
_CONTENT_HASH_LEN = 500


def _compute_screenshot_budget(
    task_instruction: str = "",
    target_url: str = "",
    is_research: bool = False,
) -> int:
    """Task-complexity-aware screenshot budget.

    Base=6. +4 for research tasks, +10 for captcha-suspect tasks, +8 for
    known-hard domains. Capped at 30 to prevent runaway cost.
    """
    budget = 6
    lower_task = (task_instruction or "").lower()
    lower_url = (target_url or "").lower()
    if is_research:
        budget += 4
    if any(kw in lower_task for kw in _CAPTCHA_KEYWORDS):
        budget += 10
    if any(dom in lower_url for dom in _HARD_DOMAINS):
        budget += 8
    return min(budget, 30)


class BrowserSessionState:
    """Per-instance state for browser session tools.

    Each Nanobot instance that registers browser tools gets its own state.
    This prevents multi-agent setups from sharing globals.
    """

    # Default budget when no task context is supplied. Use
    # configure_budget() to switch to complexity-aware allocation.
    DEFAULT_SCREENSHOT_BUDGET = 6
    CAPTCHA_MODE_ITERATIONS = 15
    MAX_CLICK_AT = 3

    def __init__(self):
        self.max_screenshots = self.DEFAULT_SCREENSHOT_BUDGET
        self.screenshot_budget = self.max_screenshots
        self.vision_calls = 0
        self.text_calls = 0
        self.start_time = 0.0
        self.sessions_opened = 0
        self.activity_log: list[str] = []
        # Per-session (reset on each browser_open)
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

        # Checkpoint & URL tracking
        self.task_id: str = ""
        self.checkpoints: list[dict] = []
        self.current_url: str = ""
        self.best_checkpoint_url: str = ""
        self.url_visit_counts: dict[str, int] = {}
        self.regression_count: int = 0
        # Dedupe key: (normalized_url, hash_of_content) — so a same URL with
        # changed content (e.g., after clicking "Load more") still allows a new
        # screenshot. Populated in mark_screenshot_taken().
        self.screenshotted_keys: set[tuple[str, str]] = set()
        self.last_screenshot_url: str = ""
        self.last_page_content_hash: str = ""
        self.step_history: list[dict] = []
        # Track consecutive click-type tool calls for loop detection
        self.consecutive_click_calls: int = 0
        # Hard guard against the brain re-clicking a target that produced
        # no DOM change. Cleared by `register_click_attempt` on a fresh
        # target; incremented when the same target re-fires AND the page
        # didn't change since the previous click. The guard refuses to
        # dispatch the (MAX_CONSECUTIVE_SAME_TARGET)th attempt — i.e.
        # one retry is allowed (some JS buttons genuinely need a second
        # click), but the third strike returns a structured error so the
        # brain is forced to switch tactic.
        self.last_click_target: str = ""
        self.last_click_dom_hash: str = ""
        self.consecutive_dead_clicks: int = 0
        self.MAX_CONSECUTIVE_SAME_TARGET = 3
        # Cross-index flail guard. consecutive_dead_clicks only catches
        # REPEATS of the same target. When the brain walks
        # [21]→[22]→[20] with every dispatch timing out, each looks like
        # a fresh target so that guard resets. Track HTTP timeouts
        # independently so two-in-a-row forces a re-screenshot.
        self.consecutive_click_timeouts: int = 0
        self.MAX_CONSECUTIVE_CLICK_TIMEOUTS = 2
        # Telemetry: how many times the TS-side snap-to-interactive
        # failed to find a clickable descendant inside the bbox we sent.
        # Incremented whenever a click response has snap.snapped=false.
        # Reset on every screenshot. Used to surface "vision bboxes are
        # habitually wrapping non-clickable containers" hints.
        self.snap_miss_count: int = 0
        # Active session ID (set by browser_open)
        self.session_id: str = ""
        # How many times has BrowserOpenTool had to refuse a redundant call?
        # The guard returns a stern message on the first few; if the LLM keeps
        # ignoring it past BLOCKED_BROWSER_OPEN_HARD_STOP, we raise to abort
        # the worker rather than silently drain its iteration budget.
        self.blocked_browser_open_count: int = 0

        # Captcha-mode: when a captcha is detected, relax the "no actions since
        # last screenshot" rule for CAPTCHA_MODE_ITERATIONS iterations. The
        # per-round counter `captcha_solve_round` is included in the dedup key
        # so each solve attempt gets its own screenshot allowance — preventing
        # the blanket bypass that used to let the worker screenshot the same
        # unchanged captcha 15 times.
        self.captcha_mode: bool = False
        self.captcha_mode_remaining: int = 0
        self.captcha_solve_round: int = 0

        # Cloudflare-interstitial navigation guard: set by BrowserNavigateTool
        # when the server reports block_class=cloudflare, cleared by a
        # successful browser_solve_captcha or navigation to a different URL.
        # While set, a repeat navigate to the same URL is refused with a
        # structured error telling the agent to solve first.
        self.last_nav_cf_blocked_url: str = ""
        self.nav_solve_called_since_block: bool = False

        # Per-index fingerprint cache. Populated on each /state fetch; read
        # by click/type tools to send `expected_fingerprint` along with the
        # request. Lets the TS side reject clicks that would land on a
        # different element than the LLM originally targeted (stale index).
        self.element_fingerprints: dict[int, str] = {}
        self.captcha_screenshots_used: int = 0
        # Hard cap on screenshots allowed within captcha mode (across rounds).
        # Kicks in only in captcha mode; the normal budget still caps overall.
        self.captcha_mode_screenshot_cap: int = 8

        # Network-layer block: set by browser_open/browser_navigate when the
        # target returns 4xx/5xx. Distinguishes "site blocks automated clients
        # before any page loads" from "page loaded but interaction failed" —
        # completely different failure classes needing different remediations
        # (IP/TLS/proxy vs. selector/timing/captcha).
        self.network_blocked: bool = False
        self.last_network_status: int | None = None

        # Human-in-the-loop handoff: when True, the TS server registers a
        # HumanInputManager for this session AND the captcha orchestrator
        # will fall back to human handoff after auto-strategies exhaust.
        # Orchestrator sets this before register_session_tools(). Default
        # False to preserve no-op behavior for workers that don't opt in.
        self.human_handoff_enabled: bool = False
        # Per-session budget — relayed to the TS server which enforces it.
        # 1 by default, overridable via SUPERBROWSER_MAX_HUMAN_HANDOFFS.
        self.human_handoff_budget: int = 1

        # Domain pinning: when set, BrowserNavigateTool rejects URLs
        # outside this domain (+ subdomains) and a small safe-list
        # (google.com, etc.). Prevents the worker LLM from hallucinating
        # to alternative sites when the target blocks it.
        self.pinned_domain: str = ""

        # Vision preprocessor bookkeeping. Populated inside
        # build_tool_result_blocks so tools that don't pass an intent
        # explicitly inherit the last one used — useful when the brain
        # fires a chained sequence (navigate → click → verify) with the
        # same underlying intent.
        self._last_intent: str = ""
        self._last_dom_hash: str = ""
        self._last_vision_summary: str = ""
        # Task context stamped by configure_budget() when the orchestrator
        # spawns a browser worker — piped into the vision prompt so
        # Gemini knows WHAT the user is trying to do before it picks
        # which bboxes to emit.
        self.task_instruction: str = ""
        self.task_target_url: str = ""
        # Cached last VisionResponse so browser_click_at can resolve a
        # vision-index reference (e.g. bbox=V3) back to the original
        # bbox without re-running the vision pass. Reset whenever a new
        # screenshot triggers a fresh vision call.
        # Typed as the actual schema when available, falls back to Any
        # so the import stays lazy for environments without vision_agent.
        try:
            from nanobot.vision_agent.schemas import VisionResponse as _VR  # noqa: F401
            self._last_vision_response: Optional["_VR"] = None  # type: ignore[assignment]
        except Exception:
            self._last_vision_response: Any = None  # type: ignore[assignment]
        # Freshness bookkeeping for the cached vision response. Mutating
        # tools read these to decide whether to piggyback
        # `_last_vision_response.as_brain_text()` onto their text reply —
        # that's the fast path that keeps bboxes in front of the brain
        # without a browser_screenshot round trip.
        self._last_vision_ts: float = 0.0
        self._last_vision_url: str = ""
        # Vision epoch — a frozen snapshot of the vision response that
        # was LAST emitted to the brain as screenshot text. Tools that
        # resolve `vision_index` (click_at, type_at, fix_text_at,
        # drag_slider_until) read this FIRST, falling back to
        # `_last_vision_response` only when the epoch is None. Needed
        # because background vision prefetches overwrite
        # `_last_vision_response` between screenshot-text-emit and
        # click-dispatch — without the epoch, the brain's `V_n`
        # picked from the screenshot resolves against a RENUMBERED
        # prefetch response and lands on the wrong element. Advanced
        # only when `BrowserScreenshotTool` emits fresh vision text;
        # cleared on reset_per_session and on successful navigate.
        self._vision_epoch_id: int = 0
        self._vision_epoch_response: Any = None
        # URL the epoch was captured on. F5 — when `current_url` no
        # longer matches this, the epoch is stale (page implicitly
        # navigated via Enter / form submit / button click) and
        # `vision_for_target_resolution` falls back to the live
        # `_last_vision_response` so the next click resolves V_n
        # against the new page's bbox list, not the prior page's.
        self._vision_epoch_url: str = ""

        # Dead-type guard state. Tracks the last browser_type call so we can
        # reject a second identical type to the same index — the pattern
        # that produces "khulnakhulna, bangladesh" when the LLM misses an
        # autocomplete dropdown and re-types the full phrase.
        self.last_type_index: int = -1
        self.last_type_text: str = ""
        self.last_type_at: float = 0.0

        # Hierarchical perceive-plan-act state. Populated by the
        # screenshot tool after a vision pass; consumed by the click
        # ladder and the browser_plan_next_steps tool.
        #   _last_blockers       — DOM-derived blockers from ui_blockers.detect
        #   _last_action_queue   — ActionQueue from action_planner.plan
        #   _pending_postcondition — Postcondition dict the next click is
        #                            supposed to satisfy; verify_action
        #                            checks it after click_at returns.
        self._last_blockers: list = []  # list[BlockerInfo]
        self._last_action_queue: Any = None  # Optional[ActionQueue]
        self._pending_postcondition: Optional[dict] = None

    @property
    def backend(self) -> str:
        """Tier of the active session. `t3` for patchright (undetected
        Chromium), `t1` for Puppeteer via the TS server. Derived from
        session_id prefix.
        """
        return "t3" if self.session_id.startswith("t3-") else "t1"

    # --- budget configuration ---------------------------------------------

    def configure_budget(
        self,
        task_instruction: str = "",
        target_url: str = "",
        is_research: bool = False,
    ) -> int:
        """Set screenshot budget based on task complexity. Returns new budget."""
        self.max_screenshots = _compute_screenshot_budget(
            task_instruction=task_instruction,
            target_url=target_url,
            is_research=is_research,
        )
        self.screenshot_budget = self.max_screenshots
        # Capture the task context so the vision agent can reason about
        # what the agent is trying to do on this site when picking which
        # regions to bbox. "Book a flight on trip.com" → the vision agent
        # should prioritize departure / destination / date / search
        # button bboxes, not navbar noise.
        self.task_instruction = (task_instruction or "")[:500]
        self.task_target_url = target_url or ""
        return self.max_screenshots

    def enter_captcha_mode(self) -> None:
        """Relax screenshot limits for the next N iterations.

        Called when browser_detect_captcha returns a captcha. Captcha
        solving requires multiple screenshots per round (before drag,
        after drag, verify result) — normal budget would starve it.
        Resets per-round counters so a re-entry doesn't inherit stale
        dedup state from a previous challenge.
        """
        self.captcha_mode = True
        self.captcha_mode_remaining = self.CAPTCHA_MODE_ITERATIONS
        self.captcha_solve_round = 0
        self.captcha_screenshots_used = 0

    def tick_captcha_mode(self) -> None:
        """Decrement captcha_mode counter. Call once per agent iteration."""
        if not self.captcha_mode:
            return
        self.captcha_mode_remaining -= 1
        if self.captcha_mode_remaining <= 0:
            self.captcha_mode = False
            self.captcha_mode_remaining = 0

    def reset_per_session(self):
        """Reset per-session counters. Budget is NOT reset."""
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0
        # Epoch from a prior session is meaningless for the new one.
        self._vision_epoch_response = None
        self._vision_epoch_id = 0
        self._vision_epoch_url = ""

    def freeze_vision_epoch(self) -> None:
        """Snapshot `_last_vision_response` as the current epoch.

        Called by `BrowserScreenshotTool` right after it emits the
        vision bbox text to the brain. Subsequent `browser_click_at(
        vision_index=V_n)` / `browser_type_at` calls resolve `V_n`
        against THIS snapshot, not against the live
        `_last_vision_response` (which a background prefetch may have
        overwritten between screenshot-text-emit and click-dispatch —
        that's the V_n drift bug).

        Also captures the URL so `vision_for_target_resolution` can
        invalidate the epoch when the page implicitly navigates
        (browser_keys(Enter), button-clicks-that-submit-a-form, etc.)
        — `state.current_url` will no longer match `_vision_epoch_url`
        and the epoch falls through to the live response.
        """
        self._vision_epoch_response = self._last_vision_response
        self._vision_epoch_url = self._last_vision_url or self.current_url or ""
        self._vision_epoch_id += 1

    def vision_for_target_resolution(self) -> Any:
        """Return the vision response V-index readers (click_at /
        type_at / fix_text_at / the slider family) should resolve
        against. Prefers the frozen epoch; falls back to the live
        `_last_vision_response` when:
          - no epoch has been captured yet (first turn / mocked test);
          - the epoch's URL no longer matches `current_url` (the page
            implicitly navigated since the brain saw the screenshot —
            F5 fix; otherwise the brain's V_n picked from page A
            resolves against page A's bbox list while the click lands
            on page B, with bboxes that no longer apply).
        """
        if self._vision_epoch_response is not None:
            epoch_url = self._normalize_url(self._vision_epoch_url or "")
            current_url = self._normalize_url(self.current_url or "")
            if epoch_url and current_url and epoch_url != current_url:
                # Page changed. Epoch is stale. Live response is the
                # post-mutation prefetch and matches the current page.
                return self._last_vision_response
            return self._vision_epoch_response
        return self._last_vision_response

    def init_if_needed(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for comparison (strip trailing slash, fragment)."""
        if not url:
            return ""
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))

    def record_url(self, url: str) -> None:
        """Track a URL visit. Updates current_url and visit counts."""
        if not url:
            return
        norm = self._normalize_url(url)
        self.current_url = url
        self.url_visit_counts[norm] = self.url_visit_counts.get(norm, 0) + 1

    def record_checkpoint(self, url: str, title: str, action: str) -> None:
        """Record a progress checkpoint (successful meaningful step)."""
        self.checkpoints.append({
            "url": url, "title": title, "action": action,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if url and url != self.best_checkpoint_url:
            self.best_checkpoint_url = url

    def is_regression(self, url: str) -> bool:
        """Check if navigating to url is going backward."""
        if not url or not self.best_checkpoint_url:
            return False
        norm = self._normalize_url(url)
        best_norm = self._normalize_url(self.best_checkpoint_url)
        # Regression = revisiting an earlier URL when we've been deeper
        if norm == best_norm:
            return False
        return self.url_visit_counts.get(norm, 0) > 0

    def should_allow_screenshot(
        self,
        url: str,
        content_hash: str = "",
    ) -> tuple[bool, str]:
        """Check if a screenshot should be allowed. Returns (allowed, reason).

        Captcha mode no longer blanket-bypasses dedup. Instead:
          - actions_since_screenshot check is relaxed (a captcha round might
            genuinely need multiple vision calls between tool actions)
          - captcha_solve_round is folded into the dedup key so each solve
            attempt gets its own allowance
          - a hard cap (captcha_mode_screenshot_cap) prevents runaway burn
            even if vision keeps failing
        """
        if self.screenshot_budget <= 0:
            return False, "[Screenshot budget exhausted] Use browser_get_markdown or browser_eval instead."
        if self.captcha_mode:
            if self.captcha_screenshots_used >= self.captcha_mode_screenshot_cap:
                return False, (
                    f"[Captcha-mode screenshot cap hit ({self.captcha_mode_screenshot_cap}). "
                    "The vision-based solve isn't converging. Call browser_ask_user for human help, "
                    "or browser_request_help to hand off to a fresh tactic.]"
                )
            norm = self._normalize_url(url)
            key = (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            if norm and key in self.screenshotted_keys:
                return False, (
                    "[Captcha screenshot already taken for this solve round with no change. "
                    "Call browser_solve_captcha or browser_click a tile — don't re-screenshot the same state.]"
                )
            return True, ""
        if self.actions_since_screenshot == 0:
            return False, "[No actions since last screenshot — reuse previous. Use browser_get_markdown to re-read content.]"
        norm = self._normalize_url(url)
        # Dedupe on (url, content_hash) — if content changed since last shot
        # at this URL, allow a fresh screenshot.
        key = (norm, content_hash or "")
        if norm and key in self.screenshotted_keys:
            return False, "[Screenshot already exists for this URL + content. Use browser_get_markdown or browser_eval to read page state instead.]"
        return True, ""

    def mark_screenshot_taken(self, url: str, content_hash: str = "") -> None:
        """Record that a screenshot was taken for (url, content_hash).

        In captcha mode the key includes the current solve round so each
        solve attempt gets its own dedup allowance.
        """
        norm = self._normalize_url(url)
        if not norm:
            return
        if self.captcha_mode:
            self.captcha_screenshots_used += 1
            self.screenshotted_keys.add(
                (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            )
        else:
            self.screenshotted_keys.add((norm, content_hash or ""))
        self.last_screenshot_url = norm
        self.last_page_content_hash = content_hash or ""

    @staticmethod
    def hash_page_content(text: str, scroll_y: int | None = None) -> str:
        """Structural fingerprint of a page for screenshot dedup.

        Replaces the old "SHA1 of first 500 chars" scheme, which was both
        insensitive to real changes (below-fold content, lazy loads) and
        over-sensitive to benign re-renders (React class-hash churn).

        Input is the `clickableElementsToString()` payload returned by the
        TS server — one line per interactive element, format roughly:
          `[0]<button aria-label="Sign in">Sign in</button>`

        Fingerprint inputs:
          - count of interactive elements (line count)
          - tag-name histogram (button/input/a/…)
          - top-N aria-label / placeholder / name values, normalized
          - scroll-Y bucketed to 100px when supplied

        All inputs are concatenated into a deterministic canonical string,
        then SHA1-hashed and truncated. Bucketing scroll keeps tiny scroll
        jitters from invalidating dedup while still distinguishing real
        scroll positions.
        """
        if not text:
            return ""
        import hashlib
        import re

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        count = len(lines)

        # Tag histogram — parse `<tag` at the start of each element snippet.
        tag_counts: dict[str, int] = {}
        attr_pat = re.compile(r'(?:aria-label|placeholder|name)="([^"]+)"')
        attr_samples: list[str] = []
        for ln in lines[:40]:  # cap at 40 to keep bounded
            m = re.search(r"<([a-zA-Z][a-zA-Z0-9]*)", ln)
            if m:
                tag = m.group(1).lower()
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            # Grab first meaningful attribute to anchor identity.
            a = attr_pat.search(ln)
            if a:
                val = a.group(1).strip().lower()
                # Normalize whitespace + drop digits (React IDs, counters).
                val = re.sub(r"\s+", " ", val)
                val = re.sub(r"\d+", "#", val)
                attr_samples.append(val[:40])

        hist = ",".join(f"{t}:{n}" for t, n in sorted(tag_counts.items()))
        # Use the first 20 normalized attributes — enough to differentiate
        # pages, few enough that a single added menu item doesn't flip the hash.
        attrs = "|".join(attr_samples[:20])
        scroll_bucket = ""
        if scroll_y is not None:
            scroll_bucket = f"s{int(scroll_y) // 100}"

        canonical = f"n={count}|h={hist}|a={attrs}|{scroll_bucket}"
        return hashlib.sha1(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def record_step(self, tool_name: str, args_summary: str, result_summary: str) -> None:
        """Record a step in the structured step history."""
        self.step_history.append({
            "tool": tool_name,
            "args": args_summary,
            "result": result_summary[:200],
            "url": self.current_url,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    def check_dead_click(self, click_target: str) -> str | None:
        """Pre-flight check before dispatching a click.

        Counts how many times this exact click target has been fired in
        a row without the page DOM changing in between. Once the count
        would reach `MAX_CONSECUTIVE_SAME_TARGET`, refuse with a
        structured error so the brain is forced to pick a different
        target. Different target OR a DOM change resets the count.

        DOM change is detected via `_last_dom_hash` (set by every state
        fetch).

        Returns a structured error string when blocking, or None when
        the click is allowed to proceed.
        """
        same_target = (click_target == self.last_click_target)
        same_dom = (
            bool(self._last_dom_hash)
            and self._last_dom_hash == self.last_click_dom_hash
        )
        if same_target and same_dom:
            # Nth consecutive dead attempt at the same target.
            self.consecutive_dead_clicks += 1
        else:
            # Fresh attempt at this target (different target OR page moved).
            self.consecutive_dead_clicks = 1
        if self.consecutive_dead_clicks >= self.MAX_CONSECUTIVE_SAME_TARGET:
            # Reset so the brain picking a new target next round clears
            # the strike count cleanly.
            self.consecutive_dead_clicks = 0
            self.last_click_target = ""
            return (
                f"[dead_click_blocked] {click_target} has been clicked "
                f"{self.MAX_CONSECUTIVE_SAME_TARGET} times in a row with "
                "no DOM change. The previous clicks did not move the "
                "page. Switch tactic: call browser_screenshot to "
                "re-observe, then pick a different [V_n]/[index], try a "
                "different role (e.g., the form's submit button instead "
                "of the input), try browser_click_selector with a stable "
                "CSS hook, or browser_wait_for content you expect to "
                "appear. Do NOT retry this exact target, and do NOT "
                "synthesize clicks via browser_run_script — JS clicks "
                "are isTrusted=false and bot-detected."
            )
        return None

    def register_click_attempt(self, click_target: str) -> None:
        """Stamp the current click target + DOM hash so the next call to
        `check_dead_click` can compare against them."""
        self.last_click_target = click_target
        self.last_click_dom_hash = self._last_dom_hash

    def advance_observation_token(self, source: str = "") -> None:
        """No-op shim retained so kept tools (click_selector,
        rewind_to_checkpoint, scroll_until, drag_slider_until) that
        were ported forward from the validator era can still call it
        without blowing up. The token machinery was part of the
        deleted validator subsystem; in the reverted architecture the
        in-tool freshness/blocker/confidence gates in click_at do
        the same job."""
        pass

    def get_last_checkpoint(self) -> dict | None:
        """Return the most recent checkpoint."""
        return self.checkpoints[-1] if self.checkpoints else None

    def export_step_history(self) -> str:
        """Export structured step history and checkpoint to disk.

        Writes TWO formats:
          - step_history.md  — human-readable markdown log
          - step_history.json — structured data the orchestrator parses
            to build domain-keyed captcha learnings and to inject prior
            context into subsequent tasks.
        """
        lines = ["## Step History"]
        for i, step in enumerate(self.step_history, 1):
            lines.append(f"{i}. [{step['time']}] {step['tool']}({step['args']}) → {step['result']}")
            if step.get("url"):
                lines.append(f"   URL: {step['url']}")

        if self.checkpoints:
            lines.append("\n## Checkpoints (progress markers)")
            for cp in self.checkpoints:
                lines.append(f"- [{cp['time']}] {cp['action']} → {cp['url']}")

        lines.append(f"\n## Best checkpoint URL: {self.best_checkpoint_url or 'none'}")
        lines.append(f"## Regressions detected: {self.regression_count}")

        content = "\n".join(lines)

        # Write to task-specific directory
        task_dir = f"/tmp/superbrowser/{self.task_id}" if self.task_id else "/tmp/superbrowser"
        os.makedirs(task_dir, exist_ok=True)
        step_path = os.path.join(task_dir, "step_history.md")
        with open(step_path, "w") as f:
            f.write(content)
        print(f"  [step history saved: {step_path}]")

        # Structured JSON export for orchestrator consumption.
        import json as _json_export
        structured = {
            "task_id": self.task_id,
            "sessions_opened": self.sessions_opened,
            "current_url": self.current_url,
            "best_checkpoint_url": self.best_checkpoint_url,
            "regression_count": self.regression_count,
            "checkpoints": self.checkpoints,
            "vision_calls": self.vision_calls,
            "text_calls": self.text_calls,
            "max_screenshots": self.max_screenshots,
            "screenshots_used": self.max_screenshots - self.screenshot_budget,
            "steps": self.step_history,
            "activity_log": self.activity_log,
        }
        json_path = os.path.join(task_dir, "step_history.json")
        try:
            with open(json_path, "w") as f:
                _json_export.dump(structured, f, indent=2, default=str)
            print(f"  [structured history saved: {json_path}]")
        except Exception as exc:  # pragma: no cover - best-effort persistence
            print(f"  [structured history save failed: {exc}]")

        # Save checkpoint as JSON for re-delegation
        if self.best_checkpoint_url:
            import json as _json
            checkpoint_data = {
                "url": self.best_checkpoint_url,
                "title": self.checkpoints[-1].get("title", "") if self.checkpoints else "",
                "regressions": self.regression_count,
            }
            cp_path = os.path.join(task_dir, "checkpoint.json")
            with open(cp_path, "w") as f:
                _json.dump(checkpoint_data, f)
            print(f"  [checkpoint saved: {cp_path}]")

        return content

    def log_activity(self, action: str, result: str = "ok"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {action}"
        if result != "ok":
            entry += f" → {result}"
        self.activity_log.append(entry)
        if len(self.activity_log) > 30:
            self.activity_log.pop(0)

    def get_activity_summary(self) -> str:
        if not self.activity_log:
            return ""
        lines = "\n".join(self.activity_log[-15:])
        return (
            f"\n--- Previous activity (DO NOT repeat failed approaches) ---\n"
            f"{lines}\n"
            f"--- Screenshots remaining: {self.screenshot_budget}/{self.max_screenshots} | Sessions opened: {self.sessions_opened} ---"
        )

    def print_summary(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget
        print(f"\n  [Session Summary]")
        print(f"  Duration: {elapsed:.1f}s | Sessions: {self.sessions_opened}")
        print(f"  Vision calls: {self.vision_calls} | Text calls: {self.text_calls} | Screenshots: {used}/{self.max_screenshots}")
        est = self.vision_calls * 0.03 + self.text_calls * 0.002
        print(f"  Estimated cost: ~${est:.3f}")

    def export_activity_log(self) -> str:
        """Export structured activity log to disk for the orchestrator to read."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget

        lines = [
            f"## Browser Worker Activity",
            f"Duration: {elapsed:.1f}s | Screenshots: {used}/{self.max_screenshots} | Tool calls: {self.vision_calls + self.text_calls}",
            "",
            "### Actions",
        ]
        lines.extend(self.activity_log)
        content = "\n".join(lines)

        # Write to disk so orchestrator/subagent can read it
        activity_path = "/tmp/superbrowser/last_activity.md"
        os.makedirs(os.path.dirname(activity_path), exist_ok=True)
        with open(activity_path, "w") as f:
            f.write(content)
        print(f"  [activity log saved: {activity_path}]")
        return content

    def save_screenshot(self, b64: str, label: str = "") -> str:
        self.step_counter += 1
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        fn = f"{self.step_counter:03d}-{label}.jpg" if label else f"{self.step_counter:03d}.jpg"
        path = os.path.join(SCREENSHOT_DIR, fn)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  [screenshot saved: {path}]")
        return path

    async def build_tool_result_blocks(
        self,
        b64: str,
        caption: str,
        *,
        intent: str | None = None,
        url: str | None = None,
        elements: str | None = None,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict] | str:
        """Async dispatch between the vision-preprocessor path and the
        legacy image-blocks path.

        When `VISION_ENABLED=1` the screenshot is sent to the dedicated
        vision agent (cheap model) and the brain only sees its textual
        summary + bboxes + flags. Otherwise we fall through to the
        legacy `build_image_blocks` that embeds the JPEG directly.

        `intent` and `url` are optional hints used solely by the vision
        path. `elements` is the DOM element listing — hashed as a cache
        key so a re-screenshot on the same URL with identical DOM hits
        the cache.
        """
        # Lazy import: keeps the vision package optional at import time
        # so a broken VISION_API_KEY doesn't blow up sessions that never
        # enable the feature.
        try:
            from vision_agent import (
                dom_hash_of,
                get_vision_agent,
                vision_agent_enabled,
            )
        except ImportError:
            vision_agent_enabled = lambda: False  # type: ignore[assignment]
            get_vision_agent = None  # type: ignore[assignment]
            dom_hash_of = None  # type: ignore[assignment]

        if vision_agent_enabled() and get_vision_agent is not None:
            dh = dom_hash_of(elements) if dom_hash_of else ""
            if dh:
                self._last_dom_hash = dh
            if intent:
                self._last_intent = intent
            effective_intent = intent or self._last_intent or "observe page"
            effective_url = url or self.current_url
            try:
                agent = get_vision_agent()
                # Read screenshot dims off the bytes once — Gemini emits
                # box_2d in [0, 1000] space; downstream click dispatch
                # needs the source pixel dims to convert back to viewport
                # coordinates accurately.
                img_w, img_h = _read_image_dims(b64)
                resp = await agent.analyze(
                    screenshot_b64=b64,
                    intent=effective_intent,
                    session_id=self.session_id,
                    url=effective_url,
                    dom_hash=dh or self._last_dom_hash,
                    previous_summary=self._last_vision_summary or None,
                    image_width=img_w,
                    image_height=img_h,
                    task_instruction=self.task_instruction or None,
                )
                self._last_vision_summary = resp.summary
                self._last_vision_response = resp
                self._last_vision_ts = time.time()
                self._last_vision_url = effective_url or self.current_url or ""
                self.vision_calls += 1
                self.actions_since_screenshot = 0
                # Freeze this response as the current epoch. The brain
                # is about to see `as_brain_text()` output — subsequent
                # V_n references MUST resolve to this snapshot, not to
                # whatever background prefetch writes into
                # `_last_vision_response` before the brain's next turn.
                self.freeze_vision_epoch()
                label = (caption or "").split("\n")[0][:30].replace(" ", "-")
                # Still save the raw screenshot locally for debugging —
                # doesn't leave the box, doesn't reach the brain.
                self.save_screenshot(b64, label)
                # Fire-and-forget push of detected bboxes to live viewers.
                # Lets the user see what Gemini "saw" (full set, color-
                # coded by role) for ~1.5s before the next click. Failure
                # is non-fatal — vision still works, just no overlay.
                try:
                    asyncio.create_task(_push_vision_bboxes(
                        self.session_id, resp,
                        url=self._last_vision_url,
                    ))
                except Exception as exc:
                    print(f"  [vision-overlay push failed: {exc}]")

                # Hierarchical planner pass — DOM-side blocker scan +
                # action sequencing. Only runs for t3 sessions (t1
                # Puppeteer sessions would need a TS-side blocker
                # endpoint; deferred). Soft-fails: any exception here
                # falls back to the vision-only caption.
                plan_text = ""
                if self.session_id.startswith("t3-") and \
                        os.environ.get("ACTION_PLANNER_AUTO", "1") != "0":
                    try:
                        from superbrowser_bridge.antibot import interactive_session as _t3mgr
                        from superbrowser_bridge.antibot.ui_blockers import detect as _detect_blockers
                        from superbrowser_bridge.action_planner import plan as _plan_actions
                        mgr = _t3mgr.default()
                        blockers = await _detect_blockers(mgr, self.session_id)
                        self._last_blockers = blockers
                        queue = _plan_actions(
                            vresp=resp,
                            blockers=blockers,
                            task_instruction=self.task_instruction or "",
                            url=effective_url or "",
                            recent_steps=self.step_history[-8:] if self.step_history else [],
                        )
                        self._last_action_queue = queue
                        plan_text = queue.to_brain_text()
                    except Exception as exc:
                        print(f"  [action-planner: skipped — {exc}]")

                brain_text = resp.as_brain_text()
                if plan_text:
                    brain_text = f"{brain_text}\n\n{plan_text}"
                text = f"{caption}\n\n{brain_text}" if caption else brain_text
                return [{"type": "text", "text": text}]
            except Exception as exc:
                # Never let a vision-layer failure break a tool result —
                # fall through to the legacy image path.
                print(f"  [vision-agent: falling back to image blocks — {exc}]")

        return self.build_image_blocks(
            b64,
            caption,
            elements_with_bounds=elements_with_bounds,
            device_pixel_ratio=device_pixel_ratio,
        )

    def build_image_blocks(
        self,
        b64: str,
        caption: str,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict]:
        """Build a vision-message-ready payload (text + image).

        If `elements_with_bounds` is provided, paint dashed bbox overlays +
        index labels on the screenshot so the LLM can ground on [index]
        instead of guessing pixel coordinates. Silently falls back to the
        raw screenshot if PIL is unavailable or overlay fails.
        """
        self.vision_calls += 1
        self.actions_since_screenshot = 0
        label = caption.split("\n")[0][:30].replace(" ", "-").replace("/", "_")

        final_b64 = b64
        if elements_with_bounds:
            try:
                from superbrowser_bridge.highlights import build_highlighted_screenshot
                final_b64 = build_highlighted_screenshot(
                    b64, elements_with_bounds, device_pixel_ratio,
                )
            except Exception:
                final_b64 = b64

        # Clamp the final payload to ≤ 2MB / ≤1568px side. Runs AFTER the
        # highlight overlay pass so overlay bloat can't tip a 1.8MB raw
        # screenshot over Gemini's 1.5MB-ish reject threshold.
        try:
            from superbrowser_bridge.image_safety import sanitize_image_b64
            final_b64 = sanitize_image_b64(final_b64)
        except Exception as e:
            print(f"  [image-safety: sanitize failed, sending raw: {e}]")

        self.save_screenshot(final_b64, label)
        return [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{final_b64}"}},
        ]

    def build_text_only(self, data: dict, prefix: str = "") -> str:
        self.action_count += 1
        self.text_calls += 1
        self.actions_since_screenshot += 1
        parts = [prefix]
        if data.get("url"):
            parts.append(f"Page: {data['url']}")
        if data.get("title"):
            parts.append(f"Title: {data['title']}")
        result = " | ".join(p for p in parts if p)
        # Auto-include interactive elements so agent knows what's on page
        # (BrowserOS pattern: every action returns updated element snapshot)
        if data.get("elements"):
            result += f"\n\nInteractive elements:\n{data['elements']}"
        if data.get("consoleErrors"):
            result += f"\nConsole errors: {data['consoleErrors']}"
        if data.get("pendingDialogs"):
            result += f"\nPending dialogs: {data['pendingDialogs']}"
        # Piggyback cached vision if it's still fresh — gives the brain
        # up-to-date bboxes after a mutating tool WITHOUT a screenshot
        # round trip + Gemini call. "Fresh" = same URL as the action's
        # response AND less than FRESH_VISION_SECONDS old. The brain can
        # then call browser_click_at(vision_index=V_n) immediately on the
        # next turn, skipping a 2-5s vision pass.
        cached = self._fresh_vision_text(data.get("url", ""))
        if cached:
            result += f"\n\n{cached}"
        if self.action_count >= 5:
            result += (
                "\n\n[HINT: Keep using browser_click_at(vision_index=V_n) / "
                "browser_type_at for every interaction — each fires a "
                "real CDP mouse event with humanized motion, which "
                "avoids bot-detection. Do NOT batch steps into "
                "browser_run_script; JS clicks are isTrusted=false and "
                "frequently rejected by WAF-protected sites.]"
            )
        return result

    # How old a cached vision response can be before we stop piggybacking
    # it onto mutating-tool replies. Short enough that the brain doesn't
    # click stale bboxes; long enough to cover a rapid click-scroll-click
    # sequence where no fresh vision has landed yet.
    FRESH_VISION_SECONDS = 10.0

    def _fresh_vision_text(self, tool_url: str) -> str:
        """Return cached vision's brain_text when safe to attach, else "".

        Safe means: we have a cached VisionResponse, its URL matches the
        URL this tool response is reporting (so the brain doesn't mistake
        pre-navigation bboxes for post-navigation state), and it's young
        enough (FRESH_VISION_SECONDS) to still reflect the page.

        Rendering is deliberately cheap — `as_brain_text()` is pure
        Python string formatting, no I/O.
        """
        resp = self._last_vision_response
        if resp is None:
            return ""
        if (time.time() - self._last_vision_ts) > self.FRESH_VISION_SECONDS:
            return ""
        # URL match — normalize to just scheme+host+path (ignore query
        # churn that doesn't meaningfully change the page).
        def _strip_query(u: str) -> str:
            if not u:
                return ""
            return u.split("?", 1)[0].split("#", 1)[0]
        if tool_url and _strip_query(tool_url) != _strip_query(self._last_vision_url):
            return ""
        try:
            return "[CACHED VISION — bboxes still valid; use vision_index=V_n to click]\n" + resp.as_brain_text()
        except Exception:
            return ""


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


# ── Tool classes — each holds a reference to shared BrowserSessionState ──

@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to open (optional)", nullable=True),
        region=StringSchema("Region code for geo-restricted sites (e.g., 'bd', 'in')", nullable=True),
        proxy=StringSchema("Direct proxy URL (e.g., 'socks5://proxy:1080')", nullable=True),
        intent=StringSchema(
            "Optional hint describing what you want from the vision agent "
            "(e.g. 'check if login is required', 'find search box'). "
            "Only used when VISION_ENABLED=1.",
            nullable=True,
        ),
        tier=StringSchema(
            "Which anti-bot tier to open the session on. "
            "'auto' (default) reads per-domain learnings and picks t1 or t3. "
            "'t1' forces the TS Puppeteer backend. "
            "'t3' forces the in-process patchright (undetected Chromium) "
            "backend — required for Akamai/DataDome/PerimeterX targets.",
            enum=("auto", "t1", "t3"),
            nullable=True,
        ),
        required=[],
    )
)
class BrowserOpenTool(Tool):
    name = "browser_open"
    description = (
        "Open a new browser session. Returns a screenshot and interactive elements. "
        "For geo-restricted sites, pass region='bd' (Bangladesh), 'in' (India), etc. "
        "Pass tier='t3' for hardened anti-bot sites (Akamai/DataDome/PerimeterX); "
        "'auto' reads the learning system."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def _open_session_on_tier(
        self,
        tier_name: str,
        *,
        url: str | None,
        region: str | None,
        proxy: str | None,
    ) -> Any:
        """Open a session on the given tier. Returns the raw data dict on
        success, or a plain string when the tier-open itself fails (rate
        limit, T3 launch exception). String returns bubble up unchanged
        so the caller can surface them to the agent.
        """
        if tier_name == "t3":
            from superbrowser_bridge.antibot import interactive_session as _t3mgr
            try:
                return await _t3mgr.default().open(
                    url,
                    task_id=self.s.task_id,
                    timeout_s=45.0,
                )
            except Exception as exc:
                return (
                    f"[t3_open_failed] Could not open Tier-3 undetected "
                    f"Chromium session: {type(exc).__name__}: {str(exc)[:200]}"
                )
        # Default: T1 via the TS server.
        payload: dict[str, Any] = {}
        if url:
            payload["url"] = url
        if region:
            payload["region"] = region
        if proxy:
            payload["proxy"] = proxy
        if self.s.human_handoff_enabled:
            payload["enableHumanHandoff"] = True
            payload["humanHandoffBudget"] = self.s.human_handoff_budget

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/create",
            json=payload,
            timeout=45.0,
        )
        if r.status_code == 429:
            return (
                "[transient_rate_limit] Browser session service is busy "
                "(HTTP 429 after retries). This is a temporary rate limit, "
                "NOT a permanent outage. Wait ~30 seconds and call "
                "browser_open again. Do not switch to a different strategy."
            )
        r.raise_for_status()
        return r.json()

    async def execute(self, url: str | None = None, region: str | None = None, proxy: str | None = None, intent: str | None = None, tier: str | None = None, **kw: Any) -> Any:
        self.s.init_if_needed()

        # --- Tier selection ---------------------------------------------
        # 'auto' reads per-domain learnings; explicit 't1'/'t3' forces it.
        chosen_tier = (tier or "auto").lower()
        if chosen_tier == "auto":
            try:
                from urllib.parse import urlparse as _urlparse
                from superbrowser_bridge.routing import choose_starting_tier
                host = _urlparse(url or "").hostname or ""
                learned = choose_starting_tier(host) if host else 0
                # Tier ≥ 3 → open directly on t3. Tier 4 isn't interactive;
                # we still open on t3 and let the vision loop decide.
                chosen_tier = "t3" if learned >= 3 else "t1"
            except Exception:
                chosen_tier = "t1"

        # --- Idempotency guard ------------------------------------------
        # Two paths reach this tool with a live session already:
        #   1. The worker's LLM is in an amnesia loop (truncated/stripped
        #      screenshots → can't tell browser_open already ran) and is
        #      firing it again for the same URL.
        #   2. The orchestrator pre-seeded self.s.session_id from a
        #      resumption artifact (orchestrator_tools.py resumption path)
        #      and the worker's LLM ignored the "DO NOT call browser_open"
        #      instruction in the prompt.
        # In both cases creating a second real session is the bug — it
        # overwrites session_id with a throwaway and discards any progress.
        # Return a plain-string message (no image blocks, so truncation
        # can't mangle it) pointing the LLM at the right next tool.
        if self.s.session_id:
            self.s.blocked_browser_open_count += 1
            if self.s.blocked_browser_open_count >= BLOCKED_BROWSER_OPEN_HARD_STOP:
                raise WorkerMustExitError(
                    f"browser_open called {self.s.blocked_browser_open_count} "
                    f"times after the idempotency guard refused it. The LLM "
                    f"is in a tight loop ignoring the guard message. "
                    f"Aborting worker to prevent iteration drain. "
                    f"session_id={self.s.session_id}"
                )
            same_url = (
                not url
                or self.s._normalize_url(url) == self.s._normalize_url(self.s.current_url)
            )
            print(
                f"\n>> browser_open BLOCKED (session already active: "
                f"{self.s.session_id}) — refusal #{self.s.blocked_browser_open_count}"
            )
            if same_url:
                return (
                    f"[SESSION_ALREADY_OPEN session_id={self.s.session_id} "
                    f"url={self.s.current_url}]\n"
                    f"A browser session is already active on this URL. "
                    f"DO NOT call browser_open again — it would discard your "
                    f"current page.\n"
                    f"Use one of these instead:\n"
                    f"  - browser_screenshot(session_id=\"{self.s.session_id}\") "
                    f"to see the current view\n"
                    f"  - browser_get_markdown(session_id=\"{self.s.session_id}\") "
                    f"to read the page text\n"
                    f"  - browser_click / browser_type to interact\n"
                    f"  - browser_navigate(session_id=\"{self.s.session_id}\", "
                    f"url=\"...\") to switch URLs on the same session"
                )
            return (
                f"[WRONG_TOOL session_id={self.s.session_id} current_url={self.s.current_url}]\n"
                f"You asked to open a different URL ({url}) but a session is "
                f"already active. Use browser_navigate on the existing session — "
                f"do NOT call browser_open, which would create a throwaway "
                f"second session and discard your current page.\n"
                f"  browser_navigate(session_id=\"{self.s.session_id}\", url=\"{url}\")"
            )

        self.s.reset_per_session()
        self.s.sessions_opened += 1

        print(f"\n>> browser_open(url={url}, region={region}, tier={chosen_tier}) [session #{self.s.sessions_opened}, screenshots left: {self.s.screenshot_budget}]")

        # Escalate=True when the caller wants T1→T3 auto-recovery. Kept
        # behind a flag so an explicit `tier='t1'` request from the agent
        # is honored without surprise upgrades.
        allow_escalation = (tier or "auto").lower() == "auto"
        data = await self._open_session_on_tier(
            chosen_tier, url=url, region=region, proxy=proxy,
        )
        if isinstance(data, str):
            # `_open_session_on_tier` returns a string on hard failure
            # (transient rate limit, t3 launch failure). Surface it.
            return data

        # --- T1 → T3 auto-escalation -------------------------------------
        # When the Tier-1 Puppeteer path hits a hard anti-bot block
        # (401/403/429/502/503) before any content loads, the Tier-3
        # patchright + stealth stack is the right next hop. Close the
        # doomed T1 session, record the block so choose_starting_tier
        # prefers T3 next time, and re-open on T3 within this tool call
        # — the caller sees one consistent result regardless of which
        # tier actually served it.
        status_code = data.get("statusCode") if isinstance(data, dict) else None
        if (
            allow_escalation
            and chosen_tier == "t1"
            and isinstance(status_code, int)
            and status_code in (401, 403, 429, 502, 503)
        ):
            print(
                f"  [T1→T3 auto-escalation] HTTP {status_code} on T1; "
                f"retrying with patchright (T3)..."
            )
            # Clean up the blocked T1 session.
            t1_sid = (data or {}).get("sessionId", "")
            if t1_sid:
                try:
                    await _request_with_backoff(
                        "DELETE",
                        f"{SUPERBROWSER_URL}/session/{t1_sid}",
                        timeout=10.0,
                    )
                except Exception:
                    pass
            # Record the T1 block so next task on this domain starts on T3.
            try:
                from urllib.parse import urlparse as _up
                from superbrowser_bridge.routing import _record_routing_outcome
                host = (_up(url or "").hostname or "").lower()
                if host:
                    block_class = (
                        "rate_limit" if status_code in (429, 503)
                        else "antibot_403"
                    )
                    _record_routing_outcome(
                        host, approach="browser", success=False,
                        tier=1, block_class=block_class,
                    )
            except Exception:
                pass
            # Re-open on T3.
            chosen_tier = "t3"
            data = await self._open_session_on_tier(
                "t3", url=url, region=region, proxy=proxy,
            )
            if isinstance(data, str):
                return data

        actual_url = data.get("url", url or "")
        self.s.session_id = data.get("sessionId", "")
        self.s.log_activity(f"browser_open({url or 'blank'})", f"session={data.get('sessionId', '?')}")
        self.s.record_url(actual_url)
        self.s.record_checkpoint(actual_url, data.get("title", ""), f"browser_open({url or 'blank'})")
        self.s.record_step("browser_open", url or "blank", f"session={data.get('sessionId', '?')}")
        self.s.consecutive_click_calls = 0

        # If human handoff is enabled, print the view URL to stdout so the
        # user can pre-open it in their browser. The view page polls the
        # /human-input endpoint and will show a banner the instant the
        # agent needs help, so having it open beforehand eliminates the
        # race where the agent blocks for 5 min before the user notices.
        #
        # For t3 sessions, the live viewer is served by the Python-side
        # aiohttp server (default :3101), NOT the TS server (:3100). The
        # browser_open call starts it on demand so the URL is live when
        # the user clicks it.
        if self.s.human_handoff_enabled and self.s.session_id:
            if chosen_tier == "t3":
                try:
                    from superbrowser_bridge.antibot import t3_viewer as _v
                    await _v.ensure_started()
                    view_url = _v.view_url(self.s.session_id)
                except Exception as exc:
                    print(f"  [t3 viewer failed to start: {exc}]")
                    view_url = ""
            else:
                public_host = os.environ.get(
                    "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
                )
                view_url = f"{public_host}/session/{self.s.session_id}/view"
            if view_url:
                print(
                    f"\n>> [HUMAN HANDOFF ENABLED] Open this URL in your browser "
                    f"and keep it open:\n>>   {view_url}\n>> "
                    f"If the agent needs help, you'll see a banner there."
                )

        caption = _format_state(data, self.s)
        caption = f"Session: {data['sessionId']}\n{caption}"

        # Network-layer block detection (4xx/5xx). Fast-fails before the worker
        # wastes iterations on an unresponsive page. 404 is treated as fatal
        # here (wrong URL) but not a network block per se.
        #
        # Special-case Cloudflare: a 403 with `block_class=cloudflare` is
        # usually the Managed Challenge interstitial, not a permanent
        # refusal. Arm the nav-guard (so duplicate navigate without solve
        # is refused) and emit a caption that routes to browser_solve_captcha.
        status_code = data.get("statusCode")
        _block_class = (
            str(data.get("block_class") or data.get("blockClass") or "")
            .lower()
        )
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(
                    status_code, actual_url, block_class=_block_class,
                )
                if _block_class == "cloudflare":
                    self.s.last_nav_cf_blocked_url = self.s._normalize_url(actual_url)
                    self.s.nav_solve_called_since_block = False
                    self.s.record_step(
                        "browser_open", url or "blank",
                        f"CF_INTERSTITIAL status={status_code}",
                    )
                else:
                    self.s.record_step("browser_open", url or "blank", f"NETWORK_BLOCKED status={status_code}")
                return caption
            elif status_code == 404:
                caption += _build_network_block_message(404, actual_url)
                return caption

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{data['sessionId']}', method='auto') to solve it."
            )

        # Show previous activity so agent knows what was already tried
        if self.s.sessions_opened > 1:
            activity = self.s.get_activity_summary()
            if activity:
                caption += activity

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe opened page",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID from browser_open"),
        url=StringSchema("URL to navigate to"),
        intent=StringSchema(
            "Optional hint for the vision agent (e.g. 'verify navigation "
            "succeeded', 'find sign-up button'). Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id", "url"],
    )
)
class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    description = "Navigate to a URL in an open browser session."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, url: str, intent: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_navigate({url})")
        gate = await _feedback_gate("browser_navigate")
        if gate:
            return gate

        # --- Domain-pinning guard -----------------------------------------
        # When pinned_domain is set, only allow navigation to the target
        # domain (+ subdomains) and a small safe-list. Prevents the worker
        # LLM from visiting alternative sites when the target blocks it.
        if self.s.pinned_domain:
            from urllib.parse import urlparse as _urlparse
            # Safe-list = OAuth + CDN only. google.com stays on the list
            # (OAuth flow needs `accounts.google.com`, `accounts.youtube.com`,
            # etc.) but SEARCH paths on it are blocked below — observed
            # 2026-04-19: LLM would pivot to google.com/search whenever
            # the real target was slow, turning every task into a Google
            # scrape that 429'd and poisoned the session.
            _SAFE_DOMAINS = ("google.com", "googleapis.com", "gstatic.com", "google.co")
            try:
                _parsed = _urlparse(url)
                _target_host = (_parsed.hostname or "").lower().replace("www.", "")
                _target_path = _parsed.path or ""
                _target_query = _parsed.query or ""
            except Exception:
                _target_host = ""
                _target_path = ""
                _target_query = ""
            _pinned = self.s.pinned_domain
            _is_pinned = _target_host == _pinned or _target_host.endswith("." + _pinned)
            _is_safe = any(
                _target_host == sd or _target_host.endswith("." + sd)
                for sd in _SAFE_DOMAINS
            )
            # Block Google Search as an escape hatch — `google.com/search`,
            # `google.com/?q=`, `google.com/images`, etc. The LLM must stay
            # on the pinned domain even when it's frustrated.
            _is_google = _target_host == "google.com" or _target_host.endswith(".google.com") or _target_host.endswith(".google.co")
            _looks_like_search = _is_google and (
                _target_path.startswith("/search")
                or _target_path.startswith("/images")
                or _target_path.startswith("/maps")
                or "q=" in _target_query
            )
            if _target_host and (not (_is_pinned or _is_safe) or _looks_like_search):
                reason = "search_escape" if _looks_like_search else "outside_pin"
                self.s.record_step("browser_navigate", url, f"BLOCKED: {reason} (pinned={_pinned})")
                print(f"   [DOMAIN_PINNED] blocked navigation to {_target_host}{_target_path} ({reason}, pinned={_pinned})")
                return (
                    f"[DOMAIN_PINNED] Navigation to {url} is BLOCKED. "
                    f"You MUST stay on {_pinned} (and its subdomains). "
                    f"Do NOT pivot to Google Search or other sites when the "
                    f"target is slow or annoying — fix the problem on "
                    f"{_pinned} itself. If {_pinned} is hard-blocked, call "
                    f"browser_escalate (to Tier 3) or browser_solve_captcha "
                    f"or browser_ask_user, or report failure via "
                    f"done(success=False)."
                )

        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        # CF-interstitial nav guard: if the last navigate to THIS URL was
        # Cloudflare-blocked and nothing has been done to resolve it, a
        # fresh page.goto will just re-trigger the same interstitial and
        # burn budget. Tell the agent to call browser_solve_captcha first.
        _norm_target = self.s._normalize_url(url)
        if (
            self.s.last_nav_cf_blocked_url
            and _norm_target == self.s.last_nav_cf_blocked_url
            and not self.s.nav_solve_called_since_block
        ):
            self.s.record_step(
                "browser_navigate", url,
                "BLOCKED: last navigate to this URL hit CF interstitial; "
                "call browser_solve_captcha first",
            )
            return (
                f"[CF_INTERSTITIAL_PENDING] The last navigate to {url} "
                f"landed on a Cloudflare Managed Challenge "
                f"('Performing security verification'). Re-navigating "
                f"before solving will just re-trigger the same challenge. "
                f"Call browser_solve_captcha(session_id='{session_id}', "
                f"method='auto') to wait for the interstitial to auto-"
                f"clear, THEN retry this navigate. If the solver also "
                f"fails, call browser_ask_user to hand off to a human."
            )

        # Detect regression before navigating
        regression = self.s.is_regression(url)
        if regression:
            self.s.regression_count += 1

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
            json={"url": url},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", url)
        self.s.log_activity(f"navigate({url})", f"title={data.get('title', '?')}")
        self.s.record_url(actual_url)
        # Drop the prior epoch — it belongs to the old page. The next
        # click will fall back to `_last_vision_response` (blank or
        # post-nav prefetch) via `vision_for_target_resolution`, and
        # the very next `browser_screenshot` re-freezes the epoch.
        self.s._vision_epoch_response = None

        # Set/clear the CF nav-guard based on what came back. `block_class`
        # is populated by interactive_session.py after the challenge wait
        # loop fails to clear. A navigate to any OTHER URL clears the
        # guard regardless — progress elsewhere means the stuck state is
        # gone.
        _block_class = (
            str(data.get("block_class") or data.get("blockClass") or "")
            .lower()
        )
        if _block_class == "cloudflare":
            self.s.last_nav_cf_blocked_url = self.s._normalize_url(actual_url)
            self.s.nav_solve_called_since_block = False
        elif _norm_target != self.s.last_nav_cf_blocked_url:
            # Navigated to a different URL that isn't CF-blocked — guard off.
            self.s.last_nav_cf_blocked_url = ""
            self.s.nav_solve_called_since_block = False

        caption = _format_state(data, self.s)

        # Network-layer block detection — same logic as browser_open. Exit
        # early so the worker doesn't try to interact with a 403/429 shell.
        # CF interstitial gets the solve-captcha routing caption and the
        # nav-guard block set above.
        status_code = data.get("statusCode")
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(
                    status_code, actual_url, block_class=_block_class,
                )
                if _block_class == "cloudflare":
                    self.s.record_step(
                        "browser_navigate", url,
                        f"CF_INTERSTITIAL status={status_code}",
                    )
                else:
                    self.s.record_step(
                        "browser_navigate", url,
                        f"NETWORK_BLOCKED status={status_code}",
                    )
                return caption
            elif status_code == 404:
                caption += _build_network_block_message(404, actual_url)
                self.s.record_step("browser_navigate", url, f"HTTP 404 at {actual_url}")
                return caption

        self.s.record_step("browser_navigate", url, f"title={data.get('title', '?')}")
        # Prefetch vision so the LLM's next browser_screenshot finds the
        # bboxes already cached.
        _schedule_vision_prefetch(self.s, session_id)

        if regression:
            caption += "\n[WARNING: You already visited this URL. Fix your approach on the CURRENT page instead of going backward. Do NOT restart from the beginning.]"

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{session_id}', method='auto') to solve it."
            )

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "verify navigation succeeded",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Optional hint for the vision agent. Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot. COSTS MONEY. Use browser_get_markdown or browser_eval to verify instead."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, intent: str | None = None, **kw: Any) -> Any:
        # Peek current page content so dedup keys on (url, content_hash)
        # — a reload or DOM change produces a different hash and unblocks.
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass

        allowed, reason = self.s.should_allow_screenshot(self.s.current_url, peek_hash)
        if not allowed:
            self.s.log_activity("screenshot(BLOCKED)", reason[:60])
            return reason

        self.s.screenshot_budget -= 1
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            # bounds=true returns selectorEntries (with x/y/width/height) +
            # devicePixelRatio so we can draw bbox overlays before the
            # screenshot goes to the vision LLM.
            params={"vision": "true", "bounds": "true"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.mark_screenshot_taken(
                actual_url,
                self.s.hash_page_content(data.get("elements", "")),
            )
        self.s.log_activity(f"screenshot({actual_url[:50] if actual_url else '?'})")
        self.s.record_step("browser_screenshot", "", f"url={actual_url[:60] if actual_url else '?'}")
        caption = _format_state(data, self.s)
        caption += f"\n[Screenshots remaining: {self.s.screenshot_budget}]"
        if data.get("screenshot"):
            entries = data.get("selectorEntries") or []
            dpr = float(data.get("devicePixelRatio") or 1.0)
            # Rename tagName → tag for the overlay (both naming schemes work
            # but tag is the overlay's canonical key).
            overlay_elements = [
                {
                    "index": e.get("index"),
                    "tag": e.get("tagName") or e.get("tag"),
                    "role": e.get("role") or (e.get("attributes") or {}).get("role"),
                    "bounds": e.get("bounds"),
                }
                for e in entries
                if e.get("bounds") and e.get("index") is not None
            ]
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe page",
                url=actual_url,
                elements=data.get("elements"),
                elements_with_bounds=overlay_elements,
                device_pixel_ratio=dpr,
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        button=StringSchema("Mouse button: left, right, middle", nullable=True),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an interactive element by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_click([{index}])")
        gate = await _feedback_gate("browser_click")
        if gate:
            return gate
        # Cross-index flail guard. If the last two clicks timed out,
        # force a re-screenshot before dispatching another HTTP click —
        # the backend is hung (blocker, loader, nav in flight) and
        # walking [N±1] just wastes the iteration budget.
        if self.s.consecutive_click_timeouts >= self.s.MAX_CONSECUTIVE_CLICK_TIMEOUTS:
            alts = _vision_alternatives_hint(self.s, limit=3)
            self.s.log_activity(
                f"click([{index}])(LOOP_BLOCKED)",
                f"timeouts={self.s.consecutive_click_timeouts}",
            )
            return (
                f"[click_loop_detected] {self.s.consecutive_click_timeouts} "
                f"consecutive click timeouts. The page is likely blocked "
                f"(loader, modal, or a pending navigation). Call "
                f"browser_screenshot to refresh vision before any further "
                f"click."
                + (f"\n{alts}" if alts else "")
            )
        target_key = f"click[{index}]"
        dead = self.s.check_dead_click(target_key)
        if dead:
            self.s.log_activity(f"click([{index}])(DEAD_CLICK_BLOCKED)", "")
            return dead
        self.s.register_click_attempt(target_key)
        self.s.consecutive_click_calls += 1
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button
        # Send the fingerprint the LLM was targeting. If the DOM shifted,
        # the TS side returns 409 + stale_index with a suggested new index.
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        elif self.s.element_fingerprints:
            # The cache has entries, just not for this index — the brain
            # is addressing an index that wasn't in the last state
            # response. Almost always means stale. Surface fast instead
            # of letting the TS click fail obscurely.
            await _fetch_elements(session_id, self.s)
            if index not in self.s.element_fingerprints:
                return (
                    f"[click_failed:unknown_index] [{index}] is not in "
                    f"the current selectorMap (fingerprints={len(self.s.element_fingerprints)} "
                    f"indices). Re-read the elements list and pick a "
                    f"valid index, or use browser_click_at(V_n) with a "
                    f"vision bbox."
                )
            cached_fp = self.s.element_fingerprints.get(index)
            if cached_fp:
                payload["expected_fingerprint"] = cached_fp

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json=payload,
                timeout=30.0,
            )
            # 409 = stale-index guard fired. Surface the suggested
            # index (if any) so the LLM retargets instead of blindly
            # retrying or falling back to click_at coords.
            if r.status_code == 409:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                stale_msg = info.get("error", "Stale index")
                suggested = info.get("suggested_index")
                current = info.get("current_element", "")
                hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
                result = f"[stale_index] {stale_msg} Current [{index}] is {current}.{hint}"
                self.s.log_activity(f"click([{index}])(STALE)", f"suggested={suggested}")
                await _fetch_elements(session_id, self.s)
                return result
            # 400 = structured TS-side failure (element not found,
            # not visible, disabled, etc.). Parse and return an
            # actionable message to the LLM.
            if r.status_code == 400:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                reason = info.get("reason", "unknown")
                err = info.get("error", f"click [{index}] failed")
                alternatives = info.get("alternatives") or []
                await _fetch_elements(session_id, self.s)
                self.s.log_activity(f"click([{index}])({reason})", err[:60])
                alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
                fresh_hint = "\nElements have been re-read above — pick a current [index]."
                return (
                    f"[click_failed:{reason}] {err}"
                    + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                    + fresh_hint
                )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            # Opaque 4xx/5xx (not 400/409). Usually network-layer.
            self.s.log_activity(f"click([{index}])(HTTP{e.response.status_code})", str(e)[:60])
            return (
                f"[click_failed:http_{e.response.status_code}] {e.response.text[:200] if e.response.text else str(e)[:200]}"
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout) as e:
            # Click dispatched but the backend never responded — almost
            # always means the page is blocked (a pending navigation, a
            # loader still running, or an overlay intercepting events).
            # Count it so the flail guard above trips on the next call.
            self.s.consecutive_click_timeouts += 1
            self.s.log_activity(
                f"click([{index}])(TIMEOUT)",
                f"count={self.s.consecutive_click_timeouts}",
            )
            alts = _vision_alternatives_hint(
                self.s, exclude_index=None, limit=3,
            )
            return (
                f"[click_failed:timeout] The backend didn't respond to "
                f"click([{index}]) within the HTTP timeout. The page is "
                f"likely waiting on navigation or blocked by a loader. "
                f"Call browser_screenshot to re-vision before retrying."
                + (f"\n{alts}" if alts else "")
            )
        except Exception as e:
            # True transport error (connection refused, etc.). Server down.
            self.s.log_activity(f"click([{index}])(TRANSPORT)", str(e)[:60])
            return f"[click_failed:transport] {str(e)[:200]} — browser service unreachable. Retry in a few seconds."

        # Successful HTTP response — clear the timeout counter so the
        # flail guard doesn't trip on a future unrelated hiccup.
        self.s.consecutive_click_timeouts = 0
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        # Snap telemetry (P3.12).
        snap = data.get("snap") if isinstance(data, dict) else None
        if isinstance(snap, dict) and snap.get("snapped") is False:
            self.s.snap_miss_count += 1
        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step("browser_click", f"index={index}", f"url={actual_url[:60] if actual_url else '?'}")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked [{index}]"),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this element). When set, the server snaps to "
                "the interactive element inside that bbox — far more "
                "accurate than clicking a guessed (x,y)."
            ),
            nullable=True,
        ),
        x=NumberSchema(description="X coordinate (CSS pixel). Ignored when vision_index is set.", nullable=True),
        y=NumberSchema(description="Y coordinate (CSS pixel). Ignored when vision_index is set.", nullable=True),
        required=["session_id"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = (
        "Click using a vision bbox (vision_index=V_n) or raw (x,y) "
        "coordinates. Prefer vision_index whenever the vision agent "
        "labelled the target — the server snaps to the actual interactive "
        "element inside the bbox, eliminating off-by-pixel misses."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        **kw: Any,
    ) -> Any:
        self.s.click_at_count += 1
        self.s.consecutive_click_calls += 1
        if self.s.click_at_count > self.s.MAX_CLICK_AT:
            return (
                f"[BLOCKED] browser_click_at used "
                f"{self.s.click_at_count} times in this session. The "
                f"task is looping on clicks — call browser_screenshot "
                f"to re-observe, then try browser_click_selector with "
                f"a stable CSS hook, or browser_rewind_to_checkpoint "
                f"if the page is stuck. Do NOT attempt "
                f"browser_run_script to click — JS clicks are "
                f"isTrusted=false and bot-detected."
            )

        # Build the target key BEFORE resolving the bbox, so the guard
        # fires on intent (vision_index=V3) not on resolved coords (which
        # could shift slightly between calls due to anti-aliasing).
        if vision_index is not None:
            target_key = f"click_at(V{int(vision_index)})"
        elif x is not None and y is not None:
            # Round to a 5px grid — micro-jitter shouldn't escape the guard.
            target_key = f"click_at({round(float(x)/5)*5},{round(float(y)/5)*5})"
        else:
            target_key = "click_at(?)"
        dead = self.s.check_dead_click(target_key)
        if dead:
            self.s.log_activity(f"click_at{target_key}(DEAD_CLICK_BLOCKED)", "")
            return dead
        self.s.register_click_attempt(target_key)

        payload: dict[str, Any]
        log_target: str
        if vision_index is not None:
            # Prefer the frozen epoch (what the brain SAW on its last
            # screenshot), fall back to the live response only when no
            # epoch is set yet (pre-first-screenshot path / tests).
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[click_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Re-fetch state to "
                    "trigger a fresh vision pass, or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[click_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
                )
            # Freshness gate — refuse to click when the last vision pass
            # flagged the screenshot as stale or uncertain. The planner
            # should re-screenshot before committing a click on a frame
            # the model itself said it couldn't trust.
            freshness = getattr(resp, "screenshot_freshness", "fresh")
            if freshness != "fresh":
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                return (
                    f"[click_at_failed:stale_vision freshness={freshness}] "
                    "Vision flagged the last screenshot as not fresh "
                    "(URL/page mismatch or loading overlay). Call "
                    "browser_screenshot to refresh vision before clicking."
                    + (f"\n{alts}" if alts else "")
                )
            # Blocker gate — if the scene has an active blocker layer
            # (cookie banner, modal, consent dialog) and this bbox lives
            # in a different layer, refuse. The planner must dismiss
            # the blocker before acting on content beneath it.
            scene = getattr(resp, "scene", None)
            active_blocker = (
                getattr(scene, "active_blocker_layer_id", None)
                if scene is not None else None
            )
            if active_blocker:
                bbox_layer = getattr(bbox, "layer_id", None)
                if bbox_layer and bbox_layer != active_blocker:
                    # Find the dismiss hint from the blocker layer so
                    # the brain has a concrete target to click first.
                    dismiss_hint = ""
                    try:
                        for layer in (getattr(scene, "layers", []) or []):
                            if getattr(layer, "id", None) == active_blocker:
                                dismiss_hint = (
                                    getattr(layer, "dismiss_hint", "") or ""
                                )
                                break
                    except Exception:
                        dismiss_hint = ""
                    hint = f" Dismiss '{dismiss_hint}' first." if dismiss_hint else ""
                    return (
                        f"[click_at_failed:blocker_active layer={active_blocker}] "
                        f"A blocker layer ({active_blocker}) is on top of "
                        f"content, and V{vision_index} sits in a different "
                        f"layer ({bbox_layer}).{hint} Then re-screenshot."
                    )
            # Confidence gate — a low-confidence bbox is Gemini's way of
            # saying "I'm not sure this is really here". Clicking it
            # lands on the wrong target more often than not. Threshold
            # is tuned via VISION_MIN_CLICK_CONFIDENCE (default 0.45).
            try:
                min_conf = float(
                    os.environ.get("VISION_MIN_CLICK_CONFIDENCE") or "0.45"
                )
            except ValueError:
                min_conf = 0.45
            if getattr(bbox, "confidence", 0.5) < min_conf:
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                return (
                    f"[click_at_failed:low_confidence V{vision_index}] "
                    f"bbox confidence={bbox.confidence:.2f} < "
                    f"{min_conf:.2f}. Call browser_screenshot to re-run "
                    "vision, then retry with a higher-confidence target."
                    + (f"\n{alts}" if alts else "")
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[click_at_failed:no_image_dims] Last vision response "
                    "has no source image dimensions; cannot denormalize "
                    "box_2d. Re-fetch state."
                )
            # CDP/JS expects CSS pixels; on retina/HiDPI viewports the
            # screenshot is physical-pixel-sized so we divide by DPR.
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            payload = {"bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}
            # Carry the vision label into the click payload so the T3
            # backend can run a post-snap semantic match check. Empty
            # label → the check is skipped on the backend, which is
            # fine for raw-coord clicks further below.
            bbox_label = (getattr(bbox, "label", "") or "").strip()
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                payload["label"] = bbox_label[:120]
            log_target = f"V{vision_index}({x0},{y0}→{x1},{y1})"
            print(f"\n>> browser_click_at(V{vision_index}) → bbox=({x0},{y0},{x1},{y1})")
        else:
            if x is None or y is None:
                return "[click_at_failed:bad_args] Provide either vision_index or both x and y."
            payload = {"x": float(x), "y": float(y)}
            log_target = f"({x},{y})"
            print(f"\n>> browser_click_at({x}, {y})")

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/click",
            json=payload,
            timeout=30.0,
        )
        # 409 = reward-band reject. Historical data says this zone
        # doesn't respond to clicks on this host; surface the hint
        # so the LLM re-reads elements instead of trying another
        # nearby coord.
        if r.status_code == 409:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = info.get("error") or "click_at rejected: low-reward zone"
            self.s.log_activity(f"click_at{log_target}(BAND_REJECT)", f"band={info.get('band')}")
            return f"[low_reward_band] {err}"
        r.raise_for_status()
        data = r.json()
        # Element-mismatch guard (P1.4). The T3 backend compared the
        # element at the click target to the vision label we sent and
        # decided they don't match. Don't dispatch — return an
        # observation so the brain can re-screenshot and pick again.
        if isinstance(data, dict) and data.get("error") == "element_mismatch":
            found = data.get("found", {}) or {}
            alts = _vision_alternatives_hint(
                self.s, exclude_index=vision_index, limit=3,
            )
            self.s.log_activity(
                f"click_at{log_target}(ELEM_MISMATCH)",
                f"found={found.get('tag','?')}",
            )
            return (
                f"[click_at_failed:element_mismatch] Vision said this "
                f"target was '{data.get('expected_label','')}' but the "
                f"element at ({data.get('coords', {}).get('x','?')},"
                f"{data.get('coords', {}).get('y','?')}) is "
                f"<{(found.get('tag') or '?').lower()} "
                f"role='{found.get('role','')}'> text='"
                f"{(found.get('text') or '')[:80]}'. Call "
                f"browser_screenshot to refresh vision."
                + (f"\n{alts}" if alts else "")
            )
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        snap = data.get("snap")  # {x, y, snapped: bool, target?: str}
        if snap:
            snap_note = (
                f" snapped→({snap.get('x')},{snap.get('y')}) {snap.get('target','')}".strip()
                if snap.get("snapped") else " (raw bbox center; no interactive element matched)"
            )
        else:
            snap_note = ""

        # Post-click verification — look up the postcondition the planner
        # attached to this target (by vision_index or by coord match)
        # and run it via verify_action. Runs only for t3 sessions and
        # when VERIFY_AFTER_CLICK is enabled (default on). A miss is
        # reported in the caption so the brain can decide to retry with
        # a different strategy or call browser_plan_next_steps.
        verify_note = ""
        if session_id.startswith("t3-") and \
                os.environ.get("VERIFY_AFTER_CLICK", "1") != "0":
            postcond = self._lookup_postcondition(vision_index, x, y)
            if postcond is not None:
                try:
                    from superbrowser_bridge.antibot import interactive_session as _t3mgr
                    from superbrowser_bridge.verify_action import verify_after, PreState
                    mgr = _t3mgr.default()
                    vr = await verify_after(
                        mgr, session_id, postcond,
                        pre_state=PreState(url=self.s.current_url or ""),
                        state=self.s,
                    )
                    if not vr.verified:
                        # Default postcondition (dom_mutated) failing means
                        # the click went out but NOTHING changed — page,
                        # DOM, URL all identical. Before bothering the
                        # brain, ESCALATE through the click ladder —
                        # many pages reject "primary" bezier clicks but
                        # respond to a direct `el.click()` (JS) dispatch
                        # or to keyboard Enter. Silent failure most
                        # often means the site's click handler has a
                        # guard our primary click tripped (0-dwell, CSS
                        # pointer-events masking, framework re-render).
                        is_silent_default = (
                            postcond.get("kind") == "dom_mutated"
                            and not getattr(
                                self.s._last_action_queue, "actions", None,
                            )
                        )
                        escalated = False
                        if is_silent_default and \
                                os.environ.get("CLICK_LADDER_AUTO", "1") != "0" and \
                                payload.get("bbox"):
                            for alt_strategy in ("js", "keyboard"):
                                try:
                                    from superbrowser_bridge.antibot import (
                                        interactive_session as _t3mgr2,
                                    )
                                    mgr2 = _t3mgr2.default()
                                    alt_bbox = payload.get("bbox")
                                    alt_x = (alt_bbox["x0"] + alt_bbox["x1"]) / 2
                                    alt_y = (alt_bbox["y0"] + alt_bbox["y1"]) / 2
                                    alt_resp = await mgr2.click_at(
                                        session_id, alt_x, alt_y,
                                        bbox=alt_bbox,
                                        strategy=alt_strategy,
                                    )
                                    if not isinstance(alt_resp, dict) or \
                                            not alt_resp.get("success"):
                                        continue
                                    # Re-verify after the escalated strategy.
                                    vr2 = await verify_after(
                                        mgr, session_id, postcond,
                                        pre_state=PreState(
                                            url=self.s.current_url or "",
                                        ),
                                        state=self.s,
                                    )
                                    if vr2.verified:
                                        escalated = True
                                        verify_note = (
                                            f"\n[click_escalated strategy={alt_strategy}] "
                                            f"Primary click was silent; "
                                            f"{alt_strategy} strategy landed the "
                                            f"action."
                                        )
                                        break
                                except Exception as exc:
                                    print(
                                        f"  [click ladder ({alt_strategy}) "
                                        f"failed: {exc}]"
                                    )
                                    continue
                        if not escalated:
                            if is_silent_default:
                                verify_note = (
                                    f"\n[click_silent reason={vr.reason}] "
                                    f"Primary + escalated (js/keyboard) "
                                    f"clicks all landed no DOM change. "
                                    f"Target likely non-interactive, "
                                    f"covered by an overlay, or waiting "
                                    f"on an async load. Call "
                                    f"browser_screenshot to re-vision, "
                                    f"dismiss any active blocker, or try "
                                    f"a different target."
                                )
                            else:
                                verify_note = (
                                    f"\n[VERIFY_MISS kind={vr.kind} reason={vr.reason}] "
                                    f"The click dispatched but the expected effect "
                                    f"({postcond.get('kind')}) didn't land. Consider "
                                    f"browser_plan_next_steps to re-sequence, or try "
                                    f"a different target."
                                )
                    elif os.environ.get("VERIFY_DEBUG") == "1":
                        verify_note = f"\n[verify_ok kind={vr.kind}]"
                except Exception as exc:
                    print(f"  [verify_action: skipped — {exc}]")

        self.s.record_step(
            "browser_click_at",
            log_target,
            f"url={actual_url[:60] if actual_url else '?'}{snap_note}",
        )
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked {log_target}{snap_note}") + verify_note,
        )

    def _lookup_postcondition(
        self,
        vision_index: int | None,
        x: float | None,
        y: float | None,
    ) -> dict | None:
        """Match the current click against the top planned action and return
        its postcondition, or fall through to a weakest-possible
        default that only catches "click dispatched but page didn't
        change at all" (the canonical silent-miss signal).

        A planner match is: the click's vision_index equals the top
        action's target_vision_index, OR the click's (x, y) falls
        inside the top action's target bbox (± 10 px slack).

        The default (dom_mutated) runs when no planner postcondition
        applies. Set VERIFY_DEFAULT=0 to disable and preserve the old
        "no postcondition, no verification" behaviour.
        """
        queue = self.s._last_action_queue
        if queue is not None and getattr(queue, "actions", None):
            top = queue.actions[0]
            # vision_index match (preferred)
            if vision_index is not None and top.target_vision_index is not None:
                if int(vision_index) == int(top.target_vision_index):
                    return top.postcondition.to_dict()
            # coord match (fallback)
            if x is not None and y is not None and top.target_bbox_pixels:
                x0, y0, x1, y1 = top.target_bbox_pixels
                if (x0 - 10) <= float(x) <= (x1 + 10) and \
                        (y0 - 10) <= float(y) <= (y1 + 10):
                    return top.postcondition.to_dict()
        # Default: "did anything change?" — dom_mutated catches the
        # "click silently missed" case even when the planner didn't
        # attach an explicit postcondition.
        if os.environ.get("VERIFY_DEFAULT", "1") != "0":
            return {"kind": "dom_mutated"}
        return None


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this input). Preferred over (x, y) whenever "
                "the vision agent has pointed at the field."
            ),
            nullable=True,
        ),
        x=NumberSchema(
            description="X coordinate (CSS pixel). Ignored when vision_index is set.",
            nullable=True,
        ),
        y=NumberSchema(
            description="Y coordinate (CSS pixel). Ignored when vision_index is set.",
            nullable=True,
        ),
        text=StringSchema("Text to type into the field at that point."),
        clear=BooleanSchema(
            description=(
                "Clear the field's existing value before typing (default: true). "
                "Uses React/Vue-aware clear so controlled components replace "
                "properly instead of appending."
            ),
            default=True,
        ),
        required=["session_id", "text"],
    )
)
class BrowserTypeAtTool(Tool):
    """Type at a vision bbox (V_n) or (x, y) coordinate. The bbox analogue
    of `browser_type(index, text)`.

    Checks the field's current value before typing — three outcomes the
    LLM sees in the return:
      - `skip_match`: field already contains the target text; no change.
      - `cleared_and_typed`: field had different content, cleared + typed.
      - `typed_into_empty`: field was empty, typed directly.

    Prefer this over `browser_click_at(V_n)` + `browser_keys([...])`,
    which appends at the cursor and turns `old|` + typing `new` into
    `oldnew` instead of `new`.
    """

    name = "browser_type_at"
    description = (
        "Type text into the input at a vision bbox (vision_index=V_n) or "
        "(x, y) coords. Probes the field's current value first and clears "
        "it (React-safe) before typing. Replaces click_at + keys for "
        "bbox-targeted typing — no more concatenation bugs."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        text: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        clear: bool = True,
        **kw: Any,
    ) -> Any:
        if text is None:
            text = ""

        # Resolve target point: vision_index first, then (x, y).
        target_x: float
        target_y: float
        label: str
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[type_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Take a screenshot "
                    "first, or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[type_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[type_at_failed:no_image_dims] Last vision response "
                    "has no source image dimensions; cannot denormalize "
                    "box_2d. Take a fresh screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            target_x = (x0 + x1) / 2
            target_y = (y0 + y1) / 2
            label = f"V{vision_index}"
            print(f"\n>> browser_type_at(V{vision_index}, text={text[:30]!r})")
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
            print(f"\n>> browser_type_at(({x},{y}), text={text[:30]!r})")
        else:
            return "[type_at_failed:bad_args] Provide either vision_index or both x and y."

        # Route through /evaluate (works on both t1 and t3) rather than
        # through a dedicated /type-at endpoint (t3-only). Mechanism is
        # identical to browser_fix_text_at: atomic probe → native-setter
        # write → dispatched input/change events → confirm-read.
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": atomic_js},
            timeout=30.0,
        )
        ev.raise_for_status()
        payload_body = ev.json()
        result = (
            payload_body.get("result") if isinstance(payload_body, dict) else None
        ) or {}
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) else "bad_shape"
            return f"[type_at_failed:{reason}] at {label}. detail={result}"

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))

        if not changed:
            caption = (
                f"Field at {label} already contained {text!r} — no typing "
                f"needed. Proceed to next action."
            )
        elif before:
            caption = (
                f'Typed "{text}" at {label} (replaced existing '
                f'{before!r}).'
            )
        else:
            caption = f'Typed "{text}" at {label}.'

        self.s.record_step(
            "browser_type_at",
            f"{label}, text={text[:30]!r}",
            "skip_match" if not changed else ("cleared_and_typed" if before else "typed_into_empty"),
        )
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
        }
        # Post-type semantic verification. Returns a caption suffix and
        # may have already corrected the field in place.
        if changed:
            from .type_verify import verify_and_correct
            field_meta = {
                "label": str(result.get("label", "") or ""),
                "name": str(result.get("name", "") or ""),
                "autocomplete": str(result.get("autocomplete", "") or ""),
                "input_type": str(result.get("input_type", "") or ""),
            }
            outcome = await verify_and_correct(
                self.s, session_id,
                target_x=target_x, target_y=target_y,
                typed_text=text, label=label,
                page_url=self.s.current_url,
                field_meta=field_meta,
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                synthetic_data["after"] = outcome.after or outcome.corrected_to
                synthetic_data["auto_corrected"] = True
                synthetic_data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(synthetic_data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index for the input to correct. "
                "Preferred over (x, y) when vision labelled the field."
            ),
            nullable=True,
        ),
        x=NumberSchema(description="X coord; used only when vision_index absent.", nullable=True),
        y=NumberSchema(description="Y coord; used only when vision_index absent.", nullable=True),
        text=StringSchema(
            "The EXACT final text the field should contain after the fix. "
            "This is the target state, not a diff or an instruction — give "
            "the corrected spelling / value verbatim."
        ),
        required=["session_id", "text"],
    )
)
class BrowserFixTextAtTool(Tool):
    """Set a text field to an exact target value in one atomic step.

    Human-like correction pathway: when you've noticed a typo or stale
    content ('dahka', 'old search', leftover default), call this with the
    CORRECT final text. The tool reads the current value, computes the
    minimal diff for logging, then writes the target with the React/Vue
    safe native-setter + input/change events — no intermediate empty
    state where a race could concatenate.

    Prefer this over click_at → clear → type_at when fixing a typo:
    surgical, single-call, deterministic.
    """

    name = "browser_fix_text_at"
    description = (
        "Atomically set an input / textarea / contenteditable to a target "
        "text value. Reads the current content, reports the diff, writes "
        "the correction in one step. Use this to fix typos or replace "
        "stale field values without multi-step click + clear + retype."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        text: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        **kw: Any,
    ) -> Any:
        if text is None:
            text = ""

        # Resolve target point.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[fix_text_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Take a screenshot first "
                    "or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[fix_text_at_failed:bad_vision_index] V{vision_index} "
                    f"out of range (only {len(resp.bboxes)} bboxes)."
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return "[fix_text_at_failed:no_image_dims] take a fresh screenshot."
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            target_x = (x0 + x1) / 2
            target_y = (y0 + y1) / 2
            label = f"V{vision_index}"
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
        else:
            return "[fix_text_at_failed:bad_args] Provide vision_index or (x, y)."

        print(f"\n>> browser_fix_text_at({label}, target={text[:40]!r})")

        # Run the whole probe-write-verify cycle inside ONE /evaluate
        # call. /evaluate works on both t1 (TS server) and t3 (patchright
        # intercept), whereas a dedicated /fix-text-at endpoint only
        # exists on t3. Doing the full op in a single evaluate is also
        # race-free: elementFromPoint → native setter → confirm-read all
        # happen within one synchronous JS tick.
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": atomic_js},
            timeout=20.0,
        )
        ev.raise_for_status()
        payload = ev.json()
        result = (
            payload.get("result") if isinstance(payload, dict) else None
        ) or {}
        if not isinstance(result, dict):
            return f"[fix_text_at_failed] unexpected evaluate shape: {type(result).__name__}"

        if not result.get("ok"):
            return (
                f"[fix_text_at_failed:{result.get('reason','unknown')}] at "
                f"{label}. detail={result}"
            )

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        diff = _diff_text(before, after) if changed else "no change"

        if not changed:
            caption = (
                f"Field at {label} already contained {text!r} — no change "
                f"needed. Proceed."
            )
        else:
            caption = (
                f"Fixed {label}: {before!r} → {after!r}\n"
                f"Edit: {diff}"
            )

        self.s.record_step(
            "browser_fix_text_at",
            f"{label}, target={text[:30]!r}",
            diff,
        )
        # Wrap result in the same shape build_text_only expects.
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
            "diff": diff,
        }
        if changed:
            from .type_verify import verify_and_correct
            field_meta = {
                "label": str(result.get("label", "") or ""),
                "name": str(result.get("name", "") or ""),
                "autocomplete": str(result.get("autocomplete", "") or ""),
                "input_type": str(result.get("input_type", "") or ""),
            }
            outcome = await verify_and_correct(
                self.s, session_id,
                target_x=target_x, target_y=target_y,
                typed_text=text, label=label,
                page_url=self.s.current_url,
                field_meta=field_meta,
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                synthetic_data["after"] = outcome.after or outcome.corrected_to
                synthetic_data["auto_corrected"] = True
                synthetic_data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(synthetic_data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        text=StringSchema("Text to type"),
        clear=BooleanSchema(description="Clear field first (default: true)", default=True),
        required=["session_id", "index", "text"],
    )
)
class BrowserTypeTool(Tool):
    name = "browser_type"
    description = "Type text into an input field by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        print(f'\n>> browser_type([{index}], "{text}")')
        gate = await _feedback_gate("browser_type")
        if gate:
            return gate

        # --- Dead-type guard --------------------------------------------
        # The LLM's most destructive misread: type "khulna" → autocomplete
        # dropdown appears → LLM doesn't notice → retypes "khulna,
        # Bangladesh" → field now reads "khulnakhulna, Bangladesh". Catch
        # the second identical-ish type and force the LLM to inspect the
        # dropdown before retyping.
        now_ts = time.time()
        if (
            index == self.s.last_type_index
            and self.s.last_type_text
            and (now_ts - self.s.last_type_at) < 12.0
        ):
            last_lower = self.s.last_type_text.lower()
            cur_lower = text.lower()
            # Consider it a dead-type if: the new text starts with the old
            # text, OR the new text is a superset of the old (contains it),
            # OR it's exactly the same.
            duplicative = (
                cur_lower == last_lower
                or cur_lower.startswith(last_lower)
                or last_lower in cur_lower
            )
            if duplicative:
                self.s.record_step(
                    "browser_type",
                    f"index={index}, text={text[:30]!r}",
                    "DEAD_TYPE: refused (autocomplete likely)",
                )
                return (
                    f"[DEAD_TYPE_REJECTED] Refused to re-type into [{index}]. "
                    f"You already typed {self.s.last_type_text!r} into this "
                    f"field seconds ago. Typing again WILL concatenate "
                    f"(producing garbage like \"{self.s.last_type_text}{text}\"). "
                    f"An autocomplete dropdown probably appeared — take a "
                    f"browser_screenshot, then browser_click the right "
                    f"suggestion (or browser_keys ArrowDown+Enter). Only "
                    f"retype if you pass clear=true AND the field is empty."
                )

        self.s.consecutive_click_calls += 1  # type is also step-by-step
        payload: dict[str, Any] = {"index": index, "text": text, "clear": clear}
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/type",
            json=payload,
            timeout=30.0,
        )
        if r.status_code == 409:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            suggested = info.get("suggested_index")
            current = info.get("current_element", "")
            hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
            await _fetch_elements(session_id, self.s)
            return f"[stale_index] Element [{index}] is now {current}.{hint}"
        # Same structured-400 handling as BrowserClickTool — avoid
        # surfacing raw 'Client error 400' which empties Gemini's
        # next turn.
        if r.status_code == 400:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            reason = info.get("reason", "unknown")
            err = info.get("error", f"type [{index}] failed")
            alternatives = info.get("alternatives") or []
            await _fetch_elements(session_id, self.s)
            self.s.log_activity(f"type([{index}])({reason})", err[:60])
            alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
            return (
                f"[type_failed:{reason}] {err}"
                + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                + "\nElements have been re-read above — pick a current [index]."
            )
        r.raise_for_status()
        data = r.json()

        # Record last-type state so the dead-type guard fires next time.
        self.s.last_type_index = index
        self.s.last_type_text = text
        self.s.last_type_at = time.time()

        # --- Post-type autocomplete dropdown scan -----------------------
        # Probe the page for newly-appeared autocomplete suggestions. If
        # we find any, surface them inline so the LLM picks one instead
        # of re-typing the full phrase.
        suggestions: list[dict] = []
        try:
            scan_js = """
            (() => {
              const seen = new Set();
              const out = [];
              const selectors = [
                '[role="listbox"] [role="option"]',
                '[role="combobox"] + * li',
                '.autocomplete-suggestions li, .autocomplete li',
                'ul.suggestions li, .suggestions li',
                '.MuiAutocomplete-listbox li',
                '[aria-live] li',
                '.dropdown-menu.show li, .dropdown-menu[style*="display: block"] li',
                '.ui-autocomplete li',
                '[class*="autocomplete"][class*="option"]',
                '[class*="suggestion"] li, [class*="suggestions"] li',
              ];
              for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                  const r = el.getBoundingClientRect();
                  if (r.width < 30 || r.height < 10) return;
                  if (r.top > window.innerHeight * 1.5) return;
                  const txt = (el.innerText || el.textContent || '').trim();
                  if (!txt || txt.length > 120 || seen.has(txt)) return;
                  seen.add(txt);
                  out.push({
                    text: txt,
                    x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2),
                  });
                });
              }
              return out.slice(0, 8);
            })();
            """
            sr = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": scan_js},
                timeout=5.0,
            )
            if sr.status_code == 200:
                body = sr.json()
                got = body.get("result") if isinstance(body, dict) else None
                if isinstance(got, list):
                    suggestions = [s for s in got if isinstance(s, dict) and s.get("text")]
        except Exception as exc:
            print(f"  [dropdown scan failed: {exc}]")

        self.s.record_step(
            "browser_type",
            f'index={index}, text="{text[:30]}"',
            f"ok ({len(suggestions)} suggestions)" if suggestions else "ok",
        )

        # Surface pre-type inspection info so the LLM knows whether we
        # actually changed the field. `pretype_action` is one of
        # `typed_into_empty` (field was empty), `cleared_and_typed`
        # (existing value replaced), or `skip_match` (field already
        # contained target text — no change).
        pre_action = data.get("pretype_action") if isinstance(data, dict) else None
        pre_value = data.get("pretype_value") if isinstance(data, dict) else None
        if pre_action == "skip_match":
            caption = (
                f'Field [{index}] already contained {text!r} — no typing '
                f'needed. Proceed to next action.'
            )
        elif pre_action == "cleared_and_typed":
            caption = (
                f'Typed "{text}" into [{index}] '
                f'(cleared existing {pre_value!r} first)'
            )
        else:
            caption = f'Typed "{text}" into [{index}]'
        if suggestions:
            caption += (
                f"\n\nAutocomplete suggestions visible ({len(suggestions)}):"
            )
            for i, s in enumerate(suggestions, start=1):
                caption += f"\n  {i}. {s['text']!r} → browser_click_at(x={s['x']}, y={s['y']})"
            caption += (
                "\nDO NOT browser_type again into this field — pick a "
                "suggestion above via browser_click_at or use browser_keys "
                "(ArrowDown + Enter) to select the first one."
            )

        # Post-type semantic verification (index-addressed variant).
        # Skip when the tool no-op'd (field already matched).
        if pre_action != "skip_match":
            from .type_verify import verify_and_correct_by_index
            outcome = await verify_and_correct_by_index(
                self.s, session_id,
                dom_index=index, typed_text=text,
                page_url=self.s.current_url,
                field_meta={},
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                if isinstance(data, dict):
                    data["auto_corrected"] = True
                    data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix

        # Prefetch vision so next screenshot call finds bboxes cached.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    name = "browser_keys"
    description = "Send keyboard keys or shortcuts."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        print(f"\n>> browser_keys({keys})")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/keys",
            json={"keys": keys},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after key press (e.g., Enter may submit form)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Sent keys: {keys}"),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = "Scroll the page up or down, or to a specific percentage."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_scroll({direction or f'{percent}%'})")
        gate = await _feedback_gate("browser_scroll")
        if gate:
            return gate
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/scroll",
            json=payload,
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after scroll (new elements may be visible)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        action = f"Scrolled to {percent}%" if percent is not None else f"Scrolled {direction or 'down'}"
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, action),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select",
            json={"index": index, "value": value},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    name = "browser_eval"
    description = "Execute JavaScript in the browser page. FREE — no screenshot cost."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0  # eval resets click loop tracking
        print(f"\n>> browser_eval({script[:60]}...)")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": script},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        result = data.get("result")
        result_str = json.dumps(result, indent=2, ensure_ascii=False)[:5000] if isinstance(result, (dict, list)) else str(result)[:5000]
        self.s.log_activity(f"eval({script[:40]}...)", result_str[:60])
        self.s.record_step("browser_eval", script[:60], result_str[:100])
        return result_str


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema(
            "Puppeteer script body. Variables: page (Puppeteer Page), context, helpers (sleep, screenshot, log)."
        ),
        context=ObjectSchema(description="Optional context data", nullable=True),
        timeout=IntegerSchema(description="Script timeout in ms (default: 60000)", nullable=True),
        mutates=BooleanSchema(
            description=(
                "Set true when the script mutates the page (click, "
                "type, input.value=, dispatchEvent). Default false — "
                "the sandbox rejects those operations and returns a "
                "[blocked_op:…] error. Keep false for read-only "
                "inspection (readyState, innerText, aria-labels). Only "
                "flip true when no cursor tool can express the action; "
                "isTrusted=false JS clicks are bot-detected by WAFs."
            ),
            default=False,
        ),
        required=["session_id", "script"],
    )
)
class BrowserRunScriptTool(Tool):
    name = "browser_run_script"
    description = (
        "Execute a Puppeteer script with full page API access. "
        "READ-ONLY by default — pass mutates=true to allow click/type/"
        "dispatchEvent/value-setter operations (rare)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, session_id: str, script: str,
        context: dict | None = None,
        timeout: int | None = None,
        mutates: bool = False,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_run_script({script[:80]}...)")
        self.s.consecutive_click_calls = 0  # script execution resets click loop tracking
        payload: dict[str, Any] = {"code": script, "mutates": bool(mutates)}
        if context:
            payload["context"] = context
        if timeout:
            payload["timeout"] = timeout

        client_timeout = max(120.0, (timeout or 60000) / 1000 + 10)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/script",
            json=payload,
            timeout=client_timeout,
        )
        r.raise_for_status()
        data = r.json()

        self.s.actions_since_screenshot += 1

        if not data.get("success"):
            error = data.get("error", "Unknown error")
            self.s.log_activity("run_script(FAILED)", error[:100])
            self.s.record_step("browser_run_script", script[:60], f"FAILED: {error[:100]}")
            # L1 sandbox rejected a mutation. Rewrite the reply so the
            # brain gets a concrete cursor-tool recommendation instead
            # of a raw JS error string (which it has been misreading
            # as a server 403).
            blocked_op = data.get("blocked_op")
            if blocked_op or error.startswith("[blocked_op:"):
                return (
                    f"[script_mutation_blocked] {error} The script "
                    f"tried to mutate the page from a mutates=false "
                    f"run. Either (a) re-call with mutates=true IF "
                    f"you genuinely need JS orchestration — rare, "
                    f"and many sites reject isTrusted=false clicks "
                    f"anyway — or (b) switch to "
                    f"browser_click_at(vision_index=V_n) / "
                    f"browser_type_at / browser_click_selector which "
                    f"use humanized isTrusted=true events."
                )
            # Fetch current elements so agent can see what's on the page and fix the script
            elements = await _fetch_elements(session_id, self.s)
            tip = "\n[TIP: Fix the script and retry in this SAME session. Do NOT navigate back to the start.]"
            if elements:
                tip += f"\n\nCurrent interactive elements:\n{elements}"
            return f"Script error: {error}{tip}"

        parts = []
        result = data.get("result")
        if result is not None:
            if isinstance(result, (dict, list)):
                parts.append(f"Result: {json.dumps(result, indent=2, ensure_ascii=False)[:5000]}")
            else:
                parts.append(f"Result: {str(result)[:5000]}")

        logs = data.get("logs", [])
        if logs:
            parts.append("Logs:\n" + "\n".join(logs[:20]))

        duration = data.get("duration", 0)
        parts.append(f"Duration: {duration}ms")
        self.s.log_activity(f"run_script(ok, {duration}ms)", str(result)[:60] if result else "void")
        self.s.record_step("browser_run_script", script[:60], str(result)[:100] if result else "void")
        self.s.record_checkpoint(self.s.current_url, "", f"run_script(ok, {duration}ms)")

        # Auto-include updated elements so agent sees current page state
        elements = await _fetch_elements(session_id, self.s)
        if elements:
            parts.append(f"\nInteractive elements:\n{elements}")

        return "\n".join(parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        text=StringSchema("Text to wait for on the page", nullable=True),
        selector=StringSchema("CSS selector to wait for", nullable=True),
        timeout=IntegerSchema(description="Max wait time in seconds (default: 10)", nullable=True),
        required=["session_id"],
    )
)
class BrowserWaitForTool(Tool):
    name = "browser_wait_for"
    description = (
        "Wait for text or a CSS selector to appear on the page. "
        "Much better than blind helpers.sleep() — polls efficiently until the condition is met. "
        "Provide either 'text' or 'selector' (not both). FREE — no screenshot cost."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        text: str | None = None,
        selector: str | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> str:
        if not text and not selector:
            return "Error: provide either 'text' or 'selector' parameter."

        timeout_s = timeout or 10
        label = f'text="{text}"' if text else f'selector="{selector}"'
        print(f"\n>> browser_wait_for({label}, timeout={timeout_s}s)")

        if text:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.body.innerText.includes({json.dumps(text)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """
        else:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.querySelector({json.dumps(selector)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """

        client_timeout = max(30.0, timeout_s + 10)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/script",
            json={"code": script, "timeout": timeout_s * 1000 + 5000},
            timeout=client_timeout,
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            self.s.log_activity(f"wait_for({label})", f"script error: {data.get('error', '?')[:60]}")
            return f"Wait failed (script error): {data.get('error', 'unknown')}"

        result = data.get("result", {})
        if result.get("found"):
            self.s.log_activity(f"wait_for({label})", "found")
            # Fetch updated elements
            elements = await _fetch_elements(session_id, self.s)
            response = f"Found! Page: {result.get('url', '?')} | Title: {result.get('title', '?')}"
            if elements:
                response += f"\n\nInteractive elements:\n{elements}"
            return response
        else:
            self.s.log_activity(f"wait_for({label})", f"timeout after {timeout_s}s")
            return (
                f"Not found after {timeout_s}s (selector/text did NOT match). "
                f"This is a RENDERING-SPEED or SELECTOR issue — NOT a network "
                f"block. DO NOT escalate to Tier 3.\n"
                f"Page: {result.get('url', '?')} | Title: {result.get('title', '?')}\n"
                f"Page preview: {result.get('bodyPreview', 'N/A')}\n"
                f"Next steps:\n"
                f"  - browser_screenshot to see the actual rendered state.\n"
                f"  - Retry browser_wait_for with a longer timeout (20-30s) "
                f"or a different selector (e.g. try 'form', 'button[type=submit]' "
                f"instead of generic 'input').\n"
                f"  - browser_run_script with `return document.body.innerText.length` "
                f"to confirm the page has actually rendered content."
            )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserGetMarkdownTool(Tool):
    name = "browser_get_markdown"
    description = "Extract page content as markdown. FREE — no screenshot cost."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/markdown",
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("content", "No content extracted")[:10000]


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        accept=BooleanSchema(description="Accept (true) or dismiss (false)"),
        text=StringSchema("Text for prompt dialogs", nullable=True),
        required=["session_id", "accept"],
    )
)
class BrowserDialogTool(Tool):
    name = "browser_dialog"
    description = "Accept or dismiss a pending JavaScript dialog."

    async def execute(self, session_id: str, accept: bool, text: str | None = None, **kw: Any) -> str:
        payload: dict[str, Any] = {"accept": accept}
        if text:
            payload["text"] = text
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/dialog",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        return f"Dialog {'accepted' if accept else 'dismissed'}"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserPlanNextStepsTool(Tool):
    """Re-run the hierarchical action planner against the cached vision
    + blocker state without taking a fresh screenshot.

    Useful after a click that missed its postcondition — the brain wants
    an updated plan without burning screenshot budget. The planner itself
    is cached by scene fingerprint, so rapid re-plans on an unchanged
    scene return the same queue in near-zero time.

    Returns the ordered [PLAN] block as plain text. The worker LLM can
    read it and pick the top action to execute, or override.
    """

    name = "browser_plan_next_steps"
    description = (
        "Re-compute the planned action queue (dismiss blockers → main goal) "
        "from the most recent vision + DOM-blocker snapshot. No screenshot, "
        "no vision call — cheap. Call after a failed dismiss or when the "
        "scene may have changed and you want the planner's latest ranking "
        "before deciding the next tool."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        if not session_id.startswith("t3-"):
            return (
                "[plan_unavailable] Planner currently runs only for t3 "
                "(undetected Chromium) sessions. Call browser_screenshot "
                "to get a fresh vision + suggested_actions instead."
            )
        resp = self.s._last_vision_response
        if resp is None:
            return (
                "[plan_unavailable] No cached vision response. Run "
                "browser_screenshot first."
            )
        try:
            from superbrowser_bridge.antibot import interactive_session as _t3mgr
            from superbrowser_bridge.antibot.ui_blockers import detect as _detect_blockers
            from superbrowser_bridge.action_planner import plan as _plan_actions
            mgr = _t3mgr.default()
            blockers = await _detect_blockers(mgr, session_id)
            self.s._last_blockers = blockers
            queue = _plan_actions(
                vresp=resp,
                blockers=blockers,
                task_instruction=self.s.task_instruction or "",
                url=self.s.current_url or "",
                recent_steps=self.s.step_history[-8:] if self.s.step_history else [],
            )
            self.s._last_action_queue = queue
            return queue.to_brain_text()
        except Exception as exc:
            return f"[plan_failed] {str(exc)[:200]}"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCloseTool(Tool):
    name = "browser_close"
    description = "Close the browser session and free resources. Always close when done."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        print(f"\n>> browser_close({session_id})")
        self.s.log_activity(f"close({session_id})")
        self.s.print_summary()
        self.s.export_activity_log()
        self.s.export_step_history()
        # Route through _request_with_backoff so t3 sessions get intercepted
        # and dispatched to the in-process patchright manager.
        r = await _request_with_backoff(
            "DELETE",
            f"{SUPERBROWSER_URL}/session/{session_id}",
            timeout=10.0,
        )
        r.raise_for_status()
        used = self.s.max_screenshots - self.s.screenshot_budget
        return f"Session closed. Vision: {self.s.vision_calls}, Text: {self.s.text_calls}, Screenshots: {used}/{self.s.max_screenshots}, Regressions: {self.s.regression_count}"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        startX=NumberSchema("Start X coordinate"),
        startY=NumberSchema("Start Y coordinate"),
        endX=NumberSchema("End X coordinate"),
        endY=NumberSchema("End Y coordinate"),
        steps=IntegerSchema("Number of intermediate steps (default 25, higher = smoother)", nullable=True),
        required=["session_id", "startX", "startY", "endX", "endY"],
    )
)
class BrowserDragTool(Tool):
    name = "browser_drag"
    description = "Drag from (startX, startY) to (endX, endY). Useful for slider CAPTCHAs and drag-to-verify puzzles."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, startX: float, startY: float, endX: float, endY: float, steps: int | None = None, **kw: Any) -> str:
        print(f"\n>> browser_drag(({startX},{startY}) -> ({endX},{endY}))")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "startX": startX, "startY": startY,
            "endX": endX, "endY": endY,
        }
        if steps is not None:
            payload["steps"] = steps

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag",
            json=payload,
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        self.s.record_step("browser_drag", f"({startX},{startY})->({endX},{endY})", data.get("url", ""))
        caption = f"Dragged from ({startX},{startY}) to ({endX},{endY})"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserDetectCaptchaTool(Tool):
    name = "browser_detect_captcha"
    description = "Check if the page has a captcha."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        # Route through _request_with_backoff so t3 sessions go to the
        # in-process patchright captcha detector, not the TS server.
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        captcha = data.get("captcha")
        if not captcha:
            return "No captcha detected."
        # Normalize across tiers: t3 returns {present: bool, type: ...};
        # TS may return a different shape where absence is represented as
        # falsy. Either way, bail if nothing is active.
        if isinstance(captcha, dict) and (
            captcha.get("present") is False
            or captcha.get("type") in ("none", None, "")
        ):
            return "No captcha detected."
        # Detecting a captcha triggers captcha_mode: screenshot dedup +
        # "no-actions-since-last-shot" rules are relaxed so the solver can
        # take repeated before/after screenshots.
        self.s.enter_captcha_mode()
        # Surface the live-view URL the moment a captcha is detected, not
        # only when handoff fires. Gives the LLM (and any UI piping tool
        # output to a human) a link to offer immediately. For t3 sessions
        # this is the Python viewer port; for t1 it's the TS server.
        view_url: str | None = None
        if self.s.backend == "t3":
            try:
                from superbrowser_bridge.antibot import t3_viewer as _v
                await _v.ensure_started()
                view_url = _v.view_url(session_id)
            except Exception:
                view_url = None
        else:
            view_url = data.get("viewUrl") or (
                f"{os.environ['SUPERBROWSER_PUBLIC_HOST'].rstrip('/')}"
                f"/session/{session_id}/view"
                if os.environ.get("SUPERBROWSER_PUBLIC_HOST")
                else None
            )
        lines = [
            f"Captcha detected: type={captcha['type']}, "
            f"siteKey={captcha.get('siteKey', 'N/A')} "
            f"(captcha_mode active for next {self.s.CAPTCHA_MODE_ITERATIONS} iterations)",
        ]
        if view_url:
            lines.append(
                f"Live view for human handoff: {view_url} "
                f"(open this URL if you decide to hand off to the user)"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCaptchaScreenshotTool(Tool):
    name = "browser_captcha_screenshot"
    description = "Take a close-up screenshot of the captcha area."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot",
            timeout=10.0,
        )
        if r.status_code == 404:
            return "No captcha area found."
        r.raise_for_status()
        # t3 returns a dict with image_base64; t1 returns raw JPEG bytes.
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if isinstance(body, dict) and body.get("image_base64"):
            b64 = body["image_base64"]
        else:
            b64 = base64.b64encode(r.content).decode()
        return await self.s.build_tool_result_blocks(
            b64,
            "Captcha area — analyze to solve",
            intent="solve captcha — locate widget + tiles + handles",
            url=self.s.current_url,
        )


# --- Iterative captcha loop -------------------------------------------------
# Shared between BrowserSolveCaptchaTool (Python path, method='vision') and
# antibot/captcha/solve_vision.py (T3 HTTP path, POST /captcha/solve). Both
# entry points end up here so a fix to the "click every tile blindly" bug
# cannot regress in one path while the other is left behind.

_SUBMIT_KEYWORDS = ("verify", "submit", "next", "continue", "check", "done", "i'm done")


def _first_actionable(resp: Any) -> Any:
    """Pick the next bbox worth acting on from a VisionResponse.

    Preference: unselected captcha_tile → slider_handle → verify/submit
    button → captcha_widget fallback. Returns None if the response has
    nothing usable. Purely local — no side effects.
    """
    bboxes = getattr(resp, "bboxes", None) or []
    tiles, handles, submits, widgets = [], [], [], []
    for b in bboxes:
        role = (getattr(b, "role", "") or "").lower()
        label = (getattr(b, "label", "") or "").lower()
        if role == "captcha_tile":
            tiles.append(b)
        elif role == "slider_handle":
            handles.append(b)
        elif role == "captcha_widget":
            widgets.append(b)
        elif role == "button" and any(kw in label for kw in _SUBMIT_KEYWORDS):
            submits.append(b)
    # Tiles first if any remain; submit only takes priority when tiles are
    # gone (i.e. grid challenge completed).
    if tiles:
        return tiles[0]
    if handles:
        return handles[0]
    if submits:
        return submits[0]
    if widgets:
        return widgets[0]
    return None


async def _solve_captcha_iterative(
    session_id: str,
    captcha_info: Any,
    vision_agent: Any,
    *,
    task_instruction: str = "",
    solve_round: int = 0,
    max_steps: int = 12,
) -> dict[str, Any]:
    """Per-step vision-driven captcha solver.

    Each iteration: screenshot → vision (returns one next action) → click
    or drag → poll structural hash until page changes (cap 600ms). Exits
    when vision reports captcha_present=false or when a safety guard
    (dead-action streak, max_steps, HTTP failure) fires.

    Returns a structured dict the orchestrator's captcha-learnings
    consumer understands — see orchestrator_tools._update_captcha_learnings.
    Never raises into the caller; failures collapse into solved=False +
    an `error` field.
    """
    actions: list[str] = []
    last_click_xy: tuple[int, int] | None = None
    # Trail of up to 6 recent click points, overlaid on the next
    # screenshot so the model can see where we've already interacted.
    cursor_trail: list[tuple[int, int]] = []
    same_action_streak = 0
    steps_taken = 0
    model_name: str | None = None
    provider_name: str | None = None

    # Surface any text prompt the native detector already captured.
    prompt_hint = ""
    if captcha_info is not None:
        for n in list(getattr(captcha_info, "notes", None) or []):
            if isinstance(n, str) and n.startswith("text_signal:"):
                prompt_hint = n.split(":", 1)[1]
                break

    for step in range(max_steps):
        steps_taken = step + 1
        # 1. Screenshot + page state.
        try:
            sr = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
                timeout=15.0,
            )
            sr.raise_for_status()
            state = sr.json()
        except Exception as exc:
            actions.append(f"step {step}: state fetch failed: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": f"state_fetch:{exc}",
            }

        b64 = state.get("screenshot")
        if not b64:
            actions.append(f"step {step}: no screenshot in state payload")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": "no_screenshot",
            }
        current_url = state.get("url", "")
        elements_str = (
            state.get("clickableElementsToString") or state.get("elements") or ""
        )
        try:
            page_hash = BrowserSessionState.hash_page_content(elements_str)
        except Exception:
            page_hash = ""
        img_w, img_h = _read_image_dims(b64)

        # 2. Ask vision for the single next action.
        last_action_hint = (
            f"Previous click: ({last_click_xy[0]},{last_click_xy[1]})"
            if last_click_xy else "Previous click: none"
        )
        try:
            resp = await vision_agent.analyze(
                screenshot_b64=b64,
                # "captcha step" routes to the solve_captcha_step intent
                # bucket in prompts.intent_bucket — step-mode suppresses
                # SoM overlay and skips result caching.
                intent="solve captcha step — pick the single next action",
                session_id=session_id,
                url=current_url,
                dom_hash=f"cap-r{solve_round}-s{step}-{page_hash}",
                image_width=img_w,
                image_height=img_h,
                task_instruction=(
                    (task_instruction or "")
                    + ("\nCaptcha prompt: " + prompt_hint if prompt_hint else "")
                    + f"\n{last_action_hint}"
                    + "\nReturn next_action committing to ONE step. If every "
                    "matching tile appears selected/dismissed, pick "
                    "action_type=submit targeting the verify button. If the "
                    "captcha appears gone, pick action_type=done. Do NOT "
                    "target within 40 pixels of the previous click unless a "
                    "visibly new tile has rendered there."
                ),
                cursor_trail=cursor_trail if cursor_trail else None,
            )
        except Exception as exc:
            actions.append(f"step {step}: vision call failed: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": f"vision_call:{exc}",
            }

        model_name = model_name or resp.model
        provider_name = provider_name or resp.provider

        # 3. Done if the captcha is gone (only trust this after step 0 —
        # the very first screenshot should see the captcha).
        if step > 0 and not resp.flags.captcha_present:
            actions.append(f"step {step}: captcha_present=false, exiting loop")
            break

        # 4. Prefer the structured next_action from step-mode. Fall back
        # to the bbox-preference picker for older vision responses that
        # don't fill next_action (e.g. providers that ignore the field,
        # or non-step intents calling the shim).
        na = getattr(resp, "next_action", None)
        forced_drag = False  # na says drag_slider — override role dispatch
        forced_type = False  # na says type_text — use target_input_bbox
        type_value = ""
        last_expect_change = "static"
        if na is not None:
            at = (getattr(na, "action_type", "") or "").lower()
            if at == "done":
                actions.append(f"step {step}: next_action=done, exiting loop")
                break
            if at == "stuck":
                actions.append(
                    f"step {step}: next_action=stuck "
                    f"(reason: {getattr(na, 'reasoning', '')[:120]})"
                )
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "vision_stuck",
                }
            forced_drag = at == "drag_slider"
            forced_type = at == "type_text"
            last_expect_change = getattr(na, "expect_change", "static") or "static"
            if forced_type:
                # type_text targets the input field, not the image.
                target = (
                    getattr(na, "target_input_bbox", None)
                    or getattr(na, "target_bbox", None)
                )
                type_value = (getattr(na, "type_value", "") or "").strip()
                if not type_value:
                    actions.append(
                        f"step {step}: type_text without type_value — treating as stuck"
                    )
                    return {
                        "solved": False, "method": "vision_iterative",
                        "subMethod": "python_vision_agent",
                        "steps": steps_taken, "actions": actions,
                        "provider": provider_name, "model": model_name,
                        "error": "type_text_missing_value",
                    }
            else:
                target = getattr(na, "target_bbox", None)
        else:
            target = None

        if target is None:
            target = _first_actionable(resp)
        if target is None:
            if step == 0 and resp.flags.captcha_widget_bbox is not None:
                target = resp.flags.captcha_widget_bbox
            else:
                actions.append(f"step {step}: no actionable bbox returned")
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "no_actionable_bbox",
                }

        try:
            cx, cy = target.center_pixels(img_w, img_h)
            x0, y0, x1, y1 = target.to_pixels(img_w, img_h)
        except Exception as exc:
            actions.append(f"step {step}: malformed bbox: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": "malformed_bbox",
            }

        # 5. Dead-action guard. Two successive near-duplicates within 40px
        # and no site change → bail to the caller (who decides handoff).
        if last_click_xy and abs(cx - last_click_xy[0]) < 40 and abs(cy - last_click_xy[1]) < 40:
            same_action_streak += 1
            if same_action_streak >= 2:
                actions.append(
                    f"step {step}: third same-target attempt within 40px — bailing"
                )
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "dead_action_streak",
                }
        else:
            same_action_streak = 0

        # 6. Dispatch. Explicit `drag_slider` / `type_text` from next_action
        # win; otherwise fall back to bbox role.
        role = (getattr(target, "role", "") or "").lower()
        if forced_type:
            try:
                tr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/type-at",
                    json={
                        "x": cx, "y": cy,
                        "text": type_value,
                        "clear": True,
                    },
                    timeout=30.0,
                )
                if tr.status_code >= 400:
                    actions.append(
                        f"step {step}: type-at HTTP {tr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: type_text {type_value!r} @({cx},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: type-at dispatch exception: {exc}")
                break
        elif forced_drag or role == "slider_handle":
            if captcha_info is not None and getattr(captcha_info, "widget_bbox", None):
                wbb = captcha_info.widget_bbox
                ex = max(float(wbb[2]) - 12, cx + 50)
            elif resp.flags.captcha_widget_bbox is not None:
                _x0, _y0, _x1, _y1 = resp.flags.captcha_widget_bbox.to_pixels(
                    img_w, img_h,
                )
                ex = max(_x1 - 12, cx + 50)
            else:
                ex = cx + 250
            try:
                dr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/drag",
                    json={
                        "startX": cx, "startY": cy,
                        "endX": ex, "endY": cy,
                        "steps": 30,
                    },
                    timeout=30.0,
                )
                if dr.status_code >= 400:
                    actions.append(
                        f"step {step}: drag HTTP {dr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: drag handle {getattr(target, 'label', '')!r} "
                    f"({cx},{cy})→({int(ex)},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: drag dispatch exception: {exc}")
                break
        else:
            try:
                cr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/click",
                    json={
                        "x": cx, "y": cy,
                        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                    },
                    timeout=30.0,
                )
                if cr.status_code == 409:
                    actions.append(
                        f"step {step}: click@({cx},{cy}) refused "
                        "(low-reward band) — re-analyzing"
                    )
                    # Do NOT advance last_click_xy/streak; re-ask vision.
                    continue
                if cr.status_code >= 400:
                    actions.append(
                        f"step {step}: click HTTP {cr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: click {role or 'bbox'} "
                    f"{getattr(target, 'label', '')!r} @({cx},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: click dispatch exception: {exc}")
                break

        last_click_xy = (cx, cy)
        cursor_trail.append((cx, cy))
        if len(cursor_trail) > 6:
            cursor_trail.pop(0)

        # 7. Poll the structural hash until it changes, capped adaptively
        # based on vision's expect_change hint. Hash-poll is faster than
        # a blind sleep on static grids and safer on slow re-rendering
        # ones. page_nav gets a much longer ceiling because whole-page
        # transitions take ~1.5s.
        poll_cap = {
            "page_nav": 2.5,
            "widget_replace": 1.0,
            "new_tile": 0.6,
            "static": 0.3,
        }.get(last_expect_change, 0.6)
        change_deadline = time.monotonic() + poll_cap
        while time.monotonic() < change_deadline:
            await asyncio.sleep(0.08)
            try:
                pr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "false"},
                    timeout=5.0,
                )
                if pr.status_code != 200:
                    continue
                ps = pr.json()
                _elems = (
                    ps.get("clickableElementsToString")
                    or ps.get("elements") or ""
                )
                if not _elems:
                    continue
                new_hash = BrowserSessionState.hash_page_content(_elems)
                if new_hash and new_hash != page_hash:
                    break
            except Exception:
                pass

    # 8. Verify once.
    solved = False
    try:
        vr = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=15.0,
        )
        if vr.status_code == 200:
            verify_state = vr.json()
            verify_b64 = verify_state.get("screenshot")
            if verify_b64:
                vresp = await vision_agent.analyze(
                    screenshot_b64=verify_b64,
                    intent="verify captcha cleared",
                    session_id=session_id,
                    url=verify_state.get("url", ""),
                    dom_hash=f"cap-verify-r{solve_round}",
                )
                solved = not vresp.flags.captcha_present
    except Exception:
        solved = False

    return {
        "solved": solved,
        "method": "vision_iterative",
        "subMethod": "python_vision_agent",
        "steps": steps_taken,
        "actions": actions,
        "provider": provider_name,
        "model": model_name,
    }


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        method=StringSchema("Solving method: 'auto', 'token', 'ai_vision', 'grid'", nullable=True),
        provider=StringSchema("Captcha solver: '2captcha' or 'anticaptcha'", nullable=True),
        api_key=StringSchema("API key for solver service", nullable=True),
        required=["session_id"],
    )
)
class BrowserSolveCaptchaTool(Tool):
    name = "browser_solve_captcha"
    description = "Solve a detected captcha automatically."

    def __init__(self, state: BrowserSessionState):
        self.s = state
        # Per-session lock so concurrent solve calls on the same session
        # serialize around captcha_solve_round / captcha_screenshots_used.
        # The agent fires single-threaded, but retry loops or stray parallel
        # tool calls can race otherwise.
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def execute(self, session_id: str, method: str | None = None, provider: str | None = None, api_key: str | None = None, **kw: Any) -> str:
        # Any solve attempt unblocks the CF nav-guard — whether or not
        # the solve succeeds, the agent has acknowledged the interstitial,
        # and re-navigating is no longer a blind loop.
        self.s.nav_solve_called_since_block = True

        # Vision-based solve path. Replaces the three deleted TS vision
        # strategies (recaptcha-ai-grid, slider-drag, generic-vision) with
        # a single call to our dedicated Python vision agent. When the
        # brain (or fast-to-human policy) picks method='vision' we run the
        # full solve loop here and return a structured result without ever
        # hitting the server's captcha/solve endpoint.
        if method == "vision":
            return await self._solve_via_vision(session_id)

        # Auto-route captcha types that the TS server registry has no
        # strategy for (text_captcha, generic) straight to the Python
        # vision loop. Without this, method='auto' on T1 would fall all
        # the way through to human_handoff for a classic distorted-word
        # captcha that vision can solve directly.
        #
        # cf_interstitial gets its own solver (wait-based, not vision):
        # the whole page IS the challenge, no clicking needed — CF's JS
        # just needs time + humanization to auto-pass.
        if method in (None, "auto"):
            try:
                dr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
                    timeout=10.0,
                )
                if dr.status_code == 200:
                    det_cap = (dr.json() or {}).get("captcha") or {}
                    det_type = (det_cap.get("type") or "").lower()
                    det_present = bool(det_cap.get("present"))
                    det_notes = det_cap.get("notes") or []
                    print(
                        f"  [auto-route] detect -> type={det_type!r} "
                        f"present={det_present} notes={det_notes}"
                    )
                    if det_type == "cf_interstitial":
                        return await self._solve_via_cf_wait(session_id)
                    if det_type in ("text_captcha", "text", "generic", "image"):
                        return await self._solve_via_vision(session_id)
                    if det_type in ("recaptcha-v2", "hcaptcha"):
                        # Cheap first attempt: let the vision loop click the
                        # widget checkbox. On a trusted-fingerprint session
                        # (patchright + good proxy) this alone often passes.
                        # Small step budget so we bail fast if the image-tile
                        # challenge appears — the token vendor / human handoff
                        # below handle that class better.
                        print(
                            f"  [vision checkbox] trying widget click "
                            f"(max_steps=3) for {det_type!r} before token vendor"
                        )
                        vr = await self._try_vision_solve_for_widget(
                            session_id, det_cap, max_steps=3,
                        )
                        if vr and vr.get("solved"):
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                            self.s.record_step(
                                "browser_solve_captcha",
                                "vision_checkbox",
                                f"solved=True via vision checkbox-click | "
                                f"{json.dumps(vr, default=str)[:300]}",
                            )
                            return (
                                f"Captcha SOLVED via vision checkbox-click "
                                f"({vr.get('steps', 0)} step(s))"
                                f"\n\nResult JSON:\n"
                                f"{json.dumps(vr, indent=2, default=str)}"
                            )
                        if vr is not None:
                            self.s.record_step(
                                "browser_solve_captcha",
                                "vision_checkbox",
                                f"solved=False via vision checkbox-click, "
                                f"falling through | "
                                f"{json.dumps(vr, default=str)[:300]}",
                            )
                else:
                    print(
                        f"  [auto-route] detect HTTP {dr.status_code} — "
                        f"falling through to TS solver"
                    )
            except Exception as exc:
                # Detection failure is non-fatal — fall through to the
                # standard TS solver path which runs its own detection.
                print(
                    f"  [auto-route] detect raised "
                    f"{type(exc).__name__}: {exc} — falling through"
                )

        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        if provider:
            payload["provider"] = provider
        if api_key:
            payload["apiKey"] = api_key
        # Advance the solve round BEFORE the call so the next screenshot
        # (which the LLM will take to inspect the result) gets a fresh dedup
        # allowance distinct from the pre-solve shots.
        self.s.captcha_solve_round += 1
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
            json=payload,
            timeout=180.0,
        )
        r.raise_for_status()
        data = r.json()

        # Build a structured result the orchestrator can parse — keeps the
        # method + subMethod + vendor + trace so per-domain captcha learnings
        # can be written automatically. The LLM sees JSON; a human-readable
        # summary is injected at the top for quick scanning.
        summary: str
        if data.get("solved"):
            summary = (
                f"Captcha SOLVED via {data.get('method', '?')}"
                f"/{data.get('subMethod', '?')} in {data.get('durationMs', 0)}ms "
                f"({data.get('attempts', 1)} attempt(s))"
            )
        else:
            summary = f"Captcha NOT solved: {data.get('error', 'all methods failed')}"

        # Record the structured method info so orchestrator can learn from it.
        structured = {
            "solved": bool(data.get("solved")),
            "captchaType": data.get("captchaType") or data.get("captcha", {}).get("type"),
            "vendorDetected": data.get("vendorDetected"),
            "method": data.get("method"),
            "subMethod": data.get("subMethod"),
            "attempts": data.get("attempts"),
            "totalRounds": data.get("totalRounds"),
            "durationMs": data.get("durationMs"),
            "siteKey": data.get("siteKey"),
            "iframeUrl": data.get("iframeUrl"),
            "error": data.get("error"),
        }
        # Drop None values so the JSON stays compact.
        structured = {k: v for k, v in structured.items() if v is not None}

        self.s.record_step(
            "browser_solve_captcha",
            method or "auto",
            f"{summary} | {json.dumps(structured, default=str)[:300]}",
        )

        # The solve freed us from captcha — end captcha_mode so the normal
        # budget rules resume.
        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

        # --- Auto-escalation to human handoff (deterministic) -------------
        # When auto-solve fails AND human handoff is enabled, immediately
        # POST to the human-input endpoint instead of returning to the LLM
        # and hoping it calls browser_ask_user. This removes the LLM
        # decision loop from the critical captcha→human path.
        if self.s.human_handoff_enabled and self.s.human_handoff_budget > 0:
            print(f"   [auto-escalation] captcha auto-solve failed, requesting human handoff")
            self.s.human_handoff_budget -= 1
            try:
                handoff_timeout = int(os.environ.get("SUPERBROWSER_HANDOFF_TIMEOUT_MS", "180000")) / 1000
                async with httpx.AsyncClient(timeout=handoff_timeout + 10) as hclient:
                    hr = await hclient.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                        json={
                            "type": "captcha",
                            "message": (
                                "Auto-solve failed for captcha. Please open the live view URL "
                                "and click through the challenge — the agent will detect when "
                                "it clears and resume automatically."
                            ),
                        },
                    )
                    if hr.status_code == 200:
                        hdata = hr.json()
                        if hdata.get("cancelled") or hdata.get("timeout"):
                            human_result = "Human handoff timed out or was cancelled."
                        else:
                            human_result = "Human solved the captcha. Resuming task."
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                    else:
                        human_result = f"Human handoff request failed (HTTP {hr.status_code})."
            except Exception as exc:
                human_result = f"Human handoff request error: {exc}"

            self.s.record_step(
                "browser_solve_captcha",
                "auto_escalation",
                human_result,
            )
            return (
                f"{summary}\n\n"
                f"[AUTO-ESCALATION] {human_result}\n\n"
                f"Result JSON:\n{json.dumps(structured, indent=2, default=str)}"
            )

        return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

    async def _solve_via_cf_wait(self, session_id: str) -> str:
        """Solve a Cloudflare Managed Challenge by waiting for auto-pass.

        Routes to `/captcha/solve` with method='cf_wait' — the T3 HTTP
        dispatcher picks up `cf_interstitial` detect type and calls the
        dedicated humanized wait loop (`antibot.captcha.solve_cf`). On
        T1 the TS server's own CF handling runs.
        """
        print(f"  [cf_wait] entering solver for session={session_id}")
        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                    json={"method": "cf_wait"},
                    timeout=120.0,
                )
                if r.status_code >= 400:
                    body_snippet = ""
                    try:
                        body_snippet = json.dumps(r.json(), default=str)[:400]
                    except Exception:
                        body_snippet = (getattr(r, "text", "") or "")[:400]
                    print(
                        f"  [cf_wait] dispatcher returned HTTP {r.status_code}: "
                        f"{body_snippet}"
                    )
                    data: dict[str, Any] = {
                        "solved": False, "method": "cf_wait",
                        "error": f"HTTP {r.status_code}",
                        "dispatcher_body": body_snippet,
                    }
                else:
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    if not isinstance(data, dict):
                        data = {"solved": False, "method": "cf_wait",
                                "error": "non-dict response"}
                    print(
                        f"  [cf_wait] result: solved={data.get('solved')} "
                        f"durationMs={data.get('durationMs')} "
                        f"iterations={data.get('iterations')} "
                        f"cookies={data.get('cookies_landed')}"
                    )
            except Exception as exc:
                import traceback as _tb
                print(
                    f"  [cf_wait] dispatch raised {type(exc).__name__}: {exc}"
                )
                print(_tb.format_exc())
                data = {
                    "solved": False, "method": "cf_wait",
                    "error": f"dispatch failed: {exc}",
                }

        self.s.record_step(
            "browser_solve_captcha",
            "cf_wait",
            f"solved={data.get('solved')} | "
            f"{json.dumps(data, default=str)[:300]}",
        )

        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            # Clear any nav-guard set by a prior failed navigate.
            self.s.last_nav_cf_blocked_url = ""
            return (
                f"Captcha SOLVED via cf_wait (CF interstitial auto-passed "
                f"in {data.get('durationMs', 0)}ms)"
                f"\n\nResult JSON:\n{json.dumps(data, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved: CF interstitial did not auto-clear. "
            f"Consider residential proxy (PROXY_POOL_RESIDENTIAL) or "
            f"headful mode (SUPERBROWSER_ALLOW_HEADFUL=1 + xvfb), or "
            f"call browser_ask_user for human handoff."
            f"\n\nResult JSON:\n{json.dumps(data, indent=2, default=str)}"
        )

    async def _try_slider_dispatch(self, session_id: str) -> dict | None:
        """POST to /captcha/solve with method='slider' and return the parsed
        result, or None if the call itself errored.

        Used by `_solve_via_vision` to give sliders first chance at the
        dedicated bezier-drag solver before falling back to vision-driven
        drag. Keeps the solver boundary clean — we just forward and let
        the tier's native slider code (antibot.captcha.solve_slider on
        T3, src/browser/captcha equivalent on T1) do its thing.
        """
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                json={"method": "slider"},
                timeout=60.0,
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return None

    async def _try_vision_solve_for_widget(
        self,
        session_id: str,
        det_cap: dict,
        *,
        max_steps: int,
    ) -> dict[str, Any] | None:
        """Attempt a vision+cursor solve for widget captchas (recaptcha-v2,
        hcaptcha) using the already-detected widget bbox. Returns the
        structured result dict from `_solve_captcha_iterative`, or None
        when vision is unavailable — the caller reads None as "skip vision,
        go straight to token vendor." Never raises.
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return None
        if not vision_agent_enabled():
            return None

        from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
        captcha_info = CaptchaInfo(
            type=det_cap.get("type", "none"),
            present=bool(det_cap.get("present", True)),
            site_key=det_cap.get("site_key", "") or "",
            widget_selector=det_cap.get("widget_selector", "") or "",
            widget_bbox=det_cap.get("widget_bbox"),
            input_bbox=det_cap.get("input_bbox"),
            frame_url=det_cap.get("frame_url", "") or "",
            notes=list(det_cap.get("notes") or []),
        )

        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            agent = get_vision_agent()
            return await _solve_captcha_iterative(
                session_id,
                captcha_info,
                agent,
                task_instruction=self.s.task_instruction,
                solve_round=self.s.captcha_solve_round,
                max_steps=max_steps,
            )

    async def _solve_via_vision(self, session_id: str) -> str:
        """Python-side captcha solver — delegates to the iterative loop.

        Per-step flow lives in `_solve_captcha_iterative` so the T3 HTTP
        dispatch path (antibot/captcha/solve_vision.py shim) and this
        Python-tool path share one implementation. This method owns the
        session-state plumbing around the loop: enable check, per-session
        serialization, captcha_mode reset, and structured-result logging.
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return (
                "Captcha NOT solved: vision_agent package not importable. "
                "Install / configure the vision agent or use method='auto'."
            )
        if not vision_agent_enabled():
            return (
                "Captcha NOT solved: method='vision' requires VISION_ENABLED=1. "
                "Set the env flag and a VISION_API_KEY, or use method='auto'."
            )

        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            # Best-effort detect for dispatcher context (slider widgets need
            # the server-side pixel bbox to clamp drag endpoints). Failure
            # is non-fatal — the loop proceeds with what vision sees.
            captcha_info: Any = None
            try:
                dr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
                    timeout=10.0,
                )
                if dr.status_code == 200:
                    cap = (dr.json() or {}).get("captcha") or {}
                    from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
                    captcha_info = CaptchaInfo(
                        type=cap.get("type", "none"),
                        present=bool(cap.get("present", True)),
                        site_key=cap.get("site_key", "") or "",
                        widget_selector=cap.get("widget_selector", "") or "",
                        widget_bbox=cap.get("widget_bbox"),
                        input_bbox=cap.get("input_bbox"),
                        frame_url=cap.get("frame_url", "") or "",
                        notes=list(cap.get("notes") or []),
                    )
            except Exception:
                captcha_info = None

            # Slider pre-dispatch: the dedicated bezier-drag solver
            # (antibot.captcha.solve_slider on T3 / TS equivalent on T1)
            # has a motion profile tuned to defeat Geetest/Tencent/DataDome
            # fingerprinting. Vision-driven drag via our /drag endpoint
            # works, but the bezier solver is materially better at this
            # specific class. Route sliders to it first; fall through to
            # the iterative loop only if the dedicated solver returns
            # unsolved.
            if captcha_info is not None and captcha_info.type == "slider":
                slider_result = await self._try_slider_dispatch(session_id)
                if slider_result is not None and slider_result.get("solved"):
                    self.s.record_step(
                        "browser_solve_captcha",
                        "vision->slider",
                        f"solved=True via dedicated slider solver | "
                        f"{json.dumps(slider_result, default=str)[:280]}",
                    )
                    self.s.captcha_mode = False
                    self.s.captcha_mode_remaining = 0
                    return (
                        f"Captcha SOLVED via dedicated slider bezier-drag"
                        f"\n\nResult JSON:\n{json.dumps(slider_result, indent=2, default=str)}"
                    )
                # Dedicated solver failed — fall through to the iterative
                # loop so vision can try. Error paths end up reported by
                # the loop result.

            agent = get_vision_agent()
            result = await _solve_captcha_iterative(
                session_id,
                captcha_info,
                agent,
                task_instruction=self.s.task_instruction,
                solve_round=self.s.captcha_solve_round,
            )

        self.s.record_step(
            "browser_solve_captcha",
            "vision",
            f"solved={result.get('solved')} | "
            f"{json.dumps(result, default=str)[:300]}",
        )

        if result.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return (
                f"Captcha SOLVED via vision_iterative "
                f"({result.get('steps', 0)} step(s))"
                f"\n\nResult JSON:\n{json.dumps(result, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved after {result.get('steps', 0)} step(s). "
            f"Per fast-to-human policy, call browser_ask_user next."
            f"\n\nResult JSON:\n{json.dumps(result, indent=2, default=str)}"
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        question=StringSchema("What to ask the user"),
        input_type=StringSchema("Type: credentials, captcha, confirmation, otp, text, choice", nullable=True),
        required=["session_id", "question"],
    )
)
class BrowserAskUserTool(Tool):
    name = "browser_ask_user"
    description = (
        "Ask the user a question and BLOCK until they respond "
        "(up to 5 minutes). Use for credentials, OTP, confirmation, or "
        "when you need a human decision. The user replies via the remote "
        "view UI at /session/<id>/view or any HTTP client. Returns the "
        "user's reply as a string; on timeout returns a sentinel message "
        "you can react to."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        question: str,
        input_type: str | None = None,
        **kw: Any,
    ) -> Any:
        # Tier-3 path: spin up the Python live viewer and return its URL
        # as a hint to the LLM. The actual blocking "wait for user" is
        # simpler on t3 — we poll for a state change on the captcha
        # widget and resume when it clears. For now, return the URL and a
        # short wait loop so the user has ~3 min to interact.
        if self.s.backend == "t3":
            try:
                from superbrowser_bridge.antibot import t3_viewer as _v
                from superbrowser_bridge.antibot import captcha as _cap
                from superbrowser_bridge.antibot import interactive_session as _t3mgr

                await _v.ensure_started()
                view = _v.view_url(session_id)
                print(f"\n[HUMAN HANDOFF — t3] Open {view} in your browser.")
                # Poll every 3s for up to 5 min for the captcha to clear.
                mgr = _t3mgr.default()
                import asyncio as _asyncio
                import time as _time
                deadline = _time.time() + 5 * 60
                cleared = False
                while _time.time() < deadline:
                    await _asyncio.sleep(3.0)
                    try:
                        info = await _cap.detect(mgr, session_id)
                    except Exception:
                        continue
                    if not info.present:
                        cleared = True
                        break
                if cleared:
                    return (
                        f"[human_handoff_cleared] Captcha / verification "
                        f"cleared via human at {view}. Resuming."
                    )
                return (
                    f"[human_handoff_timeout] No state change detected after "
                    f"5 min at {view}. You can call browser_ask_user again or "
                    f"proceed with done(success=False)."
                )
            except Exception as exc:
                print(f"[t3 human handoff error: {exc}]")
                return f"[browser_ask_user_t3_error: {exc}. Cannot proceed.]"

        # Map nanobot-side hint to the TS server's HumanInputType. Default
        # 'text' is the safest — it accepts free-form replies and the UI's
        # "Done" button also works against it.
        valid_types = {
            "credentials", "captcha", "confirmation", "otp", "card", "text", "choice",
        }
        ht = (input_type or "text").lower()
        if ht not in valid_types:
            ht = "text"

        # Capture a screenshot to include in the request payload so any UI
        # listener (not just the live-view poller) can show what page the
        # agent is stuck on. Best-effort.
        screenshot_b64 = None
        try:
            sr = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
                timeout=10.0,
            )
            sr.raise_for_status()
            sdata = sr.json()
            screenshot_b64 = sdata.get("screenshot") or None
        except Exception:
            screenshot_b64 = None

        # View URL for the user — the concrete surface where they interact.
        public_host = os.environ.get(
            "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
        )
        view_url = f"{public_host}/session/{session_id}/view"
        message = (
            f"{question}\n\n"
            f"To respond: open {view_url} in your browser. "
            f"Either interact with the page (for captchas) or click the "
            f"'Done' button when finished."
        )

        # Five-minute timeout matches HumanInputManager's default; the TS
        # server holds the HTTP connection open until the user replies or
        # the timer fires, so client-side we just wait.
        timeout_ms = 5 * 60 * 1000
        self.s.record_step(
            "browser_ask_user",
            f"type={ht}",
            f"view_url={view_url}",
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 10) as client:
                r = await client.post(
                    f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                    json={
                        "type": ht,
                        "message": message,
                        "screenshot": screenshot_b64,
                        "timeout": timeout_ms,
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            return (
                f"[browser_ask_user error: {exc}. "
                f"User was not asked. Continue without their input "
                f"or call again.]"
            )

        if data.get("timedOut"):
            return (
                f"[User did not respond within {timeout_ms // 60000} minutes. "
                f"Proceed without their input or call done(success=False).]"
            )

        response = data.get("response") or {}
        if response.get("cancelled"):
            return "[User cancelled the request. Proceed accordingly.]"

        payload = response.get("data") or {}
        if not payload:
            return "[User responded but provided no data.]"

        # Flatten the reply dict into a short readable string for the model.
        parts = [f"{k}: {v}" for k, v in payload.items()]
        return f"[User replied] {' | '.join(parts)}"


# ── Resumption-handoff helpers ───────────────────────────────────────────
# When a worker exits (stuck, captcha-blocked, or after browser_request_help),
# we save enough tactical state that the NEXT worker can resume on the same
# live Puppeteer session with knowledge of what already failed — instead of
# spawning a fresh session from the home page.
#
# File: /tmp/superbrowser/resumption.json
# Expiry: 5 minutes (RESUMPTION_TTL_SEC). Past that, the Puppeteer session
# has likely been GC'd server-side so liveness is doubtful regardless.

RESUMPTION_PATH = "/tmp/superbrowser/resumption.json"
RESUMPTION_TTL_SEC = 300


def _extract_recent_failures(step_history: list[dict], limit: int = 5) -> list[dict]:
    """Pull the most recent tool steps that look like failures.

    With Priority 1 in place, click/type results include phrases like
    '(element_covered):' or '(stale_selector):' when the structured reason
    is set. We match on those plus generic error markers.
    """
    out: list[dict] = []
    markers = ("FAILED", "failed (", "error:", "Script error", "ERROR:", "NOT solved")
    for step in reversed(step_history):
        result = str(step.get("result") or "")
        if any(m in result for m in markers):
            out.append({
                "tool": step.get("tool", ""),
                "args": str(step.get("args", ""))[:160],
                "result_excerpt": result[:220],
                "url": step.get("url", ""),
                "time": step.get("time", ""),
            })
        if len(out) >= limit:
            break
    return list(reversed(out))


def save_resumption_artifact(
    state: "BrowserSessionState",
    domain: str,
    help_reason: str = "",
    help_failed_tactics: str = "",
) -> bool:
    """Write a resumption hint so the next delegation can pick up where we left off.

    Returns True if the artifact was written. Never raises.
    """
    try:
        if not state.session_id or not state.current_url:
            return False
        payload = {
            "session_id": state.session_id,
            "current_url": state.current_url,
            "best_checkpoint_url": state.best_checkpoint_url,
            "domain": domain,
            "task_id": state.task_id,
            "recent_failures": _extract_recent_failures(state.step_history),
            "help_reason": help_reason or "",
            "help_failed_tactics": help_failed_tactics or "",
            "written_at": time.time(),
        }
        os.makedirs(os.path.dirname(RESUMPTION_PATH), exist_ok=True)
        with open(RESUMPTION_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"  [resumption artifact saved: session={state.session_id} url={state.current_url}]")
        return True
    except OSError as exc:
        print(f"  [resumption save failed: {exc}]")
        return False


async def load_resumption_artifact(domain: str) -> dict | None:
    """Read and validate a resumption artifact for the given domain.

    Returns None if the artifact is missing, expired, from a different
    domain, or the referenced Puppeteer session is no longer alive
    on the TS server.
    """
    if not os.path.exists(RESUMPTION_PATH):
        return None
    try:
        with open(RESUMPTION_PATH) as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return None

    age = time.time() - float(payload.get("written_at", 0) or 0)
    if age > RESUMPTION_TTL_SEC:
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
        return None
    if payload.get("domain") != domain:
        return None

    sid = payload.get("session_id")
    if not sid:
        return None

    # Cheap liveness probe — hit whichever backend owns this session.
    try:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{sid}/state",
            params={"vision": "false"},
            timeout=5.0,
        )
        if r.status_code != 200:
            try:
                os.remove(RESUMPTION_PATH)
            except OSError:
                pass
            return None
    except Exception:
        return None

    return payload


def clear_resumption_artifact() -> None:
    """Remove the resumption artifact (call when a new session successfully supersedes it)."""
    if os.path.exists(RESUMPTION_PATH):
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        claim=StringSchema(
            "The exact factual claim you're about to report. Include the value, unit, "
            "and what it refers to. E.g. 'Total price for 2 nights at Agoda Grand Sylhet "
            "May 3-5 is BDT 14,500' or '5-star hotel count in Sylhet on GoZayaan is 3'."
        ),
        required=["session_id", "claim"],
    )
)
class BrowserVerifyFactTool(Tool):
    """Visual sanity check before reporting an extracted value.

    Takes a fresh screenshot and frames a narrow verification question for
    the next model turn. The LLM must look at the actual page and say whether
    it supports the claim. Catches the common failure mode where an extraction
    script returned null/wrong-element and the model filled in a plausible
    number downstream.

    Intentionally bypasses normal dedup — verification screenshots are a
    deliberate, infrequent request and must see the current state.
    """

    name = "browser_verify_fact"
    description = (
        "Visually verify a factual claim against the current page before reporting it. "
        "Call this with the EXACT value you're about to return. Then look at the "
        "returned screenshot and answer honestly: does the page actually show this? "
        "If not, do NOT report the original value — go back and fix your extraction."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, claim: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()

        self.s.record_step("browser_verify_fact", claim[:80], "screenshot taken for verification")

        caption = (
            f"[VERIFY CLAIM]\n"
            f"Claim under review: {claim}\n\n"
            f"Look at the screenshot below carefully. In your NEXT reply, respond ONLY "
            f"with a JSON object of the form:\n"
            f'  {{"supported": <bool>, "observed_value": "<what you actually see on the page, '
            f"verbatim, or null if absent>\", "
            f'"reason": "<one sentence explaining what you saw>"}}\n\n'
            f"Rules:\n"
            f"- supported=true only if the claim matches what's visible on the page exactly "
            f"(values, units, context). A crossed-out price is NOT the current price.\n"
            f"- If the page shows a DIFFERENT value than the claim, set supported=false "
            f"and put the real value in observed_value.\n"
            f"- If the page doesn't show enough to tell, set supported=false with a "
            f"'cannot verify' reason — do NOT rubber-stamp.\n"
            f"- After this verify, if supported=false, FIX your extraction and retry. "
            f"If supported=true, report the claim as your final answer."
        )

        if data.get("screenshot"):
            # Don't let verify-fact screenshots eat the captcha cap (they're
            # not for captcha) nor trigger normal dedup (verification must
            # see the live page state). Route through the same async
            # vision-preprocessor hook every other screenshot tool uses, so
            # the brain never sees the raw image when VISION_ENABLED=1.
            self.s.vision_calls += 1
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent="verify fact against page",
                url=data.get("url", self.s.current_url),
                elements=data.get("elements"),
            )
        # No screenshot available — still return the caption so the caller
        # can at least reason about the textual state.
        return caption + "\n\n[No screenshot available — verify against browser_get_markdown output.]"


@tool_parameters(
    tool_parameters_schema(
        reason=StringSchema(
            "Why you're stuck. Be specific: 'element_covered by cookie banner I can't dismiss', "
            "'captcha solve failed 3 times', 'selector index keeps shifting'."
        ),
        failed_tactics=StringSchema(
            "Comma-separated list of tactics you already tried. E.g., "
            "'click [5] twice, scroll-and-retry, switch to XPath selector'."
        ),
        required=["reason", "failed_tactics"],
    )
)
class BrowserRequestHelpTool(Tool):
    """Escape hatch: worker signals 'I'm stuck' with structured context.

    Writes a resumption artifact so the orchestrator can spin up a new
    worker that RESUMES on the same live Puppeteer session with a
    different tactic — instead of starting from scratch.

    The worker should call `done(success=False, final_answer=...)` on the
    next turn after calling this tool.
    """

    name = "browser_request_help"
    description = (
        "Call this when you're stuck and a fresh tactic is needed. "
        "Writes structured state so the orchestrator can delegate a "
        "SUCCESSOR worker that resumes on the SAME live browser session "
        "with knowledge of what failed. "
        "After calling this tool, call done(success=False) with a short "
        "explanation — do NOT keep trying the same tactics."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, reason: str, failed_tactics: str, **kw: Any) -> str:
        # Lazy-import to avoid circular imports with the orchestrator module.
        from superbrowser_bridge.routing import _domain_from_url
        domain = _domain_from_url(self.s.current_url) if self.s.current_url else ""
        saved = save_resumption_artifact(
            self.s, domain,
            help_reason=reason,
            help_failed_tactics=failed_tactics,
        )
        self.s.record_step(
            "browser_request_help",
            reason[:80],
            f"saved={saved} session={self.s.session_id}",
        )
        hint = (
            "[HELP REQUESTED] Resumption state saved. "
            "Now call done(success=False, final_answer='Need different tactic: ...') "
            "with a ≤30-word summary. "
            "The orchestrator will delegate a fresh worker that resumes on this "
            "same browser session with your failed tactics excluded."
        ) if saved else (
            "[HELP REQUEST NOT SAVED] session_id or current_url is empty — "
            "resumption artifact could not be written. Proceed with done(success=False) "
            "and explain the blocker in final_answer."
        )
        return hint


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID to escalate"),
        reason=StringSchema(
            "Short reason for escalation (logged into per-domain learnings).",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserEscalateTool(Tool):
    """Migrate a Tier-1 session to Tier-3 (undetected Chromium).

    Exports the current URL + cookies + localStorage + sessionStorage
    from the t1 session, closes it, opens a fresh t3 session with those
    pre-loaded, navigates back to the same URL. From the LLM's POV the
    session_id changes; all subsequent tool calls route transparently to
    the new backend.

    Typically fired by the worker hook when `network_blocked=True` or
    vision detects a captcha; can also be called explicitly when the LLM
    wants to pre-emptively route through undetected Chromium.
    """

    name = "browser_escalate"
    description = (
        "Escalate a Tier-1 session to Tier-3 (undetected Chromium for "
        "Akamai/DataDome/PerimeterX). Preserves URL + cookies + "
        "localStorage. Form state resets — re-fill any in-progress inputs. "
        "One-way within a task. Returns the new t3 session_id."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, reason: str | None = None, force: bool = False, **kw: Any) -> str:
        if self.s.backend == "t3":
            return (
                f"[already_t3] Session {session_id} is already on Tier 3 "
                f"(backend={self.s.backend}). No escalation needed."
            )
        if not self.s.session_id or self.s.session_id != session_id:
            return (
                f"[session_mismatch] Requested session_id={session_id}, "
                f"active session_id={self.s.session_id}. Not escalating."
            )

        # --- Validation: refuse to escalate a session that isn't blocked ---
        # Observed failure mode (2026-04-19): the LLM calls
        # `browser_escalate(reason="403 Forbidden")` when browser_wait_for
        # merely TIMED OUT on a slow-rendering SPA — no 403 ever occurred.
        # The escalation then tears down the t1 session, reopens on t3, and
        # the t3 session re-encounters the same slow page, chaining more
        # spurious guesses. Refuse escalation unless we have concrete
        # evidence the session is blocked.
        last_status = self.s.last_network_status
        has_evidence = (
            bool(self.s.network_blocked)
            or (last_status is not None and last_status >= 400 and last_status != 404)
        )
        # Also accept evidence from vision: if the last vision pass flagged
        # a captcha, escalation is justified.
        vresp = getattr(self.s, "_last_vision_response", None)
        vflags = getattr(vresp, "flags", None) if vresp is not None else None
        if vflags is not None and bool(getattr(vflags, "captcha_present", False)):
            has_evidence = True

        if not has_evidence and not force:
            self.s.record_step(
                "browser_escalate", session_id,
                f"REFUSED: no block evidence (status={last_status}, "
                f"network_blocked={self.s.network_blocked}, reason={reason!r})",
            )
            return (
                f"[escalate_rejected] Session {session_id} is NOT actually "
                f"blocked. network_blocked={self.s.network_blocked}, "
                f"last_status={last_status or 'OK'}, url={self.s.current_url}. "
                f"The reason you gave ({reason!r}) is not reflected in any "
                f"tool output. Common cause: a slow-rendering SPA timing out "
                f"browser_wait_for. DO NOT confabulate failure reasons.\n"
                f"Instead:\n"
                f"  - browser_screenshot to see the actual page state.\n"
                f"  - browser_wait_for with a longer timeout (e.g. 20-30s) "
                f"or a different selector that matches what actually renders.\n"
                f"  - browser_run_script to inspect document.readyState / "
                f"document.body.innerHTML.length.\n"
                f"If you truly observe HTTP 4xx/5xx, 'Access Denied', a "
                f"visible captcha widget, or bot-wall prose in a screenshot, "
                f"call this tool again with force=true."
            )

        # --- 1. Snapshot state from the t1 session ---------------------
        t1_url = self.s.current_url or ""
        cookies: list[dict] = []
        local_storage: dict[str, str] = {}
        session_storage: dict[str, str] = {}

        try:
            ev = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": (
                    "(() => ({"
                    "localStorage: Object.fromEntries(Object.entries(localStorage)),"
                    "sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),"
                    "url: location.href,"
                    "}))()"
                )},
                timeout=15.0,
            )
            if ev.status_code == 200:
                data = ev.json()
                result = data.get("result") if isinstance(data, dict) else None
                if isinstance(result, dict):
                    local_storage = result.get("localStorage") or {}
                    session_storage = result.get("sessionStorage") or {}
                    if not t1_url:
                        t1_url = result.get("url") or ""
        except Exception as exc:
            print(f"  [escalate] localStorage export failed: {exc}")

        try:
            ck = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/script",
                json={"code": "return await page.cookies();"},
                timeout=15.0,
            )
            if ck.status_code == 200:
                payload = ck.json()
                if isinstance(payload, dict):
                    out = payload.get("output") or []
                    if isinstance(out, list):
                        cookies = [c for c in out if isinstance(c, dict)]
        except Exception as exc:
            print(f"  [escalate] cookie export failed: {exc}")

        # --- 2. Close the t1 session ------------------------------------
        try:
            await _request_with_backoff(
                "DELETE",
                f"{SUPERBROWSER_URL}/session/{session_id}",
                timeout=10.0,
            )
        except Exception as exc:
            print(f"  [escalate] t1 close failed (ignored): {exc}")

        # --- 3. Record tier-1 failure in the learning system ------------
        try:
            from urllib.parse import urlparse as _urlparse
            from superbrowser_bridge.routing import _record_routing_outcome
            host = _urlparse(t1_url).hostname or ""
            if host:
                _record_routing_outcome(
                    host, "browser", False, tier=1,
                    block_class="escalated:" + (reason or "unspecified"),
                )
        except Exception:
            pass

        # --- 4. Open a fresh t3 session with imported state -------------
        from superbrowser_bridge.antibot import interactive_session as _t3mgr
        try:
            import_state = {
                "cookies": cookies,
                "localStorage": local_storage,
                "sessionStorage": session_storage,
            }
            data = await _t3mgr.default().open(
                t1_url or None,
                task_id=self.s.task_id,
                import_state=import_state,
                timeout_s=45.0,
            )
        except Exception as exc:
            return (
                f"[escalate_failed] Tier-3 open failed: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

        new_sid = data.get("sessionId", "")
        self.s.session_id = new_sid
        self.s.network_blocked = False
        self.s.consecutive_click_calls = 0
        # Legacy idempotency guard sees a fresh session.
        self.s.blocked_browser_open_count = 0
        self.s.log_activity(f"escalate(t1->t3 reason={reason or '?'})", f"new_sid={new_sid}")
        self.s.record_step("browser_escalate", t1_url, f"reason={reason or 'unspecified'} new_sid={new_sid}")

        return (
            f"[escalated_to_t3] Session migrated to Tier 3 (undetected "
            f"Chromium). new_session_id={new_sid} url={data.get('url', t1_url)} "
            f"cookies_imported={len(cookies)} localStorage_keys={len(local_storage)} "
            f"reason={reason or 'unspecified'}\n"
            f"IMPORTANT: form inputs were reset during escalation. Re-fill any "
            f"in-progress form before submitting. All subsequent browser_* "
            f"tools use the new session_id transparently."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        target_text=StringSchema(
            "Text or regex of the element you want to scroll to. Substring "
            "match if it's not a valid regex. Optional if target_role given.",
            nullable=True,
        ),
        target_role=StringSchema(
            "ARIA role / tagName to filter on (e.g. 'button', 'h2'). "
            "Optional if target_text given.",
            nullable=True,
        ),
        direction=StringSchema(
            "'down' (default) or 'up'.",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on scroll steps. Default 10, max 40.",
            nullable=True,
        ),
        step_ratio=NumberSchema(
            description="Fraction of viewport to scroll per step (0.1–1.0). Default 0.8.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScrollUntilTool(Tool):
    name = "browser_scroll_until"
    description = (
        "Closed-loop scroll. Walks the page in `direction` until an "
        "element matching `target_text` (substring or regex) and/or "
        "`target_role` becomes visible, the page can't scroll further, "
        "or `max_iterations` elapses. Cheap — uses interactive-element "
        "polling between steps, no screenshot per iteration. Returns a "
        "structured outcome with `reason` ('matched' | 'page_end' | "
        "'page_start' | 'max_iterations') so the brain knows whether "
        "to act, retreat, or give up. Prefer this over browser_scroll "
        "when you know what you're scrolling toward — it stops at the "
        "right place AND tells you when content runs out, instead of "
        "blindly scrolling and re-screenshotting."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        target_text: str | None = None,
        target_role: str | None = None,
        direction: str | None = None,
        max_iterations: int | None = None,
        step_ratio: float | None = None,
        **kw: Any,
    ) -> Any:
        gate = await _feedback_gate("browser_scroll_until")
        if gate:
            return gate

        if not (target_text and target_text.strip()) and not (target_role and target_role.strip()):
            return (
                "[scroll_until_failed:no_target] Provide target_text or "
                "target_role. Substring match works for most cases — pass "
                "the visible text of the element you want to find."
            )

        payload: dict[str, Any] = {
            "direction": direction or "down",
        }
        if target_text and target_text.strip():
            payload["targetText"] = target_text.strip()
        if target_role and target_role.strip():
            payload["targetRole"] = target_role.strip()
        if max_iterations is not None:
            payload["maxIterations"] = int(max_iterations)
        if step_ratio is not None:
            payload["stepRatio"] = float(step_ratio)

        target_disp = target_text or f"role={target_role}"
        print(
            f"\n>> browser_scroll_until({target_disp!r}, dir={payload['direction']})"
        )

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll-until",
                json=payload,
                timeout=30.0,  # closed-loop can take 10 iterations × ~300ms
            )
        except Exception as exc:
            return f"[scroll_until_failed] request error: {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[scroll_until_failed] HTTP {r.status_code}: {err}"

        data = r.json()
        outcome = data.get("outcome") or {}
        reason = str(outcome.get("reason") or "unknown")
        iters = int(outcome.get("iterations") or 0)
        scrolled = int(outcome.get("scrolledPx") or 0)

        # Update scroll telemetry so the next vision pass sees a fresh
        # [SCROLL_STATE …] line including reached_bottom/reached_top hints
        # that came from this closed-loop call.
        _update_scroll_telemetry(
            self.s,
            data.get("scrollInfo"),
            payload["direction"],
            extra={
                "last_scroll_reason": reason,
                "reached_bottom": reason == "page_end",
                "reached_top": reason == "page_start",
            },
        )

        # Mirror the BrowserDragSliderUntilTool record convention so
        # step_history shows a clear summary line for downstream
        # loop-detection and task-graph signal evaluation.
        self.s.record_step(
            "browser_scroll_until",
            f"{target_disp!r} → {reason} in {iters} iters ({scrolled}px)",
            data.get("url", ""),
        )

        lines: list[str] = []
        if outcome.get("found"):
            matched = outcome.get("matchedText") or ""
            sel = outcome.get("matchedSelector") or ""
            lines.append(
                f"FOUND {target_disp!r} after {iters} iter(s), "
                f"scrolled {scrolled}px. matched={matched[:80]!r} "
                f"selector={sel}"
            )
        else:
            tag = (
                "page_end" if reason == "page_end"
                else "page_start" if reason == "page_start"
                else reason
            )
            lines.append(
                f"[scroll_until_failed:{tag}] target {target_disp!r} not "
                f"found after {iters} iter(s) ({scrolled}px). "
                f"reason={reason}."
            )
            if reason == "page_end":
                lines.append(
                    "  Page can't scroll further down. The target may be "
                    "above (try direction='up') or may not exist on this "
                    "page — verify by checking the elements list below."
                )
            elif reason == "page_start":
                lines.append(
                    "  Already at top of page. Try direction='down' or "
                    "verify the target text/role is correct."
                )
            elif reason == "max_iterations":
                lines.append(
                    "  Hit iteration cap. If you believe the target exists "
                    "further on, raise max_iterations (cap is 40) or "
                    "use a more specific target_text."
                )

        if data.get("elements"):
            lines.append(str(data["elements"]))

        # Schedule a vision prefetch so the next browser_screenshot is
        # cached — same convention as the other scroll tools.
        self.s.advance_observation_token("scroll_until")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task, "\n".join(lines),
            state=self.s,
        )


def _update_scroll_telemetry(
    state: "BrowserSessionState",
    scroll_info: Any,
    direction: str | None,
    extra: dict | None = None,
) -> None:
    """Record post-scroll geometry on the session state.

    Read by `_format_state` (and the [SCROLL_STATE …] caption line in
    `build_text_only`) so vision can reason about whether more scrolling
    is plausible. Tolerant of missing scrollInfo — telemetry is best-
    effort and must not break the tool path.
    """
    try:
        if not isinstance(scroll_info, dict):
            scroll_info = {}
        scroll_y = int(scroll_info.get("scrollY") or 0)
        scroll_h = int(scroll_info.get("scrollHeight") or 0)
        vp_h = int(scroll_info.get("viewportHeight") or 0)
        # 12px of slack at the bottom catches off-by-one rounding without
        # falsely flagging "reached_bottom" mid-page.
        reached_bottom = scroll_h > 0 and (scroll_y + vp_h) >= (scroll_h - 12)
        reached_top = scroll_y <= 4
        prev = getattr(state, "scroll_telemetry", None) or {}
        history = list(prev.get("direction_history") or [])
        if direction:
            history.append(direction)
            history = history[-6:]
        tel = {
            "scrollY": scroll_y,
            "scrollHeight": scroll_h,
            "viewportHeight": vp_h,
            "direction_history": history,
            "reached_bottom": reached_bottom,
            "reached_top": reached_top,
        }
        if extra:
            tel.update(extra)
        state.scroll_telemetry = tel
    except Exception:
        # Telemetry is best-effort — never let it block the scroll tool.
        pass


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)

class BrowserRewindToCheckpointTool(Tool):
    """Bail from the current page state back to the last known-good URL.

    Session memory escape hatch: when the worker has exhausted local
    retries and the brain is stuck, rewinding to `best_checkpoint_url`
    (the last meaningful checkpoint the worker recorded) lets the plan
    re-approach with a fresh vision pass. The tool forces the token
    forward + busts the vision cache so the next mutation blocks on
    genuinely fresh bboxes rather than any lingering cache from the
    stuck-state page.
    """

    name = "browser_rewind_to_checkpoint"
    description = (
        "Navigate back to the last known-good checkpoint URL when the "
        "current page is unresponsive or the plan is stuck. Invalidates "
        "vision cache + element fingerprints so the next vision pass "
        "reflects the rewound page."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        target = (self.s.best_checkpoint_url or "").strip()
        print(f"\n>> browser_rewind_to_checkpoint(target={target[:80]!r})")
        if not target:
            return (
                "[rewind_failed:no_checkpoint] No best_checkpoint_url has "
                "been recorded for this session. Call browser_navigate "
                "directly with a URL you know works, or report failure."
            )

        # Advance token FIRST — any vision prefetch already in flight
        # will see the mismatch and drop its write; the next mutation
        # cannot unblock on stale bboxes.
        self.s.advance_observation_token("rewind")

        # Clear local fingerprints/vision state so `vision_is_fresh()`
        # cannot be true until a brand new pass lands.
        self.s.element_fingerprints.clear()
        self.s._last_vision_response = None
        self.s._last_vision_ts = 0.0
        self.s._last_vision_url = ""

        # Bust the shared vision cache for this session so a replay
        # won't resurrect the stuck-state bboxes.
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
            if vision_agent_enabled():
                agent = get_vision_agent()
                cache = getattr(agent, "_cache", None)
                if cache is not None and hasattr(cache, "bust_session"):
                    await cache.bust_session(session_id)
        except Exception as exc:
            print(f"  [rewind: vision cache bust skipped — {exc}]")

        # Navigate via the TS server — same endpoint BrowserNavigateTool
        # uses. Skipping the full BrowserNavigateTool path to avoid
        # re-triggering domain-pinning / CF guards that apply to forward
        # nav but not to a known-good rewind URL.
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
                json={"url": target, "waitUntil": "domcontentloaded"},
                timeout=30.0,
            )
        except Exception as exc:
            return f"[rewind_failed:network] {exc}"
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[rewind_failed:http_{r.status_code}] {err}"

        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        self.s.record_url(target)
        self.s.record_step(
            "browser_rewind_to_checkpoint",
            f"→ {target[:80]}",
            f"title={data.get('title', '?')}" if isinstance(data, dict) else "ok",
        )
        self.s.log_activity(f"rewind → {target[:60]}")

        # Fresh prefetch on the rewound page so the next mutation's gate
        # unblocks quickly rather than hitting a cold cache.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(
                data if isinstance(data, dict) else {},
                f"Rewound to checkpoint: {target[:80]}",
            ),
            state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        startX=NumberSchema("Start X coordinate"),
        startY=NumberSchema("Start Y coordinate"),
        endX=NumberSchema("End X coordinate"),
        endY=NumberSchema("End Y coordinate"),
        steps=IntegerSchema("Number of intermediate steps (default 25, higher = smoother)", nullable=True),
        required=["session_id", "startX", "startY", "endX", "endY"],
    )
)

class BrowserGetRectTool(Tool):
    name = "browser_get_rect"
    description = (
        "Return getBoundingClientRect() for one or more CSS selectors. "
        "Pixel-exact, zero vision cost. Use to derive coordinates before "
        "calling browser_click_selector / browser_drag_selectors. "
        "Selectors ride as a JSON string (no ArraySchema in this layer)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        selectors_json: str,
        ensure_visible: bool | None = None,
        **kw: Any,
    ) -> str:
        try:
            selectors = json.loads(selectors_json)
        except (TypeError, ValueError) as exc:
            return f"[get_rect_failed] selectors_json is not valid JSON: {exc}"
        if not isinstance(selectors, list) or not all(isinstance(s, str) for s in selectors):
            return "[get_rect_failed] selectors_json must decode to a list of strings."

        print(f"\n>> browser_get_rect({len(selectors)} selectors)")
        payload = {"selectors": selectors, "ensureVisible": bool(ensure_visible)}
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/rect",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        rects = data.get("rects") or []
        lines = ["Selector rects:"]
        for sel, rect in zip(selectors, rects):
            if rect is None:
                lines.append(f"  {sel} → NOT FOUND")
                continue
            lines.append(
                f"  {sel} → cx={rect['cx']:.1f} cy={rect['cy']:.1f} "
                f"w={rect['w']:.1f} h={rect['h']:.1f} "
                f"visible={rect['visible']} inViewport={rect['inViewport']}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema("CSS selector of the element to click"),
        button=StringSchema("Mouse button: left|right|middle", nullable=True),
        click_count=IntegerSchema("Number of clicks (1 for single, 2 for double)", nullable=True),
        linear=BooleanSchema(
            description=(
                "If true (default), use deterministic teleport click (pixel-exact). "
                "Set false for stealth-critical contexts (captchas) that need Bezier humanisation."
            ),
            nullable=True,
        ),
        required=["session_id", "selector"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema("CSS selector of the element to click"),
        button=StringSchema("Mouse button: left|right|middle", nullable=True),
        click_count=IntegerSchema("Number of clicks (1 for single, 2 for double)", nullable=True),
        linear=BooleanSchema(
            description=(
                "If true (default), use deterministic teleport click (pixel-exact). "
                "Set false for stealth-critical contexts (captchas) that need Bezier humanisation."
            ),
            nullable=True,
        ),
        required=["session_id", "selector"],
    )
)
class BrowserClickSelectorTool(Tool):
    name = "browser_click_selector"
    description = (
        "Click the centre of a DOM element by CSS selector. Pixel-exact, "
        "zero Gemini cost. PREFER OVER browser_click_at(vision_index=...) "
        "whenever the target has a stable hook — chess squares "
        "(.square-54), form fields (#email), buttons with data-test-id, "
        "captcha handles. Fails fast if the selector is missing or zero-size."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        selector: str,
        button: str | None = None,
        click_count: int | None = None,
        linear: bool | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_click_selector({selector!r})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls += 1

        payload: dict[str, Any] = {"selector": selector, "ensureVisible": True}
        if button is not None:
            payload["button"] = button
        if click_count is not None:
            payload["clickCount"] = click_count
        if linear is not None:
            payload["linear"] = linear

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/click-selector",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[click_selector_failed] {err}"
        data = r.json()
        clicked = data.get("clicked", {})
        self.s.record_step(
            "browser_click_selector",
            f"{selector} @ ({clicked.get('x','?')},{clicked.get('y','?')})",
            data.get("url", ""),
        )
        # click_selector is a mutation — advance the observation token
        # and schedule a vision prefetch so the next screenshot is warm.
        self.s.advance_observation_token("click_selector")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        caption = (
            f"Clicked {selector} at "
            f"({clicked.get('x','?')},{clicked.get('y','?')})"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return await _append_fresh_vision(
            _vision_task,
            _maybe_no_effect_prefix(
                data, "browser_click_selector", caption,
                session_state=self.s,
            ),
            state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        from_selector=StringSchema("CSS selector of the drag source element"),
        to_selector=StringSchema("CSS selector of the drag destination element"),
        method=StringSchema(
            "One of 'auto' (default: try click_click, fall back to drag), "
            "'click_click' (two discrete clicks — more robust for chess/grid), "
            "'drag' (mousedown → move → mouseup — classical drag). ",
            nullable=True,
        ),
        hold_ms=IntegerSchema(
            "Milliseconds to pause between the two clicks when method=click_click, "
            "or to hold before drag start. Default 120.",
            nullable=True,
        ),
        linear=BooleanSchema(
            description="If true (default), deterministic paths. Set false for stealth-critical drags.",
            nullable=True,
        ),
        required=["session_id", "from_selector", "to_selector"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        from_selector=StringSchema("CSS selector of the drag source element"),
        to_selector=StringSchema("CSS selector of the drag destination element"),
        method=StringSchema(
            "One of 'auto' (default: try click_click, fall back to drag), "
            "'click_click' (two discrete clicks — more robust for chess/grid), "
            "'drag' (mousedown → move → mouseup — classical drag). ",
            nullable=True,
        ),
        hold_ms=IntegerSchema(
            "Milliseconds to pause between the two clicks when method=click_click, "
            "or to hold before drag start. Default 120.",
            nullable=True,
        ),
        linear=BooleanSchema(
            description="If true (default), deterministic paths. Set false for stealth-critical drags.",
            nullable=True,
        ),
        required=["session_id", "from_selector", "to_selector"],
    )
)
class BrowserDragSelectorsTool(Tool):
    name = "browser_drag_selectors"
    description = (
        "Drag from one CSS-selected element to another. Pixel-exact. "
        "Default method 'auto' tries click-click first (safer on react-dnd "
        "and grid boards like chess.com) and falls back to classical drag "
        "if the DOM didn't mutate. PREFER OVER browser_drag(x1,y1,x2,y2) "
        "whenever both endpoints have stable selectors."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        from_selector: str,
        to_selector: str,
        method: str | None = None,
        hold_ms: int | None = None,
        linear: bool | None = None,
        **kw: Any,
    ) -> str:
        method = method or "auto"
        if method not in ("auto", "click_click", "drag"):
            return f"[drag_selectors_failed] method must be auto|click_click|drag, got {method!r}"
        print(f"\n>> browser_drag_selectors({from_selector!r} → {to_selector!r}, method={method})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "fromSelector": from_selector,
            "toSelector": to_selector,
            "method": method,
        }
        if hold_ms is not None:
            payload["holdMs"] = hold_ms
        if linear is not None:
            payload["linear"] = linear

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-selectors",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_selectors_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {})
        self.s.record_step(
            "browser_drag_selectors",
            f"{from_selector}→{to_selector} via {outcome.get('methodUsed','?')}",
            data.get("url", ""),
        )
        frm = outcome.get("from", {})
        to = outcome.get("to", {})
        caption = (
            f"Dragged {from_selector} → {to_selector} "
            f"via {outcome.get('methodUsed','?')} "
            f"({frm.get('x','?')},{frm.get('y','?')}) → ({to.get('x','?')},{to.get('y','?')}) "
            f"mutated={outcome.get('mutated', False)}"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        points_json=StringSchema(
            "JSON array of {x, y} points, e.g. '[{\"x\":100,\"y\":200},{\"x\":150,\"y\":220}]'. "
            "At least two points required."
        ),
        step_ms=IntegerSchema(
            "Milliseconds between intermediate mouseMove events. Default 16 (~60fps).",
            nullable=True,
        ),
        hold_ms=IntegerSchema("Pre-press hold duration at points[0]. Default 50.", nullable=True),
        button=StringSchema("Mouse button: left|right|middle. Default left.", nullable=True),
        required=["session_id", "points_json"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        points_json=StringSchema(
            "JSON array of {x, y} points, e.g. '[{\"x\":100,\"y\":200},{\"x\":150,\"y\":220}]'. "
            "At least two points required."
        ),
        step_ms=IntegerSchema(
            "Milliseconds between intermediate mouseMove events. Default 16 (~60fps).",
            nullable=True,
        ),
        hold_ms=IntegerSchema("Pre-press hold duration at points[0]. Default 50.", nullable=True),
        button=StringSchema("Mouse button: left|right|middle. Default left.", nullable=True),
        required=["session_id", "points_json"],
    )
)
class BrowserDragPathTool(Tool):
    name = "browser_drag_path"
    description = (
        "Drag along an arbitrary polyline of (x,y) points. For jigsaw "
        "captcha traces, connect-the-dots, signature drawing, or any "
        "free-form gesture where a straight start→end drag won't work."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        points_json: str,
        step_ms: int | None = None,
        hold_ms: int | None = None,
        button: str | None = None,
        **kw: Any,
    ) -> str:
        try:
            points = json.loads(points_json)
        except (TypeError, ValueError) as exc:
            return f"[drag_path_failed] points_json is not valid JSON: {exc}"
        if not isinstance(points, list) or len(points) < 2:
            return "[drag_path_failed] points_json must decode to a list of ≥2 {x,y} objects."
        for i, p in enumerate(points):
            if not isinstance(p, dict) or not isinstance(p.get("x"), (int, float)) \
               or not isinstance(p.get("y"), (int, float)):
                return f"[drag_path_failed] point[{i}] must be {{x: number, y: number}}"

        print(f"\n>> browser_drag_path({len(points)} points)")
        self.s.actions_since_screenshot += 1

        payload: dict[str, Any] = {"points": points}
        if step_ms is not None:
            payload["stepMs"] = step_ms
        if hold_ms is not None:
            payload["holdMs"] = hold_ms
        if button is not None:
            payload["button"] = button

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-path",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_path_failed] {err}"
        data = r.json()
        self.s.record_step(
            "browser_drag_path",
            f"{len(points)} points",
            data.get("url", ""),
        )
        caption = f"Dragged along polyline of {len(points)} points"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema(
            "CSS selector for the slider element (e.g. 'input[type=range][name=monthlyContribution]'). "
            "Frame-aware: the backend probes every frame on the page."
        ),
        value_json=StringSchema(
            "Target value as JSON. Examples: '300' for a single slider, '[25, 75]' for a "
            "dual-thumb range. Values are absolute (use the slider's own units) unless "
            "as='ratio' is set, in which case they are 0.0–1.0 positions along the track."
        ),
        value_mode=StringSchema(
            "Value interpretation: 'absolute' (default; matches the slider's own min/max) "
            "or 'ratio' (0.0-1.0 position along the track).",
            nullable=True,
        ),
        method=StringSchema(
            "Strategy: 'auto' (default), 'range-input' (direct value+input event), "
            "'keyboard' (focus + arrow keys), 'drag' (pixel drag). Use 'auto' unless debugging.",
            nullable=True,
        ),
        required=["session_id", "selector", "value_json"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema(
            "CSS selector for the slider element (e.g. 'input[type=range][name=monthlyContribution]'). "
            "Frame-aware: the backend probes every frame on the page."
        ),
        value_json=StringSchema(
            "Target value as JSON. Examples: '300' for a single slider, '[25, 75]' for a "
            "dual-thumb range. Values are absolute (use the slider's own units) unless "
            "as='ratio' is set, in which case they are 0.0–1.0 positions along the track."
        ),
        value_mode=StringSchema(
            "Value interpretation: 'absolute' (default; matches the slider's own min/max) "
            "or 'ratio' (0.0-1.0 position along the track).",
            nullable=True,
        ),
        method=StringSchema(
            "Strategy: 'auto' (default), 'range-input' (direct value+input event), "
            "'keyboard' (focus + arrow keys), 'drag' (pixel drag). Use 'auto' unless debugging.",
            nullable=True,
        ),
        required=["session_id", "selector", "value_json"],
    )
)
class BrowserSetSliderTool(Tool):
    name = "browser_set_slider"
    description = (
        "Set a slider's value by number. Works for native <input type=range>, "
        "ARIA sliders (role=slider / aria-valuenow), and CSS-custom widgets. "
        "Prefer this over browser_drag for sliders — it auto-picks the most "
        "reliable strategy and crosses iframe boundaries. For dual-thumb "
        "sliders (e.g. an age range) pass value_json='[lo, hi]'. Returns the "
        "strategy used plus before/after values so you can verify the slide."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        selector: str,
        value_json: str,
        value_mode: str | None = None,
        method: str | None = None,
        **kw: Any,
    ) -> str:
        try:
            parsed = json.loads(value_json)
        except (TypeError, ValueError) as exc:
            return f"[set_slider_failed] value_json is not valid JSON: {exc}"
        if isinstance(parsed, (int, float)):
            value_payload: Any = float(parsed)
        elif (
            isinstance(parsed, list)
            and len(parsed) == 2
            and all(isinstance(n, (int, float)) for n in parsed)
        ):
            value_payload = [float(parsed[0]), float(parsed[1])]
        else:
            return "[set_slider_failed] value_json must decode to a number or [lo, hi] list"

        print(f"\n>> browser_set_slider({selector!r} → {value_payload})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {"selector": selector, "value": value_payload}
        if value_mode is not None:
            payload["as"] = value_mode
        if method is not None:
            payload["method"] = method

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[set_slider_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {}) or {}
        strategy = outcome.get("strategy", "?")
        before = outcome.get("before")
        after = outcome.get("after")
        err = outcome.get("error")
        self.s.record_step(
            "browser_set_slider",
            f"{selector} → {value_payload} via {strategy}",
            data.get("url", ""),
        )
        if strategy == "unresolved" or err:
            return f"[set_slider_failed] {err or 'unresolved'} (selector={selector})"
        caption = (
            f"Set slider {selector} via {strategy}: {before} → {after} "
            f"(min={outcome.get('min')}, max={outcome.get('max')})"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE (the "
                "draggable thumb), as shown in the latest screenshot's "
                "`V_n` listing. The tool automatically finds the adjacent "
                "slider_widget bbox (the track) for target-x computation."
            ),
        ),
        value=NumberSchema(
            description=(
                "Target value. Interpreted per value_mode. For 'absolute' "
                "pass the actual reading (e.g. 300 for $300/month); the "
                "tool reads min/max from the adjacent rendered label text. "
                "For 'ratio' pass 0.0–1.0 position along the track."
            ),
        ),
        value_mode=StringSchema(
            "'absolute' (default) or 'ratio'. Use 'ratio' when you can't "
            "read the min/max from the page.",
            nullable=True,
        ),
        value_min=NumberSchema(
            "Override for the slider's minimum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        value_max=NumberSchema(
            "Override for the slider's maximum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        required=["session_id", "vision_index", "value"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE (the "
                "draggable thumb), as shown in the latest screenshot's "
                "`V_n` listing. The tool automatically finds the adjacent "
                "slider_widget bbox (the track) for target-x computation."
            ),
        ),
        value=NumberSchema(
            description=(
                "Target value. Interpreted per value_mode. For 'absolute' "
                "pass the actual reading (e.g. 300 for $300/month); the "
                "tool reads min/max from the adjacent rendered label text. "
                "For 'ratio' pass 0.0–1.0 position along the track."
            ),
        ),
        value_mode=StringSchema(
            "'absolute' (default) or 'ratio'. Use 'ratio' when you can't "
            "read the min/max from the page.",
            nullable=True,
        ),
        value_min=NumberSchema(
            "Override for the slider's minimum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        value_max=NumberSchema(
            "Override for the slider's maximum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        required=["session_id", "vision_index", "value"],
    )
)
class BrowserSetSliderAtTool(Tool):
    name = "browser_set_slider_at"
    description = (
        "Drag a slider to a target value using its VISION bbox index. "
        "Prefer this over browser_set_slider when the page uses custom "
        "slider widgets (Chase/JPM calculators, filter ranges, any "
        "React/Angular slider with no native range input or aria-valuenow). "
        "Workflow: (1) call browser_screenshot → the vision agent emits "
        "role=slider_handle / slider_widget / text_block bboxes per "
        "slider; (2) pick the V_n of the HANDLE you want to move; (3) "
        "call this tool with the target numeric value. The tool finds "
        "the adjacent track, dispatches a humanised bezier drag, and "
        "returns the post-drag rendered label text so you can verify."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int,
        value: float,
        value_mode: str | None = None,
        value_min: float | None = None,
        value_max: float | None = None,
        **kw: Any,
    ) -> str:
        # Share the same lock as browser_drag_slider_until: the CDP/patchright
        # cursor is session-scoped, parallel drags clobber each other.
        if self.s.slider_drag_lock is None:
            self.s.slider_drag_lock = asyncio.Lock()
        async with self.s.slider_drag_lock:
            return await self._execute_inner(
                session_id, vision_index, value,
                value_mode, value_min, value_max,
            )

    async def _execute_inner(
        self,
        session_id: str,
        vision_index: int,
        value: float,
        value_mode: str | None,
        value_min: float | None,
        value_max: float | None,
    ) -> str:
        print(
            f"\n>> browser_set_slider_at(V{vision_index} → {value} "
            f"mode={value_mode or 'absolute'})"
        )
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        resp = self.s.vision_for_target_resolution()
        if resp is None:
            return (
                "[set_slider_at_failed:no_vision] No recent vision response. "
                "Call browser_screenshot first so slider bboxes are indexed."
            )

        handle_bbox = resp.get_bbox(int(vision_index))
        if handle_bbox is None:
            return (
                f"[set_slider_at_failed:bad_vision_index] V{vision_index} "
                f"is out of range (only {len(resp.bboxes)} bboxes cached)."
            )
        handle_role = (getattr(handle_bbox, "role", "") or "").lower()
        if handle_role not in ("slider_handle", "slider", "input"):
            # Accept other roles loudly but keep going — vision may
            # mis-tag a handle as 'input' or 'other'.
            print(
                f"   (note: V{vision_index} role={handle_role!r}, "
                "expected slider_handle — continuing)"
            )

        iw, ih = resp.image_width, resp.image_height
        if iw <= 0 or ih <= 0:
            return "[set_slider_at_failed:no_image_dims] Re-screenshot first."
        dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
        hx0, hy0, hx1, hy1 = handle_bbox.to_pixels(iw, ih, dpr=dpr_val)
        handle_pix = {"x": hx0, "y": hy0, "w": hx1 - hx0, "h": hy1 - hy0}
        handle_cy = hy0 + (hy1 - hy0) / 2.0

        # Find the associated track: nearest role=slider_widget whose
        # vertical centre sits within ±handle.h of the handle centre and
        # whose horizontal span encloses the handle.
        track_pix: dict[str, int] | None = None
        track_bbox = None
        best_dy: float | None = None
        for cand in resp.bboxes:
            if (getattr(cand, "role", "") or "").lower() != "slider_widget":
                continue
            cx0, cy0, cx1, cy1 = cand.to_pixels(iw, ih, dpr=dpr_val)
            ccy = cy0 + (cy1 - cy0) / 2.0
            dy = abs(ccy - handle_cy)
            if dy > max(hy1 - hy0, 24):
                continue
            if cx0 > hx0 or cx1 < hx1:
                # Track must enclose the handle horizontally.
                # (Handles at either extreme still sit within the track
                # bounds because the track includes min→max span.)
                continue
            if best_dy is None or dy < best_dy:
                best_dy = dy
                track_bbox = cand
                track_pix = {
                    "x": cx0, "y": cy0, "w": cx1 - cx0, "h": cy1 - cy0,
                }

        # Find the adjacent value-label text_block for min/max parsing +
        # before-readback. Heuristic: role=text_block whose centre Y is
        # within ±(handle.h + 40) of the handle's centre.
        label_text: str = ""
        for cand in resp.bboxes:
            if (getattr(cand, "role", "") or "").lower() != "text_block":
                continue
            cx0, cy0, cx1, cy1 = cand.to_pixels(iw, ih, dpr=dpr_val)
            ccy = cy0 + (cy1 - cy0) / 2.0
            if abs(ccy - handle_cy) <= (hy1 - hy0) + 40:
                label_text = (getattr(cand, "label", "") or "").strip()
                break

        mode = (value_mode or "absolute").lower()
        if mode == "ratio":
            ratio = max(0.0, min(1.0, float(value)))
        else:
            mn, mx = value_min, value_max
            if (mn is None or mx is None) and label_text:
                # Parse "0 to 10" / "$0 — $583" / "25 to 75" / "0 - 100"
                import re as _re
                nums = _re.findall(
                    r"-?\d+(?:\.\d+)?",
                    label_text.replace("$", "").replace("%", ""),
                )
                if len(nums) >= 2:
                    try:
                        parsed = [float(n) for n in nums[:2]]
                        mn = parsed[0] if mn is None else mn
                        mx = parsed[1] if mx is None else mx
                    except ValueError:
                        pass
            if mn is None or mx is None:
                return (
                    "[set_slider_at_failed:no_minmax] Cannot infer min/max "
                    "from adjacent label; pass value_min/value_max or use "
                    "value_mode='ratio'. label_seen="
                    + (repr(label_text) if label_text else "none")
                )
            span = float(mx) - float(mn)
            if abs(span) < 1e-9:
                ratio = 0.5
            else:
                ratio = (float(value) - float(mn)) / span
                ratio = max(0.0, min(1.0, ratio))

        if track_pix is None:
            # Fall back: use the handle's bbox as a tiny pseudo-track.
            # The drag still fires from the handle centre, but end = start,
            # so this is effectively a no-op. Return diagnostic.
            return (
                f"[set_slider_at_failed:no_track] Could not find a "
                f"role='slider_widget' bbox adjacent to V{vision_index}. "
                "Re-screenshot; if the issue persists, use browser_set_slider "
                "with a DOM selector instead."
            )

        payload = {"handle": handle_pix, "track": track_pix, "ratio": ratio}
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider-at",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[set_slider_at_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {}) or {}
        self.s.record_step(
            "browser_set_slider_at",
            f"V{vision_index} → {value} (ratio={ratio:.3f})",
            data.get("url", ""),
        )
        lines = [
            f"Dragged slider V{vision_index} to {value} "
            f"(ratio={ratio:.2f}) via vision-drag",
            f"  handle={outcome.get('handle_bbox')}",
            f"  track={outcome.get('track_bbox')}",
            f"  target_px={outcome.get('target_px')}",
        ]
        if label_text:
            lines.append(f"  label_before={label_text!r}")
        lines.append(
            "  NEXT: call browser_screenshot to read "
            "the rendered post-drag value label."
        )
        if data.get("elements"):
            lines.append(str(data["elements"]))
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)

class BrowserListSliderHandlesTool(Tool):
    name = "browser_list_slider_handles"
    description = (
        "Enumerate all slider handles on the page via DOM introspection "
        "(NO vision). Walks every frame, including cross-origin ones, "
        "and returns each handle's index, frame_url, kind, bbox (in "
        "document CSS pixels), and the closest row-level label text. "
        "Use this when vision is flaky or returning empty bboxes, or "
        "when you already know the slider's logical label (e.g. "
        "'Monthly contribution') and want to pick by text. Then pass "
        "the bbox directly into browser_drag_slider_until via the "
        "`handle_bbox` arg — skips the vision lookup entirely."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        print("\n>> browser_list_slider_handles()")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/list-slider-handles",
            json={},
            timeout=20.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[list_slider_handles_failed] {err}"
        data = r.json() or {}
        handles = data.get("handles") or []
        if not handles:
            return (
                "[list_slider_handles:empty] No slider handles found in "
                "any frame. Page may still be loading — scroll or wait, "
                "then retry. If the page clearly has sliders, they may "
                "use non-standard markup; fall back to vision via "
                "browser_screenshot."
            )
        lines = [f"Found {len(handles)} slider handle(s):"]
        for h in handles:
            lines.append(
                f"  [{h.get('index')}] kind={h.get('kind')} "
                f"bbox={h.get('bbox')} "
                f"label={h.get('label', '')!r} "
                f"frame={(h.get('frame_url') or '')[:80]}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE "
                "(the draggable thumb) from the most recent screenshot. "
                "Either this OR handle_bbox_json is required."
            ),
            nullable=True,
        ),
        handle_bbox_json=StringSchema(
            description=(
                "Alternative to vision_index: pass the handle bbox "
                "directly as a JSON object {\"x\":.., \"y\":.., \"w\":.., \"h\":..} "
                "in CSS pixel document coords. Use this when vision is "
                "unreliable — call browser_list_slider_handles to get "
                "bboxes straight from the DOM, then pass one here."
            ),
            nullable=True,
        ),
        label_hint=StringSchema(
            description=(
                "Alternative discovery: a substring of the slider's label "
                "(e.g. 'Monthly contribution'). The tool will call "
                "browser_list_slider_handles, pick the handle whose label "
                "contains this hint (case-insensitive, whitespace collapsed), "
                "and use its bbox. Use when neither vision_index nor "
                "handle_bbox_json is convenient."
            ),
            nullable=True,
        ),
        target_value=NumberSchema(
            description=(
                "The numeric value to slide to. The tool drags the handle "
                "while watching the rendered label text, stopping when the "
                "label shows this value (±tolerance)."
            ),
        ),
        label_pattern=StringSchema(
            description=(
                "JS regex matching the rendered label — the FIRST capture "
                "group must be the numeric value. Example for Chase: "
                "'Monthly contribution[^:]*:\\s*\\$?(\\d+(?:\\.\\d+)?)'. "
                "If omitted, matches any number in a text node on the same "
                "visual row as the handle (works for most sliders with a "
                "single value near the track)."
            ),
            nullable=True,
        ),
        tolerance=NumberSchema(
            "Allowed |target - observed| gap. Default 0 (exact match).",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on step iterations. Default 25.",
            nullable=True,
        ),
        step_px=IntegerSchema(
            "Initial pixel step. The tool auto-adapts from observed "
            "value-per-pixel sensitivity. Default 8.",
            nullable=True,
        ),
        direction=StringSchema(
            "'auto' (default; inferred from current vs target), 'left', 'right'.",
            nullable=True,
        ),
        required=["session_id", "target_value"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE "
                "(the draggable thumb) from the most recent screenshot. "
                "Either this OR handle_bbox_json is required."
            ),
            nullable=True,
        ),
        handle_bbox_json=StringSchema(
            description=(
                "Alternative to vision_index: pass the handle bbox "
                "directly as a JSON object {\"x\":.., \"y\":.., \"w\":.., \"h\":..} "
                "in CSS pixel document coords. Use this when vision is "
                "unreliable — call browser_list_slider_handles to get "
                "bboxes straight from the DOM, then pass one here."
            ),
            nullable=True,
        ),
        label_hint=StringSchema(
            description=(
                "Alternative discovery: a substring of the slider's label "
                "(e.g. 'Monthly contribution'). The tool will call "
                "browser_list_slider_handles, pick the handle whose label "
                "contains this hint (case-insensitive, whitespace collapsed), "
                "and use its bbox. Use when neither vision_index nor "
                "handle_bbox_json is convenient."
            ),
            nullable=True,
        ),
        target_value=NumberSchema(
            description=(
                "The numeric value to slide to. The tool drags the handle "
                "while watching the rendered label text, stopping when the "
                "label shows this value (±tolerance)."
            ),
        ),
        label_pattern=StringSchema(
            description=(
                "JS regex matching the rendered label — the FIRST capture "
                "group must be the numeric value. Example for Chase: "
                "'Monthly contribution[^:]*:\\s*\\$?(\\d+(?:\\.\\d+)?)'. "
                "If omitted, matches any number in a text node on the same "
                "visual row as the handle (works for most sliders with a "
                "single value near the track)."
            ),
            nullable=True,
        ),
        tolerance=NumberSchema(
            "Allowed |target - observed| gap. Default 0 (exact match).",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on step iterations. Default 25.",
            nullable=True,
        ),
        step_px=IntegerSchema(
            "Initial pixel step. The tool auto-adapts from observed "
            "value-per-pixel sensitivity. Default 8.",
            nullable=True,
        ),
        direction=StringSchema(
            "'auto' (default; inferred from current vs target), 'left', 'right'.",
            nullable=True,
        ),
        required=["session_id", "target_value"],
    )
)
class BrowserDragSliderUntilTool(Tool):
    name = "browser_drag_slider_until"
    description = (
        "Closed-loop slider drag. Holds the mouse down on the handle, "
        "steps incrementally, reads the rendered value label from the "
        "iframe DOM after each step, and stops when the label shows the "
        "target value. THE right tool for custom widgets where vision "
        "can't reliably identify the full track geometry (Chase/JPM "
        "calculators, React/Angular sliders with no aria-valuenow). "
        "Unlike browser_set_slider_at (open-loop), this never overshoots "
        "and recovers automatically from non-linear widget scaling. "
        "Workflow: (1) browser_screenshot → vision returns slider_handle "
        "V_n values; (2) call this tool with vision_index=V_n and your "
        "numeric target; (3) inspect the returned trace + final_value to "
        "verify the label reached the target."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        target_value: float,
        vision_index: int | None = None,
        handle_bbox_json: str | None = None,
        label_hint: str | None = None,
        label_pattern: str | None = None,
        tolerance: float | None = None,
        max_iterations: int | None = None,
        step_px: int | None = None,
        direction: str | None = None,
        **kw: Any,
    ) -> str:
        gate = await _feedback_gate("browser_drag_slider_until")
        if gate:
            return gate
        ok, gate_msg = await _require_fresh_vision(
            self.s, session_id,
            reason=f"browser_drag_slider_until(target={target_value})",
        )
        if not ok:
            return gate_msg
        # Serialize drags on this session. If the LLM fired this tool in
        # parallel for multiple sliders, we queue them up so the cursor
        # owns one slider at a time. Without this lock, concurrent drags
        # fight for the same CDP mouse and produce garbage.
        if self.s.slider_drag_lock is None:
            self.s.slider_drag_lock = asyncio.Lock()
        async with self.s.slider_drag_lock:
            return await self._execute_inner(
                session_id, target_value, vision_index, handle_bbox_json,
                label_hint, label_pattern, tolerance, max_iterations,
                step_px, direction,
            )

    async def _resolve_handle_bbox(
        self, session_id: str,
        vision_index: int | None,
        handle_bbox_json: str | None,
        label_hint: str | None,
    ) -> tuple[dict[str, float] | None, str]:
        """Returns (bbox | None, source_description). Tries, in order:
        direct handle_bbox_json → label_hint via DOM enum → vision_index.
        Returns None + reason string on failure."""
        # 1. Direct bbox — highest priority, no indirection.
        if handle_bbox_json:
            try:
                bb = json.loads(handle_bbox_json)
            except (TypeError, ValueError) as exc:
                return None, f"bad handle_bbox_json: {exc}"
            if not isinstance(bb, dict):
                return None, "handle_bbox_json must decode to a dict"
            for k in ("x", "y", "w", "h"):
                if not isinstance(bb.get(k), (int, float)):
                    return None, f"handle_bbox_json missing numeric {k!r}"
            return bb, "handle_bbox_json"

        # 2. Label hint — DOM enum, pick best fuzzy match.
        if label_hint:
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/list-slider-handles",
                    json={},
                    timeout=20.0,
                )
                handles = (r.json() or {}).get("handles") or [] if r.status_code < 400 else []
            except Exception as exc:
                return None, f"list-slider-handles failed: {exc}"
            if not handles:
                return None, "list-slider-handles returned no sliders"
            norm = label_hint.lower().strip()
            # Score each handle: label contains hint → big win; else token overlap.
            best = None
            best_score = -1.0
            for h in handles:
                lab = (h.get("label") or "").lower().strip()
                if not lab:
                    continue
                if norm in lab:
                    score = 1.0 + min(1.0, len(norm) / max(1, len(lab)))
                else:
                    ht = set(norm.split())
                    lt = set(lab.split())
                    if not ht:
                        continue
                    score = len(ht & lt) / len(ht)
                if score > best_score:
                    best_score = score
                    best = h
            if not best or best_score < 0.5:
                sample = [f"[{h.get('index')}] {h.get('label')!r}" for h in handles[:8]]
                return None, (
                    f"no handle label matched {label_hint!r}. "
                    f"candidates: {sample}"
                )
            return best.get("bbox"), f"label_hint={label_hint!r}"

        # 3. Vision index — legacy path.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return None, (
                    "no cached vision response (call browser_screenshot "
                    "first, or pass handle_bbox_json / label_hint)"
                )
            handle_bbox = resp.get_bbox(int(vision_index))
            if handle_bbox is None:
                return None, (
                    f"V{vision_index} out of range "
                    f"({len(resp.bboxes)} bboxes cached)"
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return None, "vision has no image dims; re-screenshot first"
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            hx0, hy0, hx1, hy1 = handle_bbox.to_pixels(iw, ih, dpr=dpr_val)
            return (
                {"x": hx0, "y": hy0, "w": hx1 - hx0, "h": hy1 - hy0},
                f"vision_index=V{vision_index}",
            )

        return None, "provide vision_index, handle_bbox_json, or label_hint"

    async def _execute_inner(
        self,
        session_id: str,
        target_value: float,
        vision_index: int | None,
        handle_bbox_json: str | None,
        label_hint: str | None,
        label_pattern: str | None,
        tolerance: float | None,
        max_iterations: int | None,
        step_px: int | None,
        direction: str | None,
    ) -> str:
        src = (
            f"V{vision_index}" if vision_index is not None
            else (f"hint={label_hint!r}" if label_hint
                  else ("bbox=json" if handle_bbox_json else "?"))
        )
        print(
            f"\n>> browser_drag_slider_until({src} → {target_value}"
            f"{' pattern=' + repr(label_pattern) if label_pattern else ''})"
        )
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        handle_pix, source_desc = await self._resolve_handle_bbox(
            session_id, vision_index, handle_bbox_json, label_hint,
        )
        if handle_pix is None:
            msg = f"[drag_slider_until_failed:no_handle] {source_desc}"
            print(f"   {msg}")
            return msg
        print(f"   resolved handle via {source_desc}: {handle_pix}")

        payload: dict[str, Any] = {
            "handle": handle_pix,
            "target_value": float(target_value),
        }
        if label_pattern is not None:
            payload["label_pattern"] = label_pattern
        if tolerance is not None:
            payload["tolerance"] = float(tolerance)
        if max_iterations is not None:
            payload["max_iterations"] = int(max_iterations)
        if step_px is not None:
            payload["step_px"] = int(step_px)
        if direction is not None:
            payload["direction"] = direction

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-slider-until",
            json=payload,
            timeout=60.0,  # longer — closed-loop can take a few seconds
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_slider_until_failed] {err}"
        data = r.json()
        out = data.get("outcome", {}) or {}
        self.s.record_step(
            "browser_drag_slider_until",
            f"{source_desc} → {target_value} in {out.get('iterations')} iters",
            data.get("url", ""),
        )
        # The drag moved the handle — treat as a page mutation so the
        # next click waits for fresh vision of the post-drag DOM.
        self.s.advance_observation_token("drag_slider_until")
        _schedule_vision_prefetch(self.s, session_id)
        final_v = out.get("final_value")
        init_v = out.get("initial_value")
        completed = bool(out.get("completed"))
        lines: list[str] = []
        if not completed:
            # Prefix with the FAILED tag so the agent treats this as a
            # loud failure (same convention as all other _failed returns).
            if init_v is None:
                reason = "initial_readback_failed"
            elif final_v is None:
                reason = "value_lost_mid_drag"
            else:
                reason = "target_not_reached"
            lines.append(
                f"[drag_slider_until_failed:{reason}] "
                f"{source_desc} target={target_value} "
                f"final={final_v} initial={init_v}"
            )
        else:
            lines.append(
                f"Closed-loop slider {source_desc} → target={target_value} "
                f"COMPLETED in {out.get('iterations')} iterations: "
                f"{init_v} → {final_v}"
            )
        lines.append(f"  label_text={out.get('label_text')!r}")
        trace = out.get("trace") or []
        if trace:
            lines.append("  trace (last 4):")
            for row in trace[-4:]:
                lines.append(
                    f"    iter={row.get('iter')} "
                    f"cursor_x={row.get('cursor_x')} "
                    f"value={row.get('value')}"
                )
        if not completed:
            lines.append(
                "  Fix: if label_text shows NO_MATCH, your label_pattern "
                "regex didn't match any nearby text — the 'nearby text' "
                "list in label_text shows what IS there. Adjust the regex. "
                "If label_text looks right but target wasn't reached, widen "
                "tolerance or raise max_iterations. Always call sliders "
                "SEQUENTIALLY — never in a parallel batch."
            )
        if data.get("elements"):
            lines.append(str(data["elements"]))
        return _maybe_no_effect_prefix(
            data, "browser_drag_slider_until", "\n".join(lines),
            session_state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        bbox_json=StringSchema(
            "JSON object {x, y, w, h} describing the viewport region to crop, in CSS pixels."
        ),
        quality=IntegerSchema("JPEG quality 1–100, default 80.", nullable=True),
        required=["session_id", "bbox_json"],
    )
)

class BrowserImageRegionTool(Tool):
    name = "browser_image_region"
    description = (
        "Screenshot a bounded region of the viewport and return base64 JPEG. "
        "Cheaper than a full-page Gemini pass for solvers that need to "
        "template-match a captcha piece, OCR a small area, or run a tiny "
        "focused vision query."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        bbox_json: str,
        quality: int | None = None,
        **kw: Any,
    ) -> str:
        try:
            bbox = json.loads(bbox_json)
        except (TypeError, ValueError) as exc:
            return f"[image_region_failed] bbox_json is not valid JSON: {exc}"
        for k in ("x", "y", "w", "h"):
            if not isinstance(bbox.get(k), (int, float)):
                return f"[image_region_failed] bbox must have numeric {k}"

        payload: dict[str, Any] = {"bbox": bbox}
        if quality is not None:
            payload["quality"] = quality
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/image-region",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[image_region_failed] {err}"
        data = r.json()
        b64 = data.get("base64", "")
        return (
            f"image_region: {bbox['w']}x{bbox['h']} at ({bbox['x']},{bbox['y']}), "
            f"base64_len={len(b64)}\n{b64[:200]}..."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        hint=StringSchema(
            "Optional solver name to force (chess_com, slider_captcha, jigsaw_captcha, "
            "rotation_captcha, grid_drag). Skip for auto-detect.",
            nullable=True,
        ),
        max_steps=IntegerSchema(
            "Maximum solver iterations. Default 10 — enough for most chess puzzles "
            "and captchas; increase for long puzzle lines.",
            nullable=True,
        ),
        required=["session_id"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        hint=StringSchema(
            "Optional solver name to force (chess_com, slider_captcha, jigsaw_captcha, "
            "rotation_captcha, grid_drag). Skip for auto-detect.",
            nullable=True,
        ),
        max_steps=IntegerSchema(
            "Maximum solver iterations. Default 10 — enough for most chess puzzles "
            "and captchas; increase for long puzzle lines.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserSolvePuzzleTool(Tool):
    name = "browser_solve_puzzle"
    description = (
        "Auto-detect the puzzle on the current page (chess position, "
        "slider/jigsaw/rotation captcha, generic grid-drag) and run a "
        "dedicated solver through extract → plan → execute → verify. "
        "Uses selector- and coordinate-exact primitives under the hood "
        "(zero Gemini round-trips in the move loop). Use whenever the "
        "page presents a puzzle-like challenge."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        hint: str | None = None,
        max_steps: int | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_solve_puzzle(session={session_id}, hint={hint!r}, max_steps={max_steps})")
        from superbrowser_bridge.puzzle_solvers import detect as _detect, solve as _solve
        from superbrowser_bridge.puzzle_solvers.browser import HttpSolverBrowser

        # Pull a DOM snapshot + URL to feed the detector (cheap GET).
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                state_resp = await client.get(
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "false"},
                )
                state_resp.raise_for_status()
                state_data = state_resp.json()
        except Exception as exc:
            return f"[solve_puzzle_failed] cannot read session state: {exc}"

        url = state_data.get("url", "") or ""
        dom_snippet = state_data.get("elements") or ""

        solver, conf = _detect(url, dom_snippet, hint=hint)
        if solver is None:
            return (
                "[solve_puzzle_no_match] No solver matched the current page "
                f"(url={url!r}, confidence={conf:.2f}). Pass hint=<solver name> "
                "to force, or implement a new solver for this page type."
            )

        print(f">> selected solver: {solver.name} (confidence={conf:.2f})")
        async with HttpSolverBrowser(session_id, SUPERBROWSER_URL) as browser:
            result = await _solve(solver, browser, max_steps=max_steps or 10)

        self.s.record_step(
            "browser_solve_puzzle",
            f"{solver.name} success={result.success} steps={result.steps_taken}",
            url,
        )
        lines = [
            f"Puzzle solver: {result.solver}",
            f"Success: {result.success}",
            f"Steps taken: {result.steps_taken}",
        ]
        if result.error:
            lines.append(f"Error: {result.error}")
        if result.actions:
            lines.append(f"Actions ({len(result.actions)}):")
            for a in result.actions[:8]:
                lines.append(f"  - {a.kind}: {a.reason or ''}")
            if len(result.actions) > 8:
                lines.append(f"  … ({len(result.actions) - 8} more)")
        if result.final_state:
            # Strip large base64 payloads before logging.
            redacted = {
                k: (f"<{len(v)} bytes>" if isinstance(v, str) and len(v) > 200 else v)
                for k, v in result.final_state.items()
            }
            lines.append(f"Final state: {redacted}")
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)

def register_session_tools(bot: "Nanobot", state: BrowserSessionState | None = None) -> BrowserSessionState:
    """Register all browser session tools with a nanobot instance.

    Args:
        bot: The Nanobot instance to register tools on.
        state: Optional shared state. If None, creates a new one.

    Returns:
        The BrowserSessionState used (for external access if needed).
    """
    if state is None:
        state = BrowserSessionState()

    tools = [
        BrowserOpenTool(state),
        BrowserNavigateTool(state),
        BrowserScreenshotTool(state),
        BrowserClickTool(state),
        BrowserClickAtTool(state),
        BrowserTypeAtTool(state),
        BrowserFixTextAtTool(state),
        BrowserTypeTool(state),
        BrowserKeysTool(state),
        BrowserScrollTool(state),
        BrowserScrollUntilTool(state),     # kept: scroll-until-target helper
        BrowserSelectTool(state),
        BrowserEvalTool(state),
        BrowserRunScriptTool(state),
        BrowserWaitForTool(state),
        BrowserDragTool(state),
        BrowserGetRectTool(state),         # kept: DOM rect helper
        BrowserClickSelectorTool(state),   # kept: DOM-selector fast path
        BrowserDragSelectorsTool(state),   # kept: selector-based drag
        BrowserDragPathTool(state),        # kept: polyline drag
        BrowserSetSliderTool(state),       # kept: slider family for ChaseIRA calc
        BrowserSetSliderAtTool(state),
        BrowserListSliderHandlesTool(state),
        BrowserDragSliderUntilTool(state),
        BrowserImageRegionTool(state),     # kept: image region helper
        BrowserSolvePuzzleTool(state),     # kept: puzzle solver
        BrowserGetMarkdownTool(),          # stateless
        BrowserDialogTool(),               # stateless
        BrowserDetectCaptchaTool(state),
        BrowserCaptchaScreenshotTool(state),
        BrowserSolveCaptchaTool(state),
        BrowserAskUserTool(state),
        BrowserVerifyFactTool(state),
        BrowserRequestHelpTool(state),
        BrowserEscalateTool(state),        # t1 → t3 migration
        BrowserPlanNextStepsTool(state),   # hierarchical planner
        BrowserRewindToCheckpointTool(state),  # kept: session-memory escape hatch
        BrowserCloseTool(state),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
    return state
