"""
Mid-session guardrails for the browser worker agent.

Uses the nanobot AgentHook lifecycle to inject corrective guidance
into the conversation when the worker goes off-track (click-screenshot
loops, regression navigation, stagnation, iteration budget pressure).

Guidance is injected by appending text to the last tool result message,
preserving the assistant/tool message alternation expected by LLM APIs.

Phase 3 — context eviction: before injecting fresh guidance each turn,
older tool messages have their stale ``[VISION ...]`` / ``[BRIEF ...]`` /
``[CHECKLIST]`` / ``[FOCUS_BBOX]`` / ``[ACTION_DELTA]`` etc. blocks
stripped and replaced with a one-line placeholder. The brain doesn't
need to remember the bbox list from 4 turns ago — only the action that
was taken.
"""

from __future__ import annotations

import os
import re
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.loop_detector import LoopDetector


# Evictable tag set — bracketed CAPS markers that worker_hook or the
# vision pipeline emit and that go stale once the next screenshot
# lands. Tool-error markers like [click_at_failed] / [BLOCKED] /
# [click_silent] are deliberately lowercase or have non-CAPS prefixes
# in this codebase, so they're naturally excluded — they carry signal
# the brain still needs even after eviction.
_EVICTABLE_TAG_RE = re.compile(
    r"(?:^|\n)\["
    r"(?:"
    r"VISION"
    r"|BRIEF(?:_PROGRESS)?"
    r"|CHECKLIST"
    r"|FOCUS_BBOX"
    r"|FOCUS_EXHAUSTED"
    r"|PLAN"
    r"|ACTION_DELTA"
    r"|V_PRIORITY"
    r"|CURSOR_FAILURES_SO_FAR"
    r"|GUIDANCE"
    r"|PLANNER_GUIDANCE"
    r"|CACHED VISION"
    r")"
    r"[^\]]*\]"
)


def _evict_stale_text(text: str, placeholder: str) -> str:
    """Truncate `text` at the first evictable bracketed tag and append
    `placeholder`. Returns the original `text` unchanged when no
    evictable tag is found (read-only tool results, plain action
    summaries with no vision/brief decoration).
    """
    if not text:
        return text
    m = _EVICTABLE_TAG_RE.search(text)
    if m is None:
        return text
    head = text[: m.start()].rstrip()
    # Cap the surviving head — older tool results sometimes include
    # multi-kilobyte page text dumps in the prefix. Keep enough for
    # the brain to know what the action did, drop the rest.
    if len(head) > 240:
        head = head[:240].rstrip() + "…"
    return f"{head}\n{placeholder}" if head else placeholder


def _step_one_liner(step: dict[str, Any] | None) -> str:
    """Render a step_history entry as a single-line eviction placeholder.

    The brain's continuity comes from this line — it's all that's left
    of the turn after eviction. Format: ``[step K: tool(args) → result]``.
    """
    if not step:
        return "[evicted: stale guidance trimmed]"
    tool = (step.get("tool") or "?").strip()
    args = (step.get("args") or "").strip()
    if len(args) > 60:
        args = args[:60].rstrip() + "…"
    result = (step.get("result") or "").strip().split("\n")[0]
    if len(result) > 80:
        result = result[:80].rstrip() + "…"
    return f"[evicted: {tool}({args}) → {result}]"


def _evict_old_tool_messages(
    messages: list[dict[str, Any]],
    state: BrowserSessionState,
    keep_turns: int,
) -> None:
    """In-place strip stale guidance from tool messages older than the
    keep window. The most recent `keep_turns` tool results stay fully
    intact (the brain's V_n indices resolve against the most recent
    [VISION] block; the focus block on it is also still load-bearing).

    Order matters: this runs BEFORE the guidance-injection loop in
    after_iteration. If you reverse the order, fresh guidance gets
    immediately stripped because eviction can't tell new from old.
    """
    if keep_turns < 1 or not messages:
        return
    tool_age = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        age = tool_age
        tool_age += 1
        if age < keep_turns:
            continue
        # Map tool-message age back to step_history. step_history
        # appends one entry per tool that called record_step, so the
        # mapping is best-effort: walk backward `age` entries.
        step = None
        try:
            if state.step_history and -(age + 1) >= -len(state.step_history):
                step = state.step_history[-(age + 1)]
        except Exception:
            step = None
        placeholder = _step_one_liner(step)
        content = msg.get("content")
        if isinstance(content, str):
            new = _evict_stale_text(content, placeholder)
            if new != content:
                msg["content"] = new
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                ):
                    txt = block.get("text", "") or ""
                    new = _evict_stale_text(txt, placeholder)
                    if new != txt:
                        block["text"] = new


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
        # No-progress detector for task brief. When the brief version
        # hasn't bumped for several iterations, the brain is stuck or
        # rushing toward a wrong action — emit a guidance line forcing
        # it to articulate which constraint it's trying to advance.
        self._last_brief_version_seen: int = 0
        self._last_brief_progress_iter: int = -1
        self._no_progress_nudged_at: int = -1
        # [BRIEF_PROGRESS] tracking. Set of constraint ids done at the
        # END of the previous iteration; we diff against the current
        # set each turn to attribute newly-flipped items. Also tracks
        # the iteration when the brief last advanced so the brain
        # sees a stagnation counter ("3 turns stagnant").
        self._prev_done_ids: set[int] = set()
        self._stagnant_turns: int = 0
        # Generic loop + stagnation detector (replaces consecutive_click_calls +
        # ad-hoc _stagnation_url/_stagnation_count logic).
        self._loop = LoopDetector()
        # Tier-auto-escalation: fires at most once per session to avoid
        # loop-cascading the LLM into repeated escalations.
        self._auto_escalated: bool = False
        # Phase 6 Planner re-plan tracking. When _stagnant_turns crosses
        # _PLANNER_STALL_THRESHOLD, call Planner.replan(...). Cooldown
        # via _last_planner_replan_iter to prevent re-plan spam (one
        # call per 8 iterations max).
        self._last_planner_replan_iter: int = -1
        self._planner_replan_count: int = 0
        self._planner_advice_pending: str = ""

    _PLANNER_STALL_THRESHOLD: int = 5  # turns of no progress
    _PLANNER_REPLAN_COOLDOWN: int = 8  # iterations between replans
    _PLANNER_MAX_REPLANS: int = 3  # per session — abandon after this

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

        # --- Multi-condition task brief — reconcile + pin ---------------
        # When the orchestrator pre-decomposed the user's query into a
        # checklist (`delegate_browser_task(task_checklist=[...])`), the
        # worker_state carries a TaskBrief here. Each iteration we:
        #   1. Auto-flip filter constraints whose URL or page_text
        #      predicates match the current state.
        #   2. Pin the up-to-date [BRIEF] / [FOCUS] / [CHECKLIST] blocks
        #      onto the next tool result so the brain can't lose its
        #      constraints as the conversation lengthens.
        #   3. Re-broadcast the original query verbatim every 5 iters
        #      (and at the iteration-budget warning thresholds) so the
        #      free-text framing stays in recent context too — labels
        #      lose nuance the original prose preserves.
        brief = getattr(self.state, "task_brief", None)
        if brief is not None and getattr(brief, "constraints", None):
            # Per-turn brief pin is opt-in. The pin used to fire on every
            # tool result, which created LLM pressure to "advance the
            # focus" even when the page state didn't support progress —
            # producing hallucinated URLs and premature done() marks.
            # Default OFF: brief still attaches once at task start (in the
            # delegate prompt) and is consulted at done() time via
            # reconciliation, but the per-turn drumbeat is silenced.
            # Re-enable with SHOW_BRIEF_PER_TURN=1 for tasks that need
            # the constraint checklist visible every iteration.
            pin_brief_per_turn = (
                os.environ.get("SHOW_BRIEF_PER_TURN", "0")
                in ("1", "true", "yes", "True")
            )
            v_before = brief.version
            try:
                brief.reconcile_from_url(self.state.current_url or "")
            except Exception as exc:
                print(f"[brief] reconcile_from_url failed: {exc}")
            try:
                brief.reconcile_from_page_state(
                    getattr(self.state, "_last_vision_response", None),
                    getattr(self.state, "_last_markdown", "") or "",
                )
            except Exception as exc:
                print(f"[brief] reconcile_from_page_state failed: {exc}")
            # Log once per iteration so trace logs make brief progress visible.
            if brief.version != v_before:
                # State changed this turn — print which item flipped.
                print(brief.diagnostic_line())
                for c in brief.constraints:
                    if c.status == "done" and c.evidence:
                        # Cheap dedup: only print rows whose evidence is
                        # fresh-looking. We don't track per-iter flips
                        # explicitly, so this just prints the current
                        # done set; it's noisy but tractable.
                        pass  # full state is implied by diagnostic_line
            elif iteration % 5 == 0:
                # Periodic snapshot even when nothing flipped, so logs
                # have a regular pulse on long runs.
                print(brief.diagnostic_line())

            if pin_brief_per_turn:
                guidance_parts.append(brief.render_brief())
                focus_line = brief.render_focus()
                if focus_line:
                    guidance_parts.append(focus_line)
                # FOCUS_BBOX — pre-computes which V_n in the latest vision
                # response best matches the current focus constraint, so
                # the brain doesn't have to do that mapping mentally on
                # every turn. Empty string when no vision data; the hook
                # then skips this block.
                try:
                    focus_bbox = brief.render_focus_bbox(
                        getattr(self.state, "_last_vision_response", None)
                    )
                    if focus_bbox:
                        guidance_parts.append(focus_bbox)
                except Exception as exc:
                    print(f"[brief] render_focus_bbox failed: {exc}")
                checklist_block = brief.render_checklist()
                if checklist_block:
                    guidance_parts.append(checklist_block)

            # BRIEF_PROGRESS — per-iteration narrative of what the last
            # action accomplished. The brain saw [BRIEF v=N] go up but
            # didn't have a one-line "did this work?" feedback. Now it
            # does, with stagnation counting so chronic-stuck states
            # become explicit rather than implicit.
            current_done_ids = {
                c.id for c in brief.constraints if c.status == "done"
            }
            new_done = current_done_ids - self._prev_done_ids
            if new_done:
                self._stagnant_turns = 0
                if pin_brief_per_turn:
                    # Render with the constraint label + evidence for the
                    # newly-done items. Cap to 3 to keep the line short.
                    bits: list[str] = []
                    for cid in sorted(new_done)[:3]:
                        c = next((x for x in brief.constraints if x.id == cid), None)
                        if c is None:
                            continue
                        ev = c.evidence[:40] if c.evidence else "manual mark"
                        bits.append(f"#{cid} {c.label[:30]} ({ev})")
                    f = brief.next_focus()
                    next_focus_str = (
                        f"next focus #{f.id} {f.label[:40]!r}"
                        if f
                        else "(all constraints terminal)"
                    )
                    guidance_parts.append(
                        f"[BRIEF_PROGRESS] iter {iteration}: "
                        f"+{len(new_done)} done ({'; '.join(bits)}); "
                        f"{next_focus_str}"
                    )
            else:
                # Real-progress carve-out. The brief's stagnation
                # counter is meant to detect "no progress on the focus."
                # But the brain may legitimately make page-state
                # progress (expand an accordion, scroll into a section,
                # type into a field) that doesn't immediately flip a
                # brief predicate. Reading state.last_action_delta lets
                # us reset the counter when the page genuinely changed
                # this turn — without it, [NO_PROGRESS_4] would unfairly
                # nag the brain for working through a multi-step UI.
                last_delta_info = getattr(self.state, "last_action_delta", None)
                made_real_progress = False
                if (
                    last_delta_info
                    and isinstance(last_delta_info.get("delta"), dict)
                ):
                    d = last_delta_info["delta"]
                    if (
                        d.get("url_changed")
                        or d.get("dom_changed")
                        or d.get("target_disappeared")
                        or abs(int(d.get("elem_delta") or 0)) >= 3
                    ):
                        made_real_progress = True
                if made_real_progress:
                    self._stagnant_turns = 0
                else:
                    self._stagnant_turns += 1
                if pin_brief_per_turn:
                    f = brief.next_focus()
                    if f is not None:
                        if made_real_progress:
                            guidance_parts.append(
                                f"[BRIEF_PROGRESS] iter {iteration}: "
                                f"0 brief items advanced but page state "
                                f"changed (see [ACTION_DELTA] above); "
                                f"focus still #{f.id} {f.label[:40]!r}"
                            )
                        else:
                            stag_note = (
                                " — CONSIDER alternatives"
                                if self._stagnant_turns >= 4
                                else ""
                            )
                            guidance_parts.append(
                                f"[BRIEF_PROGRESS] iter {iteration}: "
                                f"0 advanced; focus still #{f.id} "
                                f"{f.label[:40]!r} ({self._stagnant_turns} "
                                f"turns stagnant){stag_note}"
                            )
            self._prev_done_ids = current_done_ids

            # --- Phase 6: Planner stall-replan -------------------------
            # Surface any pending advice from the previous turn's
            # replan FIRST so the brain sees it as a header on this
            # turn's results (the advice was generated AFTER its last
            # action; it should appear BEFORE its next action).
            if self._planner_advice_pending:
                guidance_parts.append(
                    f"[PLANNER_GUIDANCE] {self._planner_advice_pending}"
                )
                self._planner_advice_pending = ""

            # Trigger a replan when the worker has stagnated past the
            # threshold and the cooldown has elapsed. Bounded to
            # _PLANNER_MAX_REPLANS per session — beyond that, repeated
            # replans usually mean the task is genuinely unreachable
            # and we should let the iteration budget run out so the
            # orchestrator surfaces the partial result.
            if (
                os.environ.get("PLANNER_REPLAN_ENABLED", "1") not in ("0", "false", "no")
                and self._stagnant_turns >= self._PLANNER_STALL_THRESHOLD
                and self._planner_replan_count < self._PLANNER_MAX_REPLANS
                and (
                    self._last_planner_replan_iter < 0
                    or (iteration - self._last_planner_replan_iter)
                    >= self._PLANNER_REPLAN_COOLDOWN
                )
            ):
                advice = await self._maybe_replan(iteration, brief)
                if advice:
                    # Show the advice IMMEDIATELY on this turn (the
                    # injection loop below will append guidance_parts
                    # to the last tool result). Also stash for next
                    # turn so the brain sees it framed as forward-
                    # looking guidance even if it ignored this round.
                    guidance_parts.append(f"[PLANNER_GUIDANCE] {advice}")
                    self._planner_advice_pending = advice

            # --- Per-focus attempt ledger + [FOCUS_EXHAUSTED] -----------
            # Record the most recent step on the currently-focused
            # constraint. Then check if we've crossed the warn (3) or
            # mandatory (5) thresholds and emit the directive once per
            # threshold. The reactive guards (click crosscheck, repeat-
            # type, filter-hack) catch individual bad calls; this catches
            # the *cumulative* "I keep trying the wrong tool family on
            # the same focus" pattern that those guards miss.
            focus_after = brief.next_focus()
            if focus_after is not None and last_step is not None:
                try:
                    brief.record_attempt(
                        tool=last_step.get("tool") or "",
                        target=last_step.get("args") or "",
                        result=last_step.get("result") or "",
                        iteration=iteration,
                    )
                except Exception as exc:
                    print(f"[brief] record_attempt failed: {exc}")
                # Emit once per threshold per focus. The Constraint
                # itself owns the de-dup set so re-runs of the focus
                # (after a forced-failed mark, etc.) get a fresh slate.
                if pin_brief_per_turn:
                    failed_n = brief.failed_attempts_on(focus_after.id)
                    for threshold in (3, 5):
                        if failed_n >= threshold:
                            block = brief.render_focus_exhausted(
                                focus_after.id, threshold
                            )
                            if block:
                                guidance_parts.append(block)

            # --- Post-rewind observation gate ---------------------------
            # browser_rewind_to_checkpoint sets state.rewind_just_fired.
            # Until the brain takes a deliberation action (screenshot /
            # markdown / brief_mark), refuse-by-warning any other tool
            # call. This closes the rewind→hallucinated-navigate loop
            # observed in the wineaccess.com trace.
            if getattr(self.state, "rewind_just_fired", False):
                _DELIBERATION_TOOLS = {
                    "browser_screenshot",
                    "browser_get_markdown",
                    "browser_brief_mark",
                }
                last_tool = (last_step or {}).get("tool") or ""
                # The rewind step itself doesn't count as the "next" call
                # — clear the flag only when a downstream tool runs.
                if last_tool == "browser_rewind_to_checkpoint":
                    pass
                elif last_tool in _DELIBERATION_TOOLS:
                    self.state.rewind_just_fired = False
                elif last_tool:
                    # Brain skipped re-observation. Emit the warning and
                    # clear the flag — one-shot, the warning is loud
                    # enough that re-emitting on every subsequent turn
                    # would be noise.
                    guidance_parts.append(
                        f"[REWIND_NOT_OBSERVED] You called "
                        f"{last_tool!r} immediately after a rewind without "
                        f"taking a browser_screenshot, "
                        f"browser_get_markdown, or browser_brief_mark "
                        f"first. The rewind invalidated all V_n indices "
                        f"and DOM fingerprints; whatever you did just now "
                        f"was operating on stale assumptions. Take a "
                        f"screenshot before the next mutation."
                    )
                    self.state.rewind_just_fired = False

            # --- Clear per-focus navigate lockout on deliberation -------
            # When the brain re-grounds itself (screenshot / markdown /
            # brief_mark), the prior nav refusal no longer applies — the
            # brain has fresh evidence and may legitimately retry navigate.
            if getattr(self.state, "last_navigate_refusal_focus_id", None) is not None:
                _DELIB_TOOLS = {
                    "browser_screenshot",
                    "browser_get_markdown",
                    "browser_brief_mark",
                }
                last_tool = (last_step or {}).get("tool") or ""
                # Also clear when the focus has advanced past the locked id.
                cur_focus = brief.next_focus()
                cur_focus_id = cur_focus.id if cur_focus else None
                if last_tool in _DELIB_TOOLS:
                    self.state.last_navigate_refusal_focus_id = None
                elif cur_focus_id != self.state.last_navigate_refusal_focus_id:
                    self.state.last_navigate_refusal_focus_id = None

            # Periodic full-text replay. Triggers on the iteration
            # budget warning thresholds (already computed above) and
            # every 5th iteration thereafter — so the original phrasing
            # of the query stays in cache-warm context even on long runs.
            replay_due = (
                (iteration > 0 and iteration % 5 == 0)
                or remaining <= int(self.max_iterations * 0.4)
            )
            if replay_due and brief.original_query:
                guidance_parts.append(
                    f"[TASK_REMINDER iteration={iteration}] "
                    f"Original query (do not drop any condition):\n"
                    f"{brief.original_query}\n"
                    f"Open constraints: {brief.open_count()} of "
                    f"{len(brief.constraints)}."
                )

            # Strong-stagnation guidance: when the new BRIEF_PROGRESS
            # diff has reported "stagnant" for 4+ turns, escalate from
            # the per-line counter to a full guidance block once. The
            # detailed instructions below are useful but cost tokens
            # — only emit on the first turn the threshold is crossed
            # so we don't spam the brain on every subsequent stagnant
            # turn.
            if (
                self._stagnant_turns == 4
                and brief.open_count() > 0
            ):
                f = brief.next_focus()
                focus_label = f.label if f else "(none)"
                guidance_parts.append(
                    f"[NO_PROGRESS_4] The brief checklist has not "
                    f"advanced in 4 iterations. You are either stuck "
                    f"or rushing toward an action that does NOT "
                    f"advance the [FOCUS] item.\n"
                    f"Before your next tool call, state to yourself "
                    f"in concrete terms:\n"
                    f"  • Which V_n bbox would flip constraint "
                    f"{focus_label!r} to [done]? Check the "
                    f"[FOCUS_BBOX] block above for the recommendation.\n"
                    f"  • If the recommended V_n doesn't exist on "
                    f"this page, scroll (browser_scroll_until) or "
                    f"expand a collapsed section.\n"
                    f"Do NOT navigate to a constructed URL; do NOT "
                    f"open a detail page; do NOT JS-click via "
                    f"browser_run_script. The constraint advances by "
                    f"clicking the actual filter UI vision shows you."
                )

        # --- Phase 2: form-fill checklist reminder ----------------------
        # While a form_session is active, remind the brain at every
        # iteration which fields still need filling. The session itself
        # tracks state via browser_type_at + form_commit; this hook is
        # the persistent visual nudge so the brain can't forget the
        # field hidden behind an autocomplete dropdown.
        form_sess = getattr(self.state, "form_session", None)
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

        # --- Phase 3.1 + Phase E: cursor-failure ledger -----------------
        # Stage 1 (existing): when distinct < 2, nudge to try a different
        # cursor strategy.
        # Stage 2 (Phase E1): when records ≥ 3 within recent turns AND
        # last screenshot is stale, set a force-reobserve flag that
        # cursor.py and forms.py refuse against until a fresh
        # screenshot lands.
        # Stage 3 (Phase E2): when records ≥ 3, render the last 3 with
        # concrete remediation pointing at browser_find_target.
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
            # Phase E2: concrete remediation when ≥3 records.
            if len(recs) >= 3:
                last_three = recs[-3:]
                lines = ["[CURSOR_CASCADE last 3 failures]"]
                for rec in last_three:
                    lines.append(
                        f"  · {rec.get('strategy','?')}({(rec.get('target') or '')[:40]}) "
                        f"→ {(rec.get('reason') or '')[:60]}"
                    )
                lines.append(
                    "Stop guessing — switch tactic. Best next moves:\n"
                    "  1. browser_find_target(label='<what you want>', "
                    "section='<filter section name if any>') — locates "
                    "the target across viewport + collapsed accordions, "
                    "auto-scrolls. This is the canonical fix for the "
                    "'I know it's there but no V_n labels it' loop.\n"
                    "  2. browser_get_markdown(outline=true) to see the "
                    "page section structure with viewport-relative y "
                    "offsets.\n"
                    "  3. browser_screenshot if your last vision is stale "
                    "(>3 turns old) — fresh V_n labels."
                )
                guidance_parts.append("\n".join(lines))
            # Phase E1: force-reobserve gate when cascade is real.
            self._maybe_set_force_reobserve(recs)
        except Exception:
            pass

        # --- Phase E (auto-call find_target) ----------------------------
        # When the brain searched for a focus label but found no V_n
        # match in the last vision pass AND the label IS in the cached
        # markdown, proactively inject a [AUTO_FIND_TARGET] block with
        # the structured candidate info. The brain still has to call
        # find_target itself (we don't run mutating tools on its
        # behalf), but the guidance includes the exact recommended
        # call. This is the "I know it's there but vision missed it"
        # pattern from the wineaccess trace.
        try:
            self._inject_auto_find_target_hint(guidance_parts)
        except Exception as exc:
            print(f"[auto_find_target hint failed: {exc}]")

        # --- Phase 3: evict stale guidance/vision blocks from older
        # tool results BEFORE injecting fresh guidance. Strip-then-inject
        # ordering is critical — the inverse path strips fresh guidance
        # immediately because the eviction pass can't tell new from old.
        try:
            keep_turns = int(
                os.environ.get("SUPERBROWSER_GUIDANCE_KEEP_TURNS") or "3"
            )
        except ValueError:
            keep_turns = 3
        if keep_turns >= 1 and context.messages:
            _evict_old_tool_messages(
                context.messages, self.state, keep_turns,
            )

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

    async def _maybe_replan(
        self,
        iteration: int,
        brief: Any,
    ) -> str:
        """Call Planner.replan and install the revised brief.

        Returns advice text to surface as ``[PLANNER_GUIDANCE]``, or
        an empty string when the planner declined to revise (no
        credentials, parse failure, judge said "stay the course").

        Side effects:
            * ``self.state.task_brief`` may be replaced with a fresh
              brief constructed from the planner's revised steps.
            * ``self._last_planner_replan_iter`` is updated regardless
              of outcome — failed replans count toward the cooldown
              so we don't hammer the API on repeat failures.
        """
        self._last_planner_replan_iter = iteration
        try:
            from superbrowser_bridge.planner_agent import default_planner
            planner = default_planner()
            if not planner.enabled:
                return ""
            recent_steps = (
                getattr(self.state, "step_history", []) or []
            )[-10:]
            last_vision = ""
            try:
                resp = getattr(self.state, "_last_vision_response", None)
                last_vision = (getattr(resp, "summary", "") or "") if resp else ""
            except Exception:
                last_vision = ""
            brief_text = ""
            try:
                brief_text = brief.render_for_prompt()
            except Exception:
                brief_text = "(brief unavailable)"
            task = (
                getattr(self.state, "task_instruction", None)
                or getattr(brief, "original_query", "")
                or ""
            )
            result = await planner.replan(
                task=task,
                brief_render=brief_text,
                recent_steps=recent_steps,
                last_vision_summary=last_vision,
                stall_reason=f"stagnant_turns={self._stagnant_turns}",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            print(f"[planner replan failed: {exc}]")
            return ""

        if result.abandon:
            self._planner_replan_count += 1
            return (
                f"Planner judges the goal unreachable from here: "
                f"{result.reason} — call done(success=False) and "
                "report what you learned."
            )
        if not result.steps:
            return ""

        # Install the revised brief in place. Keep the original_query
        # but bump version semantics by reusing set_task_brief which
        # constructs a fresh TaskBrief.
        try:
            original_query = (
                getattr(brief, "original_query", "")
                or getattr(self.state, "task_instruction", "")
                or ""
            )
            self.state.set_task_brief(original_query, result.steps)
            # Reset stagnation counter so the new brief gets a fair
            # start; otherwise the brain immediately sees [NO_PROGRESS_4]
            # against a brief that's only 1 turn old.
            self._stagnant_turns = 0
            self._prev_done_ids = set()
            self._planner_replan_count += 1
            print(
                f"[planner replanned iter={iteration} steps={len(result.steps)} "
                f"replan_count={self._planner_replan_count}]"
            )
        except Exception as exc:
            print(f"[planner brief install failed: {exc}]")
            return ""

        labels = "; ".join(
            f"#{i+1} {s['label'][:50]}"
            for i, s in enumerate(result.steps[:6])
        )
        return (
            f"Stalled for {self._stagnant_turns}+ turns; revised plan "
            f"installed ({len(result.steps)} steps): {labels}. "
            "Re-read the [BRIEF] / [CHECKLIST] blocks above and pursue "
            "the new focus item."
        )

    def _maybe_set_force_reobserve(self, recs: list[dict]) -> None:
        """Phase E1: when cursor failures pile up without a fresh
        screenshot, set ``state._force_reobserve_pending`` so the
        cursor / forms / eval tools refuse until a screenshot lands.

        Trigger: 3+ failures in the last 6 brain turns AND last
        vision epoch is older than 3 turns. The cap on turn-window
        prevents the gate from firing on stale failures from much
        earlier in the session.
        """
        if not recs:
            return
        current_turn = self.state._brain_turn_counter
        window_turns = 6
        recent_failures = [
            r for r in recs
            if (current_turn - int(r.get("turn", 0))) <= window_turns
        ]
        if len(recent_failures) < 3:
            # Reset gate when cascade clears.
            if getattr(self.state, "_force_reobserve_pending", False):
                self.state._force_reobserve_pending = False
            return
        epoch_turn = getattr(self.state, "_vision_epoch_turn", 0) or 0
        epoch_age_turns = current_turn - int(epoch_turn)
        if epoch_age_turns >= 3:
            self.state._force_reobserve_pending = True

    def _inject_auto_find_target_hint(self, guidance_parts: list[str]) -> None:
        """Phase E (auto find_target): detect "label is in markdown but
        no V_n labels it" and inject a structured ``[AUTO_FIND_TARGET]``
        block with the recommended call.

        We don't actually CALL find_target on the brain's behalf — the
        brain decides. But we surface enough info that the next turn's
        choice is obvious: "you want X, X is at y=1240px in the
        Region accordion (collapsed), here's the recommended call."
        """
        brief = getattr(self.state, "task_brief", None)
        if brief is None:
            return
        focus = brief.next_focus()
        if focus is None:
            return
        focus_label = (focus.label or "").strip()
        if len(focus_label) < 3:
            return
        # Did the most recent vision pass have any bbox roughly matching
        # this focus label? If yes, the brain already has a V_n to use —
        # no need to nudge.
        resp = getattr(self.state, "_last_vision_response", None)
        if resp is not None:
            label_lower = focus_label.lower()
            # Strip generic "filter" / "section" descriptor words that
            # appear in TaskBrief labels but rarely in bbox labels — the
            # brief says "Oregon filter" while vision says "Oregon (12)".
            content_tokens = [
                t for t in _word_tokens(label_lower)
                if t not in _GENERIC_FOCUS_WORDS and len(t) >= 3
            ]
            for b in (getattr(resp, "bboxes", []) or []):
                bbox_label = (getattr(b, "label", "") or "").strip().lower()
                if not bbox_label:
                    continue
                if label_lower in bbox_label or bbox_label in label_lower:
                    return
                # Strong signal: any content-bearing focus token appears
                # in the bbox label as a whole word.
                bbox_tokens = set(_word_tokens(bbox_label))
                if content_tokens and any(t in bbox_tokens for t in content_tokens):
                    return
                # Token-overlap fallback (Jaccard ≥ 0.5) on full token
                # sets — catches cases where the focus has multiple
                # content tokens and most of them are in the bbox.
                a_tokens = set(_word_tokens(label_lower))
                if a_tokens and bbox_tokens:
                    overlap = len(a_tokens & bbox_tokens)
                    union = len(a_tokens | bbox_tokens)
                    if union and overlap / union >= 0.5:
                        return
        # No vision match — does markdown contain the label?
        md = (getattr(self.state, "_last_markdown", "") or "").lower()
        if not md:
            return
        focus_lower = focus_label.lower()
        # Token-level check on markdown to avoid false positives. The
        # focus has to share at least 50% of its tokens with the
        # markdown, AND at least one token must be present as a whole
        # word.
        focus_tokens = set(_word_tokens(focus_lower))
        if not focus_tokens:
            return
        md_present = sum(1 for t in focus_tokens if t in md)
        if md_present == 0:
            return
        if md_present / len(focus_tokens) < 0.5:
            return
        # Don't re-emit on every iteration. Track when we last fired so
        # the brain has a turn or two to act before we nag again.
        last_fired = getattr(self, "_last_auto_find_iter", -100)
        current_iter = self.state._brain_turn_counter
        if current_iter - last_fired < 3:
            return
        self._last_auto_find_iter = current_iter
        # Build a recommended call. If the brief's brief.original_query
        # mentions a section keyword (Region/Filter/Variety/etc.), use
        # it as the section= hint.
        section_hint = _guess_section_hint(focus_label)
        rec = (
            f"browser_find_target(label={focus_label!r}"
            + (f", section={section_hint!r}" if section_hint else "")
            + ")"
        )
        guidance_parts.append(
            f"[AUTO_FIND_TARGET focus={focus_label!r}]\n"
            f"  Your current focus is {focus_label!r}, which appears "
            "in the page markdown but has no matching V_n in the last "
            "vision pass. The target is likely below the fold OR in a "
            "collapsed accordion section. RECOMMENDED next call:\n"
            f"  {rec}\n"
            "find_target searches markdown + DOM for the label, detects "
            "collapsed sections, auto-scrolls into view, and returns a "
            "structured next-action hint. Don't keep guessing V_n on a "
            "stale screenshot or fabricating URL params."
        )


# Module-level helpers for _inject_auto_find_target_hint.
import re as _re_word_helper


# Generic descriptor words that appear in TaskBrief labels but rarely
# in vision bbox labels — strip these from focus before token matching
# so "Oregon filter" matches "Oregon (12)" via the content-token path.
_GENERIC_FOCUS_WORDS: frozenset[str] = frozenset({
    "filter", "filters", "section", "sidebar", "panel",
    "by", "of", "the", "a", "an", "for", "with",
    "click", "select", "open", "expand", "show", "view",
    "and", "or", "to", "from", "in", "at", "on",
})


def _word_tokens(text: str) -> list[str]:
    return _re_word_helper.findall(r"\w+", text or "")


def _guess_section_hint(focus_label: str) -> str:
    """Heuristic: extract a section keyword from the focus label.

    'Oregon filter' → 'Region'? Too aggressive — we'd have to know
    the site's taxonomy. Better: return any single-word noun in the
    label that maps to a known filter category. For now we look for
    explicit section words.
    """
    lower = (focus_label or "").lower()
    # Word -> common section heading mapping. Keep small; bigger
    # mappings encourage the brain to trust this guess.
    HINTS = {
        "white": "Type",
        "red": "Type",
        "rosé": "Type",
        "rose": "Type",
        "oregon": "Region",
        "california": "Region",
        "france": "Region",
        "italy": "Region",
        "fish": "Pairings",
        "dessert": "Pairings",
        "chicken": "Pairings",
        "beef": "Pairings",
        "vegetarian": "Pairings",
        "price": "Price",
        "rating": "Rating",
    }
    for word, section in HINTS.items():
        if word in lower:
            return section
    return ""
