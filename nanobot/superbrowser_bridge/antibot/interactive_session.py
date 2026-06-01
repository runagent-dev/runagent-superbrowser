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
import re
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


# Shadow-DOM piercing helpers, mirrored from src/browser/slider-helpers.ts.
# Concatenated into every frame.evaluate(...) call in the slider methods so
# selector probes resolve into open shadow roots (Chase mds-slider, any
# Lit/React widget that hosts a native range input under a custom element).
# Keep this string in sync with the TS version.
_SHADOW_DOM_HELPERS_SRC = """
function __sb_queryDeep(root, sel) {
  if (!root) return null;
  var direct = (root.querySelector ? root.querySelector(sel) : null);
  if (direct) return direct;
  var queue = [root];
  while (queue.length) {
    var node = queue.shift();
    var children = (node.children || []);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.shadowRoot) {
        var hit = c.shadowRoot.querySelector(sel);
        if (hit) return hit;
        queue.push(c.shadowRoot);
      }
      if (c.children && c.children.length) queue.push(c);
    }
  }
  return null;
}
function __sb_queryAllDeep(root, sel) {
  if (!root) return [];
  var out = [];
  var seen = (typeof Set === 'function') ? new Set() : null;
  function pushUnique(el) {
    if (seen) { if (seen.has(el)) return; seen.add(el); }
    out.push(el);
  }
  function collect(scope) {
    if (!scope || !scope.querySelectorAll) return;
    var hits = scope.querySelectorAll(sel);
    for (var i = 0; i < hits.length; i++) pushUnique(hits[i]);
  }
  collect(root);
  var queue = [root];
  while (queue.length) {
    var node = queue.shift();
    var children = (node.children || []);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.shadowRoot) { collect(c.shadowRoot); queue.push(c.shadowRoot); }
      if (c.children && c.children.length) queue.push(c);
    }
  }
  return out;
}
function __sb_walkDeepElements(root, visit) {
  if (!root) return;
  var queue = [root];
  while (queue.length) {
    var node = queue.shift();
    var children = (node.children || []);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      var stop = visit(c);
      if (stop === false) return;
      if (c.shadowRoot) queue.push(c.shadowRoot);
      if (c.children && c.children.length) queue.push(c);
    }
  }
}
function __sb_dispatchHostSignal(el, eventNames) {
  try {
    var current = el;
    var guard = 0;
    while (current && guard++ < 8) {
      var root = current.getRootNode ? current.getRootNode() : null;
      if (!root || root === document || !root.host) break;
      for (var i = 0; i < eventNames.length; i++) {
        try { root.host.dispatchEvent(new Event(eventNames[i], { bubbles: true })); } catch (e) {}
      }
      current = root.host;
    }
  } catch (e) {}
}
"""


def _env_light_mode() -> bool:
    """Global kill-switch for T3 antibot overhead.

    When set, disables bezier cursor humanization, shortens settle
    windows, uses faster load-state for navigation, and skips the
    30s CF warm-up wait. Useful when T3 is only being used because
    a session was already on T3 (e.g. auth flow continuation) but
    the target site itself has no bot protection.

    Also returns True when INTERACTIVE_HUMANIZE_MODE=off — the
    tri-state env wins over the legacy binary one when both are set,
    but here we just need to know "should the session start light?"
    """
    if os.environ.get("T3_LIGHT_MODE", "0") == "1":
        return True
    return _humanize_mode() == "off"


def _humanize_mode() -> str:
    """Tri-state humanization policy.

    Values:
      always     — bezier + micro-motions on every action (the safest
                   default for a brand-new untrusted account on hostile
                   sites; matches behaviour before this knob existed).
      challenged — start cheap, only flip to full humanization after
                   the session observes an antibot signal (CF challenge,
                   Turnstile, captcha, 4xx from antibot). This gives
                   T3 sessions T1-grade latency on benign sites without
                   sacrificing protection where it matters. Recommended
                   default for the agent-quality complaint about T3
                   "feeling slower / behaving differently" on easy pages.
      off        — never humanize, equivalent to T3_LIGHT_MODE=1.

    `auto` is a back-compat alias for the legacy two-phase behavior:
    full humanization on first nav, then auto-downshift after a clean
    CF probe. This is the default if the env is unset so existing
    deployments don't change overnight.
    """
    val = (os.environ.get("INTERACTIVE_HUMANIZE_MODE") or "auto").strip().lower()
    if val in ("always", "challenged", "off", "auto"):
        return val
    return "auto"


async def _capture_page_ref(page: Any) -> dict[str, int]:
    """Capture the current page reference frame in a single evaluate.

    Mirrors src/browser/page-readiness.ts capturePageRef. Returned dict
    keys: scrollY, scrollHeight, viewportHeight, viewportWidth — same
    shape as the TS PageRef so log/diff messages line up. Never throws;
    on failure returns a zero snapshot so the caller can decide
    whether to skip the comparison.
    """
    try:
        ref = await page.evaluate(
            "() => ({"
            "scrollY: Math.round(window.scrollY || 0),"
            "scrollHeight: Math.max("
            "(document.body && document.body.scrollHeight) || 0,"
            "(document.documentElement && document.documentElement.scrollHeight) || 0),"
            "viewportHeight: window.innerHeight || 0,"
            "viewportWidth: window.innerWidth || 0,"
            "})"
        )
        if isinstance(ref, dict):
            return {
                "scrollY": int(ref.get("scrollY") or 0),
                "scrollHeight": int(ref.get("scrollHeight") or 0),
                "viewportHeight": int(ref.get("viewportHeight") or 0),
                "viewportWidth": int(ref.get("viewportWidth") or 0),
            }
    except Exception:
        pass
    return {"scrollY": 0, "scrollHeight": 0, "viewportHeight": 0, "viewportWidth": 0}


def _compare_viewport_shift(
    stored: Optional[dict],
    current: dict,
) -> dict[str, Any]:
    """Compare two page reference frames. Mirrors compareViewportShift
    in src/browser/page-readiness.ts.

    Returns `{shifted, reason, delta, stored, current}`. `shifted` is
    True when any per-axis delta exceeds its threshold:
      - scrollY: VIEWPORT_SHIFT_PX (default 12)
      - scrollHeight: VIEWPORT_SHIFT_HEIGHT_PX (default 100)
      - viewport dims: VIEWPORT_SHIFT_VIEWPORT_PX (default 24)

    Kill switch: VIEWPORT_SHIFT_DISABLE=1 returns shifted=False with
    reason='disabled'. Use during incident triage if the gate over-
    fires on a specific site.
    """
    zero_delta = {
        "scrollY": 0, "scrollHeight": 0, "viewportHeight": 0, "viewportWidth": 0,
    }
    if os.environ.get("VIEWPORT_SHIFT_DISABLE") == "1":
        return {
            "shifted": False, "reason": "disabled", "delta": zero_delta,
            "stored": stored, "current": current,
        }
    if not stored:
        return {
            "shifted": False, "reason": "no_baseline", "delta": zero_delta,
            "stored": stored, "current": current,
        }
    def _intenv(k: str, default: int) -> int:
        try:
            v = int(os.environ.get(k) or "")
            return max(1, v)
        except ValueError:
            return default
    scroll_px = _intenv("VIEWPORT_SHIFT_PX", 12)
    height_px = _intenv("VIEWPORT_SHIFT_HEIGHT_PX", 100)
    viewport_px = _intenv("VIEWPORT_SHIFT_VIEWPORT_PX", 24)
    delta = {
        "scrollY": int(current["scrollY"]) - int(stored["scrollY"]),
        "scrollHeight": int(current["scrollHeight"]) - int(stored["scrollHeight"]),
        "viewportHeight": int(current["viewportHeight"]) - int(stored["viewportHeight"]),
        "viewportWidth": int(current["viewportWidth"]) - int(stored["viewportWidth"]),
    }
    if abs(delta["scrollY"]) > scroll_px:
        return {"shifted": True, "reason": "scroll", "delta": delta,
                "stored": stored, "current": current}
    if abs(delta["scrollHeight"]) > height_px:
        return {"shifted": True, "reason": "height", "delta": delta,
                "stored": stored, "current": current}
    if abs(delta["viewportHeight"]) > viewport_px or abs(delta["viewportWidth"]) > viewport_px:
        return {"shifted": True, "reason": "viewport", "delta": delta,
                "stored": stored, "current": current}
    return {"shifted": False, "reason": "no_shift", "delta": delta,
            "stored": stored, "current": current}


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


_CHROME_ERR_RE = re.compile(r"net::(ERR_[A-Z0-9_]+)")


def _extract_chrome_error_code(page_url: str, nav_error: str) -> str:
    """Return a `net::ERR_*` code if this navigation landed in a
    Chromium error page, else "".

    Detects two signatures:
      1. The page URL is `chrome-error://chromewebdata/` (Chromium
         redirected to the built-in error placeholder after the request
         was canceled / RST'd / produced no body).
      2. The captured exception message contains a `net::ERR_*` token
         (Playwright propagates these from the underlying CDP).

    Returns the ERR_* code (e.g. "ERR_HTTP2_PROTOCOL_ERROR") so the
    caller can surface it for diagnostics + targeted retry policy.
    """
    if nav_error:
        m = _CHROME_ERR_RE.search(nav_error)
        if m:
            return m.group(1)
    if page_url and page_url.startswith("chrome-error://"):
        return "ERR_FAILED"
    return ""

# Match the TS server's session lifetime (src/server/http.ts:40-41).
SESSION_IDLE_TIMEOUT_S = 30 * 60
SESSION_MAX_LIFETIME_S = 2 * 60 * 60

_DOM_INDEXER_PATH = Path(__file__).parent / "dom_indexer.js"


_XVFB_STARTED: bool = False


def _display_is_live(display: str) -> bool:
    """Return True if an X server is actually serving `display`.

    Probes by checking `/tmp/.X<n>-lock` and `/tmp/.X11-unix/X<n>` —
    Xvfb writes both on bind and removes them on shutdown. Avoids
    depending on `xdpyinfo` being installed. Returns False for
    unparseable DISPLAY strings (the caller falls back to spawn).
    """
    if not display:
        return False
    # Strip leading host part ("host:99" → "99"), strip screen suffix
    # (":99.0" → "99").
    d = display.split(":", 1)[-1].split(".", 1)[0]
    if not d.isdigit():
        return False
    return (
        Path(f"/tmp/.X{d}-lock").exists()
        or Path(f"/tmp/.X11-unix/X{d}").exists()
    )


def _maybe_start_xvfb(headless: bool) -> None:
    """If we're launching headful on a host with no DISPLAY, try to spawn
    Xvfb and point the browser at it. No-op otherwise.

    Opt-out via T3_AUTO_XVFB=0 for deployments that manage their own
    display. Silent fallback when Xvfb isn't installed — caller stays
    headful-intent, but with no DISPLAY the browser will fail naturally
    and the operator sees a clear "xvfb not found" log line.

    Trusting `os.environ['DISPLAY']` blindly is wrong — `.env` may set
    `DISPLAY=:99` in anticipation of an Xvfb that never actually
    started on this host (e.g. a fresh VM). Probe the lock/socket
    files before short-circuiting.
    """
    global _XVFB_STARTED
    if _XVFB_STARTED:
        return
    if headless:
        return
    if os.environ.get("T3_AUTO_XVFB", "1") == "0":
        return
    current_display = os.environ.get("DISPLAY") or ""
    if current_display and _display_is_live(current_display):
        # Real X server already running on this DISPLAY — adopt it,
        # don't stomp.
        _XVFB_STARTED = True
        return
    import shutil as _shutil
    import subprocess as _subprocess
    xvfb_bin = _shutil.which("Xvfb")
    if not xvfb_bin:
        logger.warning(
            "T3_HEADLESS=0 but Xvfb is not installed and no live X "
            "server at DISPLAY=%r — browser launch will fail. "
            "apt install xvfb, or set T3_HEADLESS=1.",
            current_display,
        )
        return
    # Pick the display: explicit T3_XVFB_DISPLAY > stale DISPLAY > :99.
    # Reusing the stale DISPLAY value avoids a confusing mismatch
    # between what the operator set and what Xvfb actually serves.
    display = (
        os.environ.get("T3_XVFB_DISPLAY")
        or current_display
        or ":99"
    )
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
        # Wait for the lock file to materialize so Chrome doesn't race
        # the Xvfb bind. The 0.3 s blanket sleep was both flaky on slow
        # boxes (Chrome starting before Xvfb was ready, dying with
        # TargetClosedError) and wasteful on fast boxes.
        import time as _time
        d = display.split(":", 1)[-1].split(".", 1)[0]
        for _ in range(40):  # up to 4 s
            if d.isdigit() and _display_is_live(display):
                break
            _time.sleep(0.1)
        else:
            logger.warning(
                "Xvfb spawn returned but display %s never bound a "
                "lock file — browser launch may fail.",
                display,
            )
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
    # Page reference frame (scrollY/scrollHeight/viewport dims) at the
    # moment the LAST vision-capable state() was served. click_at()
    # re-captures the current ref and compares against this — when the
    # delta exceeds threshold (lazy-load injection, banner, modal),
    # the V_n bbox the brain captured resolves against a stale frame
    # so we refuse the click and ask the brain to re-screenshot.
    # Mirrors PageWrapper.lastVisionPageRef in src/browser/page.ts.
    # `None` until the first vision capture; never reset by no-vision
    # state() probes.
    last_vision_page_ref: Optional[dict] = None


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
            # Spawn Xvfb BEFORE patchright's Node host starts. Node
            # snapshots env at start(); if we set DISPLAY afterwards
            # it never reaches the Chrome subprocess and Chrome dies
            # with "Missing X server or $DISPLAY".
            headless = os.environ.get("T3_HEADLESS", "1") != "0"
            _maybe_start_xvfb(headless)
            self._pw = await async_playwright().start()
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
        # Aligned with T1's default (src/browser/engine.ts:55) — same
        # viewport ⇒ same vision pipeline output. Previously T3 used
        # (1366, 768) which is 30% shorter, so each screenshot covered
        # less above-fold content; the same vision intent produced
        # less accurate bboxes than T1. Override per-call when a host
        # genuinely needs a different size.
        viewport: tuple[int, int] = (1280, 1100),
        proxy: Optional[str] = None,
        task_id: str = "",
        import_state: Optional[dict] = None,
        timeout_s: float = 45.0,
        max_stealth: bool = False,
    ) -> dict[str, Any]:
        """Create a new session. Returns the same dict shape the TS server
        returns from POST /session/create.

        `max_stealth=True` (used by the T1-failure escalation path) forces
        the heaviest fingerprint config for this session: persistent
        per-domain profile + headful via auto-Xvfb, regardless of env
        defaults. The cost is launch latency and CPU; the benefit is
        getting past sites where plain headless patchright still trips
        Cloudflare/Akamai. Direct `tier="t3"` callers leave it False to
        get the fast default.
        """
        persist = os.environ.get("T3_PERSIST_PROFILE", "0") != "0"
        if max_stealth:
            persist = True
        if not persist:
            await self._ensure_browser()
            assert self._browser is not None
        else:
            # Persistent mode needs Playwright running even if we won't
            # touch `self._browser`. Kick the shared janitor up the same
            # way as the ephemeral path does.
            async with self._lock:
                if self._pw is None:
                    # Spawn Xvfb BEFORE patchright's Node host starts.
                    # Node snapshots env at start(); a DISPLAY mutation
                    # afterwards never reaches the Chrome subprocess and
                    # launch_persistent_context dies with "Missing X
                    # server or $DISPLAY". Idempotent — does nothing if
                    # already started or T3_AUTO_XVFB=0.
                    persist_headless = os.environ.get(
                        "T3_HEADLESS", "1",
                    ) != "0"
                    _maybe_start_xvfb(persist_headless)
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
            # NB: max_stealth deliberately does NOT flip headless here.
            # The auto-Xvfb path has a startup race (Popen returns
            # immediately, Chrome connects before Xvfb is ready, dies
            # with TargetClosedError on launch_persistent_context). The
            # persistent profile alone (forced above when max_stealth)
            # carries the bulk of the stealth signal — cf_clearance /
            # localStorage / IndexedDB / service-worker continuity.
            # Operators who want headful should set T3_HEADLESS=0 in env
            # and ensure DISPLAY/Xvfb is verified-ready before launch.
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
        # `challenged` mode: start light. Bezier + micro-motions stay
        # off until the session observes a real antibot signal, which
        # the existing CF probe in _goto_with_warmup is responsible
        # for detecting and flipping back. `always` and `auto` keep
        # the legacy heavy-on-first-nav behaviour. `off` is global
        # via _env_light_mode (already in use).
        _initial_light = _humanize_mode() == "challenged"
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
            light_mode=_initial_light,
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
        # Light mode: teleport the real cursor in a single move (skips
        # the 50-500ms bezier humanization that real anti-bot sites need
        # but easy sites don't — matches T1's perceived click latency).
        # The browser cursor still teleports, so anti-bot sensors see
        # the same single mouseMoved event they always did. We then
        # emit a short visual-only interpolation to the live-viewer bus
        # so the SVG arrow glides between (sx,sy) and (tx,ty) instead of
        # jumping. No extra real `page.mouse.move` calls — stealth
        # posture is unchanged from before this hook existed.
        if s.light_mode or _env_light_mode():
            try:
                await s.page.mouse.move(tx, ty)
            except Exception:
                pass
            try:
                from . import t3_event_bus as _bus_mod
                _bus = _bus_mod.default()
            except Exception:
                _bus = None
            if _bus is not None:
                # 8 frames at ~22ms cadence ≈ 180ms glide. The bus's
                # 33ms throttle (t3_event_bus.py:37) caps wire fan-out
                # to ~6 events even if the loop runs faster. Eased so
                # the SVG decelerates on arrival like a real cursor.
                _vis_steps = 8
                try:
                    for _i in range(1, _vis_steps + 1):
                        _t = _i / _vis_steps
                        _t2 = _t * _t * (3 - 2 * _t)
                        _vx = sx + (tx - sx) * _t2
                        _vy = sy + (ty - sy) * _t2
                        try:
                            _bus.emit_cursor_move(sid, _vx, _vy)
                        except Exception:
                            pass
                        await asyncio.sleep(0.022)
                    # Snap visual cursor to the exact target so the
                    # ensuing click_target lands on top of the SVG.
                    try:
                        _bus.emit_cursor_move(sid, tx, ty)
                    except Exception:
                        pass
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

    async def _find_cf_checkbox_target(
        self, sid: str,
    ) -> Optional[tuple[float, float]]:
        """Locate the Turnstile checkbox in a CF Managed Challenge.

        Walks the page's frames for any whose URL matches
        challenges.cloudflare.com (any path — Managed Challenge serves
        from /cdn-cgi/challenge-platform/, widget mode from /turnstile).
        For each candidate iframe, reads the parent-frame `<iframe>`
        element's bounding box and returns viewport coords of the
        checkbox: ~28 px in from the left, vertical center. Mirrors the
        TS-side `turnstile.ts:findCheckbox` heuristic.

        Returns `None` if no candidate frame exists or its bbox is
        unusable (e.g. width < 20 px). Caller decides whether to skip
        Phase 2 click and escalate.
        """
        try:
            s = self._get(sid)
        except KeyError:
            return None
        # Try the main-frame iframe element first — most builds expose
        # the iframe directly in the parent DOM.
        try:
            box = await s.page.evaluate(
                """() => {
                    const sels = [
                        'iframe[src*="challenges.cloudflare.com"]',
                        'iframe[src*="turnstile"]',
                        '.cf-turnstile',
                        '[data-sitekey]',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (!el) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20) {
                            return {
                                x: r.left + 28,
                                y: r.top + r.height / 2,
                            };
                        }
                    }
                    return null;
                }"""
            )
            if box and isinstance(box, dict):
                return float(box["x"]), float(box["y"])
        except Exception:
            pass
        # Fallback: walk child frames. Patchright's frame.frame_element()
        # returns the parent-side <iframe> handle even when the frame
        # itself is cross-origin, so its bounding_box is reliable.
        for frame in s.page.frames:
            url = frame.url or ""
            if "challenges.cloudflare.com" not in url and "turnstile" not in url:
                continue
            try:
                handle = await frame.frame_element()
            except Exception:
                continue
            if handle is None:
                continue
            try:
                box = await handle.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            w = float(box.get("width") or 0)
            h = float(box.get("height") or 0)
            if w < 20 or h < 20:
                continue
            return float(box["x"]) + 28.0, float(box["y"]) + h / 2.0
        return None

    async def _click_cf_checkbox(
        self, sid: str, x: float, y: float,
    ) -> bool:
        """Humanized click on the Turnstile checkbox at (x, y).

        CF Turnstile inside a cross-origin iframe is strict about click
        timing — Playwright's instant `mouse.click()` (~10ms down+up) is
        bot-like enough to be ignored. Real users:
          1. Move cursor to target along a curved path
          2. Settle briefly before pressing
          3. Hold the button down ~80-160ms
          4. Release
        Mirrors the TS-side `humanClick` ladder.

        Returns True on success, False on any internal failure (the
        caller treats False as "phase failed, escalate" rather than
        raising).
        """
        import random as _rng
        try:
            s = self._get(sid)
        except KeyError:
            return False
        try:
            await self._move_cursor_smooth(sid, float(x), float(y))
        except Exception:
            pass
        # Settle: cursor lands, briefly pauses before pressing. CF reads
        # cursor velocity at the press moment — landing-then-pressing
        # mimics a real user, while clicking-mid-arc looks bot-like.
        await asyncio.sleep(_rng.uniform(0.06, 0.18))
        try:
            await s.page.mouse.move(float(x), float(y))
            await s.page.mouse.down()
            # Hold the button for a real-feeling duration. <40ms is
            # bot-class, >300ms drifts into "drag" territory. Real
            # checkbox clicks measure ~80-160ms.
            await asyncio.sleep(_rng.uniform(0.08, 0.16))
            await s.page.mouse.up()
            return True
        except Exception as exc:
            logger.warning("cf_checkbox click failed (sid=%s): %s", sid, exc)
            # Best-effort clean-up: release the button if down() succeeded
            # but up() didn't. Avoids leaving the session with a held
            # mouse-down state that breaks subsequent clicks.
            try:
                await s.page.mouse.up()
            except Exception:
                pass
            return False

    async def _goto_with_warmup(
        self, sid: str, url: str, timeout_s: float,
        *,
        force_warmup: bool = False,
        extra_humanize: bool = False,
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
        # `force_warmup=True` is set by the chrome-error retry path: it
        # had a same-origin failure that landed on chrome-error://, so
        # the in-context _abck/sensor cookies are stale or bad-scored.
        # Re-visiting the homepage refreshes them before the second
        # attempt at the deep link. We still skip when target IS root
        # (no point pre-fetching the same URL).
        needs_warmup = (
            (not _light and warmup.should_warmup(url) and (cross_origin or prev_host == ""))
            or force_warmup
        ) and not target_is_root
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
        if extra_humanize:
            # Stronger pre-nav humanization for the chrome-error retry —
            # several mouse moves + a longer pause to nudge Akamai's
            # behavioral score before re-attempting the deep link.
            try:
                for _ in range(_random.randint(3, 5)):
                    await s.page.mouse.move(
                        _random.randint(150, 1100),
                        _random.randint(120, 650),
                        steps=_random.randint(8, 18),
                    )
                    await asyncio.sleep(_random.uniform(0.15, 0.35))
                await asyncio.sleep(_random.uniform(0.8, 1.5))
            except Exception:
                pass
        elif not _light and os.environ.get("T3_NAV_JITTER") != "0":
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
        # Chromium-level navigation failure (RST / no body / protocol
        # error). Lands the page on `chrome-error://chromewebdata/`, no
        # title, no status. Akamai bot-manager refusals on Best Buy
        # product pages surface this way (sensor scoring fails and the
        # CDN closes the connection rather than serving a challenge).
        # Tag the result so the caller can decide between retry,
        # fallback, or escalation — and keep this distinct from
        # "cloudflare" so the existing CF/captcha path doesn't fire.
        elif not result.get("block_class"):
            err_code = _extract_chrome_error_code(s.page.url, nav_error)
            if err_code:
                result["block_class"] = "chrome_error"
                result["chrome_error_code"] = err_code
                result["status"] = 0
                result["statusCode"] = 0
        if turnstile_info:
            # Surface the Turnstile context to the caller so the LLM can
            # decide to call browser_solve_captcha when no API key is set
            # or auto-solve didn't clear.
            result["turnstile"] = turnstile_info
        # Antibot signal observed → re-engage full humanization. Honors
        # `challenged` mode by flipping a session that started light back
        # to heavy stealth the moment we see a real bot-detection page.
        if (result.get("block_class") or result.get("turnstile")) and s.light_mode:
            s.light_mode = False
            logger.info(
                "t3 re-engaging full humanization: session=%s host=%s "
                "antibot signal observed (block_class=%s turnstile=%s)",
                sid, urlparse(url).hostname or "",
                result.get("block_class"), bool(result.get("turnstile")),
            )
        # Auto-downshift: if this nav succeeded cleanly (200-ish status,
        # no CF / Turnstile / block_class signals) and the session
        # isn't already in light mode, flip it on. Subsequent clicks &
        # nav use the T1-parity fast path. Hard sites never trip this
        # branch because the CF probe above tags `block_class` or
        # `turnstile` before we reach here.
        #
        # Disable the downshift with T3_AUTO_DOWNSHIFT=0 to preserve
        # full-armor behaviour (e.g., on sessions that start easy but
        # become hostile on sub-pages). Also suppressed when
        # INTERACTIVE_HUMANIZE_MODE=always — the operator explicitly
        # asked for max protection, don't second-guess.
        _hmode = _humanize_mode()
        if (
            not _light
            and _hmode != "always"
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
        # Chrome-error auto-recovery: nav landed on chrome-error:// (RST,
        # protocol error, blank body — Akamai-style refusal on Best Buy
        # /product/.../openbox URLs is the canonical case). Force a
        # same-origin homepage warmup + extra humanization to refresh
        # the _abck score, then retry once. Disable via
        # T3_CHROME_ERROR_RETRY=0. Skipped if the proxy-fallback path
        # already retried on this call.
        if (
            s is not None
            and not nav.get("proxy_fallback")
            and not nav.get("chrome_error_retry")
            and os.environ.get("T3_CHROME_ERROR_RETRY", "1") != "0"
            and nav.get("block_class") == "chrome_error"
        ):
            err_code = nav.get("chrome_error_code") or ""
            logger.warning(
                "navigate %s hit chrome-error (%s) — forcing same-origin "
                "warmup + humanization burst and retrying once",
                url, err_code,
            )
            try:
                nav = await self._goto_with_warmup(
                    sid, url, timeout_s,
                    force_warmup=True,
                    extra_humanize=True,
                )
                nav["chrome_error_retry"] = True
            except Exception as exc:
                logger.warning(
                    "chrome-error retry raised for %s: %s", url, exc,
                )
                nav.setdefault("chrome_error_retry_error", str(exc)[:200])
            # Surface a fallback hint when the retry didn't fix it. The
            # orchestrator's existing T2/handoff escalation path can
            # then consume this without us silently dropping JS-required
            # pages into a static-HTML fetch.
            if nav.get("block_class") == "chrome_error":
                nav.setdefault("fallback_hint", "tier2_static_html")
                # ERR_HTTP2_PROTOCOL_ERROR specifically suggests the
                # operator should consider the inverse HTTP/2 toggle
                # (default is --disable-http2; some Akamai endpoints
                # need it ON). Surface as a hint — toggling at runtime
                # would require relaunching the shared browser, which
                # would disrupt every other concurrent session.
                if "HTTP2" in (nav.get("chrome_error_code") or ""):
                    nav["fallback_hint"] = "toggle_http2_then_relaunch"
        state = await self.state(sid)
        # Live viewer telemetry — banner shows the current URL + title
        # so the operator always knows where the worker landed.
        try:
            from . import t3_event_bus as _bus
            _bus.default().emit_navigation(
                sid, state.get("url", ""), state.get("title", ""),
            )
            # Cursor heartbeat: a viewer that connects during the
            # post-nav handoff message would otherwise see a hidden SVG
            # arrow until the next click. Emitting the current cursor
            # position here makes the arrow appear at the right spot
            # the moment the viewer subscribes.
            try:
                s_obj = self._sessions.get(sid)
                if s_obj is not None:
                    _bus.default().emit_cursor_move(
                        sid, float(s_obj.cursor_x), float(s_obj.cursor_y),
                    )
            except Exception:
                pass
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

        # Capture the page reference frame whenever the brain receives
        # a screenshot. Mirrors http.ts:689 — the brain's V_n bboxes
        # are anchored to THIS frame, and click_at compares against it
        # to detect drift before dispatching. Only fires on vision-
        # capable state() calls so a no-vision probe (state polling)
        # doesn't reset the baseline mid-session.
        if use_vision and screenshot_b64:
            try:
                s.last_vision_page_ref = await _capture_page_ref(s.page)
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
        # Set true by the snap eval when the snapped element is inside
        # a typeahead/listbox popup. Primary strategy then teleports
        # the cursor straight to the target (no bezier sweep) to avoid
        # firing mouseout on neighbour options that close the dropdown.
        is_autocomplete_target = False
        if bbox is not None:
            # Layer 1 — viewport-shift gate. Mirrors src/server/http.ts:776
            # for parity. The brain's V_n bbox is in viewport-CSS
            # coordinates frozen at vision-capture time. If the page
            # has scrolled or reflowed since (lazy-load, banner, modal),
            # those CSS coords now point at the wrong absolute element
            # — labels alone can't catch this when the new occupant is
            # the same kind of widget. Reject early so the brain
            # re-screenshots instead of clicking a stale frame.
            try:
                stored = s.last_vision_page_ref
                current_ref = await _capture_page_ref(s.page)
                shift = _compare_viewport_shift(stored, current_ref)
                if os.environ.get("VIEWPORT_SHIFT_DEBUG") == "1":
                    logger.info(
                        "[viewport_shift] shifted=%s reason=%s "
                        "dy=%s dh=%s dvh=%s",
                        shift["shifted"], shift["reason"],
                        shift["delta"]["scrollY"],
                        shift["delta"]["scrollHeight"],
                        shift["delta"]["viewportHeight"],
                    )
                if shift["shifted"]:
                    return {
                        "success": False,
                        "error": "viewport_shifted",
                        "reason": shift["reason"],
                        "delta": shift["delta"],
                        "stored": shift["stored"],
                        "current": shift["current"],
                    }
            except Exception as exc:
                logger.debug("viewport-shift check failed: %s", exc)

            # Phase 1 + Phase 2 snap (mirrors src/browser/page.ts:868-1110).
            # Phase 1: try the bbox centre. If the element there is
            # interactive and (when expectedLabel given) labels match,
            # use it. Otherwise Phase 2 runs a 4x4 grid scan with
            # composite scoring (area * label-score). When a label is
            # active and no candidate scores >=0.5, return labelMismatch
            # so the caller surfaces element_mismatch to the brain
            # rather than silently misclicking a same-shape neighbour.
            #
            # Single evaluate: cheaper than 2-3 round-trips for the
            # phase-1-fail case. Result shape:
            #   { ok: true, x, y, tag, text, snapped, labelScore }
            #   { ok: false, labelMismatch: true, found: {...}, x, y }
            #   null  — no interactive element overlaps the bbox at all
            try:
                _exp_label = (expected_label or "").strip()
                snap_info = await s.page.evaluate(
                    """(args) => {
                      const b = args.b;
                      const expLc = (args.expectedLabel || '').toLowerCase().trim();
                      const labelActive = expLc.length >= 3;
                      const SEL = 'a,button,input,select,textarea,'
                        + '[role="button"],[role="link"],[role="checkbox"],'
                        + '[role="tab"],[role="menuitem"],[onclick],[tabindex]';
                      const labelScoreOf = (el) => {
                        if (!labelActive) return 1;
                        const full = (
                          (el.textContent || '') + ' '
                          + (el.getAttribute && el.getAttribute('aria-label') || '') + ' '
                          + (el.getAttribute && el.getAttribute('title') || '')
                        ).toLowerCase().replace(/\\s+/g, ' ').trim();
                        if (!full) return 0.1;
                        if (full.includes(expLc) || expLc.includes(full.slice(0, 40))) return 1;
                        // Lenient fallback (mirrors src/browser/page.ts):
                        //   - Dropdown items (role=option/menuitem/treeitem/listitem, <li>)
                        //     where vision drifts on suggestion labels.
                        //   - Value-bearing triggers (role=combobox, aria-haspopup) whose
                        //     visible text is the displayed VALUE while vision labels them
                        //     by FUNCTION (e.g. Chakra DateTimePicker shows "1:00 PM" but
                        //     vision labels it "Start Time"). Misclick risk is low because
                        //     these are singleton controls inside the bbox.
                        const role = (el.getAttribute && el.getAttribute('role') || '').toLowerCase();
                        const hasPopup = (el.getAttribute && el.getAttribute('aria-haspopup') || '').toLowerCase();
                        const isDropdownItem = (
                          role === 'option' || role === 'menuitem'
                          || role === 'treeitem' || role === 'listitem'
                        ) || (el.tagName && el.tagName.toLowerCase() === 'li');
                        const isValueBearingTrigger = (
                          role === 'combobox'
                          || (hasPopup !== '' && hasPopup !== 'false')
                        );
                        if (isDropdownItem || isValueBearingTrigger) {
                          const expWords = expLc.split(/\\s+/).filter((t) => t.length >= 3);
                          const fullWords = new Set(full.split(/\\s+/).filter((t) => t.length >= 3));
                          let common = 0;
                          for (const t of expWords) if (fullWords.has(t)) common += 1;
                          if (common >= 1 || (isValueBearingTrigger && expWords.length > 0)) {
                            return 0.7;
                          }
                        }
                        return 0.05;
                      };
                      const isRowBbox = (b.x1 - b.x0) >= 60 && (b.y1 - b.y0) >= 24;
                      const CHEVRON_CHARS = '\\u25BC\\u25B6\\u25C0\\u25B2\\u25BA\\u25C4\\u2303\\u2304\\u22EE+\\u2212\\u00D7\\u2A2F\\u203A';
                      const chevronScoreOf = (el) => {
                        if (el.getAttribute && el.getAttribute('aria-expanded') !== null) return 3;
                        if (el.getAttribute && el.getAttribute('aria-haspopup')) return 2;
                        const t = (el.textContent || '').trim();
                        if (t.length === 1 && CHEVRON_CHARS.includes(t)) return 2;
                        const al = (el.getAttribute && el.getAttribute('aria-label') || '').toLowerCase();
                        if (/(expand|collapse|toggle|more)/.test(al)) return 1;
                        return 0;
                      };
                      // Autocomplete / typeahead detector — same logic as
                      // src/browser/page.ts. When the snapped element is a
                      // suggestion in a popup, the caller will skip the
                      // bezier mouse approach (which sweeps over neighbour
                      // options and dismisses the dropdown via mouseout)
                      // and use a teleport click instead.
                      const isAutocompleteOptionEl = (el) => {
                        if (!el) return false;
                        const role = (el.getAttribute && el.getAttribute('role') || '').toLowerCase();
                        if (role === 'option' || role === 'menuitem') return true;
                        let walker = el;
                        for (let depth = 0; walker && depth < 6; depth += 1) {
                          const r = (walker.getAttribute && walker.getAttribute('role') || '').toLowerCase();
                          if (r === 'listbox' || r === 'combobox' || r === 'menu') return true;
                          const haspopup = walker.getAttribute && walker.getAttribute('aria-haspopup');
                          if (haspopup === 'listbox' || haspopup === 'menu' || haspopup === 'true') return true;
                          const ac = walker.getAttribute && walker.getAttribute('aria-autocomplete');
                          if (ac === 'list' || ac === 'both') return true;
                          const ds = walker.getAttribute && walker.getAttribute('data-state');
                          if (ds === 'open' && (
                            walker.getAttribute('data-radix-popper-content-wrapper') !== null
                            || (walker.getAttribute('class') || '').toLowerCase().includes('popover')
                          )) return true;
                          walker = walker.parentElement;
                        }
                        return false;
                      };
                      // Phase 1: bbox centre snap.
                      const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
                      const centreEl = document.elementFromPoint(cx, cy);
                      if (centreEl) {
                        const interactive = centreEl.closest ? centreEl.closest(SEL) : null;
                        if (interactive) {
                          const r = interactive.getBoundingClientRect();
                          const ex = r.left + r.width / 2, ey = r.top + r.height / 2;
                          if (ex >= b.x0 && ex <= b.x1 && ey >= b.y0 && ey <= b.y1) {
                            const ls = labelScoreOf(interactive);
                            if (!labelActive || ls >= 0.5) {
                              return {
                                ok: true, x: ex, y: ey,
                                tag: interactive.tagName.toLowerCase(),
                                text: (interactive.textContent || '').slice(0, 40),
                                snapped: true, labelScore: ls,
                                isAutocompleteOption: isAutocompleteOptionEl(interactive),
                              };
                            }
                            // Phase 1 found interactive but label diverged
                            // — fall through to Phase 2 grid-scan to look
                            // for a label-matching alternate.
                          }
                        }
                      }
                      // Phase 2: 4x4 grid scan, composite area * label.
                      let best = null, bestArea = 0, bestComposite = 0, bestLabelScore = 0;
                      for (let i = 1; i < 5; i++) {
                        for (let j = 1; j < 5; j++) {
                          const px = b.x0 + ((b.x1 - b.x0) * i) / 5;
                          const py = b.y0 + ((b.y1 - b.y0) * j) / 5;
                          let stack = [];
                          try { stack = document.elementsFromPoint(px, py); } catch (e) { stack = []; }
                          for (const el of stack) {
                            const hit = el.closest ? el.closest(SEL) : null;
                            if (!hit) continue;
                            const r = hit.getBoundingClientRect();
                            const ix = Math.max(0, Math.min(r.right, b.x1) - Math.max(r.left, b.x0));
                            const iy = Math.max(0, Math.min(r.bottom, b.y1) - Math.max(r.top, b.y0));
                            const area = ix * iy;
                            if (area <= 0) continue;
                            const cs = isRowBbox ? chevronScoreOf(hit) : 0;
                            const ls = labelScoreOf(hit);
                            const within30 = bestArea > 0 && area > bestArea * 0.7;
                            const baseScore = (cs > 0 && within30)
                              ? area + bestArea * 0.5 * cs
                              : area;
                            const composite = baseScore * ls;
                            if (composite > bestComposite) {
                              bestComposite = composite;
                              bestArea = area;
                              bestLabelScore = ls;
                              best = hit;
                            }
                          }
                        }
                      }
                      if (best) {
                        const r = best.getBoundingClientRect();
                        const ex = Math.round(r.left + r.width / 2);
                        const ey = Math.round(r.top + r.height / 2);
                        if (labelActive && bestLabelScore < 0.5) {
                          // Surface labelMismatch — the caller will
                          // refuse to dispatch and return element_mismatch
                          // to the brain so it re-screenshots.
                          return {
                            ok: false, labelMismatch: true,
                            x: ex, y: ey,
                            found: {
                              tag: best.tagName.toLowerCase(),
                              role: (best.getAttribute && best.getAttribute('role') || ''),
                              text: (best.textContent || '')
                                .replace(/\\s+/g, ' ').trim().slice(0, 120),
                            },
                          };
                        }
                        return {
                          ok: true, x: ex, y: ey,
                          tag: best.tagName.toLowerCase(),
                          text: (best.textContent || '').slice(0, 40),
                          snapped: true, labelScore: bestLabelScore,
                          isAutocompleteOption: isAutocompleteOptionEl(best),
                        };
                      }
                      return null;
                    }""",
                    {
                        "b": {
                            "x0": float(bbox.get("x0", x)),
                            "y0": float(bbox.get("y0", y)),
                            "x1": float(bbox.get("x1", x)),
                            "y1": float(bbox.get("y1", y)),
                        },
                        "expectedLabel": _exp_label,
                    },
                )
                if isinstance(snap_info, dict):
                    if snap_info.get("ok") is False and snap_info.get("labelMismatch"):
                        # Phase 2 found a candidate but its label diverged
                        # from vision's bbox label (mirrors src/browser/
                        # page.ts: labelMismatch is now ADVISORY ONLY).
                        # We dispatch at the grid-scan winner's coords —
                        # value-bearing controls (combobox / aria-haspopup
                        # triggers) systematically have role-vs-value
                        # label drift, and silently no-op'ing legitimate
                        # clicks was worse than the misclick risk on
                        # same-shape neighbours. Page-shift attacks
                        # (the original reason for this escape) are
                        # already guarded by the viewport_shifted check
                        # at 2730-2740 above. Carry snapped=False so the
                        # bridge logs the divergence as a diagnostic.
                        target_x = float(snap_info.get("x", x))
                        target_y = float(snap_info.get("y", y))
                        snapped = False
                        is_autocomplete_target = False
                        _found = snap_info.get("found") or {}
                        snap_target = (
                            f"{_found.get('tag','')}:{_found.get('text','')[:40]}"
                        )
                    elif snap_info.get("ok") is True:
                        target_x = float(snap_info.get("x", x))
                        target_y = float(snap_info.get("y", y))
                        snapped = True
                        # Autocomplete-option flag — caller skips bezier
                        # cursor approach and teleports to avoid the
                        # mouseout-on-neighbour dropdown dismiss bug.
                        is_autocomplete_target = bool(
                            snap_info.get("isAutocompleteOption")
                        )
                        snap_target = (
                            f"{snap_info.get('tag','')}:{snap_info.get('text','')}"
                        )
            except Exception as exc:
                logger.debug("snap eval failed: %s", exc)

        # Semantic-match guard (P1.4). If the caller gave us the label
        # the vision agent used for this bbox, fetch the element at the
        # snapped target and confirm it looks like the right thing.
        # Catches the "vision said 'Buy Now' but the element at (x,y)
        # is actually a parent <section>" class of misfires that would
        # otherwise land a click on the wrong target.
        #
        # When `bbox` was provided, the in-snap grid-scan already did
        # this check (with the more accurate labelScore composite) and
        # either returned element_mismatch or accepted a labelScore >=
        # 0.5 candidate — so this block is gated on `bbox is None` to
        # avoid re-checking with a stricter heuristic that could
        # contradict the snap.
        if bbox is None and expected_label and expected_label.strip():
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
                if is_autocomplete_target:
                    # Teleport — no bezier sweep across neighbour
                    # suggestions, no mouseout dismissing the dropdown
                    # before the click lands. Still emit a few visual
                    # cursor frames to the viewer so the SVG arrow
                    # tracks the jump (otherwise the viewer's last
                    # cursor position would lag the click target).
                    try:
                        await s.page.mouse.move(target_x, target_y)
                    except Exception:
                        pass
                    try:
                        from . import t3_event_bus as _bus_a
                        _bus_a.default().emit_cursor_move(
                            sid, float(target_x), float(target_y),
                        )
                    except Exception:
                        pass
                    s.cursor_x, s.cursor_y = target_x, target_y
                else:
                    await self._move_cursor_smooth(sid, target_x, target_y)
                # Skip the 50ms settle in light mode — matches T1 which
                # has no such pause. The move + click can fire in the
                # same tick.
                if not _light_click and not is_autocomplete_target:
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
                # Approach the cursor visually before the JS click so the
                # viewer's SVG arrow lands on the click site rather than
                # showing a click ring out of nowhere. The actual click
                # is dispatched via JS (no mouse event) — this only
                # affects what the viewer renders.
                await self._move_cursor_smooth(sid, target_x, target_y)
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
                # Same rationale as the js branch: keep the viewer's
                # cursor consistent across all click strategies.
                await self._move_cursor_smooth(sid, target_x, target_y)
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

    async def click_selector(
        self,
        sid: str,
        selector: str,
        *,
        button: str = "left",
        click_count: int = 1,
        ensure_visible: bool = True,
    ) -> dict[str, Any]:
        """Click the centre of a DOM element by CSS selector. Mirrors
        T1's PageWrapper.clickSelector (src/browser/page.ts:1222) —
        same return shape (`{success, clicked: {x, y, rect}, url, ...}`)
        so the bridge tool consumes it transparently.

        Critically: routes through `_move_cursor_smooth` so the live
        viewer's SVG arrow glides to the click target. Without this,
        every selector-based click landed instantly with no cursor
        motion, which was most of what the agent did in practice on
        stable selectors. T1's clickSelector used humanClick (bezier
        + cursor emit); this is the T3 mirror, kept for legacy callers.
        """
        s = self._get(sid)
        try:
            rect = await s.page.evaluate(
                "(args) => {"
                "  const el = document.querySelector(args.sel);"
                "  if (!el) return null;"
                "  if (args.ensureVisible) {"
                "    const r0 = el.getBoundingClientRect();"
                "    const inView = ("
                "      r0.top >= 0 && r0.bottom <= window.innerHeight"
                "      && r0.left >= 0 && r0.right <= window.innerWidth"
                "    );"
                "    if (!inView) {"
                "      try { el.scrollIntoView({block:'nearest', inline:'nearest', behavior:'instant'}); }"
                "      catch (e) { try { el.scrollIntoView(); } catch (e2) {} }"
                "    }"
                "  }"
                "  const r = el.getBoundingClientRect();"
                "  if (r.width <= 0 || r.height <= 0) return null;"
                "  return {x: r.left, y: r.top, w: r.width, h: r.height,"
                "          cx: r.left + r.width / 2, cy: r.top + r.height / 2};"
                "}",
                {"sel": selector, "ensureVisible": bool(ensure_visible)},
            )
        except Exception as exc:
            return {"success": False, "error": f"selector eval failed: {exc}"[:200]}
        if not isinstance(rect, dict):
            return {
                "success": False,
                "error": f"clickSelector: selector not found or zero-size: {selector}",
            }
        cx = float(rect.get("cx") or 0)
        cy = float(rect.get("cy") or 0)
        # Bezier approach via the shared helper. In light_mode this
        # still emits visual cursor frames (the P0.1 fix) so the SVG
        # glides; in full humanize it does the real bezier.
        try:
            await self._move_cursor_smooth(sid, cx, cy)
        except Exception:
            pass
        # Click target event for the viewer overlay so the operator
        # also sees the snapped target rectangle, not just the cursor.
        try:
            from . import t3_event_bus as _bus
            _bus.default().emit_click_target(
                sid,
                x=cx, y=cy, snapped=True,
                bbox={
                    "x0": float(rect.get("x") or 0),
                    "y0": float(rect.get("y") or 0),
                    "x1": float(rect.get("x") or 0) + float(rect.get("w") or 0),
                    "y1": float(rect.get("y") or 0) + float(rect.get("h") or 0),
                },
                target=f"selector:{selector}",
                strategy="primary",
            )
        except Exception:
            pass
        try:
            try:
                dwell_min = int(os.environ.get("T3_CLICK_DELAY_MS_MIN") or "40")
                dwell_max = int(os.environ.get("T3_CLICK_DELAY_MS_MAX") or "120")
            except ValueError:
                dwell_min, dwell_max = 40, 120
            import random as _rng
            dwell = _rng.uniform(dwell_min, max(dwell_min, dwell_max))
            await s.page.mouse.click(
                cx, cy,
                button=button if button in ("left", "right", "middle") else "left",
                click_count=int(click_count or 1),
                delay=dwell,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)[:200], "selector": selector}
        try:
            await s.page.wait_for_load_state(
                "domcontentloaded",
                timeout=1500 if (s.light_mode or _env_light_mode()) else 5000,
            )
        except Exception:
            pass
        st = await self.state(sid, use_vision=False, include_screenshot=False)
        return {
            "success": True,
            "clicked": {
                "x": int(cx), "y": int(cy),
                "rect": {
                    "x": int(rect.get("x") or 0),
                    "y": int(rect.get("y") or 0),
                    "w": int(rect.get("w") or 0),
                    "h": int(rect.get("h") or 0),
                },
            },
            "url": st.get("url", ""),
            "title": st.get("title", ""),
            "elements": st.get("elements", ""),
        }

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
                "() => ({w: window.innerWidth || 1280, h: window.innerHeight || 1100})",
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

    async def scroll_until(
        self,
        sid: str,
        *,
        target_text: Optional[str] = None,
        target_role: Optional[str] = None,
        direction: str = "down",
        max_iterations: int = 10,
        step_ratio: float = 0.55,
        auto_reverse: bool = True,
        container_selector: Optional[str] = None,
    ) -> dict[str, Any]:
        """Scroll the page (or a container) incrementally until a
        target text / role becomes visible. Lean port of
        src/browser/page.ts:scrollUntil — same return shape so
        navigation.py:browser_scroll_until can consume it transparently.

        Behaviour preserved:
          - find target by walking interactive + heading elements
          - regex if `target_text` compiles, otherwise substring
          - step size = max(80, viewport * step_ratio)
          - plateau detection: 2 consecutive no-progress steps → stop
          - optional auto_reverse when forward leg makes progress but
            doesn't find the target (tries the other direction once)
          - shape compat: trace[] returned (empty for now), reason
            taxonomy matches T1 (matched / page_end / page_start /
            max_iterations / no_target / reversed_no_match /
            no_scroll_surface / no_forward_progress)

        Skipped vs T1 (would balloon code without changing common-case
        behaviour):
          - per-step "what entered view" trace
          - chosenContainer diagnostics
          - cadence presets (caller passes step_ratio directly)
          - target_in_no_scrollable_container probe
        """
        s = self._get(sid)
        target_text = (target_text or "").strip()
        target_role = (target_role or "").strip()
        container_selector = (container_selector or "").strip() or None

        async def _geom() -> dict[str, int]:
            if container_selector:
                r = await s.page.evaluate(
                    "(sel) => { const el = document.querySelector(sel);"
                    "  if (!el) return null;"
                    "  return {y: Math.round(el.scrollTop),"
                    "          vp: Math.round(el.clientHeight),"
                    "          total: Math.round(el.scrollHeight)};"
                    "}", container_selector,
                )
                if isinstance(r, dict):
                    return {"y": int(r.get("y") or 0), "vp": int(r.get("vp") or 0),
                            "total": int(r.get("total") or 0)}
                return {"y": 0, "vp": 0, "total": 0}
            r = await s.page.evaluate(
                "() => ({y: Math.round(window.scrollY || 0),"
                " vp: window.innerHeight || 0,"
                " total: Math.max(document.body && document.body.scrollHeight || 0,"
                "                 document.documentElement && document.documentElement.scrollHeight || 0)})"
            )
            return {"y": int(r.get("y") or 0), "vp": int(r.get("vp") or 0),
                    "total": int(r.get("total") or 0)}

        async def _scroll_by(delta: int) -> bool:
            before = (await _geom())["y"]
            try:
                if container_selector:
                    await s.page.evaluate(
                        "(args) => { const el = document.querySelector(args.sel);"
                        "  if (el) el.scrollBy(0, args.d); }",
                        {"sel": container_selector, "d": delta},
                    )
                else:
                    await s.page.evaluate(f"window.scrollBy(0, {int(delta)})")
            except Exception:
                return False
            await asyncio.sleep(0.15)
            after = (await _geom())["y"]
            return after != before

        if not target_text and not target_role:
            info = await _geom()
            return {
                "found": False, "iterations": 0,
                "finalScrollY": info["y"], "scrolledPx": 0,
                "reason": "no_target", "trace": [],
                "startScrollY": info["y"],
                "containerSelector": container_selector or "",
            }

        # Try regex compile; fall back to substring.
        try:
            re_compile = re.compile(target_text, re.IGNORECASE) if target_text else None
        except re.error:
            re_compile = None
        regex_src = re_compile.pattern if re_compile is not None else target_text

        find_match_js = (
            "(args) => {"
            "  const rs = args.regexSrc, ir = args.isRegex, role = args.role,"
            "        container = args.container;"
            "  const matchText = (txt) => {"
            "    if (!rs) return true;"
            "    if (ir) { try { return new RegExp(rs, 'i').test(txt); }"
            "             catch (e) { return txt.toLowerCase().includes(rs.toLowerCase()); } }"
            "    return txt.toLowerCase().includes(rs.toLowerCase());"
            "  };"
            "  const root = container ? (document.querySelector(container) || document) : document;"
            "  const containerEl = container ? root : null;"
            "  const isHiddenByCollapse = (el) => {"
            "    let w = el.parentElement, d = 0;"
            "    while (w && w !== document.body && d < 12) {"
            "      if (w.tagName === 'DETAILS' && !w.open) return true;"
            "      if (w.getAttribute && w.getAttribute('aria-expanded') === 'false') return true;"
            "      w = w.parentElement; d++;"
            "    }"
            "    return false;"
            "  };"
            "  const isVisible = (el) => {"
            "    const r = el.getBoundingClientRect();"
            "    if (r.width <= 0 || r.height <= 0) return false;"
            "    if (containerEl) {"
            "      const cr = containerEl.getBoundingClientRect();"
            "      if (r.bottom < cr.top || r.top > cr.bottom) return false;"
            "    } else {"
            "      if (r.bottom < 0 || r.top > window.innerHeight) return false;"
            "    }"
            "    const cs = window.getComputedStyle(el);"
            "    if (cs.visibility === 'hidden' || cs.display === 'none') return false;"
            "    if (isHiddenByCollapse(el)) return false;"
            "    return true;"
            "  };"
            "  const sel = 'a, button, input, select, textarea, label, summary,"
            "    [role], [aria-label], [data-testid], h1, h2, h3, h4, h5,"
            "    li, td, th, span, div';"
            "  const els = root.querySelectorAll(sel);"
            "  for (const el of els) {"
            "    if (!isVisible(el)) continue;"
            "    if (role) {"
            "      const er = (el.getAttribute('role') || el.tagName.toLowerCase()).toLowerCase();"
            "      if (er !== role.toLowerCase()) continue;"
            "    }"
            "    const txt = (el.innerText || el.textContent || '').trim();"
            "    const aria = el.getAttribute('aria-label') || '';"
            "    const ph = el.getAttribute('placeholder') || '';"
            "    const composite = (txt + '\\n' + aria + '\\n' + ph).trim();"
            "    if (matchText(composite)) {"
            "      const id = el.getAttribute('id'); const dt = el.getAttribute('data-testid');"
            "      let s = el.tagName.toLowerCase();"
            "      if (id) s += '#' + id;"
            "      if (dt) s += '[data-testid=\"' + dt + '\"]';"
            "      return {selector: s, text: composite.slice(0, 120)};"
            "    }"
            "  }"
            "  return null;"
            "}"
        )

        async def _find() -> Optional[dict]:
            try:
                r = await s.page.evaluate(
                    find_match_js,
                    {
                        "regexSrc": regex_src,
                        "isRegex": re_compile is not None,
                        "role": target_role,
                        "container": container_selector,
                    },
                )
                return r if isinstance(r, dict) else None
            except Exception:
                return None

        start_info = await _geom()
        start_y = start_info["y"]
        step_delta = max(80, round(start_info["vp"] * max(0.1, min(1.0, step_ratio))))

        # Freebie check.
        m = await _find()
        if m:
            info = await _geom()
            return {
                "found": True, "iterations": 0,
                "finalScrollY": info["y"], "scrolledPx": 0,
                "reason": "matched",
                "matchedSelector": m.get("selector"),
                "matchedText": m.get("text"),
                "trace": [], "startScrollY": start_y,
                "containerSelector": container_selector or "",
            }

        async def _scan(dirn: str, iter_start: int) -> dict[str, Any]:
            iters = iter_start
            no_progress = 0
            sign = 1 if dirn == "down" else -1
            last_y = (await _geom())["y"]
            while iters < max_iterations:
                iters += 1
                moved = await _scroll_by(sign * step_delta)
                cur = await _geom()
                if not moved or cur["y"] == last_y:
                    no_progress += 1
                    if no_progress >= 2:
                        # Real plateau — page edge or non-scrollable.
                        edge = "page_end" if dirn == "down" else "page_start"
                        if iter_start == 0 and cur["y"] == 0 and dirn == "down":
                            return {"_terminal": False, "reason": "no_scroll_surface",
                                    "iterations": iters, "finalScrollY": cur["y"]}
                        return {"_terminal": False, "reason": edge,
                                "iterations": iters, "finalScrollY": cur["y"]}
                else:
                    no_progress = 0
                    last_y = cur["y"]
                m2 = await _find()
                if m2:
                    return {"_terminal": True, "reason": "matched",
                            "iterations": iters, "finalScrollY": cur["y"],
                            "matchedSelector": m2.get("selector"),
                            "matchedText": m2.get("text")}
            return {"_terminal": False, "reason": "max_iterations",
                    "iterations": iters, "finalScrollY": last_y}

        forward = await _scan(direction, 0)
        if forward["_terminal"]:
            cur = await _geom()
            return {
                "found": True, "iterations": forward["iterations"],
                "finalScrollY": forward["finalScrollY"],
                "scrolledPx": abs(forward["finalScrollY"] - start_y),
                "reason": "matched",
                "matchedSelector": forward.get("matchedSelector"),
                "matchedText": forward.get("matchedText"),
                "trace": [], "startScrollY": start_y,
                "containerSelector": container_selector or "",
            }

        # Forward leg didn't match. Auto-reverse if requested AND
        # forward made enough progress to be worth reversing (avoid
        # rewinding 0px when target is in a non-scrollable container).
        forward_progress = abs(forward["finalScrollY"] - start_y)
        if auto_reverse and forward_progress >= 100:
            other = "up" if direction == "down" else "down"
            backward = await _scan(other, forward["iterations"])
            if backward["_terminal"]:
                return {
                    "found": True, "iterations": backward["iterations"],
                    "finalScrollY": backward["finalScrollY"],
                    "scrolledPx": abs(backward["finalScrollY"] - start_y),
                    "reason": "matched", "reversed": True,
                    "matchedSelector": backward.get("matchedSelector"),
                    "matchedText": backward.get("matchedText"),
                    "trace": [], "startScrollY": start_y,
                    "containerSelector": container_selector or "",
                }
            return {
                "found": False, "iterations": backward["iterations"],
                "finalScrollY": backward["finalScrollY"],
                "scrolledPx": abs(backward["finalScrollY"] - start_y),
                "reason": "reversed_no_match", "reversed": True,
                "trace": [], "startScrollY": start_y,
                "containerSelector": container_selector or "",
            }
        return {
            "found": False, "iterations": forward["iterations"],
            "finalScrollY": forward["finalScrollY"],
            "scrolledPx": forward_progress,
            "reason": forward["reason"],
            "trace": [], "startScrollY": start_y,
            "containerSelector": container_selector or "",
        }

    async def scroll_within(
        self,
        sid: str,
        *,
        container_selector: Optional[str] = None,
        direction: str = "down",
        amount: Any = "page",  # "page", "half", or pixel int
        target_text: Optional[str] = None,
        max_iterations: int = 12,
    ) -> dict[str, Any]:
        """Scroll inside a specific container (popup/listbox/menu).

        Lean port of src/browser/page.ts:scrollWithin. Resolves the
        container — explicit selector wins; otherwise auto-detect the
        topmost visible role=listbox/menu/dialog or its scrollable
        ancestor. With `target_text`, delegates to scroll_until scoped
        to the resolved container. Without target, performs a one-shot
        scroll by `amount`.

        Skipped vs T1: z-index sort, focused-element fallback for
        container detection. T1 has them; common cases hit the popup
        path first so they rarely fire.
        """
        s = self._get(sid)
        resolved = (container_selector or "").strip()
        if not resolved:
            try:
                resolved = await s.page.evaluate(
                    "() => {"
                    "  const sels = ['[role=\"listbox\"]:not([aria-hidden=\"true\"])',"
                    "                '[role=\"menu\"]:not([aria-hidden=\"true\"])',"
                    "                '[role=\"dialog\"]:not([aria-hidden=\"true\"])',"
                    "                '[data-headlessui-state=\"open\"]',"
                    "                '[data-state=\"open\"]'];"
                    "  const findScrollHost = (start) => {"
                    "    let cur = start;"
                    "    while (cur && cur !== document.body) {"
                    "      const cs = window.getComputedStyle(cur);"
                    "      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')"
                    "          && cur.scrollHeight > cur.clientHeight + 4) return cur;"
                    "      cur = cur.parentElement;"
                    "    }"
                    "    return null;"
                    "  };"
                    "  const isVisible = (el) => {"
                    "    const r = el.getBoundingClientRect();"
                    "    if (r.width <= 0 || r.height <= 0) return false;"
                    "    const cs = window.getComputedStyle(el);"
                    "    return cs.visibility !== 'hidden' && cs.display !== 'none';"
                    "  };"
                    "  const cands = [];"
                    "  for (const sel of sels) {"
                    "    for (const el of document.querySelectorAll(sel)) {"
                    "      if (!isVisible(el)) continue;"
                    "      let host = null;"
                    "      const cs = window.getComputedStyle(el);"
                    "      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')"
                    "          && el.scrollHeight > el.clientHeight + 4) host = el;"
                    "      else host = findScrollHost(el);"
                    "      if (host) cands.push(host);"
                    "    }"
                    "  }"
                    "  if (!cands.length) return '';"
                    "  const winner = cands[0];"
                    "  const id = 'sb-scroll-host-' + Math.random().toString(36).slice(2, 10);"
                    "  winner.setAttribute('data-sb-scroll-host', id);"
                    "  return '[data-sb-scroll-host=\"' + id + '\"]';"
                    "}"
                ) or ""
            except Exception:
                resolved = ""

        if not resolved:
            info = await s.page.evaluate(
                "() => ({y: Math.round(window.scrollY || 0)})"
            ) or {}
            return {
                "found": False, "iterations": 0,
                "finalScrollY": int(info.get("y") or 0),
                "scrolledPx": 0, "reason": "no_container",
                "trace": [], "startScrollY": int(info.get("y") or 0),
                "containerSelector": container_selector or "",
                "resolvedContainer": "",
            }

        if (target_text or "").strip():
            r = await self.scroll_until(
                sid,
                target_text=target_text,
                direction=direction,
                max_iterations=max_iterations,
                container_selector=resolved,
                step_ratio=0.30,  # cadence='fine'
                auto_reverse=True,
            )
            return {**r, "resolvedContainer": resolved}

        # Pixel scroll inside the container.
        try:
            geom = await s.page.evaluate(
                "(sel) => { const el = document.querySelector(sel);"
                "  if (!el) return null;"
                "  return {y: Math.round(el.scrollTop), vp: Math.round(el.clientHeight)};"
                "}", resolved,
            ) or {}
        except Exception:
            geom = {}
        start_y = int(geom.get("y") or 0)
        vp = int(geom.get("vp") or 400)
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            delta = int(amount)
        elif amount == "half":
            delta = round(vp / 2)
        else:
            delta = vp
        if direction == "up":
            delta = -delta
        try:
            await s.page.evaluate(
                "(args) => { const el = document.querySelector(args.sel);"
                "  if (el) el.scrollBy(0, args.d); }",
                {"sel": resolved, "d": delta},
            )
        except Exception as exc:
            return {
                "found": False, "iterations": 1,
                "finalScrollY": start_y, "scrolledPx": 0,
                "reason": "no_scroll_surface",
                "trace": [], "startScrollY": start_y,
                "containerSelector": container_selector or "",
                "resolvedContainer": resolved,
                "error": str(exc)[:120],
            }
        await asyncio.sleep(0.2)
        try:
            after = await s.page.evaluate(
                "(sel) => { const el = document.querySelector(sel);"
                "  return el ? Math.round(el.scrollTop) : 0; }", resolved,
            )
        except Exception:
            after = start_y
        return {
            "found": False, "iterations": 1,
            "finalScrollY": int(after or 0),
            "scrolledPx": abs(int(after or 0) - start_y),
            "reason": "page_end" if delta != 0 and after == start_y else "matched",
            "trace": [], "startScrollY": start_y,
            "containerSelector": container_selector or "",
            "resolvedContainer": resolved,
        }

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

    async def _frame_offset(self, page: Any, frame: Any) -> tuple[float, float]:
        """Mirror of page.ts getFrameOffset with the URL-match fallback.

        Cross-origin iframes can throw on frame.frame_element(); silently
        defaulting to (0,0) lands drag coordinates in the wrong place
        (the chasecdn case). We try frame_element() first, then scan the
        parent frame's <iframe> elements for a src match.
        """
        if frame is page.main_frame:
            return 0.0, 0.0
        try:
            fe = await frame.frame_element()
            if fe is not None:
                box = await fe.bounding_box()
                if box:
                    return float(box["x"]), float(box["y"])
        except Exception:
            pass
        try:
            parent = frame.parent_frame or page.main_frame
            url = frame.url
            offset = await parent.evaluate(
                "(targetUrl) => {"
                "const ifs = Array.prototype.slice.call(document.querySelectorAll('iframe'));"
                "for (const f of ifs) {"
                "  if (f.src === targetUrl) {"
                "    const r = f.getBoundingClientRect();"
                "    return { x: r.x, y: r.y };"
                "  }"
                "}"
                "return null;"
                "}",
                url,
            )
            if offset:
                return float(offset["x"]), float(offset["y"])
        except Exception:
            pass
        logger.warning(
            "[t3._frame_offset] could not resolve offset for frame %s; using (0,0)",
            getattr(frame, "url", "?"),
        )
        return 0.0, 0.0

    async def set_slider(
        self,
        sid: str,
        selector: str,
        value: Any,
        *,
        value_mode: str = "absolute",
        method: str = "auto",
    ) -> dict[str, Any]:
        """Set a slider's value. Mirrors src/browser/page.ts setSlider.

        Tries three strategies in order: (A) native `<input type=range>`
        value-set + input/change events, (B) ARIA slider keyboard walk,
        (C) pixel drag. Frame-aware: probes every frame in `page.frames`.
        """
        s = self._get(sid)
        page = s.page
        import json as _json

        # Find the frame that contains the selector. Shadow-DOM-piercing
        # query so custom elements (mds-slider on Chase, Lit/React
        # wrappers) resolve to the inner native input.
        probe_src = (
            _SHADOW_DOM_HELPERS_SRC + ";"
            "(function(sel){"
            "var el=__sb_queryDeep(document,sel);"
            "if(!el)return null;"
            "var tag=el.tagName.toLowerCase();"
            "var type=(el.type||'').toLowerCase();"
            "var role=el.getAttribute('role');"
            "var kind='other';"
            "if(tag==='input'&&type==='range')kind='range-input';"
            "else if(role==='slider'||el.hasAttribute('aria-valuenow'))kind='aria-slider';"
            "function pn(s){if(s==null||s==='')return null;var n=parseFloat(s);return isFinite(n)?n:null;}"
            "var min=kind==='range-input'?pn(el.min):pn(el.getAttribute('aria-valuemin'));"
            "var max=kind==='range-input'?pn(el.max):pn(el.getAttribute('aria-valuemax'));"
            "var step=kind==='range-input'?pn(el.step):null;"
            "return {kind:kind,meta:{min:min,max:max,step:step}};"
            f"}})({_json.dumps(selector)})"
        )
        resolved_frame = None
        kind = "other"
        meta = {"min": None, "max": None, "step": None}
        frames_searched: list[str] = []
        frames_list = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        for f in frames_list:
            furl = f.url
            try:
                probe = await f.evaluate(probe_src)
                frames_searched.append(f"{furl} (ok, found={'y' if probe else 'n'})")
                if probe:
                    resolved_frame = f
                    kind = probe["kind"]
                    meta = probe["meta"]
                    break
            except Exception as exc:
                frames_searched.append(f"{furl} (err: {str(exc)[:100]})")
        if resolved_frame is None:
            return {
                "strategy": "unresolved", "frameUrl": "",
                "before": None, "after": None,
                "min": None, "max": None, "step": None,
                "error": f"selector not found in any frame: {selector}",
                "framesSearched": frames_searched,
            }
        frame_url = resolved_frame.url
        mn, mx = meta.get("min"), meta.get("max")
        step_n = meta.get("step") or 1

        def _to_abs(v: float) -> float:
            if value_mode == "ratio":
                if mn is None or mx is None:
                    return v
                return mn + v * (mx - mn)
            return v

        # Strategy A: native range input
        if kind == "range-input" and method in ("auto", "range-input"):
            if isinstance(value, (list, tuple)) and len(value) == 2:
                target: Any = [float(_to_abs(value[0])), float(_to_abs(value[1]))]
            else:
                target = float(_to_abs(float(value)))
            set_src = (
                _SHADOW_DOM_HELPERS_SRC + ";"
                "(function(sel,target){"
                "var first=__sb_queryDeep(document,sel);"
                "if(!first)return {ok:false,reason:'not-found'};"
                "var els=[first];"
                "if(Array.isArray(target)){"
                "  var rootScope=first.getRootNode?first.getRootNode():document;"
                "  var p=first.parentElement;"
                "  if(p){var sibs=__sb_queryAllDeep(p,'input[type=\"range\"]');"
                "    if(sibs.length<2&&rootScope&&rootScope!==document)"
                "      sibs=__sb_queryAllDeep(rootScope,'input[type=\"range\"]');"
                "    if(sibs.length>=2)els=sibs.slice(0,2);}"
                "}"
                "var before=els.map(function(e){return parseFloat(e.value);});"
                "var targets=Array.isArray(target)?target:[target];"
                "var setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');"
                "setter=setter?setter.set:null;"
                "for(var i=0;i<els.length;i++){"
                "  var el=els[i];"
                "  var tv=targets[Math.min(i,targets.length-1)];"
                "  var lo=parseFloat(el.min||String(tv));"
                "  var hi=parseFloat(el.max||String(tv));"
                "  var st=parseFloat(el.step||'1')||1;"
                "  var v=Math.max(lo,Math.min(hi,tv));"
                "  v=Math.round((v-lo)/st)*st+lo;"
                "  v=Math.round(v*1e8)/1e8;"
                "  if(setter)setter.call(el,String(v));else el.value=String(v);"
                "  el.dispatchEvent(new Event('input',{bubbles:true}));"
                "  el.dispatchEvent(new Event('change',{bubbles:true}));"
                "  __sb_dispatchHostSignal(el,['input','change']);"
                "}"
                "var after=els.map(function(e){return parseFloat(e.value);});"
                "return {ok:true,before:before,after:after};"
                f"}})({_json.dumps(selector)},{_json.dumps(target)})"
            )
            result = await resolved_frame.evaluate(set_src)
            if isinstance(result, dict) and result.get("ok"):
                before = result["before"]
                after = result["after"]
                return {
                    "strategy": "range-input", "frameUrl": frame_url,
                    "before": before[0] if len(before) == 1 else before,
                    "after": after[0] if len(after) == 1 else after,
                    "min": mn, "max": mx, "step": step_n,
                }

        # Strategy B: ARIA slider → focus + arrow keys
        if kind in ("aria-slider", "range-input") and method in ("auto", "keyboard"):
            target_scalar = float(_to_abs(value[0] if isinstance(value, (list, tuple)) else float(value)))
            state_src = (
                _SHADOW_DOM_HELPERS_SRC + ";"
                "(function(sel){"
                "var el=__sb_queryDeep(document,sel);"
                "if(!el)return null;"
                "var r=el.getBoundingClientRect();"
                "var aNow=el.getAttribute('aria-valuenow');"
                "var aMin=el.getAttribute('aria-valuemin');"
                "var aMax=el.getAttribute('aria-valuemax');"
                "return {x:r.x,y:r.y,w:r.width,h:r.height,"
                "cur:aNow!=null?parseFloat(aNow):(el.value!=null?parseFloat(el.value):NaN),"
                "lo:aMin!=null?parseFloat(aMin):(el.min?parseFloat(el.min):NaN),"
                "hi:aMax!=null?parseFloat(aMax):(el.max?parseFloat(el.max):NaN)};"
                f"}})({_json.dumps(selector)})"
            )
            st = await resolved_frame.evaluate(state_src)
            import math
            if (
                st is not None
                and all(math.isfinite(st[k]) for k in ("cur", "lo", "hi"))
            ):
                # Frame offset with URL-match fallback for cross-origin frames.
                off_x, off_y = await self._frame_offset(page, resolved_frame)
                cx = round(st["x"] + st["w"] / 2 + off_x)
                cy = round(st["y"] + st["h"] / 2 + off_y)
                try:
                    await page.mouse.click(cx, cy)
                except Exception:
                    pass
                clamped = max(st["lo"], min(st["hi"], target_scalar))
                delta = clamped - st["cur"]
                stn = step_n if step_n and step_n > 0 else 1
                n = min(500, abs(round(delta / stn)))
                if clamped == st["lo"]:
                    await page.keyboard.press("Home")
                elif clamped == st["hi"]:
                    await page.keyboard.press("End")
                else:
                    key_name = "ArrowRight" if delta >= 0 else "ArrowLeft"
                    for i in range(n):
                        await page.keyboard.press(key_name)
                        if i % 25 == 24:
                            await asyncio.sleep(0.008)
                after_src = (
                    _SHADOW_DOM_HELPERS_SRC + ";"
                    "(function(sel){"
                    "var el=__sb_queryDeep(document,sel);"
                    "if(!el)return null;"
                    "var aNow=el.getAttribute('aria-valuenow');"
                    "if(aNow!=null)return parseFloat(aNow);"
                    "return el.value!=null?parseFloat(el.value):null;"
                    f"}})({_json.dumps(selector)})"
                )
                after = await resolved_frame.evaluate(after_src)
                if after is not None and math.isfinite(after):
                    return {
                        "strategy": "keyboard", "frameUrl": frame_url,
                        "before": st["cur"], "after": after,
                        "min": st["lo"], "max": st["hi"], "step": stn,
                    }

        # Strategy C: pixel drag
        if method in ("auto", "drag"):
            if isinstance(value, (list, tuple)):
                ratio = value[0] if value_mode == "ratio" else 0.5
            else:
                if value_mode == "ratio":
                    ratio = max(0.0, min(1.0, float(value)))
                elif mn is not None and mx is not None:
                    ratio = (float(value) - mn) / max(1e-9, (mx - mn))
                else:
                    ratio = 0.5
            rect_src = (
                _SHADOW_DOM_HELPERS_SRC + ";"
                "(function(sel){"
                "var el=__sb_queryDeep(document,sel);"
                "if(!el)return null;"
                "var track=el.closest('[role=\"slider\"],[class*=\"slider\" i],[class*=\"track\" i]')||el;"
                "if(track===el){"
                "  var rs=el.getRootNode?el.getRootNode():null;"
                "  if(rs&&rs.host){var ht=rs.host.closest&&rs.host.closest('[role=\"slider\"],[class*=\"slider\" i],[class*=\"track\" i]');if(ht)track=ht;}"
                "}"
                "var tr=track.getBoundingClientRect();"
                "var hr=el.getBoundingClientRect();"
                "return {track:{x:tr.x,y:tr.y,w:tr.width,h:tr.height},"
                "thumb:{x:hr.x,y:hr.y,w:hr.width,h:hr.height}};"
                f"}})({_json.dumps(selector)})"
            )
            rect = await resolved_frame.evaluate(rect_src)
            if not rect:
                return {
                    "strategy": "unresolved", "frameUrl": frame_url,
                    "before": None, "after": None,
                    "min": mn, "max": mx, "step": step_n,
                    "error": "could not read thumb/track rect",
                    "framesSearched": frames_searched,
                }
            off_x, off_y = await self._frame_offset(page, resolved_frame)
            start_x = round(rect["thumb"]["x"] + rect["thumb"]["w"] / 2 + off_x)
            start_y = round(rect["thumb"]["y"] + rect["thumb"]["h"] / 2 + off_y)
            end_x = round(
                rect["track"]["x"] + rect["track"]["w"] * max(0.0, min(1.0, ratio)) + off_x,
            )
            end_y = start_y
            try:
                await page.mouse.move(start_x, start_y)
                await page.mouse.down()
                steps_n = 30
                for i in range(1, steps_n + 1):
                    t = i / steps_n
                    t2 = t * t * (3 - 2 * t)
                    x = start_x + (end_x - start_x) * t2
                    y = start_y + (end_y - start_y) * t2
                    await page.mouse.move(x, y)
                    await asyncio.sleep(0.01)
                await page.mouse.up()
            except Exception as exc:
                return {
                    "strategy": "unresolved", "frameUrl": frame_url,
                    "before": None, "after": None,
                    "min": mn, "max": mx, "step": step_n,
                    "error": f"drag failed: {str(exc)[:120]}",
                    "framesSearched": frames_searched,
                }
            after_src2 = (
                _SHADOW_DOM_HELPERS_SRC + ";"
                "(function(sel){"
                "var el=__sb_queryDeep(document,sel);"
                "if(!el)return null;"
                "var aNow=el.getAttribute('aria-valuenow');"
                "if(aNow!=null)return parseFloat(aNow);"
                "return el.value!=null?parseFloat(el.value):null;"
                f"}})({_json.dumps(selector)})"
            )
            after = None
            try:
                after = await resolved_frame.evaluate(after_src2)
            except Exception:
                pass
            import math as _math
            return {
                "strategy": "drag", "frameUrl": frame_url,
                "before": None,
                "after": after if (after is not None and _math.isfinite(after)) else None,
                "min": mn, "max": mx, "step": step_n,
            }

        return {
            "strategy": "unresolved", "frameUrl": frame_url,
            "before": None, "after": None,
            "min": mn, "max": mx, "step": step_n,
            "error": f"no strategy applied (method={method})",
            "framesSearched": frames_searched,
        }

    async def set_slider_at(
        self,
        sid: str,
        handle: dict[str, Any],
        track: dict[str, Any],
        ratio: float,
    ) -> dict[str, Any]:
        """Vision-indexed slider drag (patchright). Handle + track are
        CSS-pixel bboxes {x, y, w, h} already denormalised by the Python
        tool; this method just does the math and fires the drag."""
        s = self._get(sid)
        try:
            hx = float(handle.get("x", 0)); hy = float(handle.get("y", 0))
            hw = float(handle.get("w", 0)); hh = float(handle.get("h", 0))
            tx = float(track.get("x", 0));  ty = float(track.get("y", 0))
            tw = float(track.get("w", 0));  th = float(track.get("h", 0))
        except Exception as exc:
            return {
                "strategy": "unresolved",
                "error": f"bad bbox shape: {exc}",
                "handle_bbox": handle, "track_bbox": track,
            }
        r = max(0.0, min(1.0, float(ratio)))
        start_x = round(hx + hw / 2.0)
        start_y = round(hy + hh / 2.0)
        end_x = round(tx + tw * r)
        end_y = start_y
        try:
            await s.page.mouse.move(start_x, start_y)
            await s.page.mouse.down()
            steps_n = 30
            for i in range(1, steps_n + 1):
                t = i / steps_n
                t2 = t * t * (3 - 2 * t)
                x = start_x + (end_x - start_x) * t2
                y = start_y + (end_y - start_y) * t2
                await s.page.mouse.move(x, y)
                await asyncio.sleep(0.01)
            await s.page.mouse.up()
        except Exception as exc:
            return {
                "strategy": "unresolved",
                "error": f"drag failed: {str(exc)[:120]}",
                "handle_bbox": {"x": hx, "y": hy, "w": hw, "h": hh},
                "track_bbox": {"x": tx, "y": ty, "w": tw, "h": th},
            }
        return {
            "strategy": "vision-drag",
            "handle_bbox": {"x": hx, "y": hy, "w": hw, "h": hh},
            "track_bbox": {"x": tx, "y": ty, "w": tw, "h": th},
            "target_px": {"x": end_x, "y": end_y},
        }

    async def list_slider_handles(self, sid: str) -> list[dict[str, Any]]:
        """DOM-only slider handle enumeration (tier-3 patchright).

        Mirrors page.ts listSliderHandles. Walks every frame for
        slider-shaped elements, returns their bboxes (document CSS
        pixels) + nearest row-level label. No vision required.
        """
        s = self._get(sid)
        page = s.page
        scan_js = (
            _SHADOW_DOM_HELPERS_SRC + ";"
            "(function(){"
            "try {"
            "var out = [];"
            "var sel = ['input[type=\"range\"]','[role=\"slider\"]','[aria-valuenow]',"
            "'[class*=\"handle\" i]','[class*=\"thumb\" i]','[class*=\"slider-button\" i]',"
            "'[class*=\"slider-handle\" i]','[data-handle]'].join(',');"
            "var found = __sb_queryAllDeep(document, sel);"
            "var seen = new Set();"
            "for (var i = 0; i < found.length; i++) {"
            "var el = found[i]; var r = el.getBoundingClientRect();"
            "if (!r || r.width < 3 || r.height < 3) continue;"
            "if (r.width > 200 || r.height > 200) continue;"
            "var key = Math.round(r.left)+'_'+Math.round(r.top)+'_'+Math.round(r.width)+'_'+Math.round(r.height);"
            "if (seen.has(key)) continue; seen.add(key);"
            "var tag = el.tagName.toLowerCase(); var type = (el.type || '').toLowerCase();"
            "var role = el.getAttribute('role'); var kind = 'custom';"
            "if (tag === 'input' && type === 'range') kind = 'range-input';"
            "else if (role === 'slider' || el.hasAttribute('aria-valuenow')) kind = 'aria-slider';"
            "var hcy = r.top + r.height / 2;"
            "var ytol = Math.max(r.height * 4, 80);"
            "var label = ''; var bestDy = Infinity;"
            "var elRef = el;"
            "__sb_walkDeepElements(document.body || document.documentElement, function(cand){"
            "if (cand === elRef) return;"
            "try { if (cand.contains && cand.contains(elRef)) return; } catch(e){}"
            "var cr = cand.getBoundingClientRect ? cand.getBoundingClientRect() : null;"
            "if (!cr || cr.width === 0 || cr.height === 0) return;"
            "if (cr.height > 80) return;"
            "var text = (cand.textContent || '').replace(/\\s+/g, ' ').trim();"
            "if (!text || text.length > 200 || text.length < 3) return;"
            "var ccy = cr.top + cr.height / 2;"
            "var dy = Math.abs(ccy - hcy);"
            "if (dy > ytol) return;"
            "if (!/[A-Za-z]/.test(text)) return;"
            "if (dy < bestDy) { bestDy = dy; label = text; }"
            "});"
            "out.push({ kind: kind, bbox: { x: r.left, y: r.top, w: r.width, h: r.height }, label: label });"
            "}"
            "return out;"
            "} catch(e) { return { error: String(e && e.message || e) }; }"
            "})()"
        )

        result: list[dict[str, Any]] = []
        try:
            frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
        except Exception:
            frames = []
        for f in frames:
            off_x, off_y = await self._frame_offset(page, f)
            try:
                hits = await f.evaluate(scan_js)
            except Exception:
                continue
            if not isinstance(hits, list):
                continue
            for h in hits:
                if not isinstance(h, dict):
                    continue
                bb = h.get("bbox") or {}
                try:
                    x = float(bb.get("x", 0)) + off_x
                    y = float(bb.get("y", 0)) + off_y
                    w = float(bb.get("w", 0))
                    hh = float(bb.get("h", 0))
                except Exception:
                    continue
                result.append({
                    "index": len(result),
                    "frame_url": f.url,
                    "kind": h.get("kind", "custom"),
                    "bbox": {
                        "x": round(x), "y": round(y),
                        "w": round(w), "h": round(hh),
                    },
                    "label": h.get("label", ""),
                })
        return result

    async def drag_slider_until(
        self,
        sid: str,
        handle: dict[str, Any],
        target_value: float,
        *,
        label_pattern: str | None = None,
        tolerance: float = 0.0,
        max_iterations: int = 25,
        step_px: int = 8,
        direction: str = "auto",
    ) -> dict[str, Any]:
        """Closed-loop slider drag for tier-3 (patchright).

        Same semantics as page.ts dragSliderUntil: mouse down, step +
        read-value + adjust + release. All frames scanned per iteration.
        """
        s = self._get(sid)
        page = s.page
        import json as _json

        try:
            hx = float(handle.get("x", 0)); hy = float(handle.get("y", 0))
            hw = float(handle.get("w", 0)); hh = float(handle.get("h", 0))
        except Exception as exc:
            return {
                "strategy": "closed-loop", "completed": False,
                "error": f"bad handle bbox: {exc}",
                "iterations": 0,
                "initial_value": None, "final_value": None,
                "target_value": target_value, "tolerance": tolerance,
                "trace": [], "label_text": None,
            }
        handle_cx = round(hx + hw / 2.0)
        handle_cy = round(hy + hh / 2.0)
        pattern_src = label_pattern or r"(-?\d+(?:\.\d+)?)"
        y_tolerance = max(hh * 4, 80)

        # Scanner walks ELEMENTS (not text nodes) and reads textContent
        # so labels split across spans —
        #   <label>Age Range: <span>25</span> to <span>75</span></label>
        # — concatenate into one string the regex can match.
        def _scan_js(local_cy: float) -> str:
            return (
                _SHADOW_DOM_HELPERS_SRC + ";"
                "(function(pat, hcy, ytol){"
                "try{"
                "var re = new RegExp(pat);"
                "var best = null;"
                "__sb_walkDeepElements(document.body || document.documentElement, function(el){"
                "var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;"
                "if (!r || r.width === 0 || r.height === 0) return;"
                "if (r.height > 80) return;"
                "var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();"
                "if (!text || text.length > 300) return;"
                "var m = re.exec(text); if (!m) return;"
                "var num = parseFloat(m[1]); if (!isFinite(num)) return;"
                "var cy = r.top + r.height / 2;"
                "var dy = Math.abs(cy - hcy);"
                "if (dy > ytol) return;"
                "var area = r.width * r.height;"
                "if (best === null || dy < best.dy || (dy === best.dy && area < best.area)) {"
                "best = { dy: dy, area: area, value: num, text: text };"
                "}"
                "});"
                "return best ? { value: best.value, text: best.text } : null;"
                "} catch(e){ return null; }"
                f"}})({_json.dumps(pattern_src)}, {local_cy}, {y_tolerance})"
            )

        async def read_value() -> tuple[float | None, str | None]:
            try:
                frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
            except Exception:
                frames = []
            for f in frames:
                off_x, off_y = await self._frame_offset(page, f)
                local_cy = handle_cy - off_y
                try:
                    result = await f.evaluate(_scan_js(local_cy))
                except Exception:
                    continue
                if result and isinstance(result, dict) and "value" in result:
                    try:
                        return float(result["value"]), str(result.get("text") or "")
                    except (TypeError, ValueError):
                        continue
            return None, None

        initial_value, initial_text = await read_value()
        trace: list[dict[str, Any]] = [
            {"iter": 0, "cursor_x": handle_cx, "value": initial_value},
        ]
        cursor_x = handle_cx
        last_value = initial_value
        completed = False

        # Bail BEFORE pressing the mouse if we can't read the label —
        # silent dragging without feedback is what produced the
        # "hallucination" behaviour.
        if initial_value is None:
            # Same element-walk as _scan_js so diagnostic text matches
            # what the scanner sees. Returns up to 10 row-sized labels.
            sample_js = (
                _SHADOW_DOM_HELPERS_SRC + ";"
                "(function(hcy, ytol){"
                "try {"
                "var out = [];"
                "__sb_walkDeepElements(document.body || document.documentElement, function(el){"
                "var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;"
                "if (!r || r.width === 0 || r.height === 0) return;"
                "if (r.height > 80) return;"
                "var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();"
                "if (!text || text.length > 300) return;"
                "var cy = r.top + r.height / 2;"
                "if (Math.abs(cy - hcy) > ytol) return;"
                "out.push(text); if (out.length >= 10) return false;"
                "});"
                "return out;"
                "} catch(e) { return []; }"
                f"}})({handle_cy}, {y_tolerance})"
            )
            samples: list[str] = []
            try:
                frames = [page.main_frame] + [f for f in page.frames if f is not page.main_frame]
            except Exception:
                frames = []
            for f in frames:
                try:
                    part = await f.evaluate(sample_js)
                    if part and isinstance(part, list):
                        samples.extend(str(x) for x in part)
                        if len(samples) >= 10:
                            break
                except Exception:
                    pass
            return {
                "strategy": "closed-loop", "completed": False,
                "iterations": 0,
                "initial_value": None, "final_value": None,
                "target_value": target_value, "tolerance": tolerance,
                "trace": trace,
                "label_text": f"NO_MATCH — nearby text: {samples[:8]!r}",
                "label_selector_hint": None,
            }

        # Press.
        try:
            await page.mouse.move(handle_cx, handle_cy)
            await asyncio.sleep(0.05)
            await page.mouse.down()
        except Exception as exc:
            return {
                "strategy": "closed-loop", "completed": False,
                "error": f"mouse down failed: {str(exc)[:120]}",
                "iterations": 0,
                "initial_value": initial_value, "final_value": last_value,
                "target_value": target_value, "tolerance": tolerance,
                "trace": trace, "label_text": initial_text,
            }

        step = int(step_px)
        iters = 0
        consecutive_misses = 0
        try:
            for iters in range(1, int(max_iterations) + 1):
                if direction == "left":
                    dir_sign = -1
                elif direction == "right":
                    dir_sign = 1
                elif last_value is not None:
                    if abs(last_value - target_value) <= tolerance:
                        completed = True
                        break
                    dir_sign = 1 if target_value > last_value else -1
                else:
                    dir_sign = 1
                prev_x = cursor_x
                prev_val = last_value
                next_x = round(cursor_x + dir_sign * step)
                sub = 4
                for si in range(1, sub + 1):
                    t = si / sub
                    ix = round(cursor_x + (next_x - cursor_x) * t)
                    try:
                        await page.mouse.move(ix, handle_cy)
                    except Exception:
                        pass
                    await asyncio.sleep(0.008)
                cursor_x = next_x
                await asyncio.sleep(0.03)
                reading, _text = await read_value()
                trace.append({"iter": iters, "cursor_x": cursor_x, "value": reading})
                if reading is not None:
                    consecutive_misses = 0
                    if prev_val is not None and cursor_x != prev_x:
                        vpp = (reading - prev_val) / (cursor_x - prev_x)
                        import math as _math
                        if _math.isfinite(vpp) and abs(vpp) > 1e-6:
                            remaining = target_value - reading
                            suggested = abs(remaining / vpp) * 0.5
                            step = max(1, min(80, int(round(suggested))))
                    last_value = reading
                    if abs(last_value - target_value) <= tolerance:
                        completed = True
                        break
                else:
                    consecutive_misses += 1
                    if consecutive_misses >= 3:
                        # Pattern stopped matching — bail rather than drag blind.
                        break
                    step = max(1, step // 2)
        finally:
            try:
                await page.mouse.up()
            except Exception:
                pass

        return {
            "strategy": "closed-loop",
            "iterations": iters,
            "initial_value": initial_value,
            "final_value": last_value,
            "target_value": target_value,
            "tolerance": tolerance,
            "trace": trace,
            "label_text": initial_text,
            "label_selector_hint": None,
            "completed": completed,
        }

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
