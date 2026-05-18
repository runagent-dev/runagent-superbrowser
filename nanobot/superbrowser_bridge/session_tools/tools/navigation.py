"""Session-lifecycle and viewport-navigation tools.

Open/Navigate/Close session, scroll family, wait_for, rewind-to-checkpoint,
and the Tier-1 → Tier-3 escalation tool.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from .._label import clean_label
from ..effects import (
    BLOCKED_BROWSER_OPEN_HARD_STOP,
    WorkerMustExitError,
)
from ..feedback import _feedback_gate
from ..formatting import _build_network_block_message, _format_state
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..telemetry import _update_scroll_telemetry
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


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
        max_stealth: bool = False,
    ) -> Any:
        """Open a session on the given tier. Returns the raw data dict on
        success, or a plain string when the tier-open itself fails (rate
        limit, T3 launch exception). String returns bubble up unchanged
        so the caller can surface them to the agent.

        `max_stealth=True` is forwarded to the T3 manager and forces the
        heaviest fingerprint config (persistent profile + headful + auto-
        Xvfb). Used by the T1-failure escalation path to maximize the
        chance of getting through where the default T3 config still
        trips. Ignored for tier_name="t1" (TS-side stack has no
        equivalent knob).
        """
        if tier_name == "t3":
            from superbrowser_bridge.antibot import interactive_session as _t3mgr
            try:
                return await _t3mgr.default().open(
                    url,
                    task_id=self.s.task_id,
                    timeout_s=45.0,
                    max_stealth=max_stealth,
                )
            except Exception as exc:
                # Log the full traceback to stdout — without this, T3 launch
                # failures (Xvfb race, profile lock, patchright handshake)
                # surface only as a one-line string in the agent's tool
                # result, making the operator chase ghosts. Mirrors the
                # diagnostic log file at /tmp/superbrowser/t3_errors.log.
                import traceback as _tb
                print(
                    f"  [t3_open_failed] {type(exc).__name__}: "
                    f"{str(exc)[:300]}"
                )
                _tb.print_exc()
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

        # --- T1 retry on soft failures -----------------------------------
        # A single 401/403/429/503 from T1 is almost never strong enough
        # evidence that the site needs the heavyweight T3 stack: it can be
        # a fingerprint flicker, a rate-limit hiccup, or a one-off WAF
        # challenge. Retry T1 once on a fresh session before paying the
        # cost of patchright + residential proxy. 502 is upstream-broken
        # — neither a retry nor T3 will help, so skip retry on 502 and
        # fall through to the escalation block (which itself handles 502).
        T1_SOFT_RETRY_CODES = (401, 403, 429, 503)
        status_code = data.get("statusCode") if isinstance(data, dict) else None
        if (
            allow_escalation
            and chosen_tier == "t1"
            and isinstance(status_code, int)
            and status_code in T1_SOFT_RETRY_CODES
        ):
            print(
                f"  [T1 retry after HTTP {status_code}] first attempt "
                f"flagged; retrying T1 with a fresh session before "
                f"escalating..."
            )
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
            # Brief backoff so the second attempt is not back-to-back
            # against the same edge. 750ms is enough to clear most
            # short-lived WAF rate-limit windows.
            await asyncio.sleep(0.75)
            retry_data = await self._open_session_on_tier(
                "t1", url=url, region=region, proxy=proxy,
            )
            if isinstance(retry_data, str):
                # Tier-open itself failed (rate limit / launch error).
                # Surface the message — same contract as the first try.
                return retry_data
            data = retry_data
            status_code = data.get("statusCode") if isinstance(data, dict) else None

        # --- T1 → T3 auto-escalation -------------------------------------
        # When the Tier-1 Puppeteer path hits a hard anti-bot block
        # (401/403/429/502/503) before any content loads, the Tier-3
        # patchright + stealth stack is the right next hop. Close the
        # doomed T1 session, record the block so choose_starting_tier
        # prefers T3 next time, and re-open on T3 within this tool call
        # — the caller sees one consistent result regardless of which
        # tier actually served it. Only reached if the T1 retry above
        # also failed (or the original status was 502, which we don't
        # bother retrying).
        if (
            allow_escalation
            and chosen_tier == "t1"
            and isinstance(status_code, int)
            and status_code in (401, 403, 429, 502, 503)
        ):
            print(
                f"  [T1→T3 auto-escalation] HTTP {status_code} on T1 "
                f"after retry; retrying with patchright (T3)..."
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
            # Deferred to here (post-retry) so a single transient 403 does
            # not poison the routing ledger.
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
            # Re-open on T3 with MAX stealth (persistent profile +
            # headful + auto-Xvfb). The lighter T3 default is fine when
            # the agent picks tier="t3" up-front, but on escalation we've
            # already burned two T1 attempts on this domain — pay the
            # extra launch cost for the heaviest fingerprint we can ship
            # rather than risk a third stuck attempt.
            chosen_tier = "t3"
            data = await self._open_session_on_tier(
                "t3", url=url, region=region, proxy=proxy,
                max_stealth=True,
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
        # v5 — flag the next vision prefetch to ask /state for visual
        # stability (fonts swapped, above-fold images decoded, layout-
        # shift idle). Cold first-load is the canonical case where
        # bbox-vs-text-position drift breaks vision-driven clicks.
        self.s._needs_visual_settle = True

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
                iframe_signature=data.get("iframeSignature") or "",
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
            # on the pinned domain even when it's frustrated. Only fires
            # for CROSS-site hops to Google: when the pinned domain itself
            # is google.com (e.g. the user opened Google Maps directly),
            # intra-Google navigation to /maps, /search, etc. is legitimate
            # and must be allowed — otherwise the agent can't recover by
            # navigating to a coords-anchored Maps URL when geo-IP put the
            # initial viewport in the wrong region.
            _is_google = _target_host == "google.com" or _target_host.endswith(".google.com") or _target_host.endswith(".google.co")
            _looks_like_search = _is_google and not _is_pinned and (
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
        # v5 — flag the next vision prefetch to ask /state for visual
        # stability. browser_navigate is the same race as browser_open
        # (cold target page, fonts/images may take 200-1500ms to
        # settle); without this the first vision after navigate gets
        # bboxes against pre-settled text positions.
        self.s._needs_visual_settle = True

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
                iframe_signature=data.get("iframeSignature") or "",
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema(
            "Scroll direction: 'up' or 'down'. Required when using `pixels`.",
            nullable=True,
        ),
        percent=NumberSchema(
            description=(
                "ABSOLUTE position 0..100 (0=top, 100=bottom). "
                "`percent=20` JUMPS to 20% of the page from the top — it is "
                "NOT 'scroll down by 20%'. Use `pixels` for incremental "
                "motion or `browser_scroll_until` to find a specific control."
            ),
            nullable=True,
        ),
        pixels=IntegerSchema(
            description=(
                "Incremental scroll distance in pixels (positive). "
                "Pair with `direction` for sign. Use this for fine nudges "
                "below a control just past the fold."
            ),
            nullable=True,
        ),
        target_text=StringSchema(
            description=(
                "Optional label/regex to probe for in the NEW viewport "
                "after the scroll lands. When set, the response includes "
                "a `[PROBE target='X' in_viewport=true|false …]` caption "
                "line — direct DOM measurement, NOT vision. Use this "
                "whenever you're scrolling toward a NAMED control: the "
                "probe is your ground truth. If `in_viewport=false`, "
                "scroll again or call `browser_get_markdown` — do NOT "
                "emit a V_n claiming to be this label on the next turn."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = (
        "Scroll the page. Three modes: (a) `direction='up'|'down'` — "
        "small viewport step (~40% of viewport, ~440px on default "
        "viewport); use this for 'show me a bit more'; "
        "(b) `pixels=N` (with `direction`) — explicit incremental, "
        "RECOMMENDED for fine motion; (c) `percent=N` — ABSOLUTE "
        "position (0=top, 100=bottom) — `percent=20` TELEPORTS to 20% "
        "of the page, NOT 'scroll a bit'. "
        "When SEARCHING for a named below-fold control (filter, "
        "button), ALSO pass `target_text='<label>'`. The response will "
        "include `[PROBE target='<label>' in_viewport=true|false "
        "below_fold=…]` — direct DOM measurement. If `in_viewport=false` "
        "and `below_fold=true`, scroll again (pixels=600+, same "
        "target_text); if `anywhere_in_dom=false`, the label isn't on "
        "this page — try a synonym or `browser_get_markdown`. NEVER "
        "click a V_n claiming to be `<label>` when the probe said false."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        direction: str | None = None,
        percent: float | None = None,
        pixels: int | None = None,
        target_text: str | None = None,
        **kw: Any,
    ) -> Any:
        if pixels is not None:
            label = f"{direction or 'down'} {int(pixels)}px"
        elif percent is not None:
            label = f"{percent}%"
        else:
            label = direction or "down"
        if target_text and target_text.strip():
            label += f" probe={target_text.strip()!r}"
        print(f"\n>> browser_scroll({label})")
        gate = await _feedback_gate("browser_scroll")
        if gate:
            return gate
        payload: dict[str, Any] = {}
        # `pixels` (incremental) wins over `percent` (absolute) when both
        # are passed — keeps the explicit unit primary and avoids the
        # confusion that a "small percent" was trying to fix.
        if pixels is not None and pixels > 0:
            payload["pixels"] = int(pixels)
            payload["direction"] = direction or "down"
        elif percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"
        if target_text and target_text.strip():
            payload["targetText"] = target_text.strip()
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
            from ..formatting import _fetch_elements
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        # Pre/post scroll geometry comes from the server in one round-
        # trip — lets us detect silent no-op scrolls without an extra
        # GET. Brain needs this to differentiate "scroll worked but
        # didn't help" from "scroll surface mismatch / locked body".
        pre_geo = data.get("prevScrollInfo") or {}
        pre_y: int | None = (
            int(pre_geo.get("scrollY") or 0)
            if isinstance(pre_geo, dict) and pre_geo.get("scrollY") is not None
            else None
        )
        post_geo = data.get("scrollInfo") or {}
        post_y = int(post_geo.get("scrollY") or 0) if isinstance(post_geo, dict) else 0

        if pixels is not None and pixels > 0:
            base = f"Scrolled {direction or 'down'} {int(pixels)}px requested"
        elif percent is not None:
            base = f"Scrolled to {percent}% (absolute) requested"
        else:
            base = f"Scrolled {direction or 'down'} requested"

        if pre_y is not None:
            actual = post_y - pre_y
            base += f" → moved {actual:+d}px (Y {pre_y}→{post_y})"
            # Flag silent no-op scrolls. Without this caption the LLM
            # often retries the same scroll, looping forever on locked-
            # body SPAs. With Bug-1 fix in place this should be rare,
            # but the diagnostic is essential for the cases it misses.
            if (pixels is not None and pixels > 0) or (percent is None and direction):
                if abs(actual) < 5:
                    base += (
                        ". WARNING: page did not move. The real scroll "
                        "container may be a non-document element. Try "
                        "`browser_scroll_within` if a popup/modal is "
                        "open, or `browser_get_markdown` to inspect."
                    )
        # PROBE caption — anti-hallucination signal for pixel-scroll.
        # When `target_text` is set the TS server returns a `probe` dict
        # with direct DOM measurement of whether the target landed in
        # the viewport. `newly_visible` is the equivalent of
        # scroll_until's per-step trace, but for a single pixel step —
        # the brain reads it instead of guessing from the post-scroll
        # vision pass.
        probe = data.get("probe") if isinstance(data.get("probe"), dict) else None
        newly_visible_raw = data.get("newly_visible") or []
        newly_visible: list[str] = [str(x) for x in newly_visible_raw if x]

        probe_lines: list[str] = []
        if probe is not None:
            flags_bits: list[str] = [
                f"in_viewport={bool(probe.get('in_viewport'))}",
                f"fully={bool(probe.get('fully_in_viewport'))}",
                f"below_fold={bool(probe.get('below_fold'))}",
                f"above_fold={bool(probe.get('above_fold'))}",
            ]
            if probe.get("sticky_candidate"):
                flags_bits.append("sticky=true")
            if probe.get("anywhere_in_dom") and not probe.get("in_viewport"):
                flags_bits.append("anywhere_in_dom=true")
            probe_lines.append(
                f"[PROBE target={target_text!r} {' '.join(flags_bits)}]"
            )
            if probe.get("in_viewport") is False and probe.get("below_fold"):
                probe_lines.append(
                    f"  → TIP: target is below the fold. Either scroll "
                    f"more (pixels=600+, same target_text) or call "
                    f"browser_get_markdown to confirm. Do NOT emit a "
                    f"V_n claiming to be {target_text!r}."
                )
            elif (
                probe.get("anywhere_in_dom") is False
                and target_text
            ):
                probe_lines.append(
                    f"  → TIP: {target_text!r} not found anywhere in "
                    f"the DOM. Likely wrong label/synonym — try "
                    f"browser_get_markdown or a different keyword."
                )
            elif probe.get("sticky_candidate"):
                probe_lines.append(
                    f"  → TIP: {target_text!r} appears in viewport but "
                    f"its position is UNCHANGED from before the scroll "
                    f"— likely a sticky/pinned element, NOT the "
                    f"in-flow target. Verify with browser_get_markdown "
                    f"before clicking."
                )

        if newly_visible:
            rendered = ", +".join(newly_visible[:5])
            probe_lines.append(f"Newly visible: +{rendered}")

        if probe_lines:
            action = base + "\n" + "\n".join(probe_lines)
        else:
            action = base

        extra: dict | None = None
        if probe is not None:
            extra = {
                "last_probe_target": target_text,
                "last_probe_in_viewport": probe.get("in_viewport"),
                "last_probe_below_fold": probe.get("below_fold"),
            }
        _update_scroll_telemetry(
            self.s,
            data.get("scrollInfo"),
            direction or ("down" if pixels and pixels > 0 else None),
            extra=extra,
        )
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, action),
        )


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
        import json as _json_local
        if not text and not selector:
            return "Error: provide either 'text' or 'selector' parameter."

        timeout_s = timeout or 10
        label = f'text="{text}"' if text else f'selector="{selector}"'
        print(f"\n>> browser_wait_for({label}, timeout={timeout_s}s)")

        if text:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.body.innerText.includes({_json_local.dumps(text)})) {{
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
                    if (document.querySelector({_json_local.dumps(selector)})) {{
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
            from ..formatting import _fetch_elements
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
            "Safety cap on scroll steps per direction. Default 10, max 40. "
            "When auto_reverse fires, the second leg gets its own budget.",
            nullable=True,
        ),
        cadence=StringSchema(
            description=(
                "Step size preset. 'fine' (~30%% viewport, default when "
                "target_text is set) for precise scanning, 'medium' "
                "(~55%%) for general navigation, 'coarse' (~85%%, legacy "
                "behaviour) for fast traversal. `step_ratio` overrides."
            ),
            nullable=True,
        ),
        step_ratio=NumberSchema(
            description=(
                "Explicit fraction of viewport per step (0.1–1.0). "
                "Overrides `cadence` when set."
            ),
            nullable=True,
        ),
        auto_reverse=BooleanSchema(
            description=(
                "When the initial direction hits page_end / page_start "
                "without finding the target, automatically scan the "
                "OPPOSITE direction. Default true — eliminates the 'I "
                "scrolled down, didn't find it, gave up' failure mode. "
                "Returns reason='reversed_no_match' if both legs miss."
            ),
            default=True,
        ),
        container_selector=StringSchema(
            description=(
                "Optional CSS selector to scroll INSIDE this element "
                "(rather than the page). For dropdown popups / modal "
                "lists with internal overflow. Usually you want "
                "`browser_scroll_within` instead — it auto-detects the "
                "open popup."
            ),
            nullable=True,
        ),
        emit_trace=BooleanSchema(
            description=(
                "Include a per-step narrative of labels that entered "
                "the viewport. Default true. Set false on huge scans "
                "to keep prompt size down."
            ),
            default=True,
        ),
        required=["session_id"],
    )
)
class BrowserScrollUntilTool(Tool):
    name = "browser_scroll_until"
    description = (
        "[DEPRECATED — use browser_scroll_to_bbox once vision has "
        "labelled the target, or browser_scroll(pixels) for pixel-step] "
        "Closed-loop text-scan. Walks the page in small steps toward "
        "`target_text` / `target_role`. Fails on virtual lists, "
        "collapsed sections, cross-origin iframes, and multi-node text. "
        "Kept as an internal fallback; do NOT use as a primary scroll "
        "tool."
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
        cadence: str | None = None,
        auto_reverse: bool | None = None,
        container_selector: str | None = None,
        emit_trace: bool | None = None,
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
        if cadence in ("fine", "medium", "coarse"):
            payload["cadence"] = cadence
        # Auto-reverse defaults to true server-side; only forward an
        # explicit override.
        if auto_reverse is not None:
            payload["autoReverse"] = bool(auto_reverse)
        if container_selector and container_selector.strip():
            payload["containerSelector"] = container_selector.strip()
        if emit_trace is not None:
            payload["emitTrace"] = bool(emit_trace)

        target_disp = target_text or f"role={target_role}"
        print(
            f"\n>> browser_scroll_until({target_disp!r}, dir={payload['direction']}"
            f"{', cadence=' + cadence if cadence else ''}"
            f"{', auto_reverse=' + str(auto_reverse) if auto_reverse is not None else ''})"
        )

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll-until",
                json=payload,
                # Two-phase scan (auto-reverse) with up to 40 iterations
                # per leg at ~300ms each can run ~25s in the worst case.
                # Bump from 30s → 60s to cover doubled budget cleanly.
                timeout=60.0,
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
        reversed_flag = bool(outcome.get("reversed") or False)
        start_y = int(outcome.get("startScrollY") or 0)
        final_y = int(outcome.get("finalScrollY") or 0)

        # Update scroll telemetry so the next vision pass sees a fresh
        # [SCROLL_STATE …] line including reached_bottom/reached_top hints
        # that came from this closed-loop call.
        _update_scroll_telemetry(
            self.s,
            data.get("scrollInfo"),
            payload["direction"],
            extra={
                "last_scroll_reason": reason,
                "reached_bottom": reason == "page_end" or (
                    reason == "reversed_no_match" and payload["direction"] == "down"
                ),
                "reached_top": reason == "page_start" or (
                    reason == "reversed_no_match" and payload["direction"] == "up"
                ),
                "reversed": reversed_flag,
            },
        )

        # Mirror the BrowserDragSliderUntilTool record convention so
        # step_history shows a clear summary line for downstream
        # loop-detection and task-graph signal evaluation.
        self.s.record_step(
            "browser_scroll_until",
            f"{target_disp!r} → {reason} in {iters} iters ({scrolled}px)"
            + (" [reversed]" if reversed_flag else ""),
            data.get("url", ""),
        )

        lines: list[str] = []
        # Detect "vision will look identical" cases up-front so the
        # caption can explicitly tell the brain to trust the trace
        # rather than the post-call vision summary.
        ended_near_start = abs(final_y - start_y) < 50
        no_movement = scrolled == 0 and iters > 0
        if outcome.get("found"):
            matched = outcome.get("matchedText") or ""
            sel = outcome.get("matchedSelector") or ""
            if iters == 0:
                # Found in the initial viewport — no scroll happened.
                # Make this LOUD: the brain may have thought it was
                # navigating to find the target, but actually the target
                # was always visible. Vision will be a cache HIT (same
                # dom_hash) — that's not a tool failure.
                lines.append(
                    f"ALREADY VISIBLE: {target_disp!r} was on screen at "
                    f"scrollY={start_y} before any scroll. NO scroll "
                    f"happened (iters=0, scrolled=0px). matched="
                    f"{matched[:80]!r} selector={sel}. Vision cache "
                    f"may HIT — that's correct, not a bug."
                )
            else:
                lines.append(
                    f"FOUND {target_disp!r} after {iters} iter(s), "
                    f"scrolled {scrolled}px ({start_y}→{final_y}"
                    + (", reversed" if reversed_flag else "")
                    + f"). matched={matched[:80]!r} selector={sel}"
                )
        else:
            tag = reason
            lines.append(
                f"[scroll_until_failed:{tag}] target {target_disp!r} not "
                f"found after {iters} iter(s) ({scrolled}px, "
                f"{start_y}→{final_y}). reason={reason}."
            )
            if no_movement:
                # Scroll never actually moved — the page may use an
                # inner scroll container the page-level fallback didn't
                # catch, or the page genuinely isn't scrollable.
                lines.append(
                    "  WARNING: scrolledPx=0 across all iterations — "
                    "the page didn't move. The scroll surface may be a "
                    "non-document container we couldn't detect. Try: "
                    "(a) `browser_scroll_within(target_text=...)` if a "
                    "popup/modal is open; (b) `browser_get_markdown` to "
                    "verify what's actually on the page; (c) the "
                    "target may already be present off-viewport — "
                    "check the [N] elements listing below."
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
            elif reason == "reversed_no_match":
                lines.append(
                    f"  Walked from Y={start_y} to the page boundary AND "
                    f"back the other way without finding the target. The "
                    f"label is not on this page. Stop scrolling — try a "
                    f"synonym, `browser_get_markdown` to inspect the "
                    f"actual text, or accept that the control isn't here."
                )
                if ended_near_start:
                    # Auto-reverse landed back near the start — the
                    # post-call vision pass will produce the same
                    # dom_hash and HIT cache. Without this hint the
                    # brain assumes the tool did nothing.
                    lines.append(
                        "  Note: scroll position returned to ~start "
                        f"(Δ={final_y - start_y}px). Vision will be a "
                        "cache HIT showing the SAME page as before — "
                        "trust the trace above, not the vision repeat."
                    )
                if (
                    target_text
                    and any(
                        kw in target_text.lower()
                        for kw in ("intel", "amd", "ryzen", "core i", "gen ", "generation")
                    )
                ):
                    # Common smell: the brain is looking for a CPU/spec
                    # option that lives inside a CLOSED dropdown.
                    # scroll_until on the page can never find it.
                    lines.append(
                        "  HINT: this looks like a CPU/spec option that "
                        "typically lives inside a closed dropdown. "
                        "scroll_until on the PAGE can't find it. Open "
                        "the relevant dropdown first (e.g. "
                        "`browser_select_option(label='Processor', "
                        "value=...)`), or use `browser_form_plan` for "
                        "cascading filter forms."
                    )
            elif reason == "max_iterations":
                lines.append(
                    "  Hit iteration cap. If you believe the target exists "
                    "further on, raise max_iterations (cap is 40) or "
                    "use a more specific target_text."
                )
            elif reason == "no_scroll_surface":
                # v6 G1: scrollByDelta reported zero movement on 2
                # consecutive iterations even after the
                # window/scrollingElement/largest-container cascade.
                # The page's actual scroll surface is either locked
                # OR sits inside a container the heuristic missed.
                lines.append(
                    "  No scroll surface responded. The page may use a "
                    "custom scroll container we couldn't auto-detect. "
                    "Call `browser_get_markdown(include_anchors=true)` "
                    "— the trailing [SCROLL_CONTAINERS …] block lists "
                    "scrollable surfaces with their selectors. Pass "
                    "the right one as `container_selector=` to retry. "
                    "If no container looks like it would hold the "
                    "target, the target probably isn't on this page."
                )
            elif reason == "target_in_no_scrollable_container":
                # New diagnostic: the text exists in the DOM but isn't
                # inside any scrollable ancestor — typically a closed
                # <details>, an aria-hidden=true subtree, or rendered
                # via display:none. Scrolling can't reveal it.
                lines.append(
                    f"  '{target_text}' is in the DOM but not inside any "
                    "scrollable container. Likely causes: it's inside a "
                    "collapsed <details>/accordion that needs to be "
                    "expanded first; it's in a hidden subtree; or it's "
                    "rendered as part of a static header/banner that "
                    "doesn't scroll. STOP scrolling. Re-screenshot and "
                    "check whether you need to click a "
                    "section-expand/show-more control before targeting "
                    "this text."
                )
            elif reason == "no_forward_progress":
                # Forward leg moved <100px before bailing. Page is
                # effectively non-scrollable for our purposes (locked
                # body, custom scroller we couldn't drive, or
                # smooth-scroll race that even the instant override
                # didn't beat). Reversing back would erase what little
                # motion happened — we skipped it.
                lines.append(
                    f"  Scroll never moved more than {abs(final_y - start_y)}px. "
                    "Either the page has no responsive scroll surface, "
                    "or its scroll-behavior is hijacked. Try (a) "
                    "`browser_scroll(direction='down', percent=50)` for "
                    "a pixel-locked scroll, (b) "
                    "`browser_scroll_within(target_text=...)` if a popup "
                    "/modal is open, or (c) accept the page can't scroll "
                    "to this target."
                )

        # Per-step narrative — what entered the viewport at each step.
        # This is the load-bearing anti-hallucination signal: the brain
        # sees a complete log of labels we passed, so it can't claim
        # 'the target was off-screen but I missed it' if the trace
        # didn't contain the target.
        trace = outcome.get("trace") or []
        if trace:
            trace_bits: list[str] = []
            char_budget = 600
            for step in trace:
                try:
                    i = int(step.get("i") or 0)
                    passed = step.get("passed") or []
                    if not isinstance(passed, list):
                        continue
                    short = ",".join(str(p) for p in passed[:5])
                    chunk = f"[{i}]+{short}"
                    if sum(len(b) for b in trace_bits) + len(chunk) + 2 > char_budget:
                        trace_bits.append(f"…(+{len(trace) - len(trace_bits)} more)")
                        break
                    trace_bits.append(chunk)
                except Exception:
                    continue
            if trace_bits:
                lines.append("  trace: " + " ".join(trace_bits))

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


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        target_text=StringSchema(
            description=(
                "Text the option/item should match. When set, walks the "
                "container in fine steps until the option enters view "
                "(or the container can't scroll further). Substring or "
                "regex."
            ),
            nullable=True,
        ),
        container_selector=StringSchema(
            description=(
                "CSS selector for the scroll container. Optional — if "
                "omitted, auto-detects the most recently opened popup "
                "(role=listbox/menu/dialog, headlessui-state=open) and "
                "walks up its ancestor chain to find the smallest "
                "scrollable host."
            ),
            nullable=True,
        ),
        direction=StringSchema(
            "'down' (default) or 'up'.",
            nullable=True,
        ),
        amount=StringSchema(
            description=(
                "Step amount when target_text is NOT set: 'page' "
                "(~85%% of container, default), 'half' (~50%%), or pass "
                "a numeric pixel count. Ignored when target_text is set."
            ),
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            description="Max scroll steps (default 12, max 40). Used with target_text.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScrollWithinTool(Tool):
    name = "browser_scroll_within"
    description = (
        "Scroll INSIDE an open popup / listbox / menu / modal — NOT the "
        "page. Use this when a dropdown is open and the option you want "
        "is BELOW the visible portion of the menu (so it has no V_n "
        "yet). Without `container_selector`, the server auto-detects "
        "the most recently opened popup (`[role=\"listbox|menu|"
        "dialog\"]`, Headless UI, Radix). Pass `container_selector` "
        "only as an override when auto-detect picks the wrong popup. "
        "With `target_text`, walks the container in fine steps until "
        "that option enters view; without it, scrolls by `amount`. "
        "After scrolling, call `browser_screenshot` so vision labels "
        "the newly-revealed options, then `browser_click_at(V_n)`."
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
        container_selector: str | None = None,
        direction: str | None = None,
        amount: str | int | None = None,
        max_iterations: int | None = None,
        **kw: Any,
    ) -> Any:
        gate = await _feedback_gate("browser_scroll_within")
        if gate:
            return gate

        payload: dict[str, Any] = {
            "direction": direction or "down",
        }
        # Pass containerSelector only when set — empty/missing triggers
        # the TS-side popup auto-detect in page.ts:scrollWithin.
        if container_selector and container_selector.strip():
            payload["containerSelector"] = container_selector.strip()
        if target_text and target_text.strip():
            payload["targetText"] = target_text.strip()
        if amount is not None:
            # Accept "page" / "half" or an integer pixel count.
            if isinstance(amount, str) and amount in ("page", "half"):
                payload["amount"] = amount
            else:
                try:
                    payload["amount"] = int(amount)
                except (TypeError, ValueError):
                    pass
        if max_iterations is not None:
            payload["maxIterations"] = int(max_iterations)

        target_disp = target_text or f"container={container_selector or '<auto>'}"
        print(
            f"\n>> browser_scroll_within({target_disp!r}, dir={payload['direction']})"
        )

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll-within",
                json=payload,
                timeout=45.0,
            )
        except Exception as exc:
            return f"[scroll_within_failed] request error: {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[scroll_within_failed] HTTP {r.status_code}: {err}"

        data = r.json()
        outcome = data.get("outcome") or {}
        reason = str(outcome.get("reason") or "unknown")
        iters = int(outcome.get("iterations") or 0)
        scrolled = int(outcome.get("scrolledPx") or 0)
        resolved = str(outcome.get("resolvedContainer") or outcome.get("containerSelector") or "")
        reversed_flag = bool(outcome.get("reversed") or False)

        # Popup-scroll guard. When the popup actually moved, DOM
        # indices for items inside it are now stale. Flag the guard so
        # the next DOM-index click is refused with a redirect to the
        # bbox path. Cleared by the next browser_screenshot.
        if scrolled > 0 or reason in ("matched", "page_end"):
            try:
                self.s.flag_popup_scroll(reason="scroll_within")
            except Exception:
                pass

        # Track in step history for downstream loop-detection / planning.
        self.s.record_step(
            "browser_scroll_within",
            f"{target_disp!r} → {reason} in {iters} iter(s) ({scrolled}px) "
            f"in {resolved or '<no_container>'}"
            + (" [reversed]" if reversed_flag else ""),
            data.get("url", ""),
        )

        lines: list[str] = []
        if reason == "no_container":
            lines.append(
                "[scroll_within_failed:no_container] No scrollable popup "
                "found. Either no dropdown/menu/modal is currently open, "
                "or the open popup doesn't have internal overflow. "
                "Open the dropdown first (browser_click_at on its "
                "trigger), or pass an explicit container_selector."
            )
        elif outcome.get("found"):
            matched = outcome.get("matchedText") or ""
            sel = outcome.get("matchedSelector") or ""
            lines.append(
                f"FOUND {target_disp!r} inside {resolved} after {iters} "
                f"iter(s), scrolled {scrolled}px"
                + (" (reversed)" if reversed_flag else "")
                + f". matched={matched[:80]!r} selector={sel}. "
                "Take a browser_screenshot so vision emits a V_n for the "
                "target, then `browser_click_at(vision_index=V_n)`. For "
                "<select>-style dropdowns, `browser_select_option("
                "label=..., value=...)` handles the click for you."
            )
        else:
            lines.append(
                f"[scroll_within:{reason}] inside {resolved}, after "
                f"{iters} iter(s), scrolled {scrolled}px"
                + (" (reversed)" if reversed_flag else "")
                + f". reason={reason}."
            )
            if reason == "reversed_no_match":
                lines.append(
                    "  Walked the popup top-to-bottom-and-back without "
                    "finding the target. The option text is not in this "
                    "menu. Try a synonym, or close+re-open the dropdown."
                )
            elif reason in ("page_end", "page_start"):
                lines.append(
                    "  Reached the boundary of the popup. Target wasn't "
                    "in this menu — try the opposite direction or check "
                    "if you've opened the right dropdown."
                )

        # Per-step trace narrative (same shape as scroll_until).
        trace = outcome.get("trace") or []
        if trace:
            trace_bits: list[str] = []
            char_budget = 600
            for step in trace:
                try:
                    i = int(step.get("i") or 0)
                    passed = step.get("passed") or []
                    if not isinstance(passed, list):
                        continue
                    short = ",".join(str(p) for p in passed[:5])
                    chunk = f"[{i}]+{short}"
                    if sum(len(b) for b in trace_bits) + len(chunk) + 2 > char_budget:
                        trace_bits.append(f"…(+{len(trace) - len(trace_bits)} more)")
                        break
                    trace_bits.append(chunk)
                except Exception:
                    continue
            if trace_bits:
                lines.append("  trace: " + " ".join(trace_bits))

        # No telemetry update — this is in-container scroll, not page
        # scroll, so reached_bottom / reached_top would be misleading.
        if data.get("elements"):
            lines.append(str(data["elements"]))

        self.s.advance_observation_token("scroll_within")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task, "\n".join(lines),
            state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description="1-based V_n of the bbox to bring into view.",
        ),
        required=["session_id", "vision_index"],
    )
)
class BrowserScrollToBboxTool(Tool):
    name = "browser_scroll_to_bbox"
    description = (
        "Scroll a vision-labelled element into view. Picks the right "
        "surface automatically: if the bbox is inside an open dropdown "
        "popup, scrolls the popup's internal container; otherwise "
        "scrolls the page. Use this when a V_n you want to click is "
        "off-screen.\n\n"
        "Note: `browser_click_at(vision_index=V_n)` already auto-scrolls "
        "before clicking, so you usually do NOT need to call this "
        "before a click. Reach for it when you want to see a labelled "
        "section before deciding what to click."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        vision_index: int,
        **kw: Any,
    ) -> Any:
        gate = await _feedback_gate("browser_scroll_to_bbox")
        if gate:
            return gate

        if not isinstance(vision_index, int):
            try:
                vision_index = int(str(vision_index).strip())
            except (TypeError, ValueError):
                return (
                    "[scroll_to_bbox_failed:bad_vision_index] "
                    "vision_index must be an integer (the V_n)."
                )

        resp = self.s.vision_for_target_resolution()
        if resp is None:
            return (
                "[scroll_to_bbox_failed:no_vision] No recent vision "
                "response to resolve V_n against. Call "
                "browser_screenshot first."
            )
        bbox = resp.get_bbox(int(vision_index))
        if bbox is None:
            return (
                f"[scroll_to_bbox_failed:bad_vision_index] V{vision_index} "
                f"is out of range (only {len(resp.bboxes)} bboxes in "
                f"the last vision response)."
            )

        iw, ih = resp.image_width, resp.image_height
        if iw <= 0 or ih <= 0:
            return (
                "[scroll_to_bbox_failed:no_image_dims] Vision response "
                "lacks source image dimensions; cannot denormalize bbox."
            )
        dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
        x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
        payload = {"bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}

        print(f"\n>> browser_scroll_to_bbox(V{vision_index})")
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll-to-bbox",
                json=payload,
                timeout=10.0,
            )
        except Exception as exc:
            return f"[scroll_to_bbox_failed] request error: {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[scroll_to_bbox_failed] HTTP {r.status_code}: {err}"

        data = r.json()
        kind = data.get("container_kind", "?")
        delta = data.get("delta_y", 0)
        scrolled = bool(data.get("scrolled"))
        bbox_label = (getattr(bbox, "label", "") or "")[:60]
        if not scrolled and kind == "already_visible":
            caption = (
                f"V{vision_index} {bbox_label!r} is already fully on "
                f"screen. No scroll needed."
            )
        else:
            caption = (
                f"Scrolled V{vision_index} {bbox_label!r} into view via "
                f"{kind} container (delta_y={delta}px)."
            )
        self.s.record_step(
            "browser_scroll_to_bbox",
            f'V{vision_index}|"{clean_label(bbox_label)}"',
            caption,
        )
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task, caption, state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
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
