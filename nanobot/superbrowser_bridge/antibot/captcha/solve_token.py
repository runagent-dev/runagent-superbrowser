"""Token-based captcha solvers (2captcha / anticaptcha / NopeCHA).

Pattern ported from `src/browser/captcha/strategies/token-external.ts` +
`turnstile.ts` + `recaptcha-v2.ts`. All three vendors use a
submit → poll → inject flow.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal, Optional

import httpx

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)

Provider = Literal["2captcha", "anticaptcha", "nopecha"]

_POLL_INTERVAL_S = 4.0
_POLL_TIMEOUT_S = 180.0


def _provider_from_env() -> tuple[Provider, str]:
    name = (os.environ.get("CAPTCHA_PROVIDER") or "2captcha").lower()
    key = (
        os.environ.get("CAPTCHA_API_KEY")
        or os.environ.get("TWOCAPTCHA_API_KEY")
        or os.environ.get("ANTICAPTCHA_API_KEY")
        or os.environ.get("NOPECHA_API_KEY")
        or ""
    )
    if name not in ("2captcha", "anticaptcha", "nopecha"):
        name = "2captcha"
    return name, key  # type: ignore[return-value]


# --- 2captcha ---------------------------------------------------------------

async def _solve_2captcha(
    site_key: str, page_url: str, captcha_type: str, api_key: str,
) -> Optional[str]:
    method_map = {
        "recaptcha-v2": "userrecaptcha",
        "hcaptcha": "hcaptcha",
        "turnstile": "turnstile",
    }
    method = method_map.get(captcha_type)
    if not method:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://2captcha.com/in.php",
            data={
                "key": api_key, "method": method,
                "googlekey": site_key, "sitekey": site_key,
                "pageurl": page_url, "json": 1,
            },
        )
        data = r.json()
        if data.get("status") != 1:
            logger.debug("2captcha submit failed: %s", data)
            return None
        task_id = data.get("request")
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < _POLL_TIMEOUT_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            r = await client.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": task_id, "json": 1},
            )
            d = r.json()
            if d.get("status") == 1:
                return d.get("request")
            if d.get("request") not in ("CAPCHA_NOT_READY", "CAPCHA_NOT_READY"):
                # Terminal error
                logger.debug("2captcha terminal: %s", d)
                return None
    return None


# --- anticaptcha ------------------------------------------------------------

async def _solve_anticaptcha(
    site_key: str, page_url: str, captcha_type: str, api_key: str,
) -> Optional[str]:
    task_type_map = {
        "recaptcha-v2": "RecaptchaV2TaskProxyless",
        "hcaptcha": "HCaptchaTaskProxyless",
        "turnstile": "TurnstileTaskProxyless",
    }
    tt = task_type_map.get(captcha_type)
    if not tt:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://api.anti-captcha.com/createTask",
            json={"clientKey": api_key, "task": {
                "type": tt, "websiteURL": page_url, "websiteKey": site_key,
            }},
        )
        d = r.json()
        if d.get("errorId"):
            logger.debug("anticaptcha createTask error: %s", d)
            return None
        task_id = d.get("taskId")
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < _POLL_TIMEOUT_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            r = await client.post(
                "https://api.anti-captcha.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            d = r.json()
            if d.get("errorId"):
                return None
            if d.get("status") == "ready":
                sol = d.get("solution") or {}
                return (
                    sol.get("gRecaptchaResponse")
                    or sol.get("token")
                    or sol.get("solution")
                )
    return None


# --- nopecha ----------------------------------------------------------------

async def _solve_nopecha(
    site_key: str, page_url: str, captcha_type: str, api_key: str,
) -> Optional[str]:
    type_map = {
        "recaptcha-v2": "recaptcha2",
        "hcaptcha": "hcaptcha",
        "turnstile": "turnstile",
    }
    nopetype = type_map.get(captcha_type)
    if not nopetype:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://api.nopecha.com/token/",
            json={"key": api_key, "type": nopetype,
                  "sitekey": site_key, "url": page_url},
        )
        d = r.json()
        nid = d.get("data")
        if not nid:
            return None
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < _POLL_TIMEOUT_S:
            await asyncio.sleep(_POLL_INTERVAL_S)
            r = await client.get(
                "https://api.nopecha.com/token/",
                params={"key": api_key, "id": nid},
            )
            d = r.json()
            if d.get("data"):
                return d["data"]
    return None


_INJECT_JS_TEMPLATE = {
    "recaptcha-v2": """
    (token) => {
      const el = document.getElementById('g-recaptcha-response');
      if (el) { el.innerHTML = token; el.value = token; }
      if (window.grecaptcha && window.___grecaptcha_cfg) {
        try {
          const clients = window.___grecaptcha_cfg.clients || {};
          Object.values(clients).forEach(c =>
            Object.values(c).forEach(o =>
              Object.values(o || {}).forEach(w => {
                if (w && typeof w.callback === 'function') w.callback(token);
              })));
        } catch (_) {}
      }
      return true;
    }
    """,
    "hcaptcha": """
    (token) => {
      document.querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]')
        .forEach(t => { t.value = token; });
      if (window.hcaptcha) {
        try {
          const widgets = document.querySelectorAll('[data-hcaptcha-widget-id]');
          widgets.forEach(w => {
            const id = w.getAttribute('data-hcaptcha-widget-id');
            if (window.hcaptcha && typeof window.hcaptcha.execute === 'function') { /* noop */ }
          });
        } catch (_) {}
      }
      return true;
    }
    """,
    "turnstile": """
    (token) => {
      document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(i => { i.value = token; });
      if (window.turnstile && typeof window.turnstile.reset === 'function') {
        try {
          // Trigger any callback set up by the site.
          window.dispatchEvent(new CustomEvent('turnstile:solved', {detail:{token}}));
        } catch (_) {}
      }
      return true;
    }
    """,
}


async def solve_token(
    t3manager, session_id: str, info: CaptchaInfo,
) -> dict:
    """Solve `info` via the configured token-vendor and inject the result."""
    if info.type not in ("recaptcha-v2", "hcaptcha", "turnstile"):
        return {"solved": False, "method": "token", "error": f"unsupported type {info.type}"}
    if not info.site_key:
        return {"solved": False, "method": "token", "error": "no site_key detected"}

    provider, api_key = _provider_from_env()
    if not api_key:
        return {
            "solved": False, "method": "token",
            "error": f"no CAPTCHA_API_KEY set for provider {provider}",
        }

    state = await t3manager.state(session_id, include_screenshot=False)
    page_url = state.get("url", "")

    solver = {
        "2captcha": _solve_2captcha,
        "anticaptcha": _solve_anticaptcha,
        "nopecha": _solve_nopecha,
    }[provider]
    token = await solver(info.site_key, page_url, info.type, api_key)
    if not token:
        return {
            "solved": False, "method": f"token:{provider}",
            "error": "vendor returned no token (timeout or error)",
        }

    inject = _INJECT_JS_TEMPLATE[info.type]
    try:
        await t3manager.evaluate(session_id, f"({inject})({token!r})")
    except Exception as exc:
        return {
            "solved": False, "method": f"token:{provider}",
            "error": f"inject failed: {type(exc).__name__}: {str(exc)[:120]}",
        }

    return {
        "solved": True, "method": f"token:{provider}", "token_len": len(token),
    }
