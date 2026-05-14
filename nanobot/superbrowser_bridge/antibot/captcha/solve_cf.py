"""Cloudflare Managed Challenge interstitial solver.

Four-phase ladder mirroring the TS-side `src/browser/captcha/strategies/
turnstile.ts`:

  1. PROBE  — humanized mouse/wheel wait (the existing
              `T3SessionManager._wait_for_cf_clear`). Many CF deployments
              auto-pass on fingerprint alone; this is the cheap fast path.
  2. CLICK  — for builds that embed an interactive Turnstile checkbox in
              a cross-origin iframe at challenges.cloudflare.com (cars.com
              class), click the checkbox via patchright's frame walk +
              bbox math. CF stamps `cf_clearance` once the click registers.
  3. TOKEN  — if a sitekey was extracted from the iframe URL, hand it to
              2captcha (or the configured vendor) and inject the resulting
              Turnstile token into the page. Reuses `solve_token` plumbing.
  4. SUBMIT — after token injection, requestSubmit() the form holding
              the cf-turnstile-response field if no nav fires within ~5 s.

The single-phase wait predecessor still lives — Phase 1 is exactly that
loop. Phase 2-4 only run if Phase 1 timed out, so passive-CF sites still
clear in 5-10 s like before.

Distinct from `solve_token` (Turnstile widget with a site_key visible on
a `.cf-turnstile` div) and `solve_vision` (grid/click challenges).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .detect import CaptchaInfo

logger = logging.getLogger(__name__)


# Phase budgets (seconds). Total worst-case budget is roughly the sum
# minus token-vendor parallelism (token poll runs while we keep watching
# for nav). Phase 1 was 60s in the wait-only era; trimmed to 25s so the
# remaining budget can be spent on click + token escalation.
_PHASE1_WAIT_S = 25.0
# Post-click wait budget: CF transitions widget to "Verifying..." after
# the click and runs server-side scoring. Verified empirically against
# cars.com that this can take 15-30s — shorter budgets timed out while
# CF was still verifying. 30s lands above the p95 verify duration
# without blocking forever on a no-op.
_PHASE2_POST_CLICK_WAIT_S = 30.0
_PHASE4_NAV_WATCH_S = 5.0


async def _phase4_auto_submit(s_page) -> bool:
    """If a form contains the Turnstile response field, requestSubmit it.

    Direct port of `turnstile.ts:autoSubmitForm`. Token injection alone
    sometimes isn't enough — some sites wait for the form's own submit
    handler to fire before redirecting. Returns True if a form was found
    and submitted, False otherwise.
    """
    try:
        return bool(await s_page.evaluate(
            """() => {
                const field = document.querySelector(
                    '[name="cf-turnstile-response"]'
                );
                if (!field) return false;
                const form = field.closest('form');
                if (!form) return false;
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                } else {
                    form.submit();
                }
                return true;
            }"""
        ))
    except Exception:
        return False


async def _extract_sitekey_from_frames(page) -> str | None:
    """Walk page.frames for a Cloudflare/Turnstile iframe and pull the
    sitekey out of the URL.

    CF Turnstile sitekeys are 24-character `0x...` hex strings. They
    can appear as:
      - a query param:  ?k=0x4AAA... or ?sitekey=0x4AAA...
      - a path segment: .../turnstile/.../0x4AAAAAAADnPIDROrmt1Wwj/

    Returns the first match found, or None.
    """
    import re
    pat = re.compile(r"\b(0x[0-9a-zA-Z]{20,32})\b")
    for frame in page.frames:
        url = frame.url or ""
        if "challenges.cloudflare.com" not in url and "turnstile" not in url:
            continue
        try:
            from urllib.parse import urlparse, parse_qs
            u = urlparse(url)
            qs = parse_qs(u.query or "")
            for k in ("k", "sitekey"):
                if qs.get(k):
                    return qs[k][0]
            m = pat.search(u.path or "")
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


async def _has_turnstile_token(s_page) -> bool:
    """True if the page's cf-turnstile-response input is populated."""
    try:
        return bool(await s_page.evaluate(
            """() => {
                const el = document.querySelector(
                    '[name="cf-turnstile-response"]'
                );
                return !!(el && el.value && el.value.length > 20);
            }"""
        ))
    except Exception:
        return False


async def solve_cf_interstitial(
    t3manager,
    session_id: str,
    info: CaptchaInfo,
    *,
    vision_agent: Any = None,  # unused; kept for solver signature parity
    timeout_s: float | None = None,
) -> dict:
    """Resolve a Cloudflare Managed Challenge via the 4-phase ladder.

    `timeout_s` (and `T3_CF_SOLVER_WAIT_S` env override) controls the
    Phase 1 wait budget. Phases 2-4 add their own bounded waits on top.
    Total worst-case is roughly Phase 1 + Phase 2 + token-vendor poll.
    """
    # Phase 1 budget: caller-supplied -> env override -> default.
    phase1_budget = timeout_s if timeout_s is not None else float(
        os.environ.get("T3_CF_SOLVER_WAIT_S") or _PHASE1_WAIT_S,
    )
    start = time.monotonic()
    try:
        s = t3manager._get(session_id)  # type: ignore[attr-defined]
    except KeyError:
        return {
            "solved": False, "method": "cf_wait",
            "error": f"session {session_id} not found",
        }

    origin = s.page.url
    try:
        from urllib.parse import urlparse as _urlparse
        domain = (_urlparse(origin).hostname or "").lower().replace("www.", "")
    except Exception:
        domain = ""

    trace: list[str] = []

    # ----- Phase 1: passive humanized wait -----------------------------
    print(f"  [cf_wait] phase1 starting (budget={int(phase1_budget)}s)")
    p1 = await t3manager._wait_for_cf_clear(
        session_id, timeout_s=phase1_budget, origin_url=origin,
    )
    trace.append(
        f"phase1: cleared={p1.get('cleared')} "
        f"iters={p1.get('iterations')} "
        f"cookies={p1.get('cookies_landed')}"
    )
    if p1.get("cleared"):
        return _build_success(start, "phase1_passive_wait", p1, trace)

    # ----- Phase 2: click the checkbox iframe --------------------------
    target = await t3manager._find_cf_checkbox_target(session_id)
    if target is not None:
        x, y = target
        print(f"  [cf_click] found checkbox at ({x:.0f}, {y:.0f})")
        clicked = await t3manager._click_cf_checkbox(session_id, x, y)
        trace.append(
            f"phase2: target=({x:.0f},{y:.0f}) clicked={clicked}"
        )
        if clicked:
            print(
                f"  [cf_click] clicked, re-polling for cf_clearance "
                f"({int(_PHASE2_POST_CLICK_WAIT_S)}s)"
            )
            p2 = await t3manager._wait_for_cf_clear(
                session_id,
                timeout_s=_PHASE2_POST_CLICK_WAIT_S,
                origin_url=origin,
            )
            trace.append(
                f"phase2_wait: cleared={p2.get('cleared')} "
                f"cookies={p2.get('cookies_landed')}"
            )
            if p2.get("cleared"):
                return _build_success(start, "phase2_iframe_click", p2, trace)
            # Some forms register the click as a token without redirecting.
            # If the response field is now populated we can short-circuit
            # to Phase 4 (form submit) instead of paying for a token solve.
            if await _has_turnstile_token(s.page):
                trace.append("phase2_post: turnstile-response field populated")
                if await _phase4_auto_submit(s.page):
                    trace.append("phase4: form requestSubmit() fired")
                    p4 = await t3manager._wait_for_cf_clear(
                        session_id,
                        timeout_s=_PHASE4_NAV_WATCH_S,
                        origin_url=origin,
                    )
                    if p4.get("cleared"):
                        return _build_success(
                            start, "phase2_click_then_submit", p4, trace,
                        )
    else:
        trace.append("phase2: no checkbox target found, skipping")

    # ----- Phase 3: 2captcha token -------------------------------------
    site_key = (info.site_key or "").strip()
    if not site_key:
        # Detect's `document.querySelector('iframe[src*="..."]')` misses
        # the iframe when CF embeds it via closed Shadow DOM (cars.com
        # 2026-05). `page.frames` still sees the underlying frame via
        # Chrome's frame tree — pull the sitekey out of the frame URL
        # path so Phase 3 has something to submit. Format observed:
        # `.../turnstile/.../0x4AAAAAAADnPIDROrmt1Wwj/`.
        site_key = await _extract_sitekey_from_frames(s.page) or ""
        if site_key:
            trace.append(f"phase3_prep: sitekey={site_key[:14]}... (from frames)")
    provider = (os.environ.get("CAPTCHA_PROVIDER") or "2captcha").lower()
    api_key = (
        os.environ.get("CAPTCHA_API_KEY")
        or os.environ.get("TWOCAPTCHA_API_KEY")
        or os.environ.get("ANTICAPTCHA_API_KEY")
        or os.environ.get("NOPECHA_API_KEY")
        or ""
    )
    if site_key and api_key:
        print(
            f"  [cf_token] submitting sitekey={site_key[:10]}... "
            f"to {provider}"
        )
        token = await _solve_turnstile_token(
            site_key, origin, provider, api_key,
        )
        if token:
            print(f"  [cf_token] token received (len={len(token)}), injecting")
            try:
                from .solve_token import _INJECT_JS_TEMPLATE
                inject = _INJECT_JS_TEMPLATE["turnstile"]
                await t3manager.evaluate(  # type: ignore[attr-defined]
                    session_id, f"({inject})({token!r})",
                )
                trace.append(f"phase3: token injected (len={len(token)})")
                # ----- Phase 4: form submit if no nav --------------------
                p3 = await t3manager._wait_for_cf_clear(
                    session_id,
                    timeout_s=_PHASE4_NAV_WATCH_S,
                    origin_url=origin,
                )
                if p3.get("cleared"):
                    return _build_success(
                        start, "phase3_token_inject", p3, trace,
                    )
                if await _phase4_auto_submit(s.page):
                    trace.append("phase4: form requestSubmit() fired")
                    p4 = await t3manager._wait_for_cf_clear(
                        session_id,
                        timeout_s=_PHASE4_NAV_WATCH_S,
                        origin_url=origin,
                    )
                    if p4.get("cleared"):
                        return _build_success(
                            start, "phase4_token_then_submit", p4, trace,
                        )
            except Exception as exc:
                trace.append(
                    f"phase3: inject failed {type(exc).__name__}: {exc}"
                )
        else:
            trace.append(f"phase3: {provider} returned no token")
    elif site_key and not api_key:
        trace.append("phase3: sitekey present but no CAPTCHA_API_KEY — skipped")
    else:
        trace.append("phase3: no sitekey extracted — skipped")

    # All phases failed — return the existing escalation payload.
    return _build_failure(
        start, p1, trace, domain, t3manager_phase1=phase1_budget,
    )


# ---------------------------------------------------------------------------


def _build_success(
    start: float, sub_method: str, wait_result: dict, trace: list[str],
) -> dict[str, Any]:
    """Successful clear — also marks per-domain success in routing."""
    duration_ms = int((time.monotonic() - start) * 1000)
    payload: dict[str, Any] = {
        "solved": True,
        "method": "cf_wait",
        "subMethod": sub_method,
        "durationMs": duration_ms,
        "iterations": wait_result.get("iterations", 0),
        "cookies_landed": wait_result.get("cookies_landed", []),
        "final_url": wait_result.get("final_url", ""),
        "final_title": wait_result.get("final_title", ""),
        "trace": trace,
    }
    try:
        from urllib.parse import urlparse as _urlparse
        host = (
            _urlparse(wait_result.get("final_url", "")).hostname or ""
        ).lower().replace("www.", "")
        if host:
            from superbrowser_bridge import routing as _routing
            _routing.record_cf_success(host)
    except Exception:
        pass
    return payload


def _build_failure(
    start: float,
    phase1_result: dict,
    trace: list[str],
    domain: str,
    *,
    t3manager_phase1: float,
) -> dict[str, Any]:
    """All four phases exhausted — record failure + return hints."""
    duration_ms = int((time.monotonic() - start) * 1000)
    payload: dict[str, Any] = {
        "solved": False,
        "method": "cf_wait",
        "subMethod": "all_phases_exhausted",
        "block_class": "cloudflare",
        "durationMs": duration_ms,
        "iterations": phase1_result.get("iterations", 0),
        "cookies_landed": phase1_result.get("cookies_landed", []),
        "final_url": phase1_result.get("final_url", ""),
        "final_title": phase1_result.get("final_title", ""),
        "trace": trace,
        "error": (
            phase1_result.get("error")
            or f"cf_managed_challenge_not_cleared_after_4_phases"
        ),
    }
    streak = 0
    try:
        from superbrowser_bridge import routing as _routing
        if domain:
            streak = _routing.record_cf_failure(domain)
    except Exception:
        pass
    try:
        from superbrowser_bridge.antibot import proxy_tiers as _tiers
        if domain:
            _tiers.default().demote(domain)
    except Exception:
        pass
    hints: list[str] = []
    if not phase1_result.get("cookies_landed"):
        hints.append(
            "no challenge-clearance cookie persisted — set "
            "SUPERBROWSER_COOKIE_JAR=1 and pre-solve the domain in a "
            "manual session to bootstrap cf_clearance"
        )
    hints.append("set PROXY_POOL_RESIDENTIAL for residential-IP retry")
    hints.append(
        "set T3_HEADLESS=0 + T3_AUTO_XVFB=1 (Xvfb at /usr/bin/Xvfb) "
        "and restart worker to force headful Chromium"
        + (" — REQUIRED on this domain" if streak >= 2 else "")
    )
    payload["escalation_hints"] = hints
    payload["cf_failure_streak"] = streak
    return payload


async def _solve_turnstile_token(
    site_key: str, page_url: str, provider: str, api_key: str,
) -> str | None:
    """Thin proxy to the matching solver in solve_token.py.

    Kept here so this module owns its full ladder without solve_cf.py
    importing solve_token's private helpers more than necessary.
    """
    try:
        from .solve_token import (
            _solve_2captcha,
            _solve_anticaptcha,
            _solve_nopecha,
        )
    except Exception as exc:
        logger.debug("solve_token import failed: %s", exc)
        return None
    fn = {
        "2captcha": _solve_2captcha,
        "anticaptcha": _solve_anticaptcha,
        "nopecha": _solve_nopecha,
    }.get(provider, _solve_2captcha)
    try:
        return await fn(site_key, page_url, "turnstile", api_key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("token vendor %s raised: %s", provider, exc)
        return None
