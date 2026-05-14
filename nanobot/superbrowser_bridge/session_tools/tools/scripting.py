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
        "(innerText, aria-state, getBoundingClientRect, element counts). "
        "If click_at(V_n) / click([N]) isn't landing, re-screenshot to "
        "refresh vision and retry on the new V_n."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0  # eval resets click loop tracking
        print(f"\n>> browser_eval({script[:60]}...)")
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
        print(f"\n>> browser_run_script(mutates={bool(mutates)}, len={len(script or '')})")
        # Bot-detection warning on known-hard domains and heavy
        # page types. Mutating JS click/type on Cloudflare/Akamai-
        # protected listings is routinely rejected because the events
        # are isTrusted=false. Read-only scripts (data extraction) are
        # never blocked.
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
                print(f"  [run_script rejected: heavy_page_use_vision ({pt_label})]")
                return (
                    "[run_script_blocked:bot_detection_risk] "
                    f"This is a {pt_label} page{domain_note}. Mutating "
                    "scripts on bot-detected sites (Cloudflare, Akamai, "
                    "etc.) are routinely rejected because synthetic JS "
                    "clicks are isTrusted=false. Use the atomic cursor "
                    "tools — they dispatch isTrusted=true CDP events "
                    "and adapt to live state:\n"
                    "  • browser_click_at(vision_index=V_n)\n"
                    "  • browser_type_at(vision_index=V_n, value=…)\n"
                    "  • browser_select_option(vision_index=V_n, label=…)\n"
                    "  • browser_scroll_until(target_text=…)\n"
                    "  • browser_get_markdown(include_anchors=true) — "
                    "inspect page structure\n"
                    "For pure DATA EXTRACTION (no clicks/typing) you "
                    "may still call browser_run_script with "
                    "mutates=false — read-only scripts always pass."
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
                    f"browser_type_at which use humanized "
                    f"isTrusted=true events."
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
