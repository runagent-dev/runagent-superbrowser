"""Session-lifecycle tool classes — open / close / escalate / rewind."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403

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
            )
        return caption


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


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
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

        # One-shot post-rewind observation gate. The trace pattern was
        # rewind → browser_navigate(hallucinated URL) within one turn,
        # bypassing the click crosscheck on the rewound page entirely.
        # Setting this flag tells worker_hook to inject
        # [REWIND_NOT_OBSERVED] if the brain's next call isn't a
        # screenshot / get_markdown / brief_mark. The hook clears the
        # flag itself after one cycle.
        self.s.rewind_just_fired = True

        # Fresh prefetch on the rewound page so the next mutation's gate
        # unblocks quickly rather than hitting a cold cache.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        post_rewind_caption = (
            f"Rewound to checkpoint: {target[:80]}\n\n"
            f"[POST_REWIND] Vision cache + DOM fingerprints invalidated. "
            f"Next required tool: browser_screenshot (or "
            f"browser_get_markdown / browser_brief_mark if you have "
            f"explicit evidence to log). Do NOT browser_click / "
            f"browser_type / browser_navigate before re-observing — the "
            f"V_n indices and DOM [N] indices from BEFORE the rewind no "
            f"longer point at anything. The brief focus is unchanged; "
            f"if [FOCUS_EXHAUSTED] fired before the rewind, the focus "
            f"is still exhausted — consider browser_brief_mark to "
            f"advance past it instead of re-attempting the same "
            f"approach on the rewound page."
        )
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(
                data if isinstance(data, dict) else {},
                post_rewind_caption,
            ),
            state=self.s,
        )


