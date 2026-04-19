"""Tier 3: fetch via patchright (undetected Chromium) + playwright-stealth.

Stacks:
  - patchright.async_api (undetected-chromium fork)
  - playwright-stealth for navigator / sec-ch-ua / webgl patches
  - simulate_user micro-motions (mouse moves + wheel scroll)
  - overlay / consent-banner removal (post-load DOM script)
  - homepage warmup for `_abck` / `cf_clearance` via the same cookie_jar
  - typed block detection (antibot.bot_detect)

Note on headless: patchright upstream recommends headful via xvfb for the
hardest targets (Akamai sensor checks real window size). We default to
`headless=True` because most server environments lack a display. Callers
who have xvfb or a physical display can pass `headless=False` for better
success on hardened targets.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from patchright.async_api import async_playwright, BrowserContext, Page
from playwright_stealth import Stealth

from . import bot_detect, cookie_jar, proxy_tiers, warmup

logger = logging.getLogger(__name__)

_OVERLAY_REMOVAL_JS = """
(() => {
  const kill = (sel) => document.querySelectorAll(sel).forEach(el => el.remove());
  // GDPR / consent banners — best-effort common markers.
  kill('[id*=cookie-banner]');
  kill('[class*=cookie-banner]');
  kill('[id*=consent-banner]');
  kill('[class*=consent-banner]');
  kill('[id*=gdpr]');
  kill('[class*=gdpr]');
  kill('[aria-label*=cookie]');
  kill('[aria-label*=consent]');
  // Full-viewport overlays with very high z-index.
  document.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el);
    if (!s) return;
    const z = parseInt(s.zIndex, 10);
    if ((s.position === 'fixed' || s.position === 'absolute') && z > 1000) {
      const r = el.getBoundingClientRect();
      if (r.width > window.innerWidth * 0.7 && r.height > window.innerHeight * 0.4) {
        el.remove();
      }
    }
  });
  // Restore body scroll in case the overlay locked it.
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
})();
"""


async def _simulate_user(page: Page) -> None:
    """Port of crawl4ai's simulate_user micro-motions.

    Reference: crawl4ai/async_crawler_strategy.py:980-983.
    """
    try:
        for _ in range(random.randint(2, 3)):
            await page.mouse.move(
                random.randint(100, 600),
                random.randint(150, 400),
            )
            await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.wheel(0, random.randint(200, 500))
        await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception as exc:
        logger.debug("simulate_user failed: %s", exc)


async def _collect_cookies(context: BrowserContext, url: str, ua: str) -> int:
    host = warmup.hostname_of(url)
    if not host:
        return 0
    try:
        raw = await context.cookies()
    except Exception:
        return 0
    cookies: list[dict] = []
    for c in raw:
        cookies.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": c.get("sameSite", None),
        })
    return cookie_jar.save_cookies(host, cookies, user_agent=ua, capture_url=url)


async def _restore_cookies(context: BrowserContext, url: str) -> int:
    host = warmup.hostname_of(url)
    if not host:
        return 0
    cached = cookie_jar.load_cookies(host)
    if not cached:
        return 0
    restored: list[dict] = []
    for name, value in cached.items():
        restored.append({
            "name": name,
            "value": value,
            "url": warmup.root_url_for(url),
        })
    try:
        await context.add_cookies(restored)
    except Exception as exc:
        logger.debug("add_cookies failed: %s", exc)
        return 0
    return len(restored)


async def fetch_undetected(
    url: str,
    *,
    warmup_homepage: bool = True,
    simulate_user: bool = True,
    remove_overlays: bool = True,
    headless: bool = True,
    timeout_s: float = 45.0,
    viewport: tuple[int, int] = (1366, 768),
    wait_for_selector: Optional[str] = None,
    screenshot: bool = False,
) -> dict[str, Any]:
    """Fetch `url` via patchright (undetected Chromium) + playwright-stealth.

    Args:
        wait_for_selector: optional CSS selector to await after the page
            loads. Essential for SPAs where the main content arrives
            post-navigation via XHR.
        screenshot: if True, include a base64-encoded PNG in the return.
            Useful for handing the result to the vision_agent.

    Returns: {html, status, tier_used, block_class, reason, elapsed_ms,
              cookies_saved, ua, screenshot?}
    """
    start = time.time()
    host = warmup.hostname_of(url)
    tiers = proxy_tiers.default()
    proxy_url = tiers.pick(host)
    status = 0
    body = ""
    ua = ""
    cookies_saved = 0
    block_class = ""
    reason = ""
    screenshot_b64: Optional[str] = None

    launch_proxy: Optional[dict] = None
    if proxy_url:
        launch_proxy = {"server": proxy_url}

    stealth = Stealth()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            proxy=launch_proxy,
        )
        try:
            context = await browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                locale="en-US",
                timezone_id="America/New_York",
            )
            await stealth.apply_stealth_async(context)
            await _restore_cookies(context, url)
            page = await context.new_page()
            # Capture the actual UA Chromium emits (post-stealth) so we
            # can UA-pin any saved cookies correctly.
            try:
                ua = await page.evaluate("navigator.userAgent")
            except Exception:
                pass

            # Homepage warmup.
            if warmup_homepage and warmup.should_warmup(url):
                root = warmup.root_url_for(url)
                try:
                    await page.goto(root, wait_until="domcontentloaded",
                                    timeout=int(timeout_s * 1000))
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                except Exception as exc:
                    logger.debug("warmup navigation failed: %s", exc)
                cookies_saved += await _collect_cookies(context, root, ua)

            # Target navigation.
            try:
                resp = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout_s * 1000),
                )
                status = resp.status if resp else 0
            except Exception as exc:
                reason = f"navigation: {type(exc).__name__}: {exc}"

            # Let JS settle; do the simulate_user + overlay removal.
            try:
                await page.wait_for_load_state("networkidle",
                                               timeout=min(int(timeout_s * 1000), 15_000))
            except Exception:
                pass

            if wait_for_selector:
                try:
                    await page.wait_for_selector(
                        wait_for_selector,
                        timeout=min(int(timeout_s * 1000), 15_000),
                    )
                except Exception as exc:
                    logger.debug("wait_for_selector %r failed: %s",
                                 wait_for_selector, exc)

            if simulate_user:
                await _simulate_user(page)

            if remove_overlays:
                try:
                    await page.evaluate(_OVERLAY_REMOVAL_JS)
                except Exception as exc:
                    logger.debug("overlay removal failed: %s", exc)

            try:
                body = await page.content()
            except Exception:
                body = ""

            if screenshot:
                try:
                    import base64
                    png = await page.screenshot(type="png", full_page=False)
                    screenshot_b64 = base64.b64encode(png).decode("ascii")
                except Exception as exc:
                    logger.debug("screenshot failed: %s", exc)

            cookies_saved += await _collect_cookies(context, url, ua)
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    verdict = bot_detect.detect(body, status or None)
    if verdict.blocked:
        block_class = verdict.klass
        reason = reason or verdict.reason
        tiers.demote(host)

    return {
        "html": body,
        "status": status,
        "tier_used": 3,
        "block_class": block_class,
        "reason": reason,
        "elapsed_ms": int((time.time() - start) * 1000),
        "cookies_saved": cookies_saved,
        "ua": ua,
        "screenshot": screenshot_b64,
    }
