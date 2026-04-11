"""
Mid-session guardrails for the browser worker agent.

Uses the nanobot AgentHook lifecycle to inject corrective guidance
into the conversation when the worker goes off-track (click-screenshot
loops, regression navigation, stagnation, iteration budget pressure).

Guidance is injected by appending text to the last tool result message,
preserving the assistant/tool message alternation expected by LLM APIs.

Also implements:
- finalize_content: rescues empty LLM responses using worker state
- before_iteration: injects recovery context after empty/failed iterations
"""

from __future__ import annotations

from nanobot.agent.hook import AgentHook, AgentHookContext

from superbrowser_bridge.plan_tracker import PlanTracker
from superbrowser_bridge.session_tools import BrowserSessionState


class BrowserWorkerHook(AgentHook):
    """Injects mid-loop corrective guidance based on worker state."""

    def __init__(
        self,
        state: BrowserSessionState,
        max_iterations: int = 25,
        plan: PlanTracker | None = None,
    ):
        self.state = state
        self.max_iterations = max_iterations
        self.plan = plan or PlanTracker()
        self._stagnation_url: str = ""
        self._stagnation_count: int = 0
        self._last_budget_warning_at: int = -1  # iteration of last warning
        self._empty_streak: int = 0  # consecutive empty responses
        self._last_url: str = ""  # for detecting URL changes (step advancement)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """Rescue empty LLM responses using accumulated worker state.

        Called by the runner BEFORE the empty-response check (runner.py:200).
        If we return non-blank text here, the runner won't enter the retry loop.
        """
        if content and len(content.strip()) >= 10:
            self._empty_streak = 0
            return content

        # Content is empty/blank — build fallback from worker state
        self._empty_streak += 1

        # Only rescue if we actually have browser state to report
        if not self.state.step_history and not self.state.current_url:
            return content  # No state yet, let runner handle it

        parts = []
        url = self.state.current_url
        if url:
            parts.append(f"Currently on: {url}")

        steps = self.state.step_history
        if steps:
            last = steps[-1]
            parts.append(f"Last action: {last['tool']}({last['args']}) -> {last['result']}")
            parts.append(f"Total steps completed: {len(steps)}")

        if self.state.activity_log:
            recent = self.state.activity_log[-3:]
            parts.append("Recent activity: " + " | ".join(recent))

        budget = self.state.screenshot_budget
        parts.append(f"Screenshots remaining: {budget}/{self.state.MAX_SCREENSHOTS}")

        if self._empty_streak >= 2:
            parts.append(
                "I need to use browser_get_markdown to read the current page content "
                "and extract any available data, then return my findings."
            )
        else:
            parts.append(
                "Analyzing current page state to determine next action."
            )

        return " | ".join(parts) if parts else content

    async def before_iteration(self, context: AgentHookContext) -> None:
        """Inject recovery context when the agent seems stuck.

        If the previous iteration produced empty or near-empty content,
        inject a user message with current state so the LLM has context.
        """
        if context.iteration == 0 or self._empty_streak == 0:
            return

        # Build recovery context from worker state
        recovery_parts = []

        url = self.state.current_url
        if url:
            recovery_parts.append(f"You are currently on: {url}")

        steps = self.state.step_history[-3:]
        if steps:
            recovery_parts.append("Your recent steps:")
            for s in steps:
                recovery_parts.append(f"  - {s['tool']}({s['args']}) -> {s['result']}")

        remaining = self.max_iterations - context.iteration - 1
        recovery_parts.append(f"You have {remaining} iterations left.")
        recovery_parts.append(
            "IMPORTANT: You must take action NOW. Options:\n"
            "1. Use browser_run_script to interact with the page\n"
            "2. Use browser_get_markdown to extract page content\n"
            "3. Use browser_eval to inspect DOM structure\n"
            "4. If you have partial data, return it immediately\n"
            "Do NOT return an empty response."
        )

        recovery_text = "\n".join(recovery_parts)

        # Append as a user message so the LLM sees it
        context.messages.append({
            "role": "user",
            "content": f"[RECOVERY CONTEXT — your last response was empty]\n{recovery_text}",
        })

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Inject guidance after each tool execution round."""
        guidance_parts: list[str] = []

        # --- Detect browser_open loops (worker keeps creating new sessions) ---
        if self.state.sessions_opened >= 2:
            guidance_parts.append(
                f"[GUIDANCE: You have called browser_open {self.state.sessions_opened} times. "
                "Do NOT open another session. Work within your current session using "
                "browser_run_script, browser_click, browser_type, browser_eval, or "
                "browser_get_markdown. If the page requires login or is blocked, "
                "report that result back to the orchestrator — do NOT retry browser_open.]"
            )

        # --- Smart loop detection (ported from browser-use) ---
        # Record page state for stagnation detection
        current_url = self.state.current_url
        if current_url and context.tool_results:
            # Extract elements text from the last tool result for fingerprinting
            last_result = str(context.tool_results[-1]) if context.tool_results else ""
            element_count = last_result.count("[") // 2  # rough estimate
            self.state.loop_detector.record_page_state(
                current_url, last_result[:2000], element_count
            )

        loop_nudge = self.state.loop_detector.detect_loop()
        if loop_nudge:
            guidance_parts.append(f"[GUIDANCE: {loop_nudge}]")

        # Also keep the click-batching hint (complementary to loop detection)
        if self.state.consecutive_click_calls >= 3:
            guidance_parts.append(
                "[GUIDANCE: You have used click/type tools "
                f"{self.state.consecutive_click_calls} times in a row. "
                "STOP clicking elements one by one. Write ONE browser_run_script "
                "that batches ALL remaining actions into a single script.]"
            )

        # --- Iteration budget warnings ---
        iteration = context.iteration
        remaining = self.max_iterations - iteration - 1

        if remaining <= int(self.max_iterations * 0.2) and self._last_budget_warning_at != iteration:
            # Critical: 20% or less remaining
            self._last_budget_warning_at = iteration
            guidance_parts.append(
                f"[GUIDANCE: CRITICAL — only {remaining} iterations left out of "
                f"{self.max_iterations}. Extract whatever data you have NOW "
                "using browser_get_markdown and return your results immediately. "
                "Partial results are better than no results.]"
            )
        elif remaining <= int(self.max_iterations * 0.4) and self._last_budget_warning_at != iteration:
            # Warning: 40% or less remaining
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

        # --- Plan-guided step tracking ---
        if self.plan.has_plan:
            # Detect step advancement: URL changed or a successful tool result
            url_changed = current_url and current_url != self._last_url
            self._last_url = current_url or self._last_url

            # Check if the last tool result indicates success
            last_step = self.state.step_history[-1] if self.state.step_history else None
            step_succeeded = False
            step_failed = False
            if last_step:
                result_str = last_step.get("result", "").lower()
                tool_name = last_step.get("tool", "")
                if "FAILED" in last_step.get("result", "") or "error" in result_str:
                    step_failed = True
                elif tool_name in ("browser_open", "browser_navigate") and url_changed:
                    step_succeeded = True
                elif tool_name == "browser_run_script" and "FAILED" not in last_step.get("result", ""):
                    step_succeeded = True
                elif tool_name in ("browser_get_markdown", "browser_eval"):
                    step_succeeded = True
                elif tool_name in ("browser_click", "browser_type"):
                    # Click/type always count as a step attempt
                    self.plan.record_attempt()

            if step_succeeded and not self.plan.is_complete:
                self.plan.advance()
            elif step_failed and not self.plan.is_complete:
                self.plan.fail_current()
                self.plan.retry_current()

            # Grant bonus screenshots when stuck (vision-on-demand)
            if self.plan.current_attempts >= 3 and self.state.screenshot_budget <= 0:
                self.state.screenshot_budget += 2
                self.state.screenshotted_urls.clear()  # allow re-screenshot
                guidance_parts.append(
                    "[GUIDANCE: Bonus screenshots granted because you're stuck. "
                    "Take a screenshot NOW to see what's on screen.]"
                )

            # Inject plan progress
            plan_block = (
                f"\n[PLAN PROGRESS]\n{self.plan.render()}\n\n"
                f"{self.plan.render_focus()}"
            )
            guidance_parts.append(plan_block)

        # --- Vision-on-demand: grant bonus screenshots when loop detected ---
        if (
            self.state.loop_detector.is_looping
            and self.state.screenshot_budget <= 0
        ):
            self.state.screenshot_budget += 2
            self.state.screenshotted_urls.clear()
            guidance_parts.append(
                "[GUIDANCE: Bonus screenshots granted due to detected loop. "
                "Take a screenshot to see the current page state.]"
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
