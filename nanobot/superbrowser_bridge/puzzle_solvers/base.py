"""Puzzle solver abstract base.

A puzzle is anything that:
 1. Has observable state derivable from the page (DOM + optional crops).
 2. Admits a sequence of discrete actions (click, drag, drag-path).
 3. Has a verifiable success condition.

Concrete solvers fill in five hooks: `detect`, `extract_state`,
`plan_actions`, `execute`, `verify`. The `solve` loop is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol


ActionKind = Literal[
    "click_selector",
    "drag_selectors",
    "drag_path",
    "drag_xy",
    "click_xy",
    "wait",
    "noop",
]


@dataclass
class Action:
    """A discrete instruction a solver wants executed against the browser.

    The driver (registry.solve) translates it into the matching
    nanobot tool call. Keeping this as plain data lets unit tests
    verify solver reasoning without booting a browser.
    """

    kind: ActionKind
    # For selector-driven actions:
    selector: Optional[str] = None
    from_selector: Optional[str] = None
    to_selector: Optional[str] = None
    # For coordinate-driven actions (fallback when selectors don't exist,
    # e.g. canvas puzzles):
    x: Optional[float] = None
    y: Optional[float] = None
    points: Optional[list[dict[str, float]]] = None
    # Tuning:
    method: Optional[Literal["drag", "click_click", "auto"]] = None
    hold_ms: Optional[int] = None
    wait_ms: Optional[int] = None
    # For logging / causal chain
    reason: str = ""


@dataclass
class SolveResult:
    success: bool
    solver: str
    steps_taken: int
    actions: list[Action] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# Type alias for the small façade a solver receives. Keeps solvers
# decoupled from the session_tools module and easily mockable.
class SolverBrowser(Protocol):
    session_id: str

    async def get_rect(
        self, selector: str, *, ensure_visible: bool = True,
    ) -> Optional[dict[str, float]]: ...
    async def get_rects(
        self, selectors: list[str], *, ensure_visible: bool = True,
    ) -> list[Optional[dict[str, float]]]: ...
    async def click_selector(
        self, selector: str, **opts: Any,
    ) -> dict[str, Any]: ...
    async def drag_selectors(
        self, from_selector: str, to_selector: str, **opts: Any,
    ) -> dict[str, Any]: ...
    async def drag_path(
        self, points: list[dict[str, float]], **opts: Any,
    ) -> dict[str, Any]: ...
    async def image_region(
        self, bbox: dict[str, float], *, quality: int = 80,
    ) -> str: ...
    async def evaluate(self, script: str) -> Any: ...
    async def current_url(self) -> str: ...


class PuzzleSolver(Protocol):
    """Every solver satisfies this. `detect` is synchronous-by-design so the
    registry can iterate candidates cheaply; everything else is async."""

    name: str

    def detect(self, url: str, dom_snapshot: Optional[str]) -> float:
        """Confidence 0..1 that this solver applies. Use URL+DOM signals;
        don't call any async browser methods here."""
        ...

    async def extract_state(self, browser: SolverBrowser) -> dict[str, Any]:
        """Read the puzzle's current state (FEN, slider offset, piece positions)."""
        ...

    async def plan_actions(
        self, state: dict[str, Any],
    ) -> list[Action]:
        """Return next batch of actions. Pure — no I/O. May call engines."""
        ...

    async def execute(
        self, browser: SolverBrowser, action: Action,
    ) -> dict[str, Any]:
        """Apply one action via browser primitives. Default impl lives in
        `_default_execute` below; override only when the action kind needs
        custom handling (e.g. rotation captcha's per-step crop-check)."""
        ...

    async def verify(
        self, browser: SolverBrowser, state: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """(solved_yet, new_state). The driver loops until solved or max_steps."""
        ...


async def default_execute(
    browser: SolverBrowser, action: Action,
) -> dict[str, Any]:
    """Reusable action dispatcher that most solvers can delegate to."""
    if action.kind == "click_selector":
        assert action.selector, "click_selector requires selector"
        return await browser.click_selector(action.selector)
    if action.kind == "drag_selectors":
        assert action.from_selector and action.to_selector, (
            "drag_selectors requires from_selector and to_selector"
        )
        return await browser.drag_selectors(
            action.from_selector,
            action.to_selector,
            method=action.method or "auto",
            hold_ms=action.hold_ms,
        )
    if action.kind == "drag_path":
        assert action.points, "drag_path requires points"
        return await browser.drag_path(action.points, hold_ms=action.hold_ms)
    if action.kind == "click_xy":
        assert action.x is not None and action.y is not None
        # Raw xy click path lives in session_tools; callers must have
        # that in their SolverBrowser façade if they plan to use it.
        raise NotImplementedError("click_xy — wire through your browser façade")
    if action.kind == "drag_xy":
        raise NotImplementedError("drag_xy — wire through your browser façade")
    if action.kind == "wait":
        import asyncio
        await asyncio.sleep((action.wait_ms or 500) / 1000.0)
        return {"waited_ms": action.wait_ms or 500}
    if action.kind == "noop":
        return {}
    raise ValueError(f"Unknown action kind: {action.kind}")
