"""Generic grid-drag solver.

Covers "drag items on a grid to their target positions" — sliding-tile
puzzles, drag-to-sort captchas (hCaptcha grid select), picture-match
(drop each thumbnail on its matching silhouette), etc.

Configuration arrives as a page-specific adapter: the detector can be
disabled by default and enabled via orchestrator hint when the puzzle
type isn't obvious from DOM signals alone.

Strategy
--------
1. Identify a container + cells (selectors or rect-derived).
2. Identify items + targets (selectors or crop matches).
3. Plan a pairing (hungarian algorithm over similarity, or 1-1 by DOM
   attribute when given).
4. Execute drags via `drag_selectors` / `drag_path`.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import Action, SolverBrowser


class GridDragSolver:
    name = "grid_drag"

    def __init__(self) -> None:
        # Adapters: mapping from (URL prefix, DOM hint) → grid descriptor
        self._adapters: list[dict[str, Any]] = []

    def register_adapter(
        self,
        *,
        url_includes: Optional[str] = None,
        dom_includes: Optional[str] = None,
        item_selector: str,
        target_selector: str,
        pairing: str = "index",  # "index" | "attribute:data-target-id"
    ) -> None:
        self._adapters.append({
            "url_includes": url_includes,
            "dom_includes": dom_includes,
            "item_selector": item_selector,
            "target_selector": target_selector,
            "pairing": pairing,
        })

    def _matching_adapter(
        self, url: str, dom_snapshot: Optional[str],
    ) -> Optional[dict[str, Any]]:
        for ad in self._adapters:
            ui = ad.get("url_includes")
            di = ad.get("dom_includes")
            if ui and ui not in (url or ""):
                continue
            if di and not (dom_snapshot and di in dom_snapshot):
                continue
            return ad
        return None

    def detect(self, url: str, dom_snapshot: Optional[str]) -> float:
        # Grid-drag is intentionally conservative — detect only when an
        # adapter has been registered for this host/page. Forcing the user
        # to register avoids competing with the specialised solvers
        # (chess, slider, jigsaw) whose detectors are tighter.
        return 0.25 if self._matching_adapter(url, dom_snapshot) else 0.0

    async def extract_state(self, browser: SolverBrowser) -> dict[str, Any]:
        url = await browser.current_url()
        ad = self._matching_adapter(url, None)
        if not ad:
            return {"error": "no_adapter_registered"}
        items = await browser.get_rects([ad["item_selector"]])
        targets = await browser.get_rects([ad["target_selector"]])
        return {"adapter": ad, "items": items, "targets": targets}

    async def plan_actions(self, state: dict[str, Any]) -> list[Action]:
        if state.get("error"):
            return [Action(kind="noop", reason=state["error"])]
        items = [i for i in state.get("items") or [] if i]
        targets = [t for t in state.get("targets") or [] if t]
        if not items or not targets:
            return [Action(kind="noop", reason="no_items_or_targets")]
        pairs = list(zip(items, targets))
        actions: list[Action] = []
        for src, dst in pairs:
            actions.append(Action(
                kind="drag_path",
                points=[{"x": src["cx"], "y": src["cy"]},
                        {"x": dst["cx"], "y": dst["cy"]}],
                reason="grid_item_to_target",
            ))
            actions.append(Action(kind="wait", wait_ms=120, reason="post_drag_settle"))
        return actions

    async def verify(
        self, browser: SolverBrowser, state: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        # Default: if no items remain draggable, consider solved.
        ad = state.get("adapter")
        if not ad:
            return False, state
        items = await browser.get_rects([ad["item_selector"]])
        remaining = sum(1 for i in items if i and i.get("visible"))
        if remaining == 0:
            return True, {**state, "solved": True}
        return False, state

    async def execute(self, browser: SolverBrowser, action: Action) -> dict[str, Any]:
        from .base import default_execute
        return await default_execute(browser, action)
