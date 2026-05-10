"""In-page JavaScript evaluation + Puppeteer scripting.

`BrowserEvalTool` is read-only by default; `BrowserRunScriptTool` allows
mutating ops (with a cursor-first lockout) and returns post-execution
elements automatically.
"""

from __future__ import annotations

import json
import os
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..formatting import _fetch_elements
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    name = "browser_eval"
    description = (
        "Execute JavaScript in the browser page — READ-ONLY inspection "
        "only (innerText, aria-state, getBoundingClientRect, element "
        "counts). Do NOT use this to work around a click failure by "
        "stamping a custom id onto an element and then clicking it via "
        "browser_click_selector — that anti-pattern is bot-detectable on "
        "hardened sites and a sign you're working around the issue "
        "instead of fixing it. If click_at(V_n) / click([N]) isn't "
        "landing, re-screenshot to refresh vision and retry, or use "
        "browser_click_selector with a CSS attribute the page already "
        "exposes (id, data-*, aria-label)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0  # eval resets click loop tracking
        print(f"\n>> browser_eval({script[:60]}...)")
        # Record any `.id = 'X'` / setAttribute('id', 'X') stamps in this
        # script so a follow-up browser_click_selector('#X') trips the
        # stamp-id anti-pattern detector. Cheap regex scan, no false-
        # positive on read-only scripts (those just won't contain the
        # patterns).
        self.s.record_stamped_ids_from_script(script)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": script},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        result = data.get("result")
        result_str = json.dumps(result, indent=2, ensure_ascii=False)[:5000] if isinstance(result, (dict, list)) else str(result)[:5000]
        self.s.log_activity(f"eval({script[:40]}...)", result_str[:60])
        self.s.record_step("browser_eval", script[:60], result_str[:100])
        return result_str


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema(
            "Puppeteer script body. Variables: page (Puppeteer Page), context, helpers (sleep, screenshot, log)."
        ),
        context=ObjectSchema(description="Optional context data", nullable=True),
        timeout=IntegerSchema(description="Script timeout in ms (default: 60000)", nullable=True),
        mutates=BooleanSchema(
            description=(
                "Set true when the script mutates the page (click, "
                "type, input.value=, dispatchEvent). Default false — "
                "the sandbox rejects those operations and returns a "
                "[blocked_op:…] error. Keep false for read-only "
                "inspection (readyState, innerText, aria-labels). Only "
                "flip true when no cursor tool can express the action; "
                "isTrusted=false JS clicks are bot-detected by WAFs. "
                "Do NOT use this to .click() an element you could reach "
                "via click_at(V_n) or click_selector. In particular, the "
                "stamp-id anti-pattern (assign a custom .id via eval, "
                "then click via selector) is bot-detectable and emits a "
                "runtime [anti_pattern_detected] advisory."
            ),
            default=False,
        ),
        required=["session_id", "script"],
    )
)
class BrowserRunScriptTool(Tool):
    name = "browser_run_script"
    description = (
        "Execute a Puppeteer script with full page API access. "
        "READ-ONLY by default — pass mutates=true to allow click/type/"
        "dispatchEvent/value-setter operations (rare)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, session_id: str, script: str,
        context: dict | None = None,
        timeout: int | None = None,
        mutates: bool = False,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_run_script({script[:80]}...)")
        # Scroll-first gate. A page with scroll capacity remaining where
        # the brain hasn't called any scroll-class tool in the recent
        # window is the classic "hallucinated V_n on a below-fold target"
        # cascade — the brain is escalating to JS before even trying to
        # bring the target into view. Refuse and steer at scroll_until.
        # Runs BEFORE the cursor-first lockout so the more specific
        # message wins when both apply.
        if (
            bool(mutates)
            and os.environ.get("RUN_SCRIPT_REQUIRE_SCROLL_FIRST", "1")
                not in ("0", "false", "no")
        ):
            tel = getattr(self.s, "scroll_telemetry", None) or {}
            scroll_h = int(tel.get("scrollHeight", 0) or 0)
            vp_h = int(tel.get("viewportHeight", 0) or 0)
            has_capacity = scroll_h > vp_h + 200
            reached_bottom = bool(tel.get("reached_bottom"))
            reached_top = bool(tel.get("reached_top"))
            recent_tools = [
                s.get("tool", "")
                for s in (self.s.step_history or [])[-5:]
            ]
            tried_scroll = any(
                t in {
                    "browser_scroll",
                    "browser_scroll_until",
                    "browser_scroll_within",
                }
                for t in recent_tools
            )
            if (
                has_capacity
                and not reached_bottom
                and not reached_top
                and not tried_scroll
            ):
                pos = int(tel.get("scrollY", 0) or 0)
                return (
                    "[run_script_blocked:scroll_first_required] Page has "
                    f"scroll capacity remaining (Y={pos}/{scroll_h}, "
                    f"vp={vp_h}) and you haven't called any scroll-class "
                    "tool in the last 5 turns. If your target isn't "
                    "visible in current vision, call:\n"
                    f"  browser_scroll_until(session_id='{session_id}', "
                    "target_text='<the label>')\n"
                    "It walks the page in fine steps, narrates labels "
                    "passed, and tells you whether the label exists on "
                    "this page. Only after that scan returns "
                    "`reversed_no_match` should you reach for "
                    "browser_run_script."
                )
        # Phase 3.1: cursor-first lockout. Read-only scripts always
        # allowed (data extraction). Mutating scripts require evidence
        # that the brain has tried — and failed — at least 2 distinct
        # cursor strategies in this session. This forces the cursor →
        # selector → script ladder rather than letting the brain
        # short-cut to JS clicks (isTrusted=false; tripped by every
        # bot-detection edge).
        if (
            bool(mutates)
            and os.environ.get("CURSOR_FIRST_LOCKOUT", "1") not in ("0", "false", "no")
        ):
            try:
                min_strategies = int(
                    os.environ.get("CURSOR_LOCKOUT_MIN_STRATEGIES") or "2"
                )
            except ValueError:
                min_strategies = 2
            distinct = len(self.s.cursor_failure_strategies)
            if distinct < min_strategies:
                ledger = self.s.cursor_lockout_summary()
                tried_str = (
                    ", ".join(sorted(self.s.cursor_failure_strategies))
                    or "(none)"
                )
                return (
                    "[run_script_blocked:cursor_path_untried] You haven't "
                    f"exhausted cursor strategies for this session "
                    f"({distinct}/{min_strategies} distinct strategies "
                    f"failed; tried={tried_str}).\n"
                    "Try in order BEFORE running mutating JS:\n"
                    "  1. browser_click_at(vision_index=V_n) on the "
                    "target's bbox.\n"
                    "  2. browser_click_selector(<stable-css>) if the "
                    "target has a hook.\n"
                    "  3. browser_type_at / browser_scroll_until.\n"
                    "  4. browser_set_slider(<selector>, value) for "
                    "slider widgets — pierces shadow DOM (Chase mds-slider, "
                    "Lit/React custom-element wrappers) and sets the "
                    "inner range input directly.\n"
                    "  5. browser_list_slider_handles → "
                    "browser_drag_slider_until(handle_bbox=…) for custom "
                    "sliders where vision can't identify the track. "
                    "Closed-loop drag with label readback; works on "
                    "calculator widgets in cross-origin iframes.\n"
                    "Only when 2+ DIFFERENT strategies have failed with "
                    "concrete error captions can mutates=true scripts "
                    "run. JS clicks are isTrusted=false and routinely "
                    "rejected by Cloudflare / Akamai."
                    + (f"\nRecent cursor failures:\n{ledger}" if ledger else "")
                )
        # v4 D1 — Gate 3: heavy-page guard. Mutating scripts on
        # search results / product listings / maps / checkout forms /
        # known-hard domains fail ~80% because:
        #   (a) selectors are ambiguous (50 'Add to cart' buttons),
        #   (b) synthetic JS clicks are isTrusted=false → bot-detected,
        #   (c) async re-renders race against the script's verify step.
        # Refuse and redirect the brain to atomic vision tools, which
        # dispatch isTrusted=true CDP events and adapt to live state.
        # Read-only scripts (mutates=False) are ALWAYS allowed — data
        # extraction never fails for these reasons.
        if (
            bool(mutates)
            and os.environ.get("RUN_SCRIPT_HEAVY_PAGE_GUARD", "1")
                not in ("0", "false", "no")
        ):
            last_resp = getattr(self.s, "_last_vision_response", None)
            page_type = (
                getattr(last_resp, "page_type", "") or ""
            ).strip()
            current_url = (self.s.current_url or "").lower()
            try:
                from ..effects import _HARD_DOMAINS
            except Exception:
                _HARD_DOMAINS = ()
            is_hard_domain = any(d in current_url for d in _HARD_DOMAINS)
            heavy_page_types = {
                "search_results", "product_listing",
                "map_or_booking", "checkout_form",
            }
            if page_type in heavy_page_types or is_hard_domain:
                pt_label = page_type or "complex/high-value"
                domain_note = (
                    f" (domain={current_url.split('/')[2] if '://' in current_url else current_url[:40]} flagged as hard)"
                    if is_hard_domain and page_type not in heavy_page_types
                    else ""
                )
                return (
                    "[run_script_blocked:heavy_page_use_vision] "
                    f"This is a {pt_label} page{domain_note}. Mutating "
                    "scripts on such pages fail ~80% of the time:\n"
                    "  (a) selectors are ambiguous (multiple 'Add to "
                    "cart' / 'Apply' / 'Submit' on a single page),\n"
                    "  (b) synthetic JS clicks (el.click(), "
                    "dispatchEvent) are isTrusted=false and routinely "
                    "rejected by Cloudflare / Akamai bot-detection,\n"
                    "  (c) async re-renders (React/Vue) race against "
                    "your script's verify step.\n\n"
                    "Use ATOMIC vision tools instead — each call "
                    "dispatches isTrusted=true CDP events and adapts "
                    "to the LIVE page state on every call:\n"
                    "  • browser_click_at(vision_index=V_n)        — "
                    "one click per call\n"
                    "  • browser_type_at(vision_index=V_n, value=…) — "
                    "one input per call\n"
                    "  • browser_select_option(vision_index=V_n, "
                    "label=…) — for dropdowns\n"
                    "  • browser_scroll_until(target_text=…) — to "
                    "find below-fold controls\n"
                    "  • browser_get_markdown(include_anchors=true) "
                    "— inspect page structure\n\n"
                    "Group results into a single done() report at the "
                    "end. For pure DATA EXTRACTION you may still call "
                    "browser_run_script with mutates=false — read-only "
                    "scripts always pass this gate."
                )
        self.s.consecutive_click_calls = 0  # script execution resets click loop tracking
        payload: dict[str, Any] = {"code": script, "mutates": bool(mutates)}
        if context:
            payload["context"] = context
        if timeout:
            payload["timeout"] = timeout

        client_timeout = max(120.0, (timeout or 60000) / 1000 + 10)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/script",
            json=payload,
            timeout=client_timeout,
        )
        r.raise_for_status()
        data = r.json()

        self.s.actions_since_screenshot += 1
        # Anti-pattern: record any id stamps performed by this script
        # for the stamp-id detector. Only when the script actually
        # succeeded — a blocked / failed script didn't actually
        # mutate the DOM. mutates=False scripts can't change ids
        # (sandbox blocks DOM writes) so skip them entirely.
        if bool(mutates) and bool(data.get("success")):
            self.s.record_stamped_ids_from_script(script)

        # v4 D3 — record outcome in the run_script ledger so worker_hook
        # can detect 3-of-5 failures and inject the redirect-to-vision
        # hint regardless of page type. Caps at 5 entries (rotates).
        try:
            outcomes = self.s.recent_run_script_outcomes
            outcomes.append(bool(data.get("success")))
            if len(outcomes) > 5:
                del outcomes[0:len(outcomes) - 5]
        except Exception:
            pass
        if not data.get("success"):
            error = data.get("error", "Unknown error")
            self.s.log_activity("run_script(FAILED)", error[:100])
            self.s.record_step("browser_run_script", script[:60], f"FAILED: {error[:100]}")
            # L1 sandbox rejected a mutation. Rewrite the reply so the
            # brain gets a concrete cursor-tool recommendation instead
            # of a raw JS error string (which it has been misreading
            # as a server 403).
            blocked_op = data.get("blocked_op")
            if blocked_op or error.startswith("[blocked_op:"):
                return (
                    f"[script_mutation_blocked] {error} The script "
                    f"tried to mutate the page from a mutates=false "
                    f"run. Either (a) re-call with mutates=true IF "
                    f"you genuinely need JS orchestration — rare, "
                    f"and many sites reject isTrusted=false clicks "
                    f"anyway — or (b) switch to "
                    f"browser_click_at(vision_index=V_n) / "
                    f"browser_type_at / browser_click_selector which "
                    f"use humanized isTrusted=true events."
                )
            # Fetch current elements so agent can see what's on the page and fix the script
            elements = await _fetch_elements(session_id, self.s)
            tip = "\n[TIP: Fix the script and retry in this SAME session. Do NOT navigate back to the start.]"
            if elements:
                tip += f"\n\nCurrent interactive elements:\n{elements}"
            return f"Script error: {error}{tip}"

        parts = []
        result = data.get("result")
        if result is not None:
            if isinstance(result, (dict, list)):
                parts.append(f"Result: {json.dumps(result, indent=2, ensure_ascii=False)[:5000]}")
            else:
                parts.append(f"Result: {str(result)[:5000]}")

        logs = data.get("logs", [])
        if logs:
            parts.append("Logs:\n" + "\n".join(logs[:20]))

        duration = data.get("duration", 0)
        parts.append(f"Duration: {duration}ms")
        self.s.log_activity(f"run_script(ok, {duration}ms)", str(result)[:60] if result else "void")
        self.s.record_step("browser_run_script", script[:60], str(result)[:100] if result else "void")
        self.s.record_checkpoint(self.s.current_url, "", f"run_script(ok, {duration}ms)")

        # Auto-include updated elements so agent sees current page state
        elements = await _fetch_elements(session_id, self.s)
        if elements:
            parts.append(f"\nInteractive elements:\n{elements}")

        return "\n".join(parts)
