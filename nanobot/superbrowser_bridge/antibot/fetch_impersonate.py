"""Tier 2: fetch via curl_cffi with Chrome TLS impersonation.

Wraps curl_cffi.requests.AsyncSession(impersonate='chrome124') and stacks:
  - ported header bundle (antibot.headers)
  - session pool for cookie continuity + error scoring (antibot.session_pool)
  - tiered proxy rotation with auto-demote on block (antibot.proxy_tiers)
  - shared bot-protection cookie jar (antibot.cookie_jar)
  - homepage warmup for `_abck` / `cf_clearance` collection
  - typed block detection on the response (antibot.bot_detect)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from curl_cffi import requests as curl_requests

from . import bot_detect, cookie_jar, proxy_tiers, session_pool, warmup
from .headers import Profile, for_profile

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE: Profile = "chrome124_mac"
# Map our profile name -> curl_cffi's impersonate target. curl_cffi maintains
# its own browser build matrix; these are the closest matches for our table.
_IMPERSONATE_MAP = {
    "chrome124_mac": "chrome124",
    "chrome124_linux": "chrome124",
    "chrome125_mac": "chrome124",  # chrome125/126 not in curl_cffi 0.15
    "chrome125_linux": "chrome124",
    "chrome126_mac": "chrome124",
    "chrome126_linux": "chrome124",
}


async def _one_attempt(
    url: str,
    *,
    profile: Profile,
    proxy: Optional[str],
    extra_cookies: dict[str, str],
    timeout_s: float,
    referer: Optional[str],
) -> dict[str, Any]:
    headers = for_profile(profile, referer=referer)
    ua = headers["User-Agent"]
    impersonate = _IMPERSONATE_MAP.get(profile, "chrome124")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    async with curl_requests.AsyncSession(
        impersonate=impersonate,
        timeout=timeout_s,
        proxies=proxies,
    ) as client:
        # Replay any cached clearance cookies for this host.
        for k, v in extra_cookies.items():
            client.cookies.set(k, v)
        resp = await client.get(url, headers=headers, allow_redirects=True)
        body = resp.text or ""
        # Cookies the server set during this call (dict-like).
        set_cookies: list[dict] = []
        try:
            for c in resp.cookies.jar:
                set_cookies.append({
                    "name": c.name, "value": c.value,
                    "domain": c.domain or "", "path": c.path or "/",
                    "expires": c.expires or -1,
                    "secure": bool(c.secure),
                    "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly") is not None),
                })
        except Exception:
            pass
        return {
            "status": resp.status_code,
            "html": body,
            "headers": dict(resp.headers or {}),
            "cookies": set_cookies,
            "ua": ua,
        }


async def fetch_impersonate(
    url: str,
    *,
    profile: Profile = _DEFAULT_PROFILE,
    warmup_homepage: bool = True,
    max_retries: int = 2,
    timeout_s: float = 30.0,
    referer: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch `url` via curl_cffi with stacked anti-bot mitigations.

    Returns: {html, status, tier_used, block_class, reason, elapsed_ms,
              attempts, cookies_saved, ua}
    """
    start = time.time()
    host = warmup.hostname_of(url)
    tiers = proxy_tiers.default()
    pool = session_pool.default_pool()
    session = pool.acquire(profile=profile)
    block_class = ""
    reason = ""
    body = ""
    status = 0
    ua = ""
    cookies_saved = 0
    attempts = 0

    try:
        # Replay stored clearance cookies.
        extra = cookie_jar.load_cookies(host, current_ua=None)
        # Merge with the session's own cookie bag (kept across retries).
        merged = {**session.cookies, **extra}

        # One-shot homepage warmup when we don't already have clearance.
        if warmup_homepage and warmup.should_warmup(url):
            root = warmup.root_url_for(url)
            proxy = tiers.pick(host)
            try:
                attempts += 1
                r0 = await _one_attempt(
                    root,
                    profile=profile,
                    proxy=proxy,
                    extra_cookies=merged,
                    timeout_s=min(timeout_s, 15.0),
                    referer=None,
                )
                ua = r0.get("ua", "")
                # Gather any clearance cookies set by the root visit.
                for c in r0.get("cookies", []) or []:
                    if cookie_jar.is_protection_cookie(c.get("name", "")):
                        merged[c["name"]] = c["value"]
                if r0.get("cookies"):
                    cookies_saved += cookie_jar.save_cookies(
                        host, r0["cookies"], user_agent=ua, capture_url=root,
                    )
            except Exception as exc:
                logger.debug("warmup failed for %s: %s", host, exc)

        # Main fetch, with one retry loop on block detection.
        for attempt in range(max_retries + 1):
            proxy = tiers.pick(host)
            try:
                attempts += 1
                r = await _one_attempt(
                    url,
                    profile=profile,
                    proxy=proxy,
                    extra_cookies=merged,
                    timeout_s=timeout_s,
                    referer=referer,
                )
            except Exception as exc:
                session.record_failure(1.0)
                reason = f"transport: {type(exc).__name__}: {exc}"
                if attempt == max_retries:
                    break
                session = pool.rotate(session, profile=profile)
                await asyncio.sleep(0.5 + 0.5 * attempt)
                continue

            status = r["status"]
            body = r["html"]
            ua = r.get("ua", ua)
            # Persist anything new from this response too.
            for c in r.get("cookies", []) or []:
                if cookie_jar.is_protection_cookie(c.get("name", "")):
                    merged[c["name"]] = c["value"]
            if r.get("cookies"):
                cookies_saved += cookie_jar.save_cookies(
                    host, r["cookies"], user_agent=ua, capture_url=url,
                )

            verdict = bot_detect.detect(body, status, r.get("headers"))
            if not verdict.blocked:
                session.cookies.update(merged)
                session.record_success()
                return {
                    "html": body,
                    "status": status,
                    "tier_used": 2,
                    "block_class": "",
                    "reason": "",
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "attempts": attempts,
                    "cookies_saved": cookies_saved,
                    "ua": ua,
                }
            block_class = verdict.klass
            reason = verdict.reason
            session.record_failure(1.0)
            if attempt < max_retries:
                tiers.demote(host)
                session = pool.rotate(session, profile=profile)
                await asyncio.sleep(0.5 + 0.5 * attempt)

        return {
            "html": body,
            "status": status,
            "tier_used": 2,
            "block_class": block_class,
            "reason": reason,
            "elapsed_ms": int((time.time() - start) * 1000),
            "attempts": attempts,
            "cookies_saved": cookies_saved,
            "ua": ua,
        }
    finally:
        pool.release(session)
