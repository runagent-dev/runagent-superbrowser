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


class BrowserWorkerHook(AgentHook):
    """Injects mid-loop corrective guidance based on worker state."""

    def __init__(self, state: BrowserSessionState, max_iterations: int = 25):
        self.state = state
        self.max_iterations = max_iterations
        self._stagnation_url: str = ""
        self._stagnation_count: int = 0
        self._last_budget_warning_at: int = -1  # iteration of last warning

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Inject guidance after each tool execution round."""
        guidance_parts: list[str] = []

        # --- Detect click-screenshot loops ---
        if self.state.consecutive_click_calls >= 3:
            guidance_parts.append(
                "[GUIDANCE: You have used click/type tools "
                f"{self.state.consecutive_click_calls} times in a row. "
                "STOP clicking elements one by one. Write ONE browser_run_script "
                "that batches ALL remaining actions into a single script.]"
            )

        # --- Detect stagnation (same URL, no progress) ---
        current_url = self.state.current_url
        if current_url and current_url == self._stagnation_url:
            self._stagnation_count += 1
        else:
            self._stagnation_url = current_url
            self._stagnation_count = 1

        if self._stagnation_count >= 5:
            guidance_parts.append(
                "[GUIDANCE: You have been on the same page for "
                f"{self._stagnation_count} iterations without extracting data. "
                "Try a completely different approach: use browser_eval to inspect "
                "the DOM structure, then write a new browser_run_script with "
                "different selectors. If the page has the data you need, use "
                "browser_get_markdown to extract it NOW.]"
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
