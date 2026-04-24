"""Puzzle solver framework.

One `PuzzleSolver` interface; many implementations (chess.com, slider
captcha, jigsaw, rotation, generic grid-drag). The registry auto-detects
which solver applies to the current page and the orchestrator's
`solve_puzzle` tool dispatches to the winning candidate.

Every solver uses the generic selector-based input primitives
(browser_click_selector / browser_drag_selectors / browser_drag_path /
browser_image_region) so the tight loop has zero Gemini cost.
"""

from .base import (  # noqa: F401
    Action,
    PuzzleSolver,
    SolveResult,
)
from .registry import detect, registry, solve  # noqa: F401

__all__ = [
    "Action",
    "PuzzleSolver",
    "SolveResult",
    "detect",
    "registry",
    "solve",
]
