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


_XVFB_STARTED: bool = False


def _maybe_start_xvfb(headless: bool) -> None:
    """If we're launching headful on a host with no DISPLAY, try to spawn
    Xvfb and point the browser at it. No-op otherwise.

    Opt-out via T3_AUTO_XVFB=0 for deployments that manage their own
    display. Silent fallback when Xvfb isn't installed — caller stays
    headful-intent, but with no DISPLAY the browser will fail naturally
    and the operator sees a clear "xvfb not found" log line.
    """
    global _XVFB_STARTED
    if _XVFB_STARTED:
        return
    if headless:
        return
    if os.environ.get("T3_AUTO_XVFB", "1") == "0":
        return
    if os.environ.get("DISPLAY"):
        return  # Caller already has a display — don't stomp on it.
    import shutil as _shutil
    import subprocess as _subprocess
    xvfb_bin = _shutil.which("Xvfb")
    if not xvfb_bin:
        logger.warning(
            "T3_HEADLESS=0 but Xvfb is not installed and DISPLAY is "
            "unset — browser launch will likely fail. "
            "apt install xvfb, or set T3_HEADLESS=1."
        )
        return
    display = os.environ.get("T3_XVFB_DISPLAY") or ":99"
    try:
        _subprocess.Popen(
            [xvfb_bin, display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            # Detach so our asyncio loop exiting doesn't kill Xvfb.
            preexec_fn=os.setpgrp,
        )
        os.environ["DISPLAY"] = display
        _XVFB_STARTED = True
        logger.info("started Xvfb on %s (display=%s)", xvfb_bin, display)
        # Give Xvfb a moment to bind the socket before Chrome tries to
        # connect. Avoids flaky "cannot open display" on fast boxes.
        import time as _time
        _time.sleep(0.3)
    except Exception as exc:
        logger.warning("failed to start Xvfb: %s — falling back to headless", exc)


def _profile_root() -> Path:
    """Resolve the parent directory for per-domain browser profiles.

    Overridable via T3_PROFILE_ROOT; defaults to ~/.superbrowser/profiles/.
    Creation is lazy — the caller materializes the per-domain subdir
    on first use.
    """
    override = os.environ.get("T3_PROFILE_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".superbrowser" / "profiles"


def _domain_safe(domain: str) -> str:
    """Sanitize a domain string into a filesystem-safe directory name."""
    if not domain:
        return "_blank"
    # Lowercase, strip www., replace anything non-alphanum with underscore.
    s = domain.lower()
    if s.startswith("www."):
        s = s[4:]
    import re as _re
    s = _re.sub(r"[^a-z0-9._-]", "_", s)
    return s or "_blank"


def _resolve_profile_dir(domain: str) -> Path:
    """Return the per-domain profile directory, evicting it first when
    size exceeds T3_PROFILE_MAX_MB (default 200) to keep disk bounded.

    This resolver is the single seam that a future object-storage adapter
    can replace: swap this function to download-then-return a local path,
    and a close hook that uploads on shutdown, and the rest of the stack
    is unaffected.
    """
    base = _profile_root() / _domain_safe(domain)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("profile dir mkdir %s failed: %s", base, exc)
        return base

    try:
        cap_mb = float(os.environ.get("T3_PROFILE_MAX_MB") or 200.0)
    except ValueError:
        cap_mb = 200.0
    try:
        # Cheap size check — recursive du without tools.
        total = 0
        for root, _dirs, files in os.walk(base):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
        if total > cap_mb * 1024 * 1024:
            logger.warning(
                "profile %s exceeded %.0f MB (%d bytes) — evicting",
                base, cap_mb, total,
            )
            import shutil as _shutil
            _shutil.rmtree(base, ignore_errors=True)
            base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.debug("profile size check %s: %s", base, exc)
    return base


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
    # Set when this session owns a per-session Browser (i.e. was opened
    # via `launch_persistent_context` with its own user_data_dir). The
    # close path must close this in addition to the context so we don't
    # leak Chromium processes. `None` means the session shares the
    # singleton browser and close only tears down the context.
    persistent_browser: Optional[Any] = None
    # CDP session used for Page.startScreencast. Populated in `open()`
    # when the live viewer is running; close path stops the screencast
    # before tearing down the context. `None` means no screencast (CDP
    # attach failed, or viewer infra not available).
    cdp: Optional[Any] = None
    # Virtual cursor position tracked across tool calls so we can do
    # smooth bezier approaches to each target (instead of teleporting)
    # and feed those intermediate points into the viewer overlay.
    cursor_x: float = 640.0
    cursor_y: float = 384.0


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
            _maybe_start_xvfb(headless)
            args = [
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
            ]
            # HTTP/2 off by default. Some anti-bot stacks (Imperva on
            # cars.com, a couple of Akamai deployments) reject HTTP/2
            # frames from Chromium when the TLS/JA3 fingerprint doesn't
            # match their allowlist, surfacing as ERR_HTTP2_PROTOCOL_ERROR
            # before a page even renders. Forcing HTTP/1.1 makes those
            # hosts reachable at a small latency cost. Opt back into
            # HTTP/2 per-process via T3_DISABLE_HTTP2=0.
            if os.environ.get("T3_DISABLE_HTTP2", "1") != "0":
                args.append("--disable-http2")
            # Real-Chrome override: CHROME_PATH points at a real Google
            # Chrome binary (e.g. /usr/bin/google-chrome), CHROME_CHANNEL
            # uses Playwright's channel selector ("chrome", "chrome-beta",
            # "msedge"). Real Chrome carries codec/UA/JIT signals that
            # bundled Chromium lacks and Imperva/CF score for. `None` for
            # either preserves today's behaviour (bundled Chromium).
            chrome_path = os.environ.get("CHROME_PATH") or None
            chrome_channel = os.environ.get("CHROME_CHANNEL") or None
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": args,
            }
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path
            if chrome_channel:
                launch_kwargs["channel"] = chrome_channel
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
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
        persist = os.environ.get("T3_PERSIST_PROFILE", "0") != "0"
        if not persist:
            await self._ensure_browser()
            assert self._browser is not None
        else:
            # Persistent mode needs Playwright running even if we won't
            # touch `self._browser`. Kick the shared janitor up the same
            # way as the ephemeral path does.
            async with self._lock:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                if self._cleanup_task is None or self._cleanup_task.done():
                    self._cleanup_task = asyncio.create_task(self._janitor())
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
        persistent_browser: Optional[Any] = None
        if persist:
            # Per-session persistent context: each domain gets its own
            # user-data-dir so localStorage/IndexedDB/service-workers/
            # cf_clearance survive across sessions within this VM
            # lifetime. Different launch semantics from ephemeral mode:
            # `launch_persistent_context` takes launch + context kwargs
            # in a single call and returns a BrowserContext whose
            # owning Browser must be closed with it.
            profile_dir = _resolve_profile_dir(domain)
            persist_args: list[str] = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if os.environ.get("T3_DISABLE_HTTP2", "1") != "0":
                persist_args.append("--disable-http2")
            persist_headless = os.environ.get("T3_HEADLESS", "1") != "0"
            _maybe_start_xvfb(persist_headless)
            persist_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": persist_headless,
                "args": persist_args,
                "viewport": {"width": viewport[0], "height": viewport[1]},
                "locale": "en-US",
                "timezone_id": "America/New_York",
                "proxy": launch_proxy,
                "user_agent": ua,
            }
            chrome_path = os.environ.get("CHROME_PATH") or None
            chrome_channel = os.environ.get("CHROME_CHANNEL") or None
            if chrome_path:
                persist_kwargs["executable_path"] = chrome_path
            if chrome_channel:
                persist_kwargs["channel"] = chrome_channel
            context = await self._pw.chromium.launch_persistent_context(**persist_kwargs)
            # Remember the browser handle so close() can tear it down.
            persistent_browser = getattr(context, "browser", None)
        else:
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
        # Seed the virtual cursor at a realistic-ish starting point
        # (upper-left quadrant, small random offset) so smooth-move to
        # the first click doesn't start from dead center.
        import random as _init_rng
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
            persistent_browser=persistent_browser,
            cursor_x=float(_init_rng.randint(280, 620)),
            cursor_y=float(_init_rng.randint(220, 420)),
        )

        # CDP screencast → T3 viewer. Starts streaming JPEG frames as
        # fast as CfT encodes them (every other frame at quality 60 ≈
        # 12-15 FPS in practice). The emit is per-frame into the bus;
        # the viewer's WS handler fans out to subscribers. Failures
        # here are non-fatal — polling-fallback still works.
        # Opt-out via T3_DISABLE_SCREENCAST=1 (saves bandwidth when
        # the viewer isn't in use, e.g. non-interactive runs).
        if os.environ.get("T3_DISABLE_SCREENCAST") != "1":
            frame_counter = {"n": 0}
            try:
                cdp = await context.new_cdp_session(page)
                self._sessions[sid].cdp = cdp

                def _on_frame(params: dict) -> None:
                    # Runs inside patchright's event loop. Emit into
                    # the bus + schedule the CDP ack as a task (ack
                    # is async so we can't await from a sync handler).
                    try:
                        data = params.get("data") or ""
                        md = params.get("metadata") or {}
                        from . import t3_event_bus as _bus
                        _bus.default().emit_screencast_frame(
                            sid, data,
                            width=int(md.get("deviceWidth") or 0),
                            height=int(md.get("deviceHeight") or 0),
                            timestamp=float(md.get("timestamp") or 0.0),
                        )
                        frame_counter["n"] += 1
                        # One-shot log on the first frame so the
                        # operator can verify screencast actually
                        # started (not just CDP attach).
                        if frame_counter["n"] == 1:
                            print(
                                f"  [cdp screencast] first frame received "
                                f"for {sid} ({len(data)} bytes)"
                            )
                        ack_sid = params.get("sessionId")
                        if ack_sid is not None:
                            asyncio.create_task(
                                cdp.send(
                                    "Page.screencastFrameAck",
                                    {"sessionId": int(ack_sid)},
                                ),
                            )
                    except Exception as exc:
                        logger.debug("screencast frame handler: %s", exc)

                cdp.on("Page.screencastFrame", _on_frame)
                # Tight parameters for smoother viewer — quality 55
                # (small JPEGs) at everyNthFrame=1 gives ~20-24 FPS
                # on a well-provisioned VM. Bump T3_SCREENCAST_QUALITY
                # down if bandwidth is a concern.
                quality = int(os.environ.get("T3_SCREENCAST_QUALITY") or 55)
                every_nth = int(os.environ.get("T3_SCREENCAST_EVERY_N") or 1)
                await cdp.send("Page.startScreencast", {
                    "format": "jpeg",
                    "quality": quality,
                    "everyNthFrame": every_nth,
                })
                print(
                    f"  [cdp screencast] started for {sid} "
                    f"(quality={quality}, everyNthFrame={every_nth})"
                )
            except Exception as exc:
                print(
                    f"  [cdp screencast] setup FAILED for {sid}: "
                    f"{type(exc).__name__}: {exc} — viewer will use "
                    f"250ms polling fallback"
                )
                logger.debug(
                    "CDP screencast setup failed (non-fatal): %s", exc,
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
                    import traceback as _tb
                    logger.warning(
                        "retry with fresh context failed (sid=%s): %s\n%s",
                        sid, exc, _tb.format_exc(),
                    )
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
        # Persistent-profile sessions do not participate in the fresh-UA
        # retry dance: the whole point of a persistent profile is that
        # continuity (same UA, same localStorage, same cf_clearance) is
        # what passes the challenge. Rebuilding would wipe it. Skip —
        # the CF solver will still humanize-wait on the current context.
        if s.persistent_browser is not None:
            logger.info(
                "retry skipped for persistent-profile session %s "
                "(rebuild would discard the profile)", sid,
            )
            return
        # Also defensive: if the shared browser is somehow None (shouldn't
        # happen in ephemeral mode but surfaced a NoneType crash before
        # this guard landed), bail cleanly instead of attacking None.
        if self._browser is None:
            logger.warning(
                "retry aborted for %s: shared browser is None "
                "(persistent mode without persistent_browser set?)", sid,
            )
            return
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

    async def _move_cursor_smooth(
        self,
        sid: str,
        target_x: float,
        target_y: float,
        *,
        steps: Optional[int] = None,
    ) -> None:
        """Move the browser cursor along a bezier arc to (target_x,
        target_y), emitting each intermediate point to the T3 event bus.

        Two wins: (1) the live viewer's SVG cursor glides instead of
        teleporting, so the UX feels continuous; (2) Cloudflare /
        Imperva sensor code sees smooth mousemove entropy instead of
        a bot-like jump to the click coordinate. Real humans take
        ~80-250 ms to move their cursor to a visible target; we
        replicate that with variable per-step delay.

        Safe on failure — any exception inside the loop just advances
        the cursor state and returns. Never raises into the caller.
        """
        import math as _math
        import random as _rng
        try:
            s = self._get(sid)
        except KeyError:
            return
        sx, sy = float(s.cursor_x), float(s.cursor_y)
        tx, ty = float(target_x), float(target_y)
        dx, dy = tx - sx, ty - sy
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 2.0:
            # Already on target — just update state, no motion needed.
            s.cursor_x, s.cursor_y = tx, ty
            return
        # Step count scales with distance so short hops aren't
        # needlessly chunky and long travels still look smooth.
        n = steps if steps is not None else max(6, min(24, int(dist / 45)))
        # Control-point offset perpendicular to the line gives an arc,
        # not a straight line. Real mouse paths curve slightly.
        perp_mag = _rng.uniform(-0.22, 0.22) * dist
        mx = (sx + tx) / 2.0
        my = (sy + ty) / 2.0
        if dist > 0.0:
            nx = -dy / dist
            ny = dx / dist
            mx += perp_mag * nx
            my += perp_mag * ny
        # Resolve the event bus once — re-importing on every step is
        # the kind of micro-overhead that adds up at 15-24 steps.
        try:
            from . import t3_event_bus as _bus_mod
            bus = _bus_mod.default()
        except Exception:
            bus = None
        page = s.page
        try:
            for i in range(1, n + 1):
                t = i / n
                # Ease-in-out: velocity higher mid-arc than at endpoints.
                t2 = t * t * (3 - 2 * t)
                x = (1 - t2) ** 2 * sx + 2 * (1 - t2) * t2 * mx + t2 ** 2 * tx
                y = (1 - t2) ** 2 * sy + 2 * (1 - t2) * t2 * my + t2 ** 2 * ty
                # Micro-jitter mimics hand tremor; too small to matter
                # for click accuracy at the final step since we snap to
                # tx,ty after the loop.
                x += _rng.uniform(-0.8, 0.8)
                y += _rng.uniform(-0.8, 0.8)
                try:
                    await page.mouse.move(x, y)
                except Exception:
                    break
                if bus is not None:
                    try:
                        bus.emit_cursor_move(sid, x, y)
                    except Exception:
                        pass
                await asyncio.sleep(_rng.uniform(0.008, 0.022))
            # Snap to the exact target so downstream `mouse.click()` lands
            # where it was asked. Don't skip — the jitter above would
            # otherwise leave us ±1 px off.
            try:
                await page.mouse.move(tx, ty)
            except Exception:
                pass
            if bus is not None:
                try:
                    bus.emit_cursor_move(sid, tx, ty)
                except Exception:
                    pass
        finally:
            s.cursor_x, s.cursor_y = tx, ty

    async def _wait_for_cf_clear(
        self,
        sid: str,
        *,
        timeout_s: float = 30.0,
        origin_url: str = "",
    ) -> dict[str, Any]:
        """Poll an active session until a Cloudflare-style challenge clears.

        Watches for three exit conditions (any one trips):
          - title no longer matches known challenge strings
          - URL changed away from `origin_url`
          - at least one challenge-clearance cookie (cf_clearance, __cf_bm,
            datadome, _abck, ak_bmsc, incap_ses_, reese84) present

        Humanizes during the wait (mouse moves + occasional wheel) so
        Cloudflare's sensor sees interaction signals. Used by both
        `_goto_with_warmup` (post-nav auto-wait) and the `solve_cf`
        captcha solver (explicit retry after a tool-surface detect).

        Returns a structured dict — callers decide whether `cleared=false`
        warrants escalation.
        """
        s = self._get(sid)
        import math as _math
        import random as _rng
        # ONLY cookies that specifically indicate a challenge has been
        # PASSED — not bot-management cookies (__cf_bm, _abck, ak_bmsc,
        # incap_ses_) which CF/Akamai set on every request regardless
        # of challenge outcome. Treating those as success signals causes
        # false positives where the page is still on the interstitial
        # but we declare solved=true and move on (observed 2026-04-21
        # on cars.com: solver returned solved=True in 1.2s while the
        # title was still 'Just a moment...').
        clearance_cookie_names = {
            "cf_clearance",   # Cloudflare challenge passed
            "datadome",       # DataDome challenge passed
            "reese84",        # Incapsula / Imperva challenge passed
        }
        origin = origin_url or s.page.url
        deadline = time.time() + float(timeout_s)
        iteration = 0
        cleared = False
        cookies_landed: list[str] = []
        final_title = ""
        final_url = origin
        # Nothing session-local needed here — `_move_cursor_smooth`
        # reads/writes the cursor position on `s` directly. Left a
        # stub name for the inner waypoint emitter so the loop body
        # below reads naturally.
        async def _humanize_move_to(tx: float, ty: float) -> None:
            await self._move_cursor_smooth(sid, tx, ty)
        try:
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
                url_changed = current_url and current_url != origin
                final_url = current_url or origin
                final_title = title_now
                # Exit fast: page no longer looks like a challenge AND either
                # URL redirected away OR we've seen at least two iterations
                # (the first iteration fires before the challenge JS runs).
                if not looks_challenge and (url_changed or iteration > 1):
                    cleared = True
                    break
                cookies_now = await s.context.cookies()
                names = {c.get("name", "") for c in cookies_now}
                matched = names & clearance_cookie_names
                # Only exit on cookie match when the page ALSO no longer
                # looks like a challenge — protects against the case
                # where cf_clearance is present (e.g. stale from the
                # cookie jar) but CF is still serving the interstitial
                # because it rejected the replayed value.
                if matched and not looks_challenge:
                    cookies_landed = sorted(matched)
                    # Clearance landed — give the redirect a beat to finish.
                    await asyncio.sleep(1.2)
                    cleared = True
                    break
                # Humanize during the wait: smooth bezier mouse arcs +
                # occasional wheel. CF's sensor counts mousemove events
                # and scores entropy/smoothness — teleports score as
                # bot-like, curves score as human. Each waypoint sits
                # inside the visible viewport with a short dwell before
                # the next arc starts.
                try:
                    # Bias waypoints away from the current cursor so
                    # we generate actual motion (picking near-same
                    # coords gives tiny jitters that CF ignores).
                    while True:
                        tx = _rng.randint(180, 1180)
                        ty = _rng.randint(140, 640)
                        if abs(tx - s.cursor_x) + abs(ty - s.cursor_y) > 120:
                            break
                    await _humanize_move_to(float(tx), float(ty))
                    # Brief dwell — humans pause between movements.
                    await asyncio.sleep(_rng.uniform(0.15, 0.5))
                    if iteration % 3 == 0:
                        try:
                            await s.page.mouse.wheel(0, _rng.randint(80, 220))
                        except Exception:
                            pass
                except Exception:
                    pass
                # Loop pacing — shorter than the old 1s because the
                # smooth-move itself now takes 150-500ms, so the wake
                # cadence naturally lands in the "real user" band.
                await asyncio.sleep(_rng.uniform(0.35, 0.75))
        except Exception as exc:
            import traceback as _tb
            logger.warning(
                "challenge-wait loop raised (sid=%s iter=%d): %s\n%s",
                sid, iteration, exc, _tb.format_exc(),
            )
            return {
                "cleared": False, "iterations": iteration,
                "cookies_landed": cookies_landed,
                "final_url": final_url, "final_title": final_title,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }
        return {
            "cleared": cleared, "iterations": iteration,
            "cookies_landed": cookies_landed,
            "final_url": final_url, "final_title": final_title,
            "error": "",
        }

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

        # Self-clearing challenge wait (CF "Just a moment", DataDome auto-
        # verify, basic Akamai IUAM). Humanized polling lives in
        # `_wait_for_cf_clear` so the dedicated CF captcha solver can
        # reuse it. Defaults 30s, configurable via T3_CF_WAIT_S.
        wait_s = float(os.environ.get("T3_CF_WAIT_S") or 30.0)
        await self._wait_for_cf_clear(
            sid, timeout_s=wait_s, origin_url=s.page.url,
        )

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
        # Live viewer telemetry — banner shows the current URL + title
        # so the operator always knows where the worker landed.
        try:
            from . import t3_event_bus as _bus
            _bus.default().emit_navigation(
                sid, state.get("url", ""), state.get("title", ""),
            )
        except Exception:
            pass
        return {**nav, **{k: v for k, v in state.items() if k not in nav}}

    async def close(self, sid: str) -> dict[str, Any]:
        s = self._sessions.pop(sid, None)
        if s is None:
            return {"success": False}
        # Stop screencast before tearing down the context; otherwise
        # CDP warns about dangling listeners during context shutdown.
        if s.cdp is not None:
            try:
                await s.cdp.send("Page.stopScreencast")
            except Exception as exc:
                logger.debug("stop screencast %s: %s", sid, exc)
        try:
            await s.context.close()
        except Exception as exc:
            logger.debug("context close %s: %s", sid, exc)
        # Per-session persistent browser — close only applies when the
        # session was opened with launch_persistent_context. Shared
        # browser (ephemeral mode) stays alive for other sessions.
        if s.persistent_browser is not None:
            try:
                await s.persistent_browser.close()
            except Exception as exc:
                logger.debug("persistent browser close %s: %s", sid, exc)
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
        """Run a JS snippet in the page and return its result.

        Playwright's `page.evaluate(str)` only accepts a bare expression
        or a function literal — a statement body like
        `const x = foo(); return x.bar;` raises `SyntaxError: Illegal
        return statement` because `return` at the top level is invalid
        JS. Nanobot's LLM and the TS server's `/evaluate` route both
        emit statement-body scripts routinely, so we auto-wrap anything
        that looks like one into an IIFE `(() => { <body> })()`. Bare
        expressions pass through untouched.
        """
        s = self._get(sid)
        body = script.strip()
        # Wrap only when it's a statement body, never a function literal —
        # arrow fns / `async () => {}` / bare `function` / IIFE `(() => ...)()`
        # / block `{ ... }` all stay as-is so their semantics are preserved.
        starts_function = body.startswith(
            ("(", "async ", "async(", "function", "=>", "{")
        )
        needs_wrap = False
        if body and not starts_function:
            import re as _re
            has_top_return = _re.search(
                r"(?:^|[\s;{])return(?:$|[\s(;])", body,
            ) is not None
            # Multi-statement: any ; that isn't just a trailing terminator.
            has_multi_stmt = ";" in body.rstrip(" \t\n;")
            needs_wrap = has_top_return or has_multi_stmt
        if needs_wrap:
            # `async () => {}` lets callers use `await` at the top of
            # the body the same way run_script does.
            script = f"(async () => {{ {body} }})()"
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
            # Smooth bezier approach to the target — intermediate cursor
            # positions stream to the viewer so the SVG arrow glides
            # instead of teleporting, and CF sensor code sees realistic
            # mousemove entropy.
            await self._move_cursor_smooth(sid, target_x, target_y)
            await asyncio.sleep(0.05)
            await s.page.mouse.click(target_x, target_y)
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200]}

        # Live viewer telemetry — pulse the clicked location with the
        # snap state + bbox (if any) so the operator sees exactly where
        # the cursor landed.
        try:
            from . import t3_event_bus as _bus
            _bus.default().emit_click_target(
                sid,
                x=target_x, y=target_y,
                snapped=snapped, bbox=bbox, target=snap_target or None,
            )
        except Exception:
            pass

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
        # Smooth cursor approach to the field before we touch it —
        # otherwise the SVG arrow in the viewer teleports to the
        # input, which looks robotic.
        try:
            await self._move_cursor_smooth(sid, cx, cy)
        except Exception:
            pass
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
        # Live viewer telemetry — show keystrokes as they land. One
        # emit per char so the indicator's buffer fills character-by-
        # character rather than appearing in a single 40-char blob.
        try:
            from . import t3_event_bus as _bus
            bus = _bus.default()
            for ch in text:
                bus.emit_keystroke(sid, ch)
        except Exception:
            pass
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
            from . import t3_event_bus as _bus
            bus = _bus.default()
        except Exception:
            bus = None
        try:
            for k in keys:
                await s.page.keyboard.press(k)
                # Live viewer: surface each key press in the typing
                # indicator so keyboard-only interactions (Tab, Enter,
                # arrows) are visible without waiting for the next
                # screencast frame.
                if bus is not None:
                    try:
                        bus.emit_keystroke(sid, k)
                    except Exception:
                        pass
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
        # Live viewer: surface a cursor move + "scrolling" click-pulse
        # at the viewport center so the operator sees activity even
        # before the next screencast frame lands.
        try:
            from . import t3_event_bus as _bus
            vp = await s.page.evaluate(
                "() => ({w: window.innerWidth || 1366, h: window.innerHeight || 768})",
            )
            cx = float(vp.get("w", 1366)) / 2
            cy = float(vp.get("h", 768)) / 2
            _bus.default().emit_cursor_move(sid, cx, cy)
            _bus.default().emit_click_target(
                sid, x=cx, y=cy, snapped=False,
                target=f"scroll:{direction or f'{percent}%'}",
            )
        except Exception:
            pass
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
            from . import t3_event_bus as _bus
            bus = _bus.default()
        except Exception:
            bus = None
        if bus is not None:
            try:
                bus.emit_drag(sid, start_x, start_y, end_x, end_y)
            except Exception:
                pass
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
                # Cursor telemetry — throttled inside the bus, safe to
                # fire on every step.
                if bus is not None:
                    try:
                        bus.emit_cursor_move(sid, x, y)
                    except Exception:
                        pass
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
