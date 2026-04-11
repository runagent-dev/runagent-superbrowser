"""
Step-by-step plan tracking for the browser worker agent.

Inspired by browser-use's PlanItem system (agent/views.py) — tracks
plan steps with status, renders progress for injection into tool results,
and detects step advancement from browser state changes.

The orchestrator parses task instructions into steps, the PlanTracker
tracks progress, and the worker hook injects rendered progress after
every tool call so the LLM focuses on one step at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlanStep:
    """A single step in the execution plan."""

    text: str
    status: str = "pending"  # pending | current | done | failed
    attempts: int = 0


@dataclass
class PlanTracker:
    """Tracks step-by-step plan execution progress.

    Usage:
        tracker = PlanTracker()
        tracker.set_plan(["Open google.com/maps", "Search for restaurants", "Extract results"])
        # After each action, check if step should advance:
        tracker.advance()        # Mark current done, move to next
        tracker.fail_current()   # Mark current failed
        tracker.retry_current()  # Reset failed step to current
    """

    steps: list[PlanStep] = field(default_factory=list)
    current_index: int = 0

    def set_plan(self, step_texts: list[str]) -> None:
        """Initialize plan from a list of step descriptions."""
        self.steps = [PlanStep(text=t) for t in step_texts]
        self.current_index = 0
        if self.steps:
            self.steps[0].status = "current"

    def advance(self) -> None:
        """Mark current step as done and move to next."""
        if self.current_index < len(self.steps):
            self.steps[self.current_index].status = "done"
        self.current_index += 1
        if self.current_index < len(self.steps):
            self.steps[self.current_index].status = "current"

    def fail_current(self) -> None:
        """Mark current step as failed."""
        if self.current_index < len(self.steps):
            self.steps[self.current_index].status = "failed"
            self.steps[self.current_index].attempts += 1

    def retry_current(self) -> None:
        """Reset a failed step back to current for retry."""
        if self.current_index < len(self.steps):
            self.steps[self.current_index].status = "current"

    def record_attempt(self) -> None:
        """Increment attempt counter for the current step."""
        if self.current_index < len(self.steps):
            self.steps[self.current_index].attempts += 1

    @property
    def current_step(self) -> str | None:
        """Text of the current step, or None if plan is complete."""
        if self.current_index < len(self.steps):
            return self.steps[self.current_index].text
        return None

    @property
    def current_attempts(self) -> int:
        """Number of attempts on the current step."""
        if self.current_index < len(self.steps):
            return self.steps[self.current_index].attempts
        return 0

    @property
    def is_complete(self) -> bool:
        """True if all steps are done or the index is past the end."""
        return self.current_index >= len(self.steps)

    @property
    def has_plan(self) -> bool:
        """True if a plan has been set."""
        return len(self.steps) > 0

    def render(self) -> str:
        """Render plan progress as a readable string.

        Format:
            [x] 0: Open google.com/maps
            [>] 1: Search "restaurants" (current)
            [ ] 2: Extract results
        """
        if not self.steps:
            return ""

        markers = {
            "done": "[x]",
            "current": "[>]",
            "pending": "[ ]",
            "failed": "[!]",
        }
        lines = []
        for i, step in enumerate(self.steps):
            marker = markers.get(step.status, "[ ]")
            suffix = ""
            if step.status == "failed":
                suffix = f" (failed, {step.attempts} attempts)"
            elif step.status == "current" and step.attempts > 0:
                suffix = f" (attempt {step.attempts + 1})"
            lines.append(f"{marker} {i}: {step.text}{suffix}")
        return "\n".join(lines)

    def render_focus(self) -> str:
        """Render a focus directive for the current step."""
        step = self.current_step
        if not step:
            return "[All steps complete. Return your results now.]"

        remaining = len(self.steps) - self.current_index - 1
        focus = f"[FOCUS: Complete step {self.current_index} only: {step}]"
        if remaining > 0:
            focus += f"\n[Do NOT attempt the remaining {remaining} step(s) yet.]"

        if self.current_attempts >= 3:
            focus += (
                "\n[WARNING: 3+ attempts on this step. Take a screenshot to see "
                "what's on screen, or try a completely different approach.]"
            )

        return focus
