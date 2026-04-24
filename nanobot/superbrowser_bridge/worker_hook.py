"""
Mid-session guardrails for the browser worker agent.

Uses the nanobot AgentHook lifecycle to inject corrective guidance
into the conversation when the worker goes off-track (click-screenshot
loops, regression navigation, stagnation, iteration budget pressure).

Guidance is injected by appending text to the last tool result message,
preserving the assistant/tool message alternation expected by LLM APIs.

Each guidance entry is a `StructuredGuidance` record so downstream
consumers (loop detector, force-next-tool dispatcher) can react to the
same signal without re-parsing free-text. The human-facing `.text`
field is what ultimately lands in the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.loop_detector import LoopDetector


@dataclass
class StructuredGuidance:
    """A single piece of guidance emitted by the worker hook.

    `text` is what the brain reads on the next turn. Everything else is
    machine-readable so other subsystems can act on the same signal
    (e.g., force the next tool choice after N repeated failures).
    """
    kind: str  # "retry" | "subgoal_advance" | "subgoal_stale" | "replan" | "budget" | "captcha" | "escalate" | "regression" | "loop"
    retry_suggestion: str = ""
    next_tool: str | None = None
    param_override: dict[str, Any] | None = None
    severity: str = "info"  # "info" | "warn" | "force"
    text: str = ""

    @classmethod
    def info(cls, kind: str, text: str) -> "StructuredGuidance":
        return cls(kind=kind, text=text, severity="info")

    @classmethod
    def warn(cls, kind: str, text: str) -> "StructuredGuidance":
        return cls(kind=kind, text=text, severity="warn")


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
        # Generic loop + stagnation detector (replaces consecutive_click_calls +
        # ad-hoc _stagnation_url/_stagnation_count logic).
        self._loop = LoopDetector()
        # Tier-auto-escalation: fires at most once per session to avoid
        # loop-cascading the LLM into repeated escalations.
        self._auto_escalated: bool = False
        # Force-rewind is likewise one-shot — we only override the
        # brain's tool choice once per session, then drop the force if
        # it disobeys. Re-entry is allowed after a checkpoint advance.
        self._force_rewind_emitted: bool = False
        # Streak counter for [no_effect:*] prefixes. Two in a row is a
        # strong "brain is planning against an imagined state" signal —
        # the tool dispatched but the page didn't respond, and the next
        # planned step would be built on that non-response. Promote to
        # a forced rewind via the same one-shot as _maybe_force_rewind.
        self._consecutive_no_effect: int = 0
        # Adaptive replan bookkeeping: last iteration a replan fired +
        # the URL we last saw, so we can detect a URL transition + rate-
        # limit the rebuild call.
        self._last_replan_iter: int = -9999
        self._last_seen_url: str = ""
        self._replan_min_gap: int = 5
        # Blocker-dismiss bookkeeping: how many turns in a row we've
        # surfaced "dismiss this modal via semantic_click" without the
        # brain complying. First turn → info guidance. Second → force
        # the semantic_click via _forced_next_tool. Resets when the
        # blocker goes away or the brain actually uses semantic_click.
        self._blocker_nudge_count: int = 0
        self._blocker_last_hint: str = ""
        # Patience bookkeeping: when the brain makes cursor progress
        # on a URL and THEN navigates away, it's almost always a give-
        # up / "try another URL" escape. We track per-URL cursor
        # successes so `_maybe_emit_patience_warning` can call out the
        # pivot. Resets on legitimate URL transitions driven by a
        # successful submit (tracked via the URL counter on the last
        # successful tool result).
        self._cursor_success_by_url: dict[str, int] = {}
        self._patience_nudge_count: int = 0
        # Validator-rejection streak bookkeeping. Two consecutive
        # `[validator_rejected:coverage_miss]` results mean the active
        # subgoal's precondition element isn't anywhere in the fused
        # perception — brain is hunting for something that isn't on
        # screen. Guide to browser_scroll first, then escalate to
        # rewind on a third strike.
        self._consecutive_validator_rejected: int = 0
        self._last_validator_reject_reason: str = ""
        self._last_validator_reject_subgoal: str = ""

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Inject guidance after each tool execution round."""
        structured: list[StructuredGuidance] = []

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
                # Promote repeated-action nudges to session-memory retry
                # hints. Three strikes against the same (tool, args) also
                # force a rewind — the brain has proven it can't escape
                # the local minimum on its own.
                retry = self._build_retry_guidance(tool, action_nudge)
                if retry is not None:
                    structured.append(retry)
                else:
                    structured.append(
                        StructuredGuidance.info("loop", action_nudge)
                    )

            # Stagnation by (url, page-fingerprint). We proxy "page content"
            # with the truncated result string; it's good enough to detect
            # "same page, same elements" unchanged across iterations.
            stag_nudge = self._loop.record_page_state(
                last_step.get("url") or "",
                last_step.get("result") or "",
            )
            if stag_nudge:
                structured.append(StructuredGuidance.info("loop", stag_nudge))

        # --- Task graph step ---
        # Run the deterministic subgoal updater. If the active subgoal's
        # expected_signals fired in the post-action snapshot, advance the
        # pointer and tell the brain "you crossed the next barrier".
        # If the subgoal hasn't progressed for a while, surface a hint
        # (without transitioning) so the brain can re-strategize.
        graph = getattr(self.state, "task_graph", None)
        if graph is not None and last_step is not None:
            try:
                from superbrowser_bridge.task_graph import updater_check
                self.state.actions_on_active_subgoal = (
                    getattr(self.state, "actions_on_active_subgoal", 0) + 1
                )
                last_vision = getattr(self.state, "_last_vision_response", None)
                last_url = (
                    last_step.get("url")
                    or self.state.current_url
                    or ""
                )
                # Cheap interactive-element snapshot, when available, lets
                # element_visible / element_text_matches signals fire even
                # when vision didn't bbox the target.
                last_result_text = str(last_step.get("result") or "")
                scroll_tel = getattr(self.state, "scroll_telemetry", None)
                new_id, reason = updater_check(
                    graph,
                    vision_resp=last_vision,
                    dom_elements_text=last_result_text,
                    url=last_url,
                    last_action={
                        "tool": last_step.get("tool") or "",
                        "args": last_step.get("args") or "",
                    },
                    scroll_telemetry=scroll_tel if isinstance(scroll_tel, dict) else None,
                    actions_on_active=self.state.actions_on_active_subgoal,
                )
                if reason.startswith("signal fired") or reason.startswith("skipped ahead"):
                    prev_id = graph.active_id
                    graph.advance(new_id, reason)
                    self.state.actions_on_active_subgoal = 0
                    cur = graph.current()
                    cur_desc = (cur.description if cur else "").strip()
                    new_id_display = graph.active_id or "end"
                    structured.append(StructuredGuidance(
                        kind="subgoal_advance",
                        text=(
                            f"[SUBGOAL_ADVANCED {prev_id} → {new_id_display}: {reason}. "
                            f"Now focus on: {cur_desc[:140] if cur else 'task complete'}]"
                        ),
                    ))
                elif reason.startswith("stale:"):
                    cur = graph.current()
                    cur_desc = (cur.description if cur else "").strip()
                    structured.append(StructuredGuidance(
                        kind="subgoal_stale",
                        text=(
                            f"[SUBGOAL_STALE {graph.active_id}: "
                            f"{self.state.actions_on_active_subgoal} actions "
                            f"with no completion signal. Either the subgoal "
                            f"('{cur_desc[:120]}') needs a different approach, "
                            f"or revise expectations — consider whether the "
                            f"page actually supports it from here.]"
                        ),
                    ))
                    # Adaptive replan — trigger when stale AND the URL
                    # has changed since we last ran a replan. Rate-limit
                    # to one rebuild per 5 iterations so we don't thrash
                    # on a genuinely hard page.
                    await self._maybe_replan(context, graph, structured)
            except Exception:
                # Task graph plumbing must NEVER block the hook.
                pass

        # Track the last URL we saw for the next iteration's replan
        # detection. Done outside the task-graph try block so we still
        # update even if the graph isn't present.
        if last_step is not None:
            self._last_seen_url = str(last_step.get("url") or self._last_seen_url)

        # --- Iteration budget warnings ---
        iteration = context.iteration
        remaining = self.max_iterations - iteration - 1

        if remaining <= int(self.max_iterations * 0.2) and self._last_budget_warning_at != iteration:
            # 20% or less remaining — prioritize, don't panic.
            self._last_budget_warning_at = iteration
            structured.append(StructuredGuidance(
                kind="budget",
                text=(
                    f"[GUIDANCE: {remaining} iterations left out of {self.max_iterations}. "
                    "Prioritize extracting the real data with browser_get_markdown. "
                    "Do NOT fabricate values — if the data cannot be obtained, report it "
                    "honestly via done(success=False).]"
                ),
                severity="warn",
            ))
        elif remaining <= int(self.max_iterations * 0.4) and self._last_budget_warning_at != iteration:
            # 40% or less remaining
            self._last_budget_warning_at = iteration
            structured.append(StructuredGuidance(
                kind="budget",
                text=(
                    f"[GUIDANCE: {remaining} iterations left out of "
                    f"{self.max_iterations}. Switch to browser_run_script NOW "
                    "to batch all remaining work into one script call.]"
                ),
                severity="info",
            ))

        # --- Detect regression (already handled at tool level, reinforce here) ---
        if self.state.regression_count > 0 and self.state.best_checkpoint_url:
            # Only inject if regression happened this iteration
            recent_steps = self.state.step_history[-2:] if len(self.state.step_history) >= 2 else []
            for step in recent_steps:
                if step["tool"] == "browser_navigate" and "FAILED" not in step.get("result", ""):
                    step_url = step.get("url", "")
                    if step_url and self.state.is_regression(step_url):
                        structured.append(StructuredGuidance(
                            kind="regression",
                            text=(
                                "[GUIDANCE: You navigated backward instead of fixing "
                                "your approach on the current page. Your best progress "
                                f"was at: {self.state.best_checkpoint_url}. "
                                "Do NOT restart from the beginning — fix your script "
                                "on the current page.]"
                            ),
                            severity="warn",
                        ))
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
                    structured.append(StructuredGuidance(
                        kind="captcha",
                        next_tool="browser_ask_user",
                        severity="warn",
                        text=(
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
                        ),
                    ))

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
                    structured.append(StructuredGuidance(
                        kind="captcha",
                        next_tool="browser_ask_user",
                        severity="force",
                        text=(
                            f"[MANDATORY: You MUST call browser_ask_user(session_id='{sid}', "
                            "input_type='captcha', question='Please solve the captcha') NOW. "
                            "No other actions are allowed until you do. The captcha will NOT "
                            "resolve itself — a human must solve it.]"
                        ),
                    ))

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
                structured.append(StructuredGuidance(
                    kind="captcha",
                    next_tool="browser_detect_captcha",
                    severity="info",
                    text=(
                        "[GUIDANCE: This page appears to be a CAPTCHA or "
                        "security verification — NOT a login page. "
                        f"Call browser_detect_captcha(session_id='{sid}') "
                        "to check, then "
                        f"browser_solve_captcha(session_id='{sid}', "
                        "method='auto') to solve it. "
                        "Do NOT report LOGIN REQUIRED for bot protection "
                        "pages.]"
                    ),
                ))

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
                structured.append(StructuredGuidance(
                    kind="escalate",
                    next_tool="browser_escalate",
                    param_override={"session_id": sid, "reason": reason},
                    severity="warn",
                    text=(
                        "[AUTO_ESCALATION_ADVISED] Tier 1 (Puppeteer) hit "
                        f"anti-bot protection ({reason}). "
                        f"Call browser_escalate(session_id='{sid}', reason='{reason}') "
                        "NOW. This migrates the session to Tier 3 (undetected "
                        "Chromium) preserving cookies + URL. The returned "
                        "new_session_id is what all subsequent browser_* tools "
                        "must use. Do NOT retry on the current session — Akamai-"
                        "class protections will not relent on the same IP+TLS "
                        "fingerprint."
                    ),
                ))

        # --- Patience guardrail: warn on navigate-away from progress ---
        # When the brain calls `browser_navigate` after a run of
        # successful cursor actions on the current URL, it's usually a
        # give-up ("let me try a different URL") rather than a real
        # plan transition. The task graph's replan will eventually
        # catch this, but by then the brain has already left the page
        # it was making progress on. Catch it at the moment of the
        # navigate call.
        self._update_cursor_success_index(last_step)
        self._maybe_emit_patience_warning(structured, last_step)

        # --- Blocker-layer auto-dismiss guidance ---
        # When vision reports an active blocker (cookie banner, region
        # modal, consent gate) WITH a dismiss_hint, the brain should
        # click that hint via browser_semantic_click — NOT try to script
        # it away, and NOT try any other mutation that would fire against
        # content behind the blocker. Surface the guidance first turn;
        # if the brain ignores it, promote to a forced semantic_click on
        # the next turn.
        self._maybe_emit_blocker_guidance(structured, last_step)

        # --- No-effect streak detection ---
        # A `[no_effect:TOOL]` prefix on the result means the TS bridge
        # saw zero url/DOM/focus delta. One is tolerable — the click
        # might have been absorbed by a dismissing dropdown. Two in a
        # row means the brain is planning against a page that isn't
        # reacting, and its next step will compound the hallucination.
        # Promote to a forced rewind via the same one-shot that
        # `_maybe_force_rewind` uses, so we don't emit two competing
        # forces on the same turn.
        result_str = str((last_step or {}).get("result") or "")
        if result_str.startswith("[no_effect:"):
            self._consecutive_no_effect += 1
        else:
            self._consecutive_no_effect = 0

        # --- Validator-rejection streak ---
        # `[validator_rejected:reason]` means the propose→validate→fire
        # pipeline blocked a dispatch before it went out. Streaks matter:
        #   1 reject → brain will self-correct on the next turn.
        #   2 rejects (same subgoal, coverage_miss) → the element it
        #     wants is not on the current frame — force a scroll so
        #     something new comes into view.
        #   3 rejects in a row → plan drifted; rewind to checkpoint.
        if result_str.startswith("[validator_rejected:"):
            reason = result_str.split(":", 2)[1].split("]", 1)[0].strip() if "]" in result_str else "unknown"
            subgoal = ""
            # Extract subgoal id if the tag carries one: "subgoal=g2".
            import re as _re
            m = _re.search(r"subgoal=(\S+)", result_str)
            if m:
                subgoal = m.group(1).rstrip("]")
            if (
                self._last_validator_reject_reason == reason
                and self._last_validator_reject_subgoal == subgoal
            ):
                self._consecutive_validator_rejected += 1
            else:
                self._consecutive_validator_rejected = 1
            self._last_validator_reject_reason = reason
            self._last_validator_reject_subgoal = subgoal

            if self._consecutive_validator_rejected == 2 and reason == "coverage_miss":
                structured.append(StructuredGuidance(
                    kind="retry",
                    next_tool="browser_scroll",
                    severity="force",
                    text=(
                        f"[FORCED_SCROLL validator] Two consecutive "
                        f"coverage_miss rejections on subgoal {subgoal or '?'}. "
                        f"The target isn't in the current viewport's bboxes+DOM. "
                        f"Scrolling down to surface more of the page before "
                        f"giving up on this subgoal."
                    ),
                ))
            elif self._consecutive_validator_rejected >= 3 and not self._force_rewind_emitted and self.state.best_checkpoint_url:
                self._force_rewind_emitted = True
                structured.append(StructuredGuidance(
                    kind="retry",
                    next_tool="browser_rewind_to_checkpoint",
                    severity="force",
                    text=(
                        f"[FORCED_REWIND validator] "
                        f"{self._consecutive_validator_rejected} consecutive "
                        f"validator rejections ({reason}) on subgoal {subgoal or '?'}. "
                        f"Plan has drifted from what's reachable on the page — "
                        f"rewinding to {self.state.best_checkpoint_url} so fresh "
                        f"vision can re-ground the approach."
                    ),
                ))
        else:
            self._consecutive_validator_rejected = 0
            self._last_validator_reject_reason = ""
            self._last_validator_reject_subgoal = ""
        if (
            self._consecutive_no_effect >= 2
            and not self._force_rewind_emitted
            and self.state.best_checkpoint_url
        ):
            self._force_rewind_emitted = True
            structured.append(StructuredGuidance(
                kind="retry",
                next_tool="browser_rewind_to_checkpoint",
                severity="force",
                text=(
                    f"[FORCED_REWIND no_effect] {self._consecutive_no_effect} "
                    f"consecutive tool calls produced no page change. The "
                    f"brain is planning against a page that isn't reacting — "
                    f"rewinding to the last known-good checkpoint ("
                    f"{self.state.best_checkpoint_url}) so the plan can "
                    f"re-approach with fresh vision."
                ),
            ))

        # --- Session-memory retry hint on repeated failure ---
        # If the brain has fired the same (tool, args, result) three
        # times in the last window, promote the retry guidance to a
        # forced rewind. Disobedience is tolerated once: if the brain
        # doesn't comply, we drop the force on the next iteration so
        # we don't fight it indefinitely.
        force_rewind = self._maybe_force_rewind()
        if force_rewind is not None:
            structured.append(force_rewind)

        # Expose the structured list so other subsystems (action
        # planner, dispatcher) can react without parsing the text.
        try:
            context.hook_state["structured_guidance"] = structured  # type: ignore[attr-defined]
        except Exception:
            pass

        # Force-next-tool wiring. `severity="force"` means "the brain's
        # choice is overridden for the next turn." We stash it on state
        # where the tool dispatcher can read it. Disobedience guardrail:
        # only honor a force once; if the brain picks a different tool
        # anyway, clear the flag (don't keep shouting).
        self._refresh_force_next_tool(structured)

        # --- Inject guidance into the last tool result message ---
        if structured and context.messages:
            guidance_text = "\n" + "\n".join(g.text for g in structured if g.text)
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

    # ------------------------------------------------------------------
    # Helpers

    def _build_retry_guidance(
        self, tool: str, action_nudge: str,
    ) -> StructuredGuidance | None:
        """Turn a LoopDetector nudge into a session-memory retry hint.

        Scans the last few step_history entries for what the brain has
        been trying on this (tool, args). Surfaces the failure pattern
        explicitly so the brain doesn't rediscover it turn-by-turn.
        Returns None when we can't find a repeat to reason about.
        """
        recent = self.state.step_history[-6:]
        matching = [s for s in recent if (s.get("tool") or "") == tool]
        if len(matching) < 2:
            return None
        last = matching[-1]
        args = str(last.get("args") or "")[:80]
        result = str(last.get("result") or "")[:80]
        text = (
            f"[RETRY_HINT] {tool}({args}) has repeated in the last "
            f"{len(matching)} calls; last result: {result!r}. "
            "Try a different approach — swap selector, call "
            "browser_click_at with a vision bbox, or "
            "browser_rewind_to_checkpoint if you're past the known-good "
            f"URL ({self.state.best_checkpoint_url or 'none yet'})."
        )
        return StructuredGuidance(
            kind="retry",
            retry_suggestion=text,
            severity="warn" if len(matching) < 3 else "info",
            text=text,
        )

    def _maybe_force_rewind(self) -> StructuredGuidance | None:
        """Promote a 3rd-strike repeated failure to a forced rewind.

        The same (tool, args, result_fingerprint) firing three times in
        the last 6 steps is a strong "the brain is stuck" signal.
        Forcing a single rewind breaks the loop; the disobedience
        guardrail (self._refresh_force_next_tool) prevents us from
        fighting the brain if it ignores the force.
        """
        if self._force_rewind_emitted:
            return None
        if not self.state.best_checkpoint_url:
            return None
        recent = self.state.step_history[-6:]
        if len(recent) < 3:
            return None
        last = recent[-1]
        tool = last.get("tool") or ""
        args = str(last.get("args") or "")[:60]
        result = str(last.get("result") or "")[:60]
        same = [
            s for s in recent
            if (s.get("tool") or "") == tool
            and str(s.get("args") or "")[:60] == args
            and str(s.get("result") or "")[:60] == result
        ]
        if len(same) < 3:
            return None
        self._force_rewind_emitted = True
        text = (
            "[FORCED_REWIND] Three identical failures on "
            f"{tool}({args}). The brain has run out of cheap retries. "
            "Rewinding to the last known-good checkpoint so the plan "
            "can re-approach with fresh vision. Next tool must be "
            "browser_rewind_to_checkpoint."
        )
        return StructuredGuidance(
            kind="retry",
            next_tool="browser_rewind_to_checkpoint",
            severity="force",
            text=text,
        )

    async def _maybe_replan(
        self,
        context: AgentHookContext,
        graph: Any,
        structured: list[StructuredGuidance],
    ) -> None:
        """Rebuild remaining subgoals when the page diverged from plan.

        Fires only when: (a) URL changed since the last observation,
        (b) the active subgoal is stale, (c) the new URL doesn't match
        any existing expected signal, and (d) we haven't replanned in
        the last `_replan_min_gap` iterations. One LLM call per
        rebuild; on failure we silently leave the old graph in place.
        """
        try:
            iteration = getattr(context, "iteration", 0)
        except Exception:
            iteration = 0
        if iteration - self._last_replan_iter < self._replan_min_gap:
            return
        current_url = (self.state.current_url or "").strip()
        if not current_url:
            return
        if current_url == self._last_seen_url:
            return  # URL didn't change — the plan still applies

        # Quick check: does the current URL already satisfy any
        # pending subgoal's expected_signal? If yes, let updater_check
        # handle it on the next pass — no replan needed.
        try:
            from superbrowser_bridge.task_graph import evaluate_signal
            for sg in graph.subgoals.values():
                if sg.status == "done":
                    continue
                for sig in sg.expected_signals:
                    if evaluate_signal(sig, url=current_url):
                        return
        except Exception:
            pass

        try:
            from superbrowser_bridge.task_graph import rebuild_subgoals
            new_graph, reason = await rebuild_subgoals(
                graph,
                task_instruction=self.state.task_instruction or "",
                current_url=current_url,
                url_changed=True,
                vision_resp=getattr(self.state, "_last_vision_response", None),
            )
        except Exception:
            return

        if not reason:
            return  # rebuild no-op'd (no API key, malformed, etc.)

        # Swap the graph and invalidate the vision token so the next
        # mutation blocks on a fresh pass aligned with the new plan.
        self.state.task_graph = new_graph
        self._last_replan_iter = iteration
        self.state.actions_on_active_subgoal = 0
        try:
            self.state.advance_observation_token("replan")
        except Exception:
            pass
        new_active = new_graph.current()
        desc = (new_active.description if new_active else "").strip()
        structured.append(StructuredGuidance(
            kind="replan",
            severity="warn",
            text=(
                f"[SUBGOAL_REPLANNED {reason}] Path adjusted for new URL "
                f"{current_url[:80]}. Now focus on: {desc[:140] or '(complete)'}"
            ),
        ))

    def _update_cursor_success_index(self, last_step: dict | None) -> None:
        """Bump the cursor_success counter for the URL the last step
        ran on. Reads the `[cursor_success:TOOL]` prefix that
        `_maybe_no_effect_prefix` attaches to successful cursor tool
        replies. Called from `after_iteration` once per iteration.
        """
        if not last_step:
            return
        result = str(last_step.get("result") or "")
        if not result.startswith("[cursor_success:"):
            return
        url = str(last_step.get("url") or "")
        if not url:
            return
        self._cursor_success_by_url[url] = (
            self._cursor_success_by_url.get(url, 0) + 1
        )

    def _maybe_emit_patience_warning(
        self,
        structured: list[StructuredGuidance],
        last_step: dict | None,
    ) -> None:
        """When the LAST step was `browser_navigate` AND the brain had
        >= 2 cursor successes on the URL it just left, flag the pivot
        as likely-premature.

        Escalation:
          * 1st nav-away from progress → severity=warn
          * 2nd consecutive → severity=force with next_tool=
            `browser_rewind_to_checkpoint` so the brain goes back to
            the page where it was making progress.
        """
        if not last_step:
            return
        tool = last_step.get("tool") or ""
        # Reset the streak when the brain actually obeys us — i.e.
        # it rewinds to checkpoint, or it uses a cursor tool on the
        # prior URL (came back without a nav). Non-navigate tools
        # otherwise LEAVE the counter alone; the same premature-pivot
        # pattern across two separate flows still escalates cleanly.
        if tool == "browser_rewind_to_checkpoint":
            self._patience_nudge_count = 0
            return
        if tool != "browser_navigate":
            return
        # `browser_navigate`'s step history entry uses the NEW url in
        # its 'url' field. We need the URL it LEFT. Best proxy: the
        # second-to-last step's url.
        steps = self.state.step_history
        if len(steps) < 2:
            return
        prev_url = str(steps[-2].get("url") or "")
        if not prev_url:
            return
        # Only fire when the nav actually changed URL — same-URL navs
        # (refresh) are fine.
        new_url = str(last_step.get("url") or "")
        if prev_url == new_url:
            return
        prior_successes = int(
            self._cursor_success_by_url.get(prev_url, 0) or 0
        )
        if prior_successes < 2:
            # Not enough cursor progress on the previous URL to call
            # this a premature pivot.
            self._patience_nudge_count = 0
            return

        self._patience_nudge_count += 1
        if (
            self._patience_nudge_count >= 2
            and self.state.best_checkpoint_url
            and not self._force_rewind_emitted
        ):
            self._force_rewind_emitted = True
            structured.append(StructuredGuidance(
                kind="retry",
                next_tool="browser_rewind_to_checkpoint",
                severity="force",
                text=(
                    f"[FORCED_REWIND patience] You navigated away from "
                    f"{prev_url[:80]} (where you had "
                    f"{prior_successes} successful cursor actions) TWICE. "
                    f"That's giving up, not planning. Rewinding to the "
                    f"last checkpoint ({self.state.best_checkpoint_url})."
                ),
            ))
        else:
            structured.append(StructuredGuidance(
                kind="retry",
                severity="warn",
                text=(
                    f"[PATIENCE] You navigated away from {prev_url[:80]} "
                    f"after {prior_successes} successful cursor actions "
                    f"there. That's usually a give-up (\"let me try a "
                    f"different URL\") rather than a real plan step. "
                    f"If the page had a multi-step flow (autocomplete → "
                    f"calendar → time → search), CONTINUE ON THE ORIGINAL "
                    f"PAGE via browser_rewind_to_checkpoint or just "
                    f"pressing the back button — starting over at a new "
                    f"URL will likely land you right back here."
                ),
            ))

    def _maybe_emit_blocker_guidance(
        self,
        structured: list[StructuredGuidance],
        last_step: dict | None,
    ) -> None:
        """Proactively force a dismiss when the fused perception shows
        the page is walled.

        Detection goes through `perception_fusion.detect_active_blocker`,
        which layers four signals (scene graph → flags → page_type →
        sparse-page heuristic). The old version only watched the scene
        graph and waited two turns before forcing — that let the brain
        detour through `navigate` / `run_script` / `get_markdown` on the
        first turn, which is exactly the hallucination pattern the user
        flagged. We now **force on turn 1** whenever a blocker is
        detected AND the brain isn't already trying to dismiss it.

        Resets when:
          - The blocker disappears from the fused perception, OR
          - The last tool was already a cursor dismiss whose label
            matches the dismiss_hint (the brain IS complying; we wait
            to see if it lands before re-escalating).
        """
        from superbrowser_bridge.perception_fusion import (
            detect_active_blocker,
            _label_overlap,
        )

        resp = getattr(self.state, "_last_vision_response", None)
        info = detect_active_blocker(resp)
        if info is None or not info.dismiss_hint:
            self._blocker_nudge_count = 0
            self._blocker_last_hint = ""
            return

        hint = info.dismiss_hint

        # If the last tool was already an attempted dismiss, don't stack
        # another force on top. The post-action effect classifier will
        # decide whether the wall came down; if it's still up next
        # turn, this branch fires fresh.
        last_tool = (last_step or {}).get("tool") or ""
        last_args = str((last_step or {}).get("args") or "")
        if last_tool in {"browser_semantic_click", "browser_click_at",
                         "browser_click", "browser_click_selector"}:
            # Heuristic: if the args text mentions the dismiss_hint,
            # the brain is complying. Reset the counter.
            if _label_overlap(last_args, hint) >= 0.4:
                self._blocker_nudge_count = 0
                self._blocker_last_hint = ""
                return
            # Brain clicked something, but not the dismiss. Escalate
            # harder below.

        self._blocker_nudge_count += 1
        self._blocker_last_hint = hint

        # Always force from turn 1 — reactive/warn mode was exactly
        # what let the brain burn iterations on route-around tactics.
        # The validator's blocker-unaddressed gate gives the brain a
        # second chance to comply if it ignores this; they complement.
        text = (
            f"[BLOCKER_DETECTED source={info.source} "
            f"reason={info.reason or '-'}] A wall is covering the page. "
            f"dismiss_hint='{hint}'. Your ONLY valid next tool is "
            f"browser_semantic_click(target='{hint}'). Do NOT call any "
            f"other tool (no navigate, no run_script, no get_markdown) "
            f"until this is dismissed — the validator will reject "
            f"anything else on this frame as blocker_unaddressed."
        )
        structured.append(StructuredGuidance(
            kind="blocker",
            next_tool="browser_semantic_click",
            param_override={"target": hint},
            severity="force",
            text=text,
        ))

    def _refresh_force_next_tool(
        self, structured: list[StructuredGuidance],
    ) -> None:
        """Stash the single highest-severity force on state; respect the
        disobedience guardrail."""
        # If a prior force was set but the brain chose a different tool
        # on its last action, honor that choice and drop the force.
        prior = getattr(self.state, "_forced_next_tool", None)
        last_step = self.state.step_history[-1] if self.state.step_history else None
        if prior and last_step:
            last_tool = last_step.get("tool") or ""
            if last_tool and last_tool != prior:
                # Brain ignored us once — don't keep fighting.
                self.state._forced_next_tool = None  # type: ignore[attr-defined]
                prior = None
        forced = next(
            (g for g in structured if g.severity == "force" and g.next_tool),
            None,
        )
        if forced is not None:
            self.state._forced_next_tool = forced.next_tool  # type: ignore[attr-defined]
