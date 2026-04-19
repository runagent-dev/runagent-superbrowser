"""Persistent patchright session manager — Tier 3 interactive browser.

Mirrors the TS Puppeteer server's semantic verbs (open, navigate, click,
type, screenshot, eval, close, etc.) through a module-singleton playwright
+ browser instance with per-session context+page tracking.

The verbs return the same dict shapes the TS server returns so a tool that
routes through `_call_backend` sees identical data regardless of tier.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import unquote as _url_unquote, urlparse

from patchright.async_api import async_playwright, BrowserContext, Page
from playwright_stealth import Stealth


def _proxy_to_playwright(url: Optional[str]) -> Optional[dict]:
    """Split a `scheme://user:pass@host:port` proxy URL into the dict
    shape playwright expects: {server, username, password}. Playwright
    does NOT parse inline auth from `server`; passing a URL that
    contains `user:pass@` results in HTTP 407 because only the
    `scheme://host:port` portion is extracted and the credentials are
    dropped. Observed on Oxylabs datacenter pool 2026-04-19.
    """
    if not url:
        return None
    try:
        p = urlparse(url)
    except Exception:
        return None
    if not p.hostname:
        return None
    scheme = p.scheme or "http"
    host = p.hostname
    port = f":{p.port}" if p.port else ""
    out: dict = {"server": f"{scheme}://{host}{port}"}
    if p.username:
        out["username"] = _url_unquote(p.username)
    if p.password:
        out["password"] = _url_unquote(p.password)
    return out

from . import cookie_jar, proxy_tiers, warmup
from . import content as _content

logger = logging.getLogger(__name__)

# Match the TS server's session lifetime (src/server/http.ts:40-41).
SESSION_IDLE_TIMEOUT_S = 30 * 60
SESSION_MAX_LIFETIME_S = 2 * 60 * 60

_DOM_INDEXER_PATH = Path(__file__).parent / "dom_indexer.js"


@dataclass
class _ManagedSession:
    id: str
    context: BrowserContext
    page: Page
    created_at: float
    last_accessed: float
    domain: str
    proxy: Optional[str]
    ua: str = ""
    task_id: str = ""


class T3SessionManager:
    """Singleton-style manager. One playwright+browser at module scope;
    per-session BrowserContext + Page map.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._sessions: dict[str, _ManagedSession] = {}
        self._indexer_js: Optional[str] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            self._pw = await async_playwright().start()
            headless = os.environ.get("T3_HEADLESS", "1") != "0"
            self._browser = await self._pw.chromium.launch(
                headless=headless,
                args=[
                    # Existing canonical stealth flag.
                    "--disable-blink-features=AutomationControlled",
                    # Reduce site-isolation signals that Cloudflare's JS reads
                    # to tell headless Chromium apart from a real Chrome build.
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-site-isolation-trials",
                    # Suppress first-run / default-browser prompts that delay
                    # page navigation and leave telltale console output.
                    "--no-first-run",
                    "--no-default-browser-check",
                    # Keep GPU ON — disabling it is itself a headless tell.
                ],
            )
            # Start a janitor that closes idle/aged sessions. Survives the
            # lifetime of the worker process.
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._janitor())

    async def _janitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self._sweep_expired()
        except asyncio.CancelledError:
            return

    async def _sweep_expired(self) -> None:
        now = time.time()
        expired: list[str] = []
        for sid, s in list(self._sessions.items()):
            if (now - s.last_accessed) > SESSION_IDLE_TIMEOUT_S:
                expired.append(sid)
            elif (now - s.created_at) > SESSION_MAX_LIFETIME_S:
                expired.append(sid)
        for sid in expired:
            try:
                await self.close(sid)
            except Exception as exc:
                logger.debug("janitor close %s failed: %s", sid, exc)

    async def _load_indexer(self) -> str:
        if self._indexer_js is None:
            try:
                self._indexer_js = _DOM_INDEXER_PATH.read_text()
            except OSError:
                self._indexer_js = ""
        return self._indexer_js

    def _get(self, session_id: str) -> _ManagedSession:
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(f"no such t3 session: {session_id}")
        s.last_accessed = time.time()
        return s

    # --- lifecycle -----------------------------------------------------------

    async def open(
        self,
        url: Optional[str] = None,
        *,
        viewport: tuple[int, int] = (1366, 768),
        proxy: Optional[str] = None,
        task_id: str = "",
        import_state: Optional[dict] = None,
        timeout_s: float = 45.0,
    ) -> dict[str, Any]:
        """Create a new session. Returns the same dict shape the TS server
        returns from POST /session/create.
        """
        await self._ensure_browser()
        assert self._browser is not None
        domain = urlparse(url or "about:blank").hostname or ""

        if proxy is None:
            proxy = proxy_tiers.default().pick(domain)
        launch_proxy = _proxy_to_playwright(proxy)

        # Pin a real Chrome UA on the HTTP layer — patchright's stealth only
        # patches JS-side `navigator.userAgent`, the actual `User-Agent` HTTP
        # header still leaks `HeadlessChrome/...` when launched headless.
        # Anti-bot edges read the HTTP header, not the JS property, so without
        # this override every request is fingerprinted as a bot.
        from .headers import for_profile, random_profile
        profile = os.environ.get("T3_UA_PROFILE") or random_profile()
        try:
            ua = for_profile(profile)["User-Agent"]
        except Exception:
            ua = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        context = await self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            locale="en-US",
            timezone_id="America/New_York",
            proxy=launch_proxy,
            user_agent=ua,
        )
        # Conservative playwright-stealth surface. Keep UA, webdriver,
        # languages, permissions, plugins, WebGL, sec-ch-ua patches on —
        # these are read by Cloudflare IUAM and benefit from spoofing.
        # Leave chrome_csi / chrome_load_times / chrome_app / chrome_runtime
        # OFF: over-patching chrome.* globals is itself a detection vector
        # (observed 2026-04-19 on gozayaan.com, which 403'd with the full
        # set but loads clean with this trimmed set).
        stealth = Stealth(
            navigator_user_agent_override=ua,
            navigator_webdriver=True,
            navigator_languages=True,
            navigator_permissions=True,
            navigator_plugins=True,
            navigator_vendor=True,
            navigator_platform=True,
            navigator_hardware_concurrency=True,
            navigator_user_agent_data=True,
            webgl_vendor=True,
            hairline=True,
            iframe_content_window=True,
            media_codecs=True,
            sec_ch_ua=True,
        )
        await stealth.apply_stealth_async(context)

        # Replay cached bot-protection cookies for this domain.
        if domain:
            cached = cookie_jar.load_cookies(domain)
            if cached:
                await context.add_cookies([
                    {"name": k, "value": v, "url": f"https://{domain}/"}
                    for k, v in cached.items()
                ])

        # Imported state (from t1 → t3 escalation).
        if import_state:
            storage_entries = import_state.get("localStorage") or {}
            ss_entries = import_state.get("sessionStorage") or {}
            if storage_entries or ss_entries:
                js = (
                    "try { "
                    f"Object.entries({json.dumps(storage_entries)}).forEach("
                    "([k, v]) => localStorage.setItem(k, v)); "
                    f"Object.entries({json.dumps(ss_entries)}).forEach("
                    "([k, v]) => sessionStorage.setItem(k, v)); "
                    "} catch (_) {}"
                )
                await context.add_init_script(js)
            for c in import_state.get("cookies") or []:
                try:
                    await context.add_cookies([c])
                except Exception:
                    pass

        page = await context.new_page()

        # Capture the real UA post-stealth so UA-pinned cookies are saved
        # with a matching key.
        try:
            ua = await page.evaluate("navigator.userAgent")
        except Exception:
            ua = ""

        sid = f"t3-session-{uuid.uuid4().hex}"
        now = time.time()
        self._sessions[sid] = _ManagedSession(
            id=sid,
            context=context,
            page=page,
            created_at=now,
            last_accessed=now,
            domain=domain,
            proxy=proxy,
            ua=ua,
            task_id=task_id,
        )

        if url:
            nav = await self._goto_with_warmup(sid, url, timeout_s)
            # One retry on hard Cloudflare block: if we still see a
            # challenge title and no clearance cookie landed, rebuild the
            # context with a fresh UA (+ demote proxy tier) and try once
            # more. Single retry, bounded cost.
            still_blocked = await self._cf_still_blocked(sid, nav)
            if still_blocked:
                try:
                    await self._rebuild_context_for_retry(
                        sid, domain, viewport, import_state,
                    )
                    nav = await self._goto_with_warmup(sid, url, timeout_s)
                    nav["retry_used"] = True
                except Exception as exc:
                    logger.debug("retry with fresh context failed: %s", exc)
                    nav.setdefault("block_class", "cloudflare")
        else:
            nav = {"url": "about:blank", "status": 0, "title": ""}

        state = await self.state(sid)
        return {
            "sessionId": sid,
            **nav,
            **{k: v for k, v in state.items() if k not in nav},
        }

    async def _cf_still_blocked(self, sid: str, nav: dict) -> bool:
        """True if the page still looks like a Cloudflare challenge page.
        Title is the ground truth — cookie presence is NOT sufficient to
        declare cleared (stale cf_clearance values from the jar get
        replayed onto a new session, but CF may still reject them and
        render the challenge). If the browser is showing us "Just a
        moment...", we are blocked, cookie or not.
        """
        title = (nav.get("title") or "").lower()
        if (
            "just a moment" in title
            or "one moment" in title
            or "verifying" in title
            or "attention required" in title
        ):
            return True
        return False

    async def _rebuild_context_for_retry(
        self,
        sid: str,
        domain: str,
        viewport: tuple[int, int],
        import_state: Optional[dict],
    ) -> None:
        """Close and recreate the session's context with a different UA
        and a demoted proxy tier. Used as a one-shot retry on hard CF
        blocks. Preserves the session_id so the LLM's subsequent calls
        keep routing to the same slot."""
        from .headers import for_profile, random_profile
        s = self._sessions.get(sid)
        if s is None:
            raise KeyError(f"session not found: {sid}")
        # Demote the proxy tier for this domain so the next attempt may
        # pick a stricter pool (residential if configured).
        try:
            proxy_tiers.default().demote(domain)
        except Exception:
            pass
        new_proxy = proxy_tiers.default().pick(domain)
        launch_proxy = _proxy_to_playwright(new_proxy)
        # Pick a fresh UA profile distinct from the previous one.
        old_profile = os.environ.get("T3_UA_PROFILE")
        try:
            profile = random_profile()
            if old_profile and profile == old_profile:
                profile = random_profile()
            new_ua = for_profile(profile)["User-Agent"]
        except Exception:
            new_ua = s.ua
        # Close the old context + page.
        try:
            await s.context.close()
        except Exception:
            pass
        # Build a fresh context.
        new_context = await self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            locale="en-US",
            timezone_id="America/New_York",
            proxy=launch_proxy,
            user_agent=new_ua,
        )
        stealth = Stealth(
            navigator_user_agent_override=new_ua,
            navigator_webdriver=True,
            navigator_languages=True,
            navigator_permissions=True,
            navigator_plugins=True,
            navigator_vendor=True,
            navigator_platform=True,
            navigator_hardware_concurrency=True,
            navigator_user_agent_data=True,
            webgl_vendor=True,
            hairline=True,
            iframe_content_window=True,
            media_codecs=True,
            sec_ch_ua=True,
        )
        await stealth.apply_stealth_async(new_context)
        if domain:
            cached = cookie_jar.load_cookies(domain, current_ua=new_ua)
            if cached:
                await new_context.add_cookies([
                    {"name": k, "value": v, "url": f"https://{domain}/"}
                    for k, v in cached.items()
                ])
        new_page = await new_context.new_page()
        s.context = new_context
        s.page = new_page
        s.ua = new_ua
        s.proxy = new_proxy

    async def _goto_with_warmup(
        self, sid: str, url: str, timeout_s: float
    ) -> dict[str, Any]:
        s = self._get(sid)
        prev_url = s.page.url or ""
        target_host = urlparse(url).hostname or ""
        prev_host = urlparse(prev_url).hostname or ""
        root = warmup.root_url_for(url)
        # Same-origin navigation inside an already-loaded session is what a
        # real user clicks into — skip the homepage warmup (it would be a
        # redundant second navigation on the same host and looks botty).
        cross_origin = (prev_host != target_host) and prev_host != ""
        # CRITICAL: if the target URL IS the homepage, we must NOT do a
        # separate warmup navigation — the target nav IS the warmup. Two
        # identical page.goto calls within 2 seconds is a textbook bot
        # signature that DataDome/Akamai flag instantly.
        target_is_root = url.rstrip("/") == root.rstrip("/")
        needs_warmup = (
            warmup.should_warmup(url)
            and (cross_origin or prev_host == "")
            and not target_is_root
        )
        if needs_warmup:
            try:
                await s.page.goto(
                    root,
                    wait_until="domcontentloaded",
                    timeout=int(min(timeout_s, 15) * 1000),
                )
                await asyncio.sleep(2.0)
                # Persist any bot-protection cookies gathered during warmup.
                await self._save_domain_cookies(sid, root)
            except Exception as exc:
                logger.debug("warmup %s failed: %s", root, exc)

        # Pre-navigation humanization — brief mouse motion + timing jitter.
        # Breaks the "two page.goto calls fired 50ms apart" signature that
        # Akamai/DataDome use to flag scripted nav. Costs ~0.4-1.2s per
        # navigate. Tune via T3_NAV_JITTER=0 to disable.
        import random as _random
        if os.environ.get("T3_NAV_JITTER") != "0":
            try:
                await s.page.mouse.move(
                    _random.randint(200, 800),
                    _random.randint(150, 500),
                )
                await asyncio.sleep(_random.uniform(0.2, 0.8))
            except Exception:
                pass

        # Referer: if we're navigating from a real previous page, pass it as
        # the Referer so the request looks like a click rather than a cold GET.
        # Only set when prev_url is an http(s) URL to avoid leaking about:blank.
        referer = prev_url if prev_url.startswith(("http://", "https://")) else None

        try:
            goto_kwargs: dict[str, Any] = {
                "wait_until": "domcontentloaded",
                "timeout": int(timeout_s * 1000),
            }
            if referer:
                goto_kwargs["referer"] = referer
            resp = await s.page.goto(url, **goto_kwargs)
            status = resp.status if resp else 0
        except Exception as exc:
            logger.warning("navigate %s failed: %s", url, exc)
            status = 0

        try:
            await s.page.wait_for_load_state(
                "networkidle",
                timeout=min(int(timeout_s * 1000), 15_000),
            )
        except Exception:
            pass

        # If we landed on a self-clearing challenge (Cloudflare "Just a
        # moment", DataDome auto-verify, basic Akamai IUAM) give the JS
        # time to run and stamp clearance cookies before we hand control
        # back. Detected by title + presence of challenge cookie names.
        # Defaults to 30s (configurable via T3_CF_WAIT_S). Mouse motion +
        # occasional wheel scroll fires during the poll loop to look
        # human while the challenge JS is scoring us.
        import random as _rng
        origin_challenge_url = s.page.url
        try:
            challenge_cookie_names = {
                "cf_clearance", "__cf_bm", "datadome", "_abck",
                "ak_bmsc", "incap_ses_", "reese84",
            }
            wait_s = float(os.environ.get("T3_CF_WAIT_S") or 30.0)
            deadline = time.time() + wait_s
            iteration = 0
            while time.time() < deadline:
                iteration += 1
                title_now = (await s.page.title() or "").lower()
                body_snippet = (await s.page.evaluate(
                    "() => (document.body ? document.body.innerText.slice(0, 400) : '').toLowerCase()"
                )) or ""
                looks_challenge = (
                    "just a moment" in title_now
                    or "verifying" in body_snippet
                    or "security check" in body_snippet
                    or "performing" in body_snippet
                    or "checking your browser" in body_snippet
                    or "one moment" in body_snippet
                    or "verify you are human" in body_snippet
                )
                current_url = s.page.url
                url_changed = current_url and current_url != origin_challenge_url
                # Exit fast: page no longer looks like a challenge AND either
                # URL redirected away OR the title changed meaningfully.
                if not looks_challenge and (url_changed or iteration > 1):
                    break
                cookies_now = await s.context.cookies()
                names = {c.get("name", "") for c in cookies_now}
                if names & challenge_cookie_names:
                    # Clearance landed — give the redirect a beat to finish.
                    await asyncio.sleep(1.2)
                    break
                # Humanize during the wait: small mouse moves + occasional
                # wheel. Cloudflare's sensor counts mousemove + wheel events
                # when deciding whether to clear automatically.
                try:
                    await s.page.mouse.move(
                        _rng.randint(150, 900),
                        _rng.randint(120, 600),
                    )
                    if iteration % 3 == 0:
                        await s.page.mouse.wheel(0, _rng.randint(120, 320))
                except Exception:
                    pass
                await asyncio.sleep(1.0)
        except Exception as exc:
            logger.debug("challenge-wait loop: %s", exc)

        # If the page still shows a challenge after the wait, see whether
        # a Cloudflare Turnstile iframe is present and try to auto-solve
        # via the existing token-solver path. Requires CAPTCHA_API_KEY.
        turnstile_info: dict[str, Any] = {}
        try:
            current_title = (await s.page.title() or "").lower()
            still_challenge = (
                "just a moment" in current_title
                or "verifying" in current_title
                or "one moment" in current_title
            )
            if still_challenge:
                from .captcha import detect as _cap_detect
                from .captcha import solve_token as _cap_solve
                info = await _cap_detect(self, sid)
                if info.present and info.type == "turnstile" and info.site_key:
                    turnstile_info = {
                        "site_key": info.site_key,
                        "frame_url": info.frame_url,
                        "widget_selector": info.widget_selector,
                    }
                    if os.environ.get("CAPTCHA_API_KEY"):
                        try:
                            res = await _cap_solve(self, sid, info)
                        except Exception as exc:
                            logger.debug("turnstile solve raised: %s", exc)
                            res = {"solved": False, "error": str(exc)[:120]}
                        turnstile_info["solve_result"] = res
                        if res and res.get("solved"):
                            # Wait up to 10s more for cf_clearance to land.
                            end = time.time() + 10.0
                            while time.time() < end:
                                cks = await s.context.cookies()
                                names = {c.get("name", "") for c in cks}
                                if "cf_clearance" in names:
                                    break
                                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.debug("turnstile autosolve guard: %s", exc)

        await self._save_domain_cookies(sid, url)
        title = ""
        try:
            title = await s.page.title()
        except Exception:
            pass
        result: dict[str, Any] = {
            "url": s.page.url, "status": status, "title": title, "statusCode": status,
        }
        # If the page STILL looks like a Cloudflare interstitial after the
        # wait + optional autosolve, mark the result so the caller can
        # choose to escalate (residential proxy, human handoff) instead of
        # retrying more tools on a dead session. Title is authoritative:
        # any stale cf_clearance value from the jar may be replayed onto
        # the fresh session but CF can still reject it and show the
        # challenge — in which case we ARE blocked despite the cookie.
        lower_title = title.lower()
        if (
            "just a moment" in lower_title
            or "one moment" in lower_title
            or "attention required" in lower_title
            or "verifying" in lower_title
        ):
            result["block_class"] = "cloudflare"
            if status in (0, 200):
                result["status"] = 403
                result["statusCode"] = 403
        if turnstile_info:
            # Surface the Turnstile context to the caller so the LLM can
            # decide to call browser_solve_captcha when no API key is set
            # or auto-solve didn't clear.
            result["turnstile"] = turnstile_info
        return result

    async def _save_domain_cookies(self, sid: str, url: str) -> None:
        s = self._get(sid)
        host = urlparse(url).hostname or s.domain
        if not host:
            return
        try:
            raw = await s.context.cookies()
        except Exception:
            return
        entries = [
            {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c.get("expires", -1),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": c.get("sameSite", None),
            }
            for c in raw
        ]
        cookie_jar.save_cookies(host, entries, user_agent=s.ua, capture_url=url)

    async def navigate(self, sid: str, url: str, *, timeout_s: float = 45.0) -> dict[str, Any]:
        nav = await self._goto_with_warmup(sid, url, timeout_s)
        state = await self.state(sid)
        return {**nav, **{k: v for k, v in state.items() if k not in nav}}

    async def close(self, sid: str) -> dict[str, Any]:
        s = self._sessions.pop(sid, None)
        if s is None:
            return {"success": False}
        try:
            await s.context.close()
        except Exception as exc:
            logger.debug("context close %s: %s", sid, exc)
        return {"success": True}

    # --- observation ---------------------------------------------------------

    async def screenshot(self, sid: str, *, full_page: bool = False, quality: int = 75) -> bytes:
        s = self._get(sid)
        return await s.page.screenshot(
            type="jpeg",
            quality=quality,
            full_page=full_page,
        )

    async def state(
        self,
        sid: str,
        *,
        use_vision: bool = False,
        include_screenshot: bool = True,
    ) -> dict[str, Any]:
        s = self._get(sid)
        url = s.page.url
        title = ""
        try:
            title = await s.page.title()
        except Exception:
            pass
        elements: list[dict] = []
        try:
            elements = await self._index_elements(sid)
        except Exception as exc:
            logger.debug("index elements failed: %s", exc)

        screenshot_b64: Optional[str] = None
        if include_screenshot:
            try:
                raw = await s.page.screenshot(type="jpeg", quality=75)
                screenshot_b64 = base64.b64encode(raw).decode("ascii")
            except Exception as exc:
                logger.debug("screenshot failed: %s", exc)

        scroll_info: dict[str, Any] = {}
        try:
            scroll_info = await s.page.evaluate(
                "() => ({scrollY: window.scrollY, innerHeight: window.innerHeight, "
                "scrollHeight: document.documentElement.scrollHeight})"
            )
        except Exception:
            pass

        # Compute element fingerprints for the stale-index guard.
        fingerprints: dict[int, str] = {}
        for el in elements:
            idx = el.get("index")
            if idx is None:
                continue
            sig = f"{el.get('tag','')}|{el.get('text','')[:40]}|{el.get('attrs','')}"
            fingerprints[int(idx)] = hashlib.sha256(sig.encode()).hexdigest()[:16]

        return {
            "url": url,
            "title": title,
            "screenshot": screenshot_b64,
            "elements": self._render_elements_for_brain(elements),
            "elementList": elements,
            "scrollInfo": scroll_info,
            "fingerprints": fingerprints,
        }

    def _render_elements_for_brain(self, elements: list[dict]) -> str:
        """Render the element list as the brain-readable string the TS side
        emits. One line per element: `[N] tag "text" (x0,y0→x1,y1)`.
        """
        lines: list[str] = []
        for el in elements:
            idx = el.get("index")
            tag = el.get("tag", "")
            text = (el.get("text") or "").strip()[:50]
            bbox = el.get("bbox") or [0, 0, 0, 0]
            lines.append(
                f"[{idx}] {tag} \"{text}\" ({bbox[0]},{bbox[1]}→{bbox[2]},{bbox[3]})"
            )
        return "\n".join(lines)

    async def _index_elements(self, sid: str) -> list[dict]:
        s = self._get(sid)
        js = await self._load_indexer()
        if not js:
            return []
        try:
            return await s.page.evaluate(js)
        except Exception as exc:
            logger.debug("index_elements eval failed: %s", exc)
            return []

    async def get_markdown(self, sid: str) -> str:
        s = self._get(sid)
        html = await s.page.content()
        return _content.to_markdown(html or "")

    async def evaluate(self, sid: str, script: str) -> Any:
        s = self._get(sid)
        return await s.page.evaluate(script)

    async def run_script(self, sid: str, code: str) -> dict[str, Any]:
        """Execute a user-supplied async JS body in page context.

        Provides a browser-side shim for `helpers` and `page` that roughly
        matches the TS server's script runner (src/browser/script-runner.ts).
        This means scripts written assuming Puppeteer-style `await page.click`,
        `helpers.sleep`, `await page.waitForSelector` mostly work on t3 too.

        Semantics that cannot be faithfully implemented in browser context:
          - `page.goto(url)` triggers navigation via window.location; caller
            should follow with a `browser_wait_for` or re-fetch state.
          - `page.cookies()` / `page.setCookie` not available (use
            browser-level tools instead).
          - `page.screenshot()` — call `browser_screenshot` separately.
        """
        s = self._get(sid)
        t0 = time.time()
        # Detect and strip the wrapper shapes the TS side accepts so bare
        # function-body snippets, arrow-function wrappers, and `export
        # default async function({page})` all produce the same body.
        import re
        body = code.strip()
        m = re.match(
            r"^async\s*(?:function\s*\w*)?\s*\([^)]*\)\s*(?:=>)?\s*\{([\s\S]*)\}\s*;?\s*$",
            body,
        )
        if m:
            body = m.group(1)
        else:
            m2 = re.match(
                r"^export\s+default\s+async\s+function\s*\([^)]*\)\s*\{([\s\S]*)\}\s*;?\s*$",
                body,
            )
            if m2:
                body = m2.group(1)

        prelude = """
        const helpers = {
          sleep: (ms) => new Promise(r => setTimeout(r, ms)),
          log: (...args) => console.log(...args),
          screenshot: async () => { throw new Error('helpers.screenshot not available on t3; call browser_screenshot separately'); },
        };
        const page = {
          url: () => window.location.href,
          title: () => document.title,
          content: () => document.documentElement.outerHTML,
          goto: (u) => { window.location = u; return new Promise(r => setTimeout(r, 2000)); },
          click: (sel) => {
            const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
            if (!el) throw new Error('page.click: no element for ' + sel);
            el.click();
          },
          type: async (sel, text, opts = {}) => {
            const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
            if (!el) throw new Error('page.type: no element for ' + sel);
            el.focus();
            const delay = opts.delay ?? 0;
            el.value = '';
            for (const ch of (text || '').toString()) {
              el.value += ch;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              if (delay) await new Promise(r => setTimeout(r, delay));
            }
            el.dispatchEvent(new Event('change', {bubbles: true}));
          },
          waitForSelector: async (sel, opts = {}) => {
            const timeout = opts.timeout ?? 30000;
            const t0 = Date.now();
            while (Date.now() - t0 < timeout) {
              const el = document.querySelector(sel);
              if (el) {
                if (opts.visible) {
                  const r = el.getBoundingClientRect();
                  if (r.width > 0 && r.height > 0) return el;
                } else {
                  return el;
                }
              }
              await new Promise(r => setTimeout(r, 50));
            }
            throw new Error('page.waitForSelector timeout: ' + sel);
          },
          waitForTimeout: (ms) => new Promise(r => setTimeout(r, ms)),
          waitForFunction: async (fn, opts = {}, ...args) => {
            const timeout = opts.timeout ?? 30000;
            const t0 = Date.now();
            while (Date.now() - t0 < timeout) {
              const r = await fn(...args);
              if (r) return r;
              await new Promise(r => setTimeout(r, 50));
            }
            throw new Error('page.waitForFunction timeout');
          },
          evaluate: async (fn, ...args) => {
            if (typeof fn === 'function') return fn(...args);
            return eval(fn);
          },
          $: (sel) => document.querySelector(sel),
          $$: (sel) => Array.from(document.querySelectorAll(sel)),
          $eval: async (sel, fn, ...args) => {
            const el = document.querySelector(sel);
            if (!el) throw new Error('page.$eval: no element for ' + sel);
            return fn(el, ...args);
          },
          $$eval: async (sel, fn, ...args) => fn(Array.from(document.querySelectorAll(sel)), ...args),
          keyboard: {
            press: (k) => { throw new Error('page.keyboard.press: not supported in t3 run_script (call browser_keys instead)'); },
            type: async (text) => {
              for (const ch of text) {
                document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', {key: ch}));
                document.activeElement?.dispatchEvent(new KeyboardEvent('keyup', {key: ch}));
              }
            },
          },
        };
        const context = {};
        """
        wrapper = f"(async () => {{ {prelude}\n{body} }})()"
        try:
            result = await s.page.evaluate(wrapper)
            duration = int((time.time() - t0) * 1000)
            # Mirror TS shape so BrowserRunScriptTool's data.get("success")/
            # data.get("result") etc. all work identically.
            return {
                "success": True,
                "result": result,
                "output": result,
                "duration": duration,
                "duration_ms": duration,
                "error": None,
                "logs": [],
            }
        except Exception as exc:
            duration = int((time.time() - t0) * 1000)
            return {
                "success": False,
                "result": None,
                "output": None,
                "duration": duration,
                "duration_ms": duration,
                "error": str(exc)[:500],
                "logs": [],
            }

    # --- interaction ---------------------------------------------------------

    async def click_at(
        self, sid: str, x: float, y: float, *, bbox: Optional[dict] = None,
    ) -> dict[str, Any]:
        s = self._get(sid)
        # If bbox is given, attempt element-snapping inside it.
        target_x, target_y = x, y
        snapped = False
        snap_target = ""
        if bbox is not None:
            try:
                snap = await s.page.evaluate(
                    """({x0, y0, x1, y1}) => {
                      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
                      const el = document.elementFromPoint(cx, cy);
                      if (!el) return null;
                      const r = el.getBoundingClientRect();
                      // Only snap if the element's center is within the bbox.
                      const ex = r.left + r.width / 2, ey = r.top + r.height / 2;
                      if (ex < x0 || ex > x1 || ey < y0 || ey > y1) return null;
                      return {x: ex, y: ey, tag: el.tagName.toLowerCase(),
                              text: (el.innerText || '').slice(0, 40)};
                    }""",
                    {
                        "x0": float(bbox.get("x0", x)),
                        "y0": float(bbox.get("y0", y)),
                        "x1": float(bbox.get("x1", x)),
                        "y1": float(bbox.get("y1", y)),
                    },
                )
                if isinstance(snap, dict):
                    target_x = float(snap.get("x", x))
                    target_y = float(snap.get("y", y))
                    snapped = True
                    snap_target = f"{snap.get('tag','')}:{snap.get('text','')}"
            except Exception as exc:
                logger.debug("snap eval failed: %s", exc)

        try:
            await s.page.mouse.move(target_x, target_y)
            await asyncio.sleep(0.05)
            await s.page.mouse.click(target_x, target_y)
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}

        try:
            await s.page.wait_for_load_state(
                "domcontentloaded", timeout=5000,
            )
        except Exception:
            pass
        st = await self.state(sid)
        return {
            "success": True,
            "url": st["url"],
            "title": st["title"],
            "elements": st["elements"],
            "snap": {"snapped": snapped, "target": snap_target, "x": target_x, "y": target_y},
        }

    async def click(self, sid: str, index: int) -> dict[str, Any]:
        elements = await self._index_elements(sid)
        target = next((e for e in elements if e.get("index") == index), None)
        if not target:
            return {"success": False, "error": f"index {index} not found"}
        bbox = target.get("bbox") or [0, 0, 0, 0]
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        return await self.click_at(sid, cx, cy, bbox={
            "x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3],
        })

    async def fix_text_at(
        self,
        sid: str,
        x: float,
        y: float,
        target_text: str,
        *,
        target_label: str = "",
    ) -> dict[str, Any]:
        """Atomically set the field at (x, y) to `target_text`, regardless
        of what it contained before. No intermediate state — the field
        either HAS the target text or the tool failed. React/Vue safe
        (uses the native value setter + dispatches the events a real
        keystroke would).

        This is the correction path: when the LLM realises it typed
        'dahka' into the city field and wants 'dhaka', it calls
        fix_text_at with target_text='dhaka'. The response includes a
        diff summary so the brain sees what actually changed.
        """
        s = self._get(sid)
        cx = float(x)
        cy = float(y)
        label = target_label or f"({int(cx)},{int(cy)})"
        js = """
        ({x, y, target}) => {
          const el = document.elementFromPoint(x, y);
          if (!el) return {ok: false, reason: 'no_element'};
          const tag = el.tagName.toLowerCase();
          const isInput = tag === 'input' || tag === 'textarea';
          const isEditable = !!el.isContentEditable;
          if (!isInput && !isEditable) {
            return {ok: false, reason: 'not_input', tag};
          }
          if (isInput) {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            if (['file','checkbox','radio','hidden','submit','button',
                 'image','reset','range','color'].includes(t)) {
              return {ok: false, reason: 'non_text_input', tag, input_type: t};
            }
          }
          const before = isInput ? (el.value || '') : (el.innerText || '');
          if (before === target) {
            return {ok: true, before, after: target, changed: false, tag};
          }
          // Focus first so framework components don't discard the change.
          el.focus();
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
            return {ok: false, reason: 'exception', error: String(e).slice(0, 120), before};
          }
          const after = isInput ? (el.value || '') : (el.innerText || '');
          return {ok: after === target, before, after, changed: before !== after, tag};
        }
        """
        try:
            result = await s.page.evaluate(js, {"x": cx, "y": cy, "target": target_text})
        except Exception as exc:
            return {"success": False, "error": f"evaluate failed: {str(exc)[:200]}"}

        if not isinstance(result, dict):
            return {"success": False, "error": "unexpected evaluate return shape"}

        if not result.get("ok"):
            return {
                "success": False,
                "error": result.get("reason", "unknown"),
                "detail": result,
                "label": label,
            }

        before = result.get("before", "") or ""
        after = result.get("after", "") or ""
        changed = bool(result.get("changed"))

        # Diff description: find common prefix / suffix and report the edit.
        def _diff(a: str, b: str) -> str:
            if a == b:
                return "no change"
            # common prefix
            p = 0
            while p < len(a) and p < len(b) and a[p] == b[p]:
                p += 1
            # common suffix (not overlapping with prefix)
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

        diff = _diff(before, after)
        st = await self.state(sid)
        return {
            "success": True,
            "elements": st["elements"],
            "before": before,
            "after": after,
            "changed": changed,
            "diff": diff,
            "label": label,
        }

    async def type_at(
        self,
        sid: str,
        x: float,
        y: float,
        text: str,
        *,
        clear: bool = True,
        bbox: Optional[dict] = None,
        target_label: str = "",
    ) -> dict[str, Any]:
        """Type at a given (x, y) coordinate with the same pre-probe +
        React-aware clear logic as `type(index)`. The bbox-based analogue
        of `type(index)`: targets the element at `elementFromPoint(x, y)`
        rather than looking up via the DOM indexer's numbering.

        Used by browser_type_at to support the vision → bbox → type loop
        without falling back to click_at + keys (which appends to existing
        content instead of replacing it).
        """
        cx = float(x)
        cy = float(y)
        label = target_label or f"({int(cx)},{int(cy)})"
        return await self._type_at_coords(sid, cx, cy, text, clear=clear, label=label)

    async def type(
        self, sid: str, index: int, text: str, *, clear: bool = True,
    ) -> dict[str, Any]:
        elements = await self._index_elements(sid)
        target = next((e for e in elements if e.get("index") == index), None)
        if not target:
            return {"success": False, "error": f"index {index} not found"}
        bbox = target.get("bbox") or [0, 0, 0, 0]
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        return await self._type_at_coords(
            sid, cx, cy, text, clear=clear, label=f"[{index}]",
        )

    async def _type_at_coords(
        self,
        sid: str,
        cx: float,
        cy: float,
        text: str,
        *,
        clear: bool = True,
        label: str = "",
    ) -> dict[str, Any]:
        s = self._get(sid)
        # Pre-type probe: read the field's CURRENT value before deciding
        # what to do. Three cases:
        #   1. field already contains `text` → skip the type entirely
        #      (rare but it happens on retry loops).
        #   2. field is empty → just type, no clear needed.
        #   3. field has something different → React/Vue-aware clear, then
        #      type. Naive `Ctrl+A + Delete` doesn't clear controlled
        #      components because framework state re-hydrates the value
        #      milliseconds later.
        current_value = ""
        try:
            probe = await s.page.evaluate(
                """({x, y}) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return null;
                  if ('value' in el && el.value !== undefined) return {v: el.value};
                  if (el.isContentEditable) return {v: el.innerText || ''};
                  return {v: (el.textContent || '').trim()};
                }""",
                {"x": cx, "y": cy},
            )
            if isinstance(probe, dict):
                current_value = str(probe.get("v", "") or "")
        except Exception as exc:
            logger.debug("type pre-probe failed: %s", exc)

        if current_value == text and current_value != "":
            st = await self.state(sid)
            return {
                "success": True,
                "elements": st["elements"],
                "note": f"field {label} already contains target text ({len(text)} chars); skipped typing",
                "pretype_value": current_value,
                "pretype_action": "skip_match",
            }

        try:
            await s.page.mouse.click(cx, cy)
            # Tiny settle pause — some components rely on focus-change to
            # open their dropdown / clear placeholder state.
            await asyncio.sleep(0.08)
            if clear and current_value:
                # Three-layer clear: native setter (React/Vue-safe) +
                # Ctrl+A/Delete keystroke (standard inputs) + Select-All
                # via triple-click as a further fallback for rich-text.
                await s.page.evaluate(
                    """({x, y}) => {
                      const el = document.elementFromPoint(x, y);
                      if (!el) return false;
                      try {
                        // React-controlled inputs: use the native value
                        // setter so React's onChange observer fires.
                        let proto = null;
                        if (el.tagName === 'TEXTAREA') proto = HTMLTextAreaElement.prototype;
                        else if (el.tagName === 'INPUT') proto = HTMLInputElement.prototype;
                        if (proto) {
                          const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                          if (desc && desc.set) {
                            desc.set.call(el, '');
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                          } else {
                            el.value = '';
                          }
                        } else if (el.isContentEditable) {
                          el.textContent = '';
                          el.dispatchEvent(new Event('input', {bubbles: true}));
                        }
                      } catch (_) {}
                      return true;
                    }""",
                    {"x": cx, "y": cy},
                )
                # Also hit Ctrl+A + Delete as a belt-and-braces for
                # non-framework inputs (plain HTML forms).
                await s.page.keyboard.press("Control+a")
                await s.page.keyboard.press("Delete")
                # Verify the clear worked. If not, try triple-click select +
                # delete once more.
                try:
                    after_clear = await s.page.evaluate(
                        """({x, y}) => {
                          const el = document.elementFromPoint(x, y);
                          if (!el) return '';
                          if ('value' in el) return el.value || '';
                          return el.textContent || '';
                        }""",
                        {"x": cx, "y": cy},
                    )
                    if after_clear:
                        await s.page.mouse.click(cx, cy, click_count=3)
                        await s.page.keyboard.press("Delete")
                except Exception:
                    pass
            await s.page.keyboard.type(text, delay=30)
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}
        st = await self.state(sid)
        action = "cleared_and_typed" if current_value else "typed_into_empty"
        return {
            "success": True,
            "elements": st["elements"],
            "pretype_value": current_value,
            "pretype_action": action,
        }

    async def keys(self, sid: str, keys: list[str]) -> dict[str, Any]:
        s = self._get(sid)
        try:
            for k in keys:
                await s.page.keyboard.press(k)
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}
        st = await self.state(sid)
        return {"success": True, "elements": st["elements"]}

    async def scroll(
        self,
        sid: str,
        *,
        direction: Optional[str] = None,
        percent: Optional[float] = None,
    ) -> dict[str, Any]:
        s = self._get(sid)
        try:
            if percent is not None:
                await s.page.evaluate(
                    f"window.scrollTo({{top: "
                    f"document.documentElement.scrollHeight * {float(percent) / 100}, "
                    f"behavior: 'smooth'}})"
                )
            else:
                dy = 500 if direction in (None, "down") else -500
                await s.page.mouse.wheel(0, dy)
            await asyncio.sleep(0.3)
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}
        st = await self.state(sid)
        return {"success": True, "elements": st["elements"], "scrollInfo": st["scrollInfo"]}

    async def drag(
        self,
        sid: str,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        steps: int = 20,
    ) -> dict[str, Any]:
        s = self._get(sid)
        try:
            await s.page.mouse.move(start_x, start_y)
            await s.page.mouse.down()
            for i in range(1, steps + 1):
                t = i / steps
                # Ease in/out for more human-like motion.
                t2 = t * t * (3 - 2 * t)
                x = start_x + (end_x - start_x) * t2
                y = start_y + (end_y - start_y) * t2
                await s.page.mouse.move(x, y)
                await asyncio.sleep(0.01)
            await s.page.mouse.up()
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}
        st = await self.state(sid)
        return {"success": True, "url": st["url"], "title": st["title"], "elements": st["elements"]}

    async def select(self, sid: str, index: int, value: str) -> dict[str, Any]:
        elements = await self._index_elements(sid)
        target = next((e for e in elements if e.get("index") == index), None)
        if not target or target.get("tag") != "select":
            return {"success": False, "error": f"index {index} is not a <select>"}
        selector = target.get("selector") or ""
        s = self._get(sid)
        try:
            if selector:
                await s.page.select_option(selector, value=value)
            else:
                # Fallback: click the option via JS.
                await s.page.evaluate(
                    "(args) => { "
                    "const el = document.querySelectorAll('select')[args.pos]; "
                    "if (el) { el.value = args.value; el.dispatchEvent(new Event('change',{bubbles:true})); } }",
                    {"pos": target.get("select_pos", 0), "value": value},
                )
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}
        st = await self.state(sid)
        return {"success": True, "elements": st["elements"]}

    async def wait_for(
        self, sid: str, *, selector: Optional[str] = None, timeout_s: float = 10.0,
    ) -> dict[str, Any]:
        s = self._get(sid)
        try:
            if selector:
                await s.page.wait_for_selector(selector, timeout=int(timeout_s * 1000))
            else:
                await s.page.wait_for_load_state(
                    "networkidle", timeout=int(timeout_s * 1000),
                )
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}
        st = await self.state(sid)
        return {"success": True, "elements": st["elements"]}

    # --- state import/export (for t1 → t3 escalation) ------------------------

    async def export_state(self, sid: str) -> dict[str, Any]:
        """Mirror method — the manager doesn't typically call this on itself,
        but the t1-side escalator reads parallel data from the TS /state and
        we expose this so callers have a consistent shape.
        """
        s = self._get(sid)
        url = s.page.url
        cookies: list[dict] = []
        try:
            cookies = list(await s.context.cookies())
        except Exception:
            pass
        local_storage: dict[str, str] = {}
        session_storage: dict[str, str] = {}
        try:
            local_storage = await s.page.evaluate(
                "() => Object.fromEntries(Object.entries(localStorage))"
            )
            session_storage = await s.page.evaluate(
                "() => Object.fromEntries(Object.entries(sessionStorage))"
            )
        except Exception:
            pass
        return {
            "url": url,
            "cookies": cookies,
            "localStorage": local_storage,
            "sessionStorage": session_storage,
        }


_DEFAULT: Optional[T3SessionManager] = None


def default() -> T3SessionManager:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = T3SessionManager()
    return _DEFAULT


# High-level dispatch used by `_call_backend` in session_tools.py.
# Keeps the tool-facing surface narrow and async.

_VERBS = {
    "open", "navigate", "close", "screenshot", "state",
    "click", "click_at", "type", "keys", "scroll", "drag", "select",
    "wait_for", "evaluate", "run_script", "get_markdown",
    "export_state",
}


async def dispatch(verb: str, session_id: str, **kwargs: Any) -> Any:
    if verb not in _VERBS:
        raise ValueError(f"unknown t3 verb: {verb}")
    mgr = default()
    if verb == "open":
        return await mgr.open(**kwargs)
    if verb == "close":
        return await mgr.close(session_id)
    # Everything else takes sid as the first positional arg.
    return await getattr(mgr, verb)(session_id, **kwargs)
