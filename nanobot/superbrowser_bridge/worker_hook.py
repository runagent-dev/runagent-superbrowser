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
        # Arch v3: track constraint-satisfied count so we can grant
        # iteration bonuses on genuine progress.
        self._base_max_iterations = max_iterations
        # Cap dynamic budget at 2× base — prevents runaway extensions on
        # tasks that keep flipping constraints with no real progress.
        self._max_iter_cap = max_iterations * 2
        self._last_constraint_satisfied_count: int = 0
        self._last_budget_warning_at: int = -1  # iteration of last warning
        self._captcha_guidance_given: bool = False
        self._captcha_solve_attempts: int = 0
        self._captcha_escalation_pending: bool = False
        self._captcha_escalation_turns: int = 0
        # Arch v3: tracks whether we've already nudged the brain to
        # verify the most recent click on a dense scene. Reset on each
        # state-change tool.
        self._dense_verify_nudged: bool = False
        # Arch v4 Phase I — fires the [CLICK_MISS_RETRY] nudge once per
        # missed click_at, resets on the next non-click tool.
        self._click_miss_nudged: bool = False
        # Arch v4 Phase K — fires the [CLICK_AT_REMAPPED] soft nudge
        # once per auto-remap, resets on the next non-click tool.
        self._click_at_remap_nudged: bool = False
        # Generic loop + stagnation detector. Owned by state so the
        # screenshot tool can record stale-screenshot streaks and the
        # impasse-tool refusal can read the count. `_loop` stays as an
        # alias so existing hook code (record_action / record_page_state
        # below) doesn't change.
        self._loop = state.loop_detector
        # Tier-auto-escalation: fires at most once per session to avoid
        # loop-cascading the LLM into repeated escalations.
        self._auto_escalated: bool = False
        # Arch v4 Move 7: [PROGRESS] delta tracking. Snapshot of per-
        # constraint status as of the previous iteration; lets us detect
        # transitions and emit a delta line. Stagnation count tracks how
        # many consecutive turns have passed without a flip — kind-aware
        # thresholds gate the "stuck" variant of the [PROGRESS] message.
        self._prev_constraint_statuses: list[str] = []
        self._stagnation_turns: int = 0
        self._last_progress_emit_at: int = -1

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

    # Arch v4 Move 7: kind-aware stagnation thresholds. Numeric and
    # ordering constraints (sliders, sort dropdowns) often need many
    # iterations of drag/refine before URL/state changes; emitting the
    # "stuck" variant after 3 quiet turns would be noisy and push the
    # brain off a working approach. Filters and orderings flip faster.
    _STAGNATION_THRESHOLD_BY_KIND = {
        "filter": 3,
        "attribute": 4,
        "negative": 4,
        "ordering": 3,
        "numeric": 6,
    }

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

    def _emit_progress_block(
        self,
        brief,
        iteration: int,
        guidance_parts: list[str],
    ) -> None:
        """Emit [PROGRESS] delta or stagnation line based on per-constraint
        status transitions vs last iteration's snapshot.

        Behavior:
          - On any status transition (e.g. unverified→satisfied), emit a
            "+/- N this turn" delta with the changed constraint(s) and
            updated tally. Stagnation counter resets.
          - With no transitions, increment stagnation counter; emit the
            "stuck" variant only when count meets the focus constraint's
            kind-aware threshold (filter=3, numeric=6, etc.). Suppresses
            noise on legitimately slow constraints.
          - Snapshots are stored on instance state so transitions across
            iterations are detectable; first call with N constraints
            seeds the snapshot without emitting (no prior to compare).
        """
        constraints = list(getattr(brief, "constraints", []) or [])
        if not constraints:
            return
        prev = list(self._prev_constraint_statuses or [])
        cur = [getattr(c, "status", "unverified") for c in constraints]

        # Seed on first call OR on schema-mismatch (constraint count
        # changed between iterations, e.g. handoff merge added one).
        if not prev or len(prev) != len(cur):
            self._prev_constraint_statuses = list(cur)
            return

        flips: list[tuple[int, str, str]] = []
        for i, (p, n) in enumerate(zip(prev, cur)):
            if p != n:
                flips.append((i, p, n))

        total = len(constraints)
        sat = sum(1 for s in cur if s == "satisfied")
        fail = sum(1 for s in cur if s == "failed")
        remaining = sum(1 for s in cur if s == "unverified")

        # Phase B: use brief.current_focus_idx (system-managed pointer
        # populated by compute_focus). Falls back to first-unverified
        # only when the focus pointer is unset / out of range — keeps
        # legacy briefs that haven't been recomputed yet working.
        next_focus_idx = getattr(brief, "current_focus_idx", -1)
        if not (0 <= next_focus_idx < len(constraints)) or \
                constraints[next_focus_idx].status != "unverified":
            next_focus_idx = -1
            for i, c in enumerate(constraints):
                if getattr(c, "status", "") == "unverified":
                    next_focus_idx = i
                    break

        if flips:
            # Reset stagnation; emit delta line.
            self._stagnation_turns = 0
            gained = [
                (i, constraints[i]) for i, p, n in flips if n == "satisfied"
            ]
            lost = [
                (i, constraints[i]) for i, p, n in flips if n == "failed"
            ]
            parts: list[str] = []
            if gained:
                names = ", ".join(
                    f"{(c.canonical_value or c.text)!r}" for _, c in gained
                )
                parts.append(f"+{len(gained)} satisfied this turn: {names}")
            if lost:
                names = ", ".join(
                    f"{(c.canonical_value or c.text)!r}" for _, c in lost
                )
                parts.append(f"+{len(lost)} failed this turn: {names}")
            if not parts:
                # Other transitions (e.g. unverified→not_applicable).
                parts.append(f"{len(flips)} constraint(s) changed status")
            tally = (
                f"Now {sat}/{total} verified"
                + (f", {fail} failed" if fail else "")
                + f", {remaining} remaining."
            )
            next_focus = ""
            if next_focus_idx >= 0:
                fc = constraints[next_focus_idx]
                next_focus = (
                    f" Next focus: #{next_focus_idx + 1} "
                    f"{(fc.canonical_value or fc.text)!r}."
                )
            guidance_parts.append(
                f"[PROGRESS] {' | '.join(parts)}. {tally}{next_focus}"
            )
            self._last_progress_emit_at = iteration
        else:
            # No flip — increment stagnation. Emit stuck-variant only
            # when count meets the kind-aware threshold for the current
            # focus constraint.
            self._stagnation_turns += 1
            if next_focus_idx >= 0:
                focus_kind = getattr(constraints[next_focus_idx], "kind", "filter")
                threshold = self._STAGNATION_THRESHOLD_BY_KIND.get(focus_kind, 4)
                if self._stagnation_turns >= threshold:
                    fc = constraints[next_focus_idx]
                    cv = fc.canonical_value or fc.text
                    guidance_parts.append(
                        f"[PROGRESS] No constraint flipped in "
                        f"{self._stagnation_turns} turns. Current focus "
                        f"#{next_focus_idx + 1} {cv!r} ({focus_kind}) still "
                        f"unverified. Reconsider: is this constraint "
                        f"achievable on this page, or should you mark it "
                        f"not_applicable via browser_update_task_brief and "
                        f"move on?"
                    )
                    self._last_progress_emit_at = iteration
                    # Reset so we don't re-emit on every subsequent stuck
                    # turn — give the brain breathing room to act on the
                    # nudge before re-firing.
                    self._stagnation_turns = 0

        self._prev_constraint_statuses = list(cur)

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Inject guidance after each tool execution round."""
        guidance_parts: list[str] = []

        # Decrement captcha_mode counter once per iteration. Lets the
        # screenshot-budget limiter automatically re-engage after solving.
        self.state.tick_captcha_mode()

        # --- Arch v4 Move 5: pin [ORIGINAL_QUERY] full-verbatim ---------
        # First guidance line on every iteration when a brief exists.
        # Long sessions otherwise let the user's verbatim query scroll out
        # of recent context. Re-pinning costs ~50–300 tokens but keeps the
        # brain's primary objective unambiguous on multi-constraint tasks.
        # Kill switch: PIN_ORIGINAL_QUERY=0.
        if __import__("os").environ.get("PIN_ORIGINAL_QUERY", "1") != "0":
            brief_oq = getattr(self.state, "task_brief", None)
            if brief_oq is not None:
                try:
                    oq = (brief_oq.original_query or "").strip()
                    if oq:
                        guidance_parts.append(f"[ORIGINAL_QUERY] {oq}")
                except Exception:
                    pass

        # --- Arch v4 Move 1: [GATE_BACKOFF] notice ---------------------
        # When the preplan gate auto-disengages after 3 consecutive
        # refusals, surface that to the brain so it understands why an
        # action just went through without a fresh preplan. Reset the
        # flag after rendering so it shows once.
        try:
            if getattr(self.state, "preplan_backoff_just_fired", False):
                self.state.preplan_backoff_just_fired = False
                guidance_parts.append(
                    "[GATE_BACKOFF n=1] The preplan gate yielded after "
                    "3 consecutive refusals — your last state-change "
                    "tool ran without a fresh browser_preplan to break "
                    "the deadlock. Describe your stuck state via "
                    "browser_update_task_brief (cot_note=...) and call "
                    "browser_preplan before your next action so the "
                    "system can resume tracking declared-vs-observed "
                    "outcomes."
                )
        except Exception:
            pass

        # --- Arch v4 Move 2: [FOCUS] line on every iteration ------------
        # Renders the system-recommended next constraint right under
        # [ORIGINAL_QUERY] so the brain sees the prioritization signal
        # before reading [BUDGET] / brief checklist. The brain may
        # override via browser_update_task_brief; until then this is the
        # system's recommendation. Kill switch: FOCUS_LINE=0.
        if __import__("os").environ.get("FOCUS_LINE", "1") != "0":
            brief_focus = getattr(self.state, "task_brief", None)
            if brief_focus is not None:
                try:
                    focus_text = brief_focus.focus_line()
                    if focus_text:
                        guidance_parts.append(focus_text)
                except Exception:
                    pass

        # --- Generic action-repetition detection ---
        # Inspect the last recorded step (added by the tool that just ran)
        # and feed it to the loop detector.
        last_step = self.state.step_history[-1] if self.state.step_history else None
        # Arch v3: detect constraint progress this iteration. If a brief
        # constraint just flipped to satisfied, this is GENUINE progress —
        # suppress repetition/stagnation nudges that would otherwise push
        # the brain off a winning approach. Computed before the loop
        # detector is consulted so we can short-circuit.
        constraint_progress_this_iter = False
        brief_for_loop = getattr(self.state, "task_brief", None)
        if brief_for_loop is not None:
            try:
                _, _sat_now, _ = brief_for_loop.counts()
                if _sat_now > self._last_constraint_satisfied_count:
                    constraint_progress_this_iter = True
            except Exception:
                pass

        if last_step:
            tool = last_step.get("tool") or ""
            args = {"args_summary": last_step.get("args", "")}
            action_nudge = self._loop.record_action(tool, args)
            if action_nudge and not constraint_progress_this_iter:
                guidance_parts.append(action_nudge)

            # Stagnation by (url, page-fingerprint). We proxy "page content"
            # with the truncated result string; it's good enough to detect
            # "same page, same elements" unchanged across iterations.
            stag_nudge = self._loop.record_page_state(
                last_step.get("url") or "",
                last_step.get("result") or "",
            )
            if stag_nudge and not constraint_progress_this_iter:
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

        # --- Iteration budget bonus on constraint progress (arch v3) ----
        # Each constraint that flips unverified -> satisfied earns +5
        # iterations. Capped at 2× base. Brain self-paces because the
        # remaining-iter budget is rendered into the brief.
        brief = getattr(self.state, "task_brief", None)
        if brief is not None:
            try:
                _, sat, _ = brief.counts()
                gain = sat - self._last_constraint_satisfied_count
                if gain > 0:
                    bonus = gain * 5
                    new_max = min(self.max_iterations + bonus, self._max_iter_cap)
                    if new_max > self.max_iterations:
                        self.max_iterations = new_max
                    self._last_constraint_satisfied_count = sat
                elif gain < 0:
                    # Constraint regressed (rare); don't penalize.
                    self._last_constraint_satisfied_count = sat
            except Exception:
                pass

        # --- Iteration budget warning (single, soft) --------------------
        # Arch v3: dropped the 40%-warning (it pushed the brain into
        # premature browser_run_script use). Single 20%-warning kept,
        # softened to "prioritize highest-value unverified constraint."
        iteration = context.iteration
        remaining = self.max_iterations - iteration - 1

        if remaining <= int(self.max_iterations * 0.2) and self._last_budget_warning_at != iteration:
            self._last_budget_warning_at = iteration
            constraint_hint = ""
            if brief is not None:
                try:
                    unverified = [
                        c for c in brief.constraints
                        if c.status == "unverified"
                    ]
                    if unverified:
                        cv = unverified[0].canonical_value or unverified[0].text
                        constraint_hint = (
                            f" Highest-priority unverified constraint: {cv!r}."
                        )
                except Exception:
                    pass
            guidance_parts.append(
                f"[GUIDANCE: {remaining}/{self.max_iterations} iterations left. "
                f"Prioritize the highest-value unverified constraint and "
                f"capture honest results.{constraint_hint} If a constraint "
                f"truly cannot be satisfied on this site, mark it "
                f"not_applicable via browser_update_task_brief and continue.]"
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

        # --- Arch v3: TaskBrief constraint checklist + budget readout --
        # The brief is the primary working memory. Render it before the
        # plan so the brain's first thing-to-read is "what constraints
        # are still open + how much budget remains". Skipped when no
        # brief is set or when BRIEF_RENDER_IN_HOOK=0.
        if (
            brief is not None
            and __import__("os").environ.get("BRIEF_RENDER_IN_HOOK", "1") != "0"
        ):
            try:
                _, sat, fail = brief.counts()
                total = len(brief.constraints)
                budget_line = (
                    f"[BUDGET turn={iteration + 1}/{self.max_iterations}  "
                    f"constraints={sat}/{total}"
                    + (f"  failed={fail}" if fail else "")
                    + f"  remaining_iter={remaining}]"
                )
                guidance_parts.append(budget_line)
                # Render compact form to keep token cost low — the full
                # form is in build_tool_result_blocks for screenshot
                # turns where the brain is making strategic decisions.
                brief_compact = brief.to_brain_text(compact=True)
                if brief_compact:
                    guidance_parts.append(brief_compact)
            except Exception:
                pass

        # --- Arch v4 Move 7: [PROGRESS] delta block ---------------------
        # Compare per-constraint statuses against the snapshot from the
        # previous iteration. On any transition, emit a delta line. On
        # quiet iterations, emit the "stuck" variant only when the
        # stagnation count meets the focus constraint's kind-aware
        # threshold — prevents per-iteration noise on legitimately slow
        # constraints (sliders, sort orderings) while still flagging
        # genuine stalls. Kill switch: PROGRESS_BLOCK=0.
        if (
            brief is not None
            and __import__("os").environ.get("PROGRESS_BLOCK", "1") != "0"
        ):
            try:
                self._emit_progress_block(brief, iteration, guidance_parts)
            except Exception:
                pass

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

        # --- Arch v3: dense-scene verify nudge --------------------------
        # When the most recent click landed on a scene with ≥5 candidate
        # bboxes within 80px, push the brain to call browser_verify_action
        # before its next action. This is the cheap "auto-fire" — we
        # don't run vision from the hook (would couple state in awkward
        # ways), we surface a strong nudge and let the brain dispatch
        # the tool. Fires once per click; resets on next state-change.
        try:
            if last_step and last_step.get("tool") in (
                "browser_click_at", "browser_click_selector",
                "browser_type_at", "browser_set_slider_at",
                "browser_drag", "browser_drag_path",
            ):
                if not self._dense_verify_nudged:
                    last_resp = getattr(self.state, "_last_vision_response", None)
                    last_ctx = getattr(self.state, "last_action_context", None)
                    is_dense = bool(
                        last_ctx is not None
                        and getattr(last_ctx, "is_dense_scene", lambda: False)()
                    )
                    # Fallback heuristic when last_action_context isn't set:
                    # ≥5 bboxes total in a small viewport area.
                    if not is_dense and last_resp is not None:
                        try:
                            n = len(getattr(last_resp, "bboxes", None) or [])
                            is_dense = n >= 12  # global density proxy
                        except Exception:
                            is_dense = False
                    if is_dense:
                        self._dense_verify_nudged = True
                        sid = self.state.session_id or "<session_id>"
                        tool_used = last_step.get("tool") or "click"
                        guidance_parts.append(
                            f"[DENSE_SCENE_VERIFY] The previous {tool_used} "
                            f"landed on a dense scene (≥5 candidate bboxes "
                            f"nearby). Call browser_verify_action(session_id='"
                            f"{sid}', expected='<what you intended to happen>')"
                            f" before the next action — this catches misclicks "
                            f"on filter modals/dropdowns/pickers cheaply. If "
                            f"the verifier returns recommendation='undo', "
                            f"navigate back or close any opened modal before "
                            f"retrying."
                        )
            else:
                # Reset nudge flag whenever a non-mutating tool runs.
                self._dense_verify_nudged = False
        except Exception:
            pass

        # --- Arch v4 Phase K: [CLICK_AT_REMAPPED] soft nudge ------------
        # When the bridge auto-remapped vision_index to a different V_n
        # because target_label matched it better, surface the recovery
        # to the brain so it learns "trust target_label, vision_index
        # is just a tiebreaker hint". Avoids the brain second-guessing
        # the click that just succeeded. Fires once per remap; resets
        # on the next non-click step. Kill switch:
        # CLICK_AT_REMAP_NUDGE=0.
        try:
            if (
                __import__("os").environ.get("CLICK_AT_REMAP_NUDGE", "1") != "0"
                and last_step
                and last_step.get("tool") == "browser_click_at"
            ):
                result_str = str(last_step.get("result") or "")
                if "[click_at_remap" in result_str:
                    if not getattr(self, "_click_at_remap_nudged", False):
                        self._click_at_remap_nudged = True
                        guidance_parts.append(
                            "[CLICK_AT_REMAPPED] The previous browser_"
                            "click_at remapped V_N→V_M because your "
                            "target_label matched a different V_n than "
                            "the index you passed. The click DID go "
                            "through; this is just a signal that next "
                            "time you can trust target_label and the "
                            "system will pick the right V_n. The "
                            "vision_index hint is a tiebreaker, not a "
                            "contract — pick the most-recently-named "
                            "label and don't worry about index drift."
                        )
            else:
                if last_step and last_step.get("tool") != "browser_click_at":
                    self._click_at_remap_nudged = False
        except Exception:
            pass

        # --- Arch v4 Phase I: stay-on-click_at after a misclick ---------
        # When the most recent click_at returned a miss marker (verify
        # failed, dom unchanged, auto-retry exhausted), nudge the brain
        # to take a fresh screenshot and re-issue click_at on the SAME
        # target_label with the new V_n — not pivot to eval/run_script/
        # navigate. The auto-retry already burned one fresh-vision
        # attempt; ask the brain for at least one more before switching
        # tactics. Fires once per miss; resets on the next non-click
        # tool. Kill switch: CLICK_MISS_RETRY_NUDGE=0.
        try:
            if (
                __import__("os").environ.get("CLICK_MISS_RETRY_NUDGE", "1") != "0"
                and last_step
                and last_step.get("tool") == "browser_click_at"
            ):
                result_str = str(last_step.get("result") or "")
                miss_markers = (
                    "[click_silent",  # may carry a reason= suffix
                    "[VERIFY_MISS",   # may carry kind=/reason= suffix
                    "[BBOX_AUTO_RETRY_NO_MATCH",
                    "outcome=failed]",  # from auto-retry annotation
                )
                if any(m in result_str for m in miss_markers):
                    if not getattr(self, "_click_miss_nudged", False):
                        self._click_miss_nudged = True
                        sid = self.state.session_id or "<session_id>"
                        guidance_parts.append(
                            f"[CLICK_MISS_RETRY] The previous browser_click_at "
                            f"missed (no DOM change OR verify failed). The "
                            f"system already auto-retried once on a fresh "
                            f"V_n. Recovery: take browser_screenshot to "
                            f"refresh V_n, then call browser_preplan + "
                            f"browser_click_at AGAIN on the SAME target_label "
                            f"with the new V_n. Try at least 2 more "
                            f"screenshot+click_at attempts before pivoting to "
                            f"browser_eval / browser_run_script / "
                            f"browser_navigate — those are last-resort and "
                            f"trip bot-detection. If 2 more click_at attempts "
                            f"on the same target also miss, the element may "
                            f"genuinely be a non-button div: try clicking the "
                            f"PARENT row label or scroll the target into a "
                            f"different viewport position via "
                            f"browser_scroll_until."
                        )
            else:
                # Reset on any non-click step so the next miss re-arms.
                if last_step and last_step.get("tool") != "browser_click_at":
                    self._click_miss_nudged = False
        except Exception:
            pass

        # --- v5: chain-of-thought trail ---------------------------------
        # If the previous action set a `narration` on state, surface it
        # back here as `[last_intended: ...]`. Brain compares its prior
        # intent against the actual outcome (subgoal_advanced / not_satisfied
        # / page-state) on its own — no code-side substring matching.
        # Cleared after rendering so each narration shows exactly once.
        try:
            last_narration = getattr(self.state, "_last_narration", "") or ""
            if (
                last_narration
                and __import__("os").environ.get("NARRATION_RENDER", "1") != "0"
            ):
                guidance_parts.append(
                    f"[last_intended: {last_narration!r}] — compare against "
                    "what the prior tool reply actually returned. If they "
                    "diverge, take a screenshot before deciding the next move."
                )
                self.state._last_narration = ""
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
