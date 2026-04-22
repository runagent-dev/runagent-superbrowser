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


def _env_light_mode() -> bool:
    """Global kill-switch for T3 antibot overhead.

    When set, disables bezier cursor humanization, shortens settle
    windows, uses faster load-state for navigation, and skips the
    30s CF warm-up wait. Useful when T3 is only being used because
    a session was already on T3 (e.g. auth flow continuation) but
    the target site itself has no bot protection.
    """
    return os.environ.get("T3_LIGHT_MODE", "0") == "1"


def _labels_match(expected: str, el_info: dict) -> bool:
    """Return True when the element at the click target plausibly
    matches the vision agent's label for this bbox.

    The matcher is intentionally lax — different sources of text
    (aria-label vs inner text vs placeholder) legitimately differ
    from the vision label by a few words — and it exists purely to
    catch the gross misfire where vision said "Buy Now" but we're
    about to click a wrapper <div> or a completely different element.

    Accepts a match when:
      - any word in `expected` (>= 3 chars) appears in any of the
        element's readable fields, case-insensitive, OR
      - the element is an input/textarea/select whose role/type
        matches the vision label semantics (input-like labels from
        vision can legitimately have no visible text).
    """
    if not expected:
        return True
    exp = expected.strip().lower()
    if not exp:
        return True
    fields = [
        (el_info.get("text") or "").lower(),
        (el_info.get("aria") or "").lower(),
        (el_info.get("title") or "").lower(),
        (el_info.get("placeholder") or "").lower(),
        (el_info.get("value") or "").lower(),
        (el_info.get("role") or "").lower(),
    ]
    haystack = " ".join(fields)
    if not haystack.strip():
        # Inputs/selects may legitimately have no visible text. Don't
        # reject them just for being empty — the snap already verified
        # the element sits within the bbox, which is strong enough.
        tag = (el_info.get("tag") or "").upper()
        if tag in {"INPUT", "TEXTAREA", "SELECT", "BUTTON"}:
            return True
        return False
    # Any non-trivial word from the expected label found in the
    # haystack is a pass. Split on whitespace and common JSON-ish
    # separators so phrases like "sign-in" still match.
    import re
    words = [
        w for w in re.split(r"[\s\-_/,.:;()\"']+", exp)
        if len(w) >= 3
    ]
    if not words:
        # Expected label was all punctuation / short tokens — fall back
        # to substring match.
        return exp in haystack
    return any(w in haystack for w in words)


# Chromium error substrings that mean "the proxy itself is broken, not
# the target site." On these, retrying the same URL against the same
# proxy will hit the exact same wall — the only useful recovery is to
# bypass the proxy entirely for this session.
#
# Excludes ERR_NAME_NOT_RESOLVED / ERR_CONNECTION_REFUSED / ERR_TIMED_OUT:
# those can be site-down or network-flaky and shouldn't silently demote
# the proxy config.
_PROXY_ERROR_SUBSTRINGS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_HTTPS_PROXY_TUNNEL_RESPONSE",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
    "ERR_UNEXPECTED_PROXY_AUTH",
)


def _is_proxy_error(nav_error: str) -> bool:
    """True when a navigation failure came from the proxy layer.

    Covers CONNECT refused (dead proxy), auth rejected (bad creds /
    expired subscription), and cert-chain mismatches on HTTPS proxies.
    Everything else — DNS, timeouts, site 5xx, CF challenges — routes
    through different recovery paths and MUST NOT bypass the proxy
    (doing so would leak the real IP to sites that block scraping).
    """
    if not nav_error:
        return False
    msg = nav_error.upper()
    return any(sig in msg for sig in _PROXY_ERROR_SUBSTRINGS)

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


# Injected via `context.add_init_script` at session open AND via
# on-demand `ensure_scene_observer()` (patchright's init_script goes
# through an HTTP inject route that doesn't fire on data: URLs or
# set_content, so synthetic test harnesses need the on-demand path).
# Every page gets a long-lived MutationObserver that watches for
# overlay churn: when a `position:fixed|absolute` element with
# z-index > 1000 is ADDED — the strong signal for "a modal just
# appeared" — the observer stamps `window.__superbrowser_scene_dirty_ts__`
# with `Date.now()`. Python-side code reads this in the screenshot
# path to bust the vision cache key.
#
# Removal-detection is intentionally weaker: `getComputedStyle` on a
# removed node returns empty, so our `significantNode` check won't
# fire for removedNodes. That's fine — removal is either driven by
# the brain's own dismiss click (handled by `verify_action.bbox_disappeared`)
# or by site JS auto-closing, which results in a new element appearing
# below (still triggers the add path). The observer's job is to catch
# SURPRISE modals that appear without a concurrent user action.
#
# The observer is attached to `document.documentElement` rather than
# `document.body` so it survives full page replacements (SPAs that
# replace body via route changes still keep the observer). It uses a
# small debounce (150ms) to coalesce bursts of mutations that often
# accompany overlay animations (the banner animates into view, then
# its children mutate several more times in the same frame).
#
# Safety: wrapped in a try/catch + idempotency guard so double-injection
# (iframe navigate, same-document nav) doesn't crash the page.
_SCENE_DIRTY_OBSERVER_JS = r"""
(() => {
  try {
    if (window.__superbrowser_scene_obs_installed__) return;
    window.__superbrowser_scene_obs_installed__ = true;
    window.__superbrowser_scene_dirty_ts__ = 0;

    const THRESHOLD_Z = 1000;
    const significantNode = (node) => {
      if (!(node instanceof Element)) return false;
      let cs;
      try { cs = getComputedStyle(node); } catch { return false; }
      if (!cs) return false;
      if (cs.position !== 'fixed' && cs.position !== 'absolute') return false;
      const z = parseInt(cs.zIndex, 10) || 0;
      return z > THRESHOLD_Z;
    };

    let debounceTimer = null;
    const markDirty = () => {
      if (debounceTimer !== null) return;
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        window.__superbrowser_scene_dirty_ts__ = Date.now();
      }, 150);
    };

    const install = () => {
      if (!document.documentElement) return false;
      const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
          for (const n of m.addedNodes) {
            if (significantNode(n)) { markDirty(); return; }
          }
          for (const n of m.removedNodes) {
            if (significantNode(n)) { markDirty(); return; }
          }
          // Style/class mutations on an element that might be fixed
          // — cheap check: its current computed style qualifies.
          if (m.type === 'attributes' && significantNode(m.target)) {
            markDirty();
            return;
          }
        }
      });
      obs.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['style', 'class', 'hidden'],
      });
      window.__superbrowser_scene_obs__ = obs;
      return true;
    };

    if (install()) return;
    // documentElement not ready — hook once it is.
    const poll = setInterval(() => {
      if (install()) clearInterval(poll);
    }, 50);
    setTimeout(() => clearInterval(poll), 5000);
  } catch (_e) { /* never break the page */ }
})();
"""


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
    # Last time the in-page MutationObserver reported a significant
    # scene change (new position:fixed z>1000 element added or
    # removed). Python-side timestamp; refreshed from
    # `window.__superbrowser_scene_dirty_ts__` during screenshot cache
    # key computation. Drives cache-bust for event-driven re-vision.
    scene_dirty_ts: float = 0.0
    # Timestamp of the last vision pass that CONSUMED scene_dirty_ts.
    # A scene_dirty event is considered "fresh" when scene_dirty_ts >
    # last_vision_ts; consumed otherwise.
    last_vision_ts: float = 0.0
    # Proxy was disabled for this session after a proxy-class nav error
    # (ERR_TUNNEL_CONNECTION_FAILED / ERR_PROXY_CONNECTION_FAILED).
    # Prevents the retry path from re-picking the same broken proxy on
    # every subsequent navigate. A broken proxy won't heal in 100ms;
    # the user re-enables it by closing the session (+ fixing .env).
    proxy_disabled: bool = False
    # Auto-downshift: when True, skip the antibot overhead (bezier
    # humanization, long CF waits, networkidle settle) for this
    # session. Set after a successful nav with NO CF/PX signals, so
    # easy sites match T1 performance while hard sites keep the full
    # armor. Forced on by T3_LIGHT_MODE=1.
    light_mode: bool = False


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

        # Event-driven re-vision hook: inject a MutationObserver that
        # flips a timestamp whenever a significant overlay appears or
        # disappears (position:fixed|absolute with z-index > 1000). The
        # screenshot code reads this timestamp before deciding whether
        # the vision cache key is still valid. If a modal appeared since
        # the last vision pass, cache is busted even though the URL +
        # DOM hash match. Falls back to passive (planner-driven) revision
        # on pages where the observer can't attach for any reason.
        if os.environ.get("SCENE_DIRTY_OBSERVER", "1") != "0":
            try:
                await context.add_init_script(_SCENE_DIRTY_OBSERVER_JS)
            except Exception as exc:
                logger.debug("scene-dirty observer inject failed: %s", exc)

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

        await self._attach_screencast(sid, context, page)

        if url:
            nav = await self._goto_with_warmup(sid, url, timeout_s)
            # Proxy-failure auto-recovery (checked FIRST — no point
            # checking CF block state if the proxy refused the tunnel,
            # the page is a chrome-error:// shell). Opt-out via
            # T3_PROXY_FALLBACK=0.
            if (
                os.environ.get("T3_PROXY_FALLBACK", "1") != "0"
                and _is_proxy_error(nav.get("nav_error", ""))
            ):
                logger.warning(
                    "open %s hit proxy error (%s) — falling back to "
                    "DIRECT connection and retrying once",
                    url, nav.get("nav_error", "")[:120],
                )
                try:
                    await self._rebuild_context_for_retry(
                        sid, domain, viewport, import_state,
                        force_direct=True,
                    )
                    nav = await self._goto_with_warmup(sid, url, timeout_s)
                    nav["proxy_fallback"] = True
                except Exception as exc:
                    logger.warning(
                        "proxy-fallback retry failed on open (sid=%s): %s",
                        sid, exc,
                    )
                    nav.setdefault("proxy_fallback_error", str(exc)[:200])
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

    async def _attach_screencast(
        self,
        sid: str,
        context: Any,
        page: Any,
    ) -> None:
        """Attach a CDP screencast for the live viewer. Idempotent-safe:
        call once per context (new session OR post-rebuild). Failures
        are non-fatal — the viewer falls back to 250ms polling.

        After a context rebuild (`_rebuild_context_for_retry` /
        `_relaunch_persistent_direct`), callers MUST invoke this to
        restart the screencast; otherwise the viewer stays frozen on
        the last frame from the old (dead) context, which is confusing
        because the dead frame usually shows a chrome-error page while
        the real new context has already loaded the target site.
        """
        if os.environ.get("T3_DISABLE_SCREENCAST") == "1":
            return
        frame_counter = {"n": 0}
        try:
            cdp = await context.new_cdp_session(page)
            s = self._sessions.get(sid)
            if s is not None:
                s.cdp = cdp

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

    async def _relaunch_persistent_direct(
        self,
        sid: str,
        viewport: tuple[int, int],
    ) -> None:
        """Tear down a persistent-profile session and relaunch it DIRECT.

        Used when the current proxy is refusing connections — a case
        where profile continuity is worth less than basic reachability.
        Keeps the same `user_data_dir` so any localStorage /
        IndexedDB / service-worker state the previous session managed
        to land stays intact.
        """
        s = self._sessions.get(sid)
        if s is None:
            raise KeyError(f"session not found: {sid}")
        domain = s.domain or ""
        profile_dir = _resolve_profile_dir(domain)
        # Stop the CDP screencast FIRST — closing the context while the
        # screencast task is mid-flight produces a "TargetClosedError:
        # CDPSession.send" that asyncio logs as "Task exception was
        # never retrieved". Cosmetic but noisy. Mirror the teardown
        # order in `close()`.
        if s.cdp is not None:
            try:
                await s.cdp.send("Page.stopScreencast")
            except Exception as exc:
                logger.debug("stop screencast before relaunch %s: %s", sid, exc)
            s.cdp = None
        try:
            await s.context.close()
        except Exception:
            pass
        try:
            if s.persistent_browser is not None:
                await s.persistent_browser.close()
        except Exception:
            pass
        persist_headless = os.environ.get("T3_HEADLESS", "1") != "0"
        _maybe_start_xvfb(persist_headless)
        persist_args: list[str] = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if os.environ.get("T3_DISABLE_HTTP2", "1") != "0":
            persist_args.append("--disable-http2")
        persist_kwargs: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": persist_headless,
            "args": persist_args,
            "viewport": {"width": viewport[0], "height": viewport[1]},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            # No proxy key at all — direct connection.
            "user_agent": s.ua,
        }
        chrome_path = os.environ.get("CHROME_PATH") or None
        chrome_channel = os.environ.get("CHROME_CHANNEL") or None
        if chrome_path:
            persist_kwargs["executable_path"] = chrome_path
        if chrome_channel:
            persist_kwargs["channel"] = chrome_channel
        if self._pw is None:
            # Shouldn't happen — _ensure_browser ran during open() — but
            # fail cleanly rather than crash if someone calls this mid-
            # teardown.
            raise RuntimeError("playwright not initialized; cannot relaunch")
        new_context = await self._pw.chromium.launch_persistent_context(
            **persist_kwargs,
        )
        # Apply the same stealth surface as the original launch.
        stealth = Stealth(
            navigator_user_agent_override=s.ua,
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
        try:
            await stealth.apply_stealth_async(new_context)
        except Exception as exc:
            logger.debug("stealth reapply failed (non-fatal): %s", exc)
        # launch_persistent_context gives back a context with at least
        # one page already. Reuse it instead of creating another.
        pages = new_context.pages
        new_page = pages[0] if pages else await new_context.new_page()
        s.context = new_context
        s.page = new_page
        s.persistent_browser = getattr(new_context, "browser", None)
        s.proxy = None
        s.proxy_disabled = True
        # Re-attach CDP screencast so the live viewer doesn't stay
        # frozen on the old chrome-error frame. Without this the UI
        # looks like "site is temporarily down" even after the direct
        # retry has successfully loaded the target.
        await self._attach_screencast(sid, new_context, new_page)
        logger.info(
            "persistent session %s relaunched DIRECT (profile=%s)",
            sid, profile_dir,
        )

    async def _rebuild_context_for_retry(
        self,
        sid: str,
        domain: str,
        viewport: tuple[int, int],
        import_state: Optional[dict],
        *,
        force_direct: bool = False,
    ) -> None:
        """Close and recreate the session's context with a different UA
        and a demoted proxy tier. Used as a one-shot retry on hard CF
        blocks. Preserves the session_id so the LLM's subsequent calls
        keep routing to the same slot.

        When `force_direct=True`, we skip `proxy_tiers.pick()` entirely
        and launch the new context with NO proxy. Used to recover from
        `ERR_TUNNEL_CONNECTION_FAILED` / `ERR_PROXY_CONNECTION_FAILED`
        — conditions where the proxy itself is the blocker, not the
        site. The session is flagged so subsequent navigations also
        stay direct (a broken proxy will still be broken in 100ms).
        """
        from .headers import for_profile, random_profile
        s = self._sessions.get(sid)
        if s is None:
            raise KeyError(f"session not found: {sid}")
        # Persistent-profile sessions normally skip the fresh-UA retry
        # dance — the whole point of a persistent profile is that
        # continuity (same UA, same localStorage, same cf_clearance) is
        # what passes the CF challenge. Rebuilding for CF blocks would
        # discard that and defeat the purpose.
        #
        # BUT: for force_direct (proxy dead), we MUST rebuild even on
        # persistent profiles. A broken proxy means every request is
        # failing upstream of the site, so profile continuity is moot
        # — nothing reached the site to populate the profile anyway.
        # Relaunch the persistent context with proxy=None, keeping the
        # same user_data_dir so any prior accumulated state is intact.
        if s.persistent_browser is not None and not force_direct:
            logger.info(
                "retry skipped for persistent-profile session %s "
                "(rebuild would discard the profile)", sid,
            )
            return
        if s.persistent_browser is not None and force_direct:
            await self._relaunch_persistent_direct(sid, viewport)
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
        if force_direct:
            # Proxy recovery path: don't demote (which would pick a
            # different proxy that's probably just as broken), just skip
            # the proxy layer entirely for this session. Remember it on
            # the session so future navigations stay direct.
            new_proxy = None
            launch_proxy = None
            s.proxy_disabled = True
            logger.info(
                "session %s switched to DIRECT (bypassing proxy) after "
                "proxy-class navigation error", sid,
            )
        else:
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
        # Stop CDP screencast before context close to avoid a stray
        # "TargetClosedError: CDPSession.send" on the background task.
        if s.cdp is not None:
            try:
                await s.cdp.send("Page.stopScreencast")
            except Exception:
                pass
            s.cdp = None
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
        # Re-attach CDP screencast — see note in _relaunch_persistent_direct.
        await self._attach_screencast(sid, new_context, new_page)

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
        # Light mode: teleport the cursor in a single move. Skips the
        # 50-500ms bezier humanization that real anti-bot sites need
        # but easy sites don't. Matches T1's "no cursor simulation"
        # behaviour so perceived click latency drops to T1 levels.
        if s.light_mode or _env_light_mode():
            try:
                await s.page.mouse.move(tx, ty)
            except Exception:
                pass
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
        _light = bool(s.light_mode or _env_light_mode())
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
            not _light
            and warmup.should_warmup(url)
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
        # navigate. Tune via T3_NAV_JITTER=0 to disable. Auto-skipped in
        # light mode.
        import random as _random
        if not _light and os.environ.get("T3_NAV_JITTER") != "0":
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

        nav_error: str = ""
        try:
            goto_kwargs: dict[str, Any] = {
                # Light mode uses 'commit' — the fastest safe signal
                # (response headers received). T1's default is also
                # 'commit'-equivalent (no full-load wait), so this
                # matches T1's perceived snap. Full mode keeps
                # 'domcontentloaded' so the warmup + CF probe still
                # have a parsed page to inspect.
                "wait_until": "commit" if _light else "domcontentloaded",
                "timeout": int(timeout_s * 1000),
            }
            if referer:
                goto_kwargs["referer"] = referer
            resp = await s.page.goto(url, **goto_kwargs)
            status = resp.status if resp else 0
        except Exception as exc:
            logger.warning("navigate %s failed: %s", url, exc)
            status = 0
            # Preserve the raw error string so the caller can classify it
            # (proxy failure vs DNS vs site-down vs CF block). Chromium
            # errors show up as "net::ERR_*" in the exception message;
            # without capturing we can only see status=0.
            nav_error = str(exc)[:500]

        # Skip the networkidle wait in light mode — T1 doesn't do
        # this either; it just fires the tool observation after commit
        # and lets the next screenshot pick up any post-nav changes.
        if not _light:
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
        # reuse it. Defaults 30s, configurable via T3_CF_WAIT_S. Light
        # mode skips the wait entirely — the whole point of light mode
        # is "this site has no CF"; the auto-downshift logic below will
        # refuse to flip on sessions that hit a CF challenge.
        if not _light:
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
        if nav_error:
            result["nav_error"] = nav_error
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
        # Auto-downshift: if this nav succeeded cleanly (200-ish status,
        # no CF / Turnstile / block_class signals) and the session
        # isn't already in light mode, flip it on. Subsequent clicks &
        # nav use the T1-parity fast path. Hard sites never trip this
        # branch because the CF probe above tags `block_class` or
        # `turnstile` before we reach here.
        #
        # Disable the downshift with T3_AUTO_DOWNSHIFT=0 to preserve
        # full-armor behaviour (e.g., on sessions that start easy but
        # become hostile on sub-pages).
        if (
            not _light
            and os.environ.get("T3_AUTO_DOWNSHIFT", "1") != "0"
            and not s.light_mode
            and not result.get("block_class")
            and not result.get("turnstile")
            and 200 <= int(result.get("status") or 0) < 400
        ):
            s.light_mode = True
            logger.info(
                "t3 auto-downshift: session=%s host=%s (no CF signals)",
                sid, urlparse(url).hostname or "",
            )
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
        # Proxy-failure auto-recovery: if Chromium reported a proxy-layer
        # error (tunnel refused, auth rejected, etc.) AND we're not
        # already running direct, rebuild the context without a proxy
        # and retry once. Opt-out via T3_PROXY_FALLBACK=0 for deployments
        # that MUST keep the proxy (real IP leak is worse than failure).
        s = self._sessions.get(sid)
        if (
            s is not None
            and not s.proxy_disabled
            and os.environ.get("T3_PROXY_FALLBACK", "1") != "0"
            and _is_proxy_error(nav.get("nav_error", ""))
        ):
            logger.warning(
                "navigate %s hit proxy error (%s) — falling back to "
                "DIRECT connection and retrying once",
                url, nav.get("nav_error", "")[:120],
            )
            try:
                # Reuse the existing viewport/import_state knobs by
                # reading them off the current session.
                viewport = (1280, 720)
                try:
                    vs = await s.page.evaluate(
                        "() => [window.innerWidth || 1280, window.innerHeight || 720]"
                    )
                    if isinstance(vs, list) and len(vs) == 2:
                        viewport = (int(vs[0]), int(vs[1]))
                except Exception:
                    pass
                await self._rebuild_context_for_retry(
                    sid, s.domain, viewport, None, force_direct=True,
                )
                nav = await self._goto_with_warmup(sid, url, timeout_s)
                nav["proxy_fallback"] = True
            except Exception as exc:
                logger.warning(
                    "proxy-fallback retry failed for %s: %s", url, exc,
                )
                nav.setdefault("proxy_fallback_error", str(exc)[:200])
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

    async def ensure_scene_observer(self, sid: str) -> None:
        """Idempotently install the MutationObserver on the current page.

        Patchright's `context.add_init_script` goes through an
        inject_route that only fires on HTTP(S) navigations; in local
        test harnesses (data: URLs, set_content) the script never lands.
        To stay robust across both real and synthetic environments, we
        also install the observer on-demand here. The JS is guarded by
        `window.__superbrowser_scene_obs_installed__` so re-injection
        on a live page is a no-op.
        """
        if os.environ.get("SCENE_DIRTY_OBSERVER", "1") == "0":
            return
        s = self._sessions.get(sid)
        if not s:
            return
        try:
            await s.page.evaluate(_SCENE_DIRTY_OBSERVER_JS)
        except Exception as exc:
            logger.debug("ensure_scene_observer inject failed: %s", exc)

    async def consume_scene_dirty(self, sid: str) -> bool:
        """Was the scene changed by a DOM mutation since the last vision pass?

        Reads `window.__superbrowser_scene_dirty_ts__` (set by the
        injected MutationObserver) and compares it to the session's
        `last_vision_ts`. Returns True if a mutation was recorded since
        the last consume + updates `last_vision_ts` so the next call
        only reports NEW mutations.

        On probe failure (page closed, binding unavailable), fail safe:
        return False so we don't thrash the vision cache. The planner-
        driven revision path still kicks in for explicit URL/flag changes.
        """
        s = self._sessions.get(sid)
        if not s:
            return False
        # Ensure the observer exists on this page (handles navigation to
        # a fresh document that wiped the prior instance).
        await self.ensure_scene_observer(sid)
        try:
            raw = await s.page.evaluate(
                "() => (window.__superbrowser_scene_dirty_ts__ || 0)"
            )
        except Exception as exc:
            logger.debug("consume_scene_dirty eval failed: %s", exc)
            return False
        try:
            ts_ms = float(raw or 0) / 1000.0
        except (TypeError, ValueError):
            return False
        if ts_ms <= 0:
            return False
        # Convert to a comparable "since session start" monotonic; here
        # we just compare the JS Date.now() epoch seconds against the
        # last consume stamp. Python's time.time() is also epoch seconds
        # so the comparison is valid, subject to minor clock skew —
        # acceptable because we only care about "has anything happened
        # since the last pass, within this process".
        changed = ts_ms > s.last_vision_ts
        if changed:
            s.last_vision_ts = max(ts_ms, s.last_vision_ts)
            s.scene_dirty_ts = ts_ms
        return changed

    async def evaluate(self, sid: str, script: str, arg: Any = None) -> Any:
        """Run a JS snippet in the page and return its result.

        Playwright's `page.evaluate(str)` only accepts a bare expression
        or a function literal — a statement body like
        `const x = foo(); return x.bar;` raises `SyntaxError: Illegal
        return statement` because `return` at the top level is invalid
        JS. Nanobot's LLM and the TS server's `/evaluate` route both
        emit statement-body scripts routinely, so we auto-wrap anything
        that looks like one into an IIFE `(() => { <body> })()`. Bare
        expressions pass through untouched.

        `arg` — when the script is a function literal (`(x) => ...`,
        `async (x) => ...`), Playwright passes `arg` as that function's
        single parameter. For statement-body scripts auto-wrapped into
        an IIFE, arg is ignored (no way to thread it through).
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
        if arg is not None:
            return await s.page.evaluate(script, arg)
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
        self,
        sid: str,
        x: float,
        y: float,
        *,
        bbox: Optional[dict] = None,
        strategy: str = "primary",
        expected_label: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute a click. `strategy` selects the dispatch mechanism.

        Strategies, in escalating order of desperation:
          - "primary"  — smooth-cursor bezier approach + mouse.click (default).
                          What real users look like. Tier-1 CF sensors
                          see realistic mousemove entropy here.
          - "keyboard" — focus the element via JS, then keyboard.press('Enter').
                          Works when the element has `pointer-events: none`
                          on the visual and the real hit target is an
                          invisible overlay a few pixels off.
          - "js"       — `el.click()` directly on the snapped element.
                          Bypasses any CSS-level pointer masking.
          - "parent"   — walk up to nearest button/a/[role=button] and click
                          *its* center. Cookie banners often have a padded
                          wrapper eating pointer events.

        Wrapper code (session_tools.BrowserClickAtTool) runs the ladder:
        call primary, verify postcondition, if it missed → call keyboard,
        verify again, and so on. `click_at` itself is stateless — one
        call = one strategy = one attempt.
        """
        s = self._get(sid)
        target_x, target_y = x, y
        snapped = False
        snap_target = ""
        snap_info: Optional[dict] = None
        if bbox is not None:
            try:
                snap_info = await s.page.evaluate(
                    """({x0, y0, x1, y1}) => {
                      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
                      const el = document.elementFromPoint(cx, cy);
                      if (!el) return null;
                      const r = el.getBoundingClientRect();
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
                if isinstance(snap_info, dict):
                    target_x = float(snap_info.get("x", x))
                    target_y = float(snap_info.get("y", y))
                    snapped = True
                    snap_target = f"{snap_info.get('tag','')}:{snap_info.get('text','')}"
            except Exception as exc:
                logger.debug("snap eval failed: %s", exc)

        # Semantic-match guard (P1.4). If the caller gave us the label
        # the vision agent used for this bbox, fetch the element at the
        # snapped target and confirm it looks like the right thing.
        # Catches the "vision said 'Buy Now' but the element at (x,y)
        # is actually a parent <section>" class of misfires that would
        # otherwise land a click on the wrong target.
        if expected_label and expected_label.strip():
            try:
                elem_info = await s.page.evaluate(
                    """([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el) return null;
                        const attr = (k) => (el.getAttribute(k) || '').trim();
                        return {
                            tag: el.tagName,
                            role: attr('role'),
                            aria: attr('aria-label'),
                            title: attr('title'),
                            placeholder: attr('placeholder'),
                            value: (el.value || '').toString().trim(),
                            text: (el.innerText || '').trim().slice(0, 160),
                        };
                    }""",
                    [target_x, target_y],
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("elementFromPoint for semantic match failed: %s", exc)
                elem_info = None
            if isinstance(elem_info, dict):
                if not _labels_match(expected_label, elem_info):
                    return {
                        "success": False,
                        "error": "element_mismatch",
                        "expected_label": expected_label[:120],
                        "found": {
                            "tag": elem_info.get("tag"),
                            "role": elem_info.get("role"),
                            "text": (
                                elem_info.get("text")
                                or elem_info.get("aria")
                                or elem_info.get("title")
                                or ""
                            )[:120],
                        },
                        "coords": {"x": int(target_x), "y": int(target_y)},
                        "strategy": strat if False else (strategy or "primary"),
                    }

        strat = (strategy or "primary").lower()
        # Human-like dwell between mousedown and mouseup. A same-tick
        # click (playwright's default delay=0) fires mousedown +
        # mouseup with no gap, which breaks:
        #   - React / Vue click handlers that read intermediate state
        #   - drag recognizers (treat 0ms dwell as null-drag → no click)
        #   - analytics / bot scoring (0ms dwell flagged, handler
        #     silently dropped)
        # Tune via T3_CLICK_DELAY_MS_MIN / MAX. Defaults match the
        # 40-120ms range observed on real human mousedown traces.
        try:
            dwell_min = int(os.environ.get("T3_CLICK_DELAY_MS_MIN") or "40")
            dwell_max = int(os.environ.get("T3_CLICK_DELAY_MS_MAX") or "120")
        except ValueError:
            dwell_min, dwell_max = 40, 120
        import random as _rng
        click_dwell_ms = _rng.uniform(dwell_min, max(dwell_min, dwell_max))
        _light_click = bool(s.light_mode or _env_light_mode())
        try:
            if strat == "primary":
                await self._move_cursor_smooth(sid, target_x, target_y)
                # Skip the 50ms settle in light mode — matches T1 which
                # has no such pause. The move + click can fire in the
                # same tick.
                if not _light_click:
                    await asyncio.sleep(0.05)
                await s.page.mouse.click(
                    target_x, target_y, delay=click_dwell_ms,
                )
            elif strat == "keyboard":
                await self._move_cursor_smooth(sid, target_x, target_y)
                await asyncio.sleep(0.05)
                focus_js = """([x, y]) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return {ok: false, reason: 'no element'};
                  try {
                    if (typeof el.focus === 'function') el.focus();
                  } catch {}
                  return {ok: true, tag: el.tagName,
                          role: el.getAttribute('role') || ''};
                }"""
                info = await s.page.evaluate(focus_js, [target_x, target_y])
                # Default to Enter; Space for checkbox/radio roles.
                key = "Enter"
                if isinstance(info, dict):
                    r = (info.get("role") or "").lower()
                    if r in ("checkbox", "radio"):
                        key = "Space"
                await s.page.keyboard.press(key)
            elif strat == "js":
                js_click = """([x, y]) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return {ok: false, reason: 'no element'};
                  try {
                    if (typeof el.click === 'function') el.click();
                    else el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                  } catch (e) { return {ok: false, reason: String(e).slice(0, 80)}; }
                  return {ok: true, tag: el.tagName};
                }"""
                await s.page.evaluate(js_click, [target_x, target_y])
            elif strat == "parent":
                parent_js = """([x, y]) => {
                  let el = document.elementFromPoint(x, y);
                  if (!el) return {ok: false, reason: 'no element'};
                  let n = el;
                  let hops = 0;
                  while (n && n !== document.body && hops < 8) {
                    const tag = n.tagName;
                    const role = n.getAttribute && n.getAttribute('role');
                    if (tag === 'BUTTON' || tag === 'A' || role === 'button') {
                      try { n.click(); return {ok: true, tag, hops}; }
                      catch (e) {}
                    }
                    n = n.parentElement;
                    hops++;
                  }
                  return {ok: false, reason: 'no button ancestor'};
                }"""
                await s.page.evaluate(parent_js, [target_x, target_y])
            else:
                return {"success": False, "error": f"unknown strategy: {strat}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200], "strategy": strat}

        try:
            from . import t3_event_bus as _bus
            _bus.default().emit_click_target(
                sid,
                x=target_x, y=target_y,
                snapped=snapped, bbox=bbox, target=snap_target or None,
                strategy=strat,
            )
        except Exception:
            pass

        _light = bool(s.light_mode or _env_light_mode())
        try:
            await s.page.wait_for_load_state(
                "domcontentloaded",
                # Light mode uses a shorter wait — on easy sites the
                # DCL event lands almost immediately, and a 5s ceiling
                # just delays the state() fetch on SPAs that don't
                # fire DCL twice.
                timeout=1500 if _light else 5000,
            )
        except Exception:
            pass
        # Brief JS-render settle so React/Vue effects commit before we
        # snapshot the DOM and screenshot. Without this the screenshot
        # can catch a mid-transition frame and vision anchors on
        # placeholders rather than the settled elements. 250ms is enough
        # for most SPAs; configurable via T3_POST_CLICK_SETTLE_MS. Light
        # mode defaults to 0 (T1 parity).
        try:
            default_settle = "0" if _light else "250"
            settle_ms = int(
                os.environ.get("T3_POST_CLICK_SETTLE_MS") or default_settle
            )
        except ValueError:
            settle_ms = 0 if _light else 250
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
        # Skip networkidle in light mode — matches T1's simpler
        # waitForIdle(1.5s) semantics (T1 doesn't wait for networkidle
        # either; it waits for a short idle window).
        if not _light:
            try:
                await s.page.wait_for_load_state("networkidle", timeout=1500)
            except Exception:
                pass
        st = await self.state(sid)
        return {
            "success": True,
            "strategy": strat,
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
            try:
                type_min = int(os.environ.get("T3_CLICK_DELAY_MS_MIN") or "40")
                type_max = int(os.environ.get("T3_CLICK_DELAY_MS_MAX") or "120")
            except ValueError:
                type_min, type_max = 40, 120
            import random as _rng
            await s.page.mouse.click(
                cx, cy, delay=_rng.uniform(type_min, max(type_min, type_max)),
            )
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
        # Post-type DOM readback + self-heal. Autocomplete dropdowns,
        # IME composition glitches, and input-event handlers can swallow
        # or substitute characters silently — you intended "Kevin Hart"
        # and the field ends up with "Kevin Hert". If the readback shows
        # a tiny mismatch (≤3 chars), we fix it in place using the same
        # native-setter approach the clear path uses. Larger diffs are
        # left alone (legit autocomplete expansion, e.g. "Kevin Hart" ->
        # "Kevin Hart Live 2026").
        settle = float(os.environ.get("T3_POSTTYPE_SETTLE_MS") or 120) / 1000.0
        if settle > 0:
            await asyncio.sleep(settle)
        posttype_note = ""
        if os.environ.get("T3_POSTTYPE_HEAL", "1") != "0":
            try:
                probe2 = await s.page.evaluate(
                    """({x, y}) => {
                      const el = document.elementFromPoint(x, y);
                      if (!el) return null;
                      if ('value' in el && el.value !== undefined)
                        return {v: el.value};
                      if (el.isContentEditable)
                        return {v: el.innerText || ''};
                      return {v: (el.textContent || '').trim()};
                    }""",
                    {"x": cx, "y": cy},
                )
                post_value = str((probe2 or {}).get("v", "") or "")
                if post_value != text:
                    def _levenshtein(a: str, b: str, cap: int = 4) -> int:
                        # Bounded Wagner-Fischer — returns cap+1 once the
                        # distance is known to exceed cap. O(n*m) time
                        # but n,m are tiny strings so it's ~µs.
                        if abs(len(a) - len(b)) > cap:
                            return cap + 1
                        if not a:
                            return len(b) if len(b) <= cap else cap + 1
                        if not b:
                            return len(a) if len(a) <= cap else cap + 1
                        prev = list(range(len(b) + 1))
                        for i in range(1, len(a) + 1):
                            cur = [i] + [0] * len(b)
                            best = cur[0]
                            for j in range(1, len(b) + 1):
                                ins = cur[j - 1] + 1
                                dele = prev[j] + 1
                                sub = prev[j - 1] + (0 if a[i-1] == b[j-1] else 1)
                                cur[j] = min(ins, dele, sub)
                                if cur[j] < best:
                                    best = cur[j]
                            if best > cap:
                                return cap + 1
                            prev = cur
                        return prev[-1]
                    dist = _levenshtein(post_value, text, cap=3)
                    if dist <= 3 and dist > 0:
                        # Small drift — reset the field to the intended
                        # value via native setter (React/Vue-safe).
                        try:
                            await s.page.evaluate(
                                """({x, y, target}) => {
                                  const el = document.elementFromPoint(x, y);
                                  if (!el) return false;
                                  try {
                                    let proto = null;
                                    if (el.tagName === 'TEXTAREA') proto = HTMLTextAreaElement.prototype;
                                    else if (el.tagName === 'INPUT') proto = HTMLInputElement.prototype;
                                    if (proto) {
                                      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                                      if (desc && desc.set) {
                                        desc.set.call(el, target);
                                        el.dispatchEvent(new Event('input', {bubbles: true}));
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        return true;
                                      }
                                      el.value = target;
                                      return true;
                                    }
                                    if (el.isContentEditable) {
                                      el.textContent = target;
                                      el.dispatchEvent(new Event('input', {bubbles: true}));
                                      return true;
                                    }
                                  } catch (_) {}
                                  return false;
                                }""",
                                {"x": cx, "y": cy, "target": text},
                            )
                            posttype_note = (
                                f"self_heal: was {post_value!r}, "
                                f"reset to {text!r} (dist={dist})"
                            )
                            logger.info(
                                "type self-heal at (%s,%s): %r -> %r (dist=%d)",
                                cx, cy, post_value, text, dist,
                            )
                        except Exception as exc:
                            logger.debug("self-heal apply failed: %s", exc)
                    elif dist > 3:
                        # Large drift — probably autocomplete expansion.
                        # Log but don't touch.
                        posttype_note = (
                            f"skipped_self_heal: dom={post_value!r} "
                            f"diff>3 chars (assuming autocomplete)"
                        )
            except Exception as exc:
                logger.debug("post-type probe failed: %s", exc)
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
        if posttype_note:
            st = dict(st)
            st["posttype_note"] = posttype_note
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
