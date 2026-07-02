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

from ..effects import WorkerMustExitError
from ..formatting import _fetch_elements
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


def _consec_script_abort_threshold() -> int:
    """Consecutive browser_eval / browser_run_script calls (no intervening
    cursor success) after which the worker is HARD-aborted.

    ``_maybe_script_usage_warning`` only *warns* at 2+; a weak model can ignore
    the advisory and loop on read-only scripts until the wall-clock timeout,
    draining the entire token/iteration budget for a guaranteed failure (seen in
    the model-split eval: 86 iterations / ~2.9M tokens, then a 900s timeout).
    This bounds that runaway so a stuck run fails fast and cheap. Deliberately
    high so normal inspect-a-few-things use never trips it. 0 disables.
    Env: ``SUPERBROWSER_MAX_CONSEC_SCRIPTS`` (default 16).
    """
    try:
        return max(0, int(os.environ.get("SUPERBROWSER_MAX_CONSEC_SCRIPTS", "16")))
    except (TypeError, ValueError):
        return 16


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
        # Wire the inverse counter so _maybe_script_usage_warning can
        # surface a `[script_warning]` advisory at 2+ consecutive
        # eval/run_script calls. Reset on cursor success in effects.py.
        try:
            self.s.consecutive_script_calls = int(
                getattr(self.s, "consecutive_script_calls", 0) or 0
            ) + 1
        except Exception:
            self.s.consecutive_script_calls = 1
        # Safety valve: hard-abort a pathological read-only-script loop before it
        # drains the whole budget against the wall-clock timeout. Bubbles via
        # WorkerMustExitError → delegation.py returns a clean failure (and
        # re-delegation is itself capped). Env-tunable; 0 disables.
        _consec_abort = _consec_script_abort_threshold()
        if _consec_abort and int(self.s.consecutive_script_calls or 0) >= _consec_abort:
            raise WorkerMustExitError(
                f"{self.s.consecutive_script_calls} consecutive browser_eval / "
                f"browser_run_script calls with no cursor progress — the worker "
                f"is looping on scripts instead of acting on the page. Aborting "
                f"to stop an unbounded token drain against the wall-clock timeout "
                f"(tune/disable via SUPERBROWSER_MAX_CONSEC_SCRIPTS)."
            )
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
        # Surface the script-usage advisory when the brain is on a
        # 2+ eval/run_script streak with clickable bboxes available.
        try:
            from ..effects import _maybe_script_usage_warning
            _warning = _maybe_script_usage_warning(self.s)
        except Exception:
            _warning = ""
        return result_str + (_warning or "")


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
        # Prepended to the eventual return when the heavy-page guard is
        # released after cursor tools have demonstrably failed (see below).
        # Empty string otherwise, so the f-string concat is a no-op.
        release_advisory = ""
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
                # Cursor-first escape hatch. The block below is correct as
                # a DEFAULT — but once cursor tools have DEMONSTRABLY failed
                # on this page, "use vision instead" is useless advice
                # (vision already failed) and hard-blocking just deadlocks
                # the worker. Consult the cursor-failure ledger; lift the
                # lockout when enough recent failures have accrued. Fails
                # CLOSED (keeps blocking) on any error. Thresholds match the
                # contract surfaced in the worker prompt + worker_hook hint.
                try:
                    _window = int(os.environ.get("RUN_SCRIPT_CURSOR_FAIL_WINDOW", "10"))
                    _min_distinct = int(os.environ.get("RUN_SCRIPT_CURSOR_FAIL_MIN_DISTINCT", "3"))
                    _min_total = int(os.environ.get("RUN_SCRIPT_CURSOR_FAIL_MIN_TOTAL", "5"))
                    released, n_distinct, n_total = self.s.cursor_failures_released(
                        window=_window,
                        min_distinct=_min_distinct,
                        min_total=_min_total,
                    )
                except Exception:
                    released, n_distinct, n_total = False, 0, 0
                    _window = 10
                if not released:
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
                # Released — build the advisory and FALL THROUGH so the
                # counter bookkeeping below (consecutive_script_calls,
                # recent_run_script_outcomes) runs like any other script
                # call. Do NOT early-return here.
                print(
                    f"  [run_script released: cursor_first_exhausted "
                    f"({pt_label}) distinct={n_distinct} total={n_total}]"
                )
                release_advisory = (
                    "[run_script_released:cursor_first_exhausted] "
                    f"{n_distinct} distinct / {n_total} total cursor "
                    f"strategies failed on this {pt_label} page within the "
                    f"last {_window} actions, so the mutating-script lockout "
                    "is lifted for this call. NOTE: synthetic JS clicks/"
                    "typing are isTrusted=false and this page (or its WAF) "
                    "may STILL reject the mutation — if you get "
                    "[blocked_op:...] or a challenge URL, do NOT retry the "
                    "script. The safest fallback is browser_navigate to a "
                    "self-constructed filtered URL (build the query params "
                    "yourself, e.g. ?location=...&distance=...&type=...) — "
                    "browser_navigate is NOT gated by this guard and stays "
                    "on the pinned domain."
                )
                if is_hard_domain:
                    release_advisory += (
                        " This domain is on the high-detection list — prefer "
                        "browser_navigate; reserve mutates=true for in-iframe "
                        "frame.evaluate() where cursor events can't reach."
                    )
                release_advisory += "\n"
        self.s.consecutive_click_calls = 0  # script execution resets click loop tracking
        # Wire the inverse counter so _maybe_script_usage_warning can
        # surface a `[script_warning]` advisory at 2+ consecutive
        # eval/run_script calls. Reset on cursor success in effects.py.
        try:
            self.s.consecutive_script_calls = int(
                getattr(self.s, "consecutive_script_calls", 0) or 0
            ) + 1
        except Exception:
            self.s.consecutive_script_calls = 1
        # Safety valve: hard-abort a pathological read-only-script loop before it
        # drains the whole budget against the wall-clock timeout. Bubbles via
        # WorkerMustExitError → delegation.py returns a clean failure (and
        # re-delegation is itself capped). Env-tunable; 0 disables.
        _consec_abort = _consec_script_abort_threshold()
        if _consec_abort and int(self.s.consecutive_script_calls or 0) >= _consec_abort:
            raise WorkerMustExitError(
                f"{self.s.consecutive_script_calls} consecutive browser_eval / "
                f"browser_run_script calls with no cursor progress — the worker "
                f"is looping on scripts instead of acting on the page. Aborting "
                f"to stop an unbounded token drain against the wall-clock timeout "
                f"(tune/disable via SUPERBROWSER_MAX_CONSEC_SCRIPTS)."
            )
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
            try:
                from ..effects import _maybe_script_usage_warning
                _err_warning = _maybe_script_usage_warning(self.s)
            except Exception:
                _err_warning = ""
            return release_advisory + f"Script error: {error}{tip}" + (_err_warning or "")

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

        # Surface the script-usage advisory on success too — even when
        # the script "worked", 2+ consecutive script calls with visible
        # clickable V_n means the brain is off-track from the tool ladder.
        try:
            from ..effects import _maybe_script_usage_warning
            _ok_warning = _maybe_script_usage_warning(self.s)
        except Exception:
            _ok_warning = ""
        return release_advisory + "\n".join(parts) + (_ok_warning or "")
