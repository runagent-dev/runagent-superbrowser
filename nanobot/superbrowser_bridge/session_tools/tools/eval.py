"""Script / eval tools — direct JS execution against the page."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    name = "browser_eval"
    description = "Execute JavaScript in the browser page. FREE — no screenshot cost."

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
                "isTrusted=false JS clicks are bot-detected by WAFs."
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
        # Post-mutation observation gate: after a click/navigate, the brain
        # must observe before running scripts. Even read-only scripts query
        # a DOM the brain hasn't seen — results will be misinterpreted.
        if self.s._mutation_needs_observation:
            return (
                "[run_script_refused:observe_first] Your last action changed "
                "the page but you haven't taken a browser_screenshot or "
                "browser_get_markdown to see what happened. The DOM may "
                "look completely different now — running a script against "
                "your stale mental model will return unexpected results. "
                "Observe first, then script if needed."
            )
        # Capture pre-snapshot only when this script is allowed to mutate
        # — read-only scripts return raw data, not a build_text_only
        # response, so the delta block would be misleading.
        if bool(mutates):
            self.s._brain_turn_counter += 1
            self.s.capture_action_snapshot(target_index=None)
            await self.s.inter_action_pause()
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
                    "  1. browser_screenshot to refresh the V_n bbox list.\n"
                    "  2. browser_click_at(vision_index=V_n) on the target's bbox.\n"
                    "  3. browser_type_at / browser_scroll_until.\n"
                    "Only when 2+ DIFFERENT strategies have failed with "
                    "concrete error captions can mutates=true scripts "
                    "run. JS clicks are isTrusted=false and routinely "
                    "rejected by Cloudflare / Akamai."
                    + (f"\nRecent cursor failures:\n{ledger}" if ledger else "")
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


