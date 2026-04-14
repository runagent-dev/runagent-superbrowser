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

    def __init__(self, state: BrowserSessionState, max_iterations: int = 25):
        self.state = state
        self.max_iterations = max_iterations
        self._last_budget_warning_at: int = -1  # iteration of last warning
        self._captcha_guidance_given: bool = False
        self._captcha_solve_attempts: int = 0
        # Generic loop + stagnation detector (replaces consecutive_click_calls +
        # ad-hoc _stagnation_url/_stagnation_count logic).
        self._loop = LoopDetector()

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
        recent_steps = self.state.step_history[-1:] if self.state.step_history else []
        for step in recent_steps:
            if step["tool"] == "browser_solve_captcha":
                self._captcha_solve_attempts += 1
                # Only nudge on a FAILED attempt — successful solves
                # include "SOLVED" in the result string.
                result_text = str(step.get("result", ""))
                solved = "SOLVED" in result_text or '"solved": true' in result_text
                if not solved and self._captcha_solve_attempts >= 1:
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
                        "The tool blocks for up to 5 minutes while the "
                        "user solves. Do NOT call browser_solve_captcha again.]"
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
