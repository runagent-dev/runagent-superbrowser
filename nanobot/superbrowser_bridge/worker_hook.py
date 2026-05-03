"""
Mid-session guardrails for the browser worker agent.

Uses the nanobot AgentHook lifecycle to inject corrective guidance
into the conversation when the worker goes off-track (click-screenshot
loops, regression navigation, stagnation, iteration budget pressure).

Guidance is injected by appending text to the last tool result message,
preserving the assistant/tool message alternation expected by LLM APIs.
"""

from __future__ import annotations

from nanobot.agent.hook import AgentHook, AgentHookContext

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.loop_detector import LoopDetector


class BrowserWorkerHook(AgentHook):
    """Injects mid-loop corrective guidance based on worker state."""

    def __init__(self, state: BrowserSessionState, max_iterations: int = 50):
        self.state = state
        self.max_iterations = max_iterations
        self._last_budget_warning_at: int = -1  # iteration of last warning
        self._captcha_guidance_given: bool = False
        self._captcha_solve_attempts: int = 0
        self._captcha_escalation_pending: bool = False
        self._captcha_escalation_turns: int = 0
        # Generic loop + stagnation detector. Owned by state so the
        # screenshot tool can record stale-screenshot streaks and the
        # impasse-tool refusal can read the count. `_loop` stays as an
        # alias so existing hook code (record_action / record_page_state
        # below) doesn't change.
        self._loop = state.loop_detector
        # Tier-auto-escalation: fires at most once per session to avoid
        # loop-cascading the LLM into repeated escalations.
        self._auto_escalated: bool = False

    # Tools that, by definition, observe the page rather than change it.
    # When one of these runs we clear the failure flag — the brain has
    # looked at the page (or rebuilt its state) since the last failure.
    _OBSERVATION_TOOLS = frozenset({
        "browser_screenshot",
        "browser_look_again",
        "browser_get_markdown",
        "browser_image_region",
    })

    # Tools that produce an action result the brain needs to act on.
    # If their result string contains a failure marker we set the
    # failure flag.
    _STATE_CHANGE_TOOLS = frozenset({
        "browser_click", "browser_click_at", "browser_click_selector",
        "browser_type", "browser_type_at", "browser_fix_text_at",
        "browser_navigate", "browser_drag", "browser_drag_selectors",
        "browser_drag_path", "browser_keys", "browser_select",
        "browser_select_option", "browser_set_slider",
        "browser_set_slider_at", "browser_drag_slider_until",
        "browser_eval", "browser_run_script", "browser_wait_for",
    })

    # Substrings in tool result text that mark a failure / no-effect
    # outcome — the brain should re-screenshot before pivoting tools.
    _FAILURE_MARKERS = (
        "_failed", "_timeout", "click_silent", "navigate_unverified",
        "VERIFY_MISS", "selector_ambiguous", "BLOCKED:", "NETWORK_BLOCKED",
        "subgoal_unsatisfiable", "click_loop_detected",
    )

    def _update_failure_flag(self, last_step: dict) -> None:
        """Set or clear `state.last_failure_without_screenshot` based on
        the last tool's outcome. See class-level marker sets."""
        tool = last_step.get("tool") or ""
        if tool in self._OBSERVATION_TOOLS:
            self.state.clear_action_failed()
            return
        if tool not in self._STATE_CHANGE_TOOLS:
            return
        result = str(last_step.get("result") or "")
        for marker in self._FAILURE_MARKERS:
            if marker in result:
                self.state.mark_action_failed(f"{tool}: {result[:120]}")
                return

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Inject guidance after each tool execution round."""
        guidance_parts: list[str] = []

        # Decrement captcha_mode counter once per iteration. Lets the
        # screenshot-budget limiter automatically re-engage after solving.
        self.state.tick_captcha_mode()

        # --- Generic action-repetition detection ---
        # Inspect the last recorded step (added by the tool that just ran)
        # and feed it to the loop detector.
        last_step = self.state.step_history[-1] if self.state.step_history else None
        if last_step:
            tool = last_step.get("tool") or ""
            args = {"args_summary": last_step.get("args", "")}
            action_nudge = self._loop.record_action(tool, args)
            if action_nudge:
                guidance_parts.append(action_nudge)

            # Stagnation by (url, page-fingerprint). We proxy "page content"
            # with the truncated result string; it's good enough to detect
            # "same page, same elements" unchanged across iterations.
            stag_nudge = self._loop.record_page_state(
                last_step.get("url") or "",
                last_step.get("result") or "",
            )
            if stag_nudge:
                guidance_parts.append(stag_nudge)

            # --- Refusal-tool gate: failure flag bookkeeping --------
            # Set last_failure_without_screenshot when the previous tool
            # surfaced a failure marker; clear it when a fresh screenshot
            # was taken. The flag gates browser_request_help and
            # browser_run_script(mutates=true) — see
            # BrowserSessionState.must_screenshot_before_giving_up.
            try:
                self._update_failure_flag(last_step)
            except Exception:
                pass

        # --- Iteration budget warnings ---
        iteration = context.iteration
        remaining = self.max_iterations - iteration - 1

        if remaining <= int(self.max_iterations * 0.2) and self._last_budget_warning_at != iteration:
            # 20% or less remaining — prioritize, don't panic.
            self._last_budget_warning_at = iteration
            guidance_parts.append(
                f"[GUIDANCE: {remaining} iterations left out of {self.max_iterations}. "
                "Prioritize extracting the real data with browser_get_markdown. "
                "Do NOT fabricate values — if the data cannot be obtained, report it "
                "honestly via done(success=False).]"
            )
        elif remaining <= int(self.max_iterations * 0.4) and self._last_budget_warning_at != iteration:
            # 40% or less remaining
            self._last_budget_warning_at = iteration
            guidance_parts.append(
                f"[GUIDANCE: {remaining} iterations left out of "
                f"{self.max_iterations}. Switch to browser_run_script NOW "
                "to batch all remaining work into one script call.]"
            )

        # --- Detect regression (already handled at tool level, reinforce here) ---
        if self.state.regression_count > 0 and self.state.best_checkpoint_url:
            # Only inject if regression happened this iteration
            recent_steps = self.state.step_history[-2:] if len(self.state.step_history) >= 2 else []
            for step in recent_steps:
                if step["tool"] == "browser_navigate" and "FAILED" not in step.get("result", ""):
                    step_url = step.get("url", "")
                    if step_url and self.state.is_regression(step_url):
                        guidance_parts.append(
                            "[GUIDANCE: You navigated backward instead of fixing "
                            "your approach on the current page. Your best progress "
                            f"was at: {self.state.best_checkpoint_url}. "
                            "Do NOT restart from the beginning — fix your script "
                            "on the current page.]"
                        )
                        break

        # --- One auto-solve attempt, then straight to human ---
        # Previously this allowed up to 3 solve attempts before nudging the
        # agent off the loop. That pattern trained sites to fingerprint us
        # as a bot. Under the fast-to-human policy a single failed auto
        # solve means pivot to the human: browser_ask_user surfaces the
        # live view, the user clicks through once, and bot-protection
        # cookies get persisted by cookie-jar for the next run.
        #
        # The auto-escalation in BrowserSolveCaptchaTool now handles this
        # deterministically (no LLM decision needed). This hook is the
        # backup: if the LLM somehow calls browser_solve_captcha without
        # auto_escalate, or if it ignores the auto-escalation result.
        recent_steps = self.state.step_history[-1:] if self.state.step_history else []
        for step in recent_steps:
            if step["tool"] == "browser_solve_captcha":
                self._captcha_solve_attempts += 1
                result_text = str(step.get("result", ""))
                solved = "SOLVED" in result_text or '"solved": true' in result_text
                if not solved and self._captcha_solve_attempts >= 1:
                    self._captcha_escalation_pending = True
                    sid = self.state.session_id or "<session_id>"
                    guidance_parts.append(
                        "[GUIDANCE: Auto-solve failed once — do NOT retry. "
                        "Sites fingerprint repeated solver pings as bot activity. "
                        "Hand off to the human NOW:\n"
                        f"  browser_ask_user(session_id='{sid}', "
                        "input_type='captcha', "
                        "question='Please open the live view URL and "
                        "click through the captcha — I will detect when "
                        "it clears and resume.')\n"
                        "The tool blocks while the user solves. "
                        "Do NOT call browser_solve_captcha again.]"
                    )

        # If escalation was requested but the LLM didn't call browser_ask_user
        # on its next turn, escalate the urgency.
        if self._captcha_escalation_pending:
            last_tool = (self.state.step_history[-1].get("tool") or "") if self.state.step_history else ""
            if last_tool == "browser_ask_user":
                self._captcha_escalation_pending = False
                self._captcha_escalation_turns = 0
            elif last_tool != "browser_solve_captcha":
                self._captcha_escalation_turns += 1
                if self._captcha_escalation_turns >= 2:
                    sid = self.state.session_id or "<session_id>"
                    guidance_parts.append(
                        f"[MANDATORY: You MUST call browser_ask_user(session_id='{sid}', "
                        "input_type='captcha', question='Please solve the captcha') NOW. "
                        "No other actions are allowed until you do. The captcha will NOT "
                        "resolve itself — a human must solve it.]"
                    )

        # --- Detect verification/captcha pages ---
        if self.state.session_id and not self._captcha_guidance_given:
            current_url = self.state.current_url or ""
            # Check URL patterns that indicate verification/bot-protection
            blocking_url_patterns = [
                "/login", "/signin", "/auth", "/verify",
                "/challenge", "/captcha", "/security",
            ]
            url_looks_blocking = any(
                p in current_url.lower() for p in blocking_url_patterns
            )

            # Check recent step results for blocking signals
            recent = (
                self.state.step_history[-3:]
                if self.state.step_history
                else []
            )
            text_signals = [
                "verify", "captcha", "security check", "just a moment",
                "are you a robot", "human verification", "prove you",
                "slide to verify", "complete the puzzle",
            ]
            result_looks_blocking = any(
                any(
                    sig in (step.get("result", "") or "").lower()
                    for sig in text_signals
                )
                for step in recent
            )

            if url_looks_blocking or result_looks_blocking:
                self._captcha_guidance_given = True
                sid = self.state.session_id
                guidance_parts.append(
                    "[GUIDANCE: This page appears to be a CAPTCHA or "
                    "security verification — NOT a login page. "
                    f"Call browser_detect_captcha(session_id='{sid}') "
                    "to check, then "
                    f"browser_solve_captcha(session_id='{sid}', "
                    "method='auto') to solve it. "
                    "Do NOT report LOGIN REQUIRED for bot protection "
                    "pages.]"
                )

        # --- Tier auto-escalation (t1 → t3) -----------------------------
        # Fires when a t1 tool flagged network_blocked OR vision detected a
        # captcha. Surfaces a crisp, directive guidance block telling the LLM
        # to call browser_escalate next. We do NOT call browser_escalate from
        # the hook itself — tool dispatch stays under the LLM's control so
        # the tool call appears in the transcript and the brain can react to
        # the return value. One-shot per session.
        if (
            not self._auto_escalated
            and self.state.session_id
            and self.state.backend == "t1"
        ):
            should_escalate = False
            reason = ""
            if self.state.network_blocked:
                should_escalate = True
                reason = f"network_blocked:HTTP_{self.state.last_network_status or '?'}"
            else:
                last_vision = getattr(self.state, "_last_vision_response", None)
                flags = getattr(last_vision, "flags", None)
                if flags is not None and bool(getattr(flags, "captcha_present", False)):
                    should_escalate = True
                    ct = getattr(flags, "captcha_type", None)
                    reason = f"vision_captcha:{ct or 'unspecified'}"

            if should_escalate:
                self._auto_escalated = True
                sid = self.state.session_id
                guidance_parts.append(
                    "[AUTO_ESCALATION_ADVISED] Tier 1 (Puppeteer) hit "
                    f"anti-bot protection ({reason}). "
                    f"Call browser_escalate(session_id='{sid}', reason='{reason}') "
                    "NOW. This migrates the session to Tier 3 (undetected "
                    "Chromium) preserving cookies + URL. The returned "
                    "new_session_id is what all subsequent browser_* tools "
                    "must use. Do NOT retry on the current session — Akamai-"
                    "class protections will not relent on the same IP+TLS "
                    "fingerprint."
                )

        # --- TaskPlan rendering -----------------------------------------
        # If the brain committed to a multi-step plan via
        # browser_set_task_plan, render it on every iteration so the
        # cursor never falls out of working memory. Composition rule
        # (see task_plan.py): when a form_session is ALSO active, the
        # form checklist is the primary view and the TaskPlan renders
        # as a single-line cursor; otherwise the full plan checklist
        # is shown. Verify_action handles per-step advancement; this
        # hook is the persistent visual reminder.
        plan = getattr(self.state, "task_plan", None)
        form_sess = getattr(self.state, "form_session", None)
        if plan is not None:
            try:
                compact = form_sess is not None
                plan_text = plan.to_brain_text(compact=compact)
                if plan_text:
                    guidance_parts.append(plan_text)
            except Exception:
                pass

        # --- Phase 2: form-fill checklist reminder ----------------------
        # While a form_session is active, remind the brain at every
        # iteration which fields still need filling. The session itself
        # tracks state via browser_type_at + form_commit; this hook is
        # the persistent visual nudge so the brain can't forget the
        # field hidden behind an autocomplete dropdown.
        if form_sess is not None:
            try:
                checklist = form_sess.remaining_checklist(max_lines=10)
                if checklist:
                    needs = form_sess.needs_screenshot(
                        getattr(self.state, "_brain_turn_counter", 0)
                    )
                    pieces = [checklist]
                    if needs:
                        pieces.append(needs)
                    guidance_parts.append("\n".join(pieces))
            except Exception:
                pass

        # --- Phase 2.5: filter-dialog-without-form-session nudge --------
        # When the brain has likely opened a multi-option filter dialog
        # (≥3 checkbox/radio/option controls in the latest tool result's
        # interactive elements listing) AND the original task names
        # multiple filter values (joined by "and"/"with both"), but no
        # form_session is active, inject a one-line reminder. Filter-modal
        # tasks reliably failed when the worker applied the first visible
        # filter then panic-navigated instead of opening form_begin —
        # which would have kept the pending-fields pressure on. Fires at
        # most once per task; never refuses a tool.
        if form_sess is None and not getattr(self, "_filter_nudge_fired", False):
            try:
                import re as _re
                task_lower = (self.state.task_instruction or "").lower()
                # Tight intent match: require a real filter-intent verb
                # NEAR an "and" conjunction. Loose `" and " + "match"`
                # would over-fire on any task that happens to contain the
                # word "match" and a clause join (e.g. "click Fall 2023
                # and find courses that match"), and previously caused
                # the brain to treat term-picker options as multi-value
                # filter fields — clicking Fall 2023 *and later* an
                # unrelated option as if filling form_session fields.
                multi_filter_pattern = _re.compile(
                    r"\bwith\s+both\b"
                    r"|\b(?:include|including|includes|featuring|"
                    r"with|amenities|amenity|filter(?:s|ed|ing)?\s+(?:by|for))"
                    r"\b[^.,;\n]{0,60}\band\b",
                    _re.IGNORECASE,
                )
                if multi_filter_pattern.search(task_lower):
                    last_tool_text = ""
                    for m in reversed(context.messages):
                        if m.get("role") == "tool":
                            c = m.get("content")
                            if isinstance(c, str):
                                last_tool_text = c
                            elif isinstance(c, list):
                                last_tool_text = "\n".join(
                                    (b.get("text") or "")
                                    for b in c
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            break
                    n_options = len(_re.findall(
                        r'type="(?:checkbox|radio)"|role="(?:checkbox|radio|option)"',
                        last_tool_text,
                    ))
                    # Higher threshold: 5+ controls is a real filter
                    # panel; 3-4 covers cookie-banner consent rows and
                    # one-off radio groups (single-pick term selector,
                    # gender selector, etc.) that are NOT multi-value
                    # filter dialogs.
                    if n_options >= 5:
                        snippet = (self.state.task_instruction or "")[:160]
                        guidance_parts.append(
                            "[FILTER_DIALOG_OPEN_NO_SESSION] "
                            f"{n_options} checkbox/radio/option controls are "
                            "visible in the current page elements but no "
                            "form_session is active. The task names multiple "
                            f"filter values: \"{snippet}\"\n"
                            "Call browser_form_begin(intent='...', fields=[...]) "
                            "with EVERY filter value from the task BEFORE the "
                            "next click in this dialog. The pending-fields "
                            "reminder is what stops you from navigating away "
                            "after only the first checkbox. For options below "
                            "the modal fold, browser_scroll_until(target_text="
                            "'<exact label>') now scrolls inside open dialogs."
                        )
                        self._filter_nudge_fired = True
            except Exception:
                pass

        # --- Phase 3.1: cursor-failure ledger reminder ------------------
        # When the brain has failed at least one cursor strategy, surface
        # the ledger so it knows to try a DIFFERENT cursor strategy next
        # rather than reach for browser_run_script (which the lockout
        # gate will refuse anyway until a second strategy fails).
        try:
            recs = getattr(self.state, "cursor_failure_records", None) or []
            distinct = len(getattr(self.state, "cursor_failure_strategies", set()) or set())
            if recs and distinct < 2:
                tried = ", ".join(sorted(self.state.cursor_failure_strategies)) or "(none)"
                guidance_parts.append(
                    "[CURSOR_FAILURES_SO_FAR strategies_tried="
                    f"{tried} distinct={distinct}/2]\n"
                    "Try a DIFFERENT cursor strategy before considering "
                    "browser_run_script(mutates=true). The script lockout "
                    "will refuse it until 2 distinct cursor strategies have "
                    "failed."
                )
        except Exception:
            pass

        # --- Inject guidance into the last tool result message ---
        if guidance_parts and context.messages:
            guidance_text = "\n" + "\n".join(guidance_parts)
            # Find the last tool-result message and append guidance to it
            for i in range(len(context.messages) - 1, -1, -1):
                msg = context.messages[i]
                if msg.get("role") == "tool":
                    if isinstance(msg.get("content"), str):
                        msg["content"] += guidance_text
                    elif isinstance(msg.get("content"), list):
                        # Multimodal content (image blocks) — append as text block
                        msg["content"].append({"type": "text", "text": guidance_text})
                    break
