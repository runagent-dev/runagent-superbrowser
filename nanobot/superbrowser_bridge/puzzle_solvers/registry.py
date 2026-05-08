"""Puzzle solver registry + shared solve loop.

`registry` is the list of all known concrete solvers. `detect` picks the
one with highest confidence for a given URL/DOM. `solve` runs the shared
extract → plan → execute → verify loop.

Import paths are lazy so a broken/missing optional dep (Stockfish, OpenCV)
doesn't prevent the module from loading other solvers.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import Action, PuzzleSolver, SolveResult, SolverBrowser, default_execute

logger = logging.getLogger(__name__)


def _load_registered() -> list[PuzzleSolver]:
    solvers: list[PuzzleSolver] = []
    # Each import is guarded — a missing optional engine (Stockfish, OpenCV,
    # Tesseract) should not prevent other solvers from registering.
    try:
        from .chess_com import ChessComSolver
        solvers.append(ChessComSolver())
    except Exception as e:
        logger.warning("chess_com solver not loaded: %s", e)
    try:
        from .slider_captcha import SliderCaptchaSolver
        solvers.append(SliderCaptchaSolver())
    except Exception as e:
        logger.warning("slider_captcha solver not loaded: %s", e)
    try:
        from .jigsaw_captcha import JigsawCaptchaSolver
        solvers.append(JigsawCaptchaSolver())
    except Exception as e:
        logger.warning("jigsaw_captcha solver not loaded: %s", e)
    try:
        from .rotation_captcha import RotationCaptchaSolver
        solvers.append(RotationCaptchaSolver())
    except Exception as e:
        logger.warning("rotation_captcha solver not loaded: %s", e)
    try:
        from .grid_drag import GridDragSolver
        solvers.append(GridDragSolver())
    except Exception as e:
        logger.warning("grid_drag solver not loaded: %s", e)
    return solvers


# Lazy singleton — first call populates.
_registry_cache: Optional[list[PuzzleSolver]] = None


def registry() -> list[PuzzleSolver]:
    global _registry_cache
    if _registry_cache is None:
        _registry_cache = _load_registered()
    return _registry_cache


def detect(
    url: str,
    dom_snapshot: Optional[str] = None,
    *,
    hint: Optional[str] = None,
) -> tuple[Optional[PuzzleSolver], float]:
    """Pick the highest-confidence solver. `hint` is an escape hatch —
    match by solver.name first, fall back to auto-detection."""
    solvers = registry()
    if hint:
        for s in solvers:
            if s.name == hint:
                return s, 1.0
    best: Optional[PuzzleSolver] = None
    best_score = 0.0
    for s in solvers:
        try:
            score = float(s.detect(url, dom_snapshot))
        except Exception as e:
            logger.warning("solver %s detect() raised: %s", s.name, e)
            continue
        if score > best_score:
            best = s
            best_score = score
    # Require at least 0.2 to commit — below that, treat as "no match".
    if best_score < 0.2:
        return None, best_score
    return best, best_score


async def solve(
    solver: PuzzleSolver,
    browser: SolverBrowser,
    *,
    max_steps: int = 10,
) -> SolveResult:
    """Run the shared extract → plan → execute → verify loop."""
    actions_log: list[Action] = []
    final_state: dict = {}
    for step in range(max_steps):
        try:
            state = await solver.extract_state(browser)
        except Exception as e:
            return SolveResult(
                success=False, solver=solver.name, steps_taken=step,
                actions=actions_log, final_state=final_state,
                error=f"extract_state failed: {e}",
            )
        final_state = state

        solved, state = await solver.verify(browser, state)
        final_state = state
        if solved:
            return SolveResult(
                success=True, solver=solver.name, steps_taken=step,
                actions=actions_log, final_state=final_state,
            )

        try:
            actions = await solver.plan_actions(state)
        except Exception as e:
            return SolveResult(
                success=False, solver=solver.name, steps_taken=step,
                actions=actions_log, final_state=final_state,
                error=f"plan_actions failed: {e}",
            )
        if not actions:
            return SolveResult(
                success=False, solver=solver.name, steps_taken=step,
                actions=actions_log, final_state=final_state,
                error="plan_actions returned empty — solver gave up",
            )

        for act in actions:
            actions_log.append(act)
            try:
                executor = getattr(solver, "execute", None)
                if executor is not None and callable(executor):
                    await executor(browser, act)
                else:
                    await default_execute(browser, act)
            except NotImplementedError:
                await default_execute(browser, act)
            except Exception as e:
                return SolveResult(
                    success=False, solver=solver.name, steps_taken=step,
                    actions=actions_log, final_state=final_state,
                    error=f"execute failed on {act.kind}: {e}",
                )

    # Hit max_steps without a solved verification.
    return SolveResult(
        success=False, solver=solver.name, steps_taken=max_steps,
        actions=actions_log, final_state=final_state,
        error=f"max_steps ({max_steps}) reached without solving",
    )
