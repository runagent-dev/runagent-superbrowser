"""HTTP client helpers for talking to the SuperBrowser TS bridge and the
in-process t3 (patchright) shim.

`_request_with_backoff` is the single chokepoint every browser tool
uses; it transparently re-routes `t3-` session ids to the in-process
`T3SessionManager` so the rest of the code base can stay HTTP-shaped.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

from .constants import SUPERBROWSER_URL

def _auth_headers() -> dict[str, str]:
    """Bearer header injected on every TS-server request when token is set.

    The TS server (src/server/auth.ts:tokenAuth) gates `/session/:id/script` and
    `/function` behind TOKEN; without this header those endpoints 403, which
    historically broke the deterministic-script escape hatch on hard sites.
    """
    tok = os.environ.get("SUPERBROWSER_TOKEN") or os.environ.get("TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}
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
    headers = _auth_headers()
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
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
