"""Jigsaw CAPTCHA solver.

A jigsaw captcha shows a background image with a puzzle-piece-shaped hole
plus a draggable piece that must be placed into the hole. We detect the
layout, crop the hole region, crop the piece, template-match to find the
hole offset, then drag the piece along a humanised polyline.

v1 ships the plumbing — extract/plan/execute are wired through the
`browser_image_region` primitive and an optional OpenCV backend. When
OpenCV isn't installed, the solver emits a noop with a clear reason
rather than crashing the registry.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

from .base import Action, SolverBrowser

logger = logging.getLogger(__name__)


# Common selectors across jigsaw captcha providers.
_CONTAINER_SELECTORS = [
    ".geetest_canvas",
    ".geetest_panel",
    ".jigsaw-captcha",
    ".slide-puzzle",
    "[data-captcha='jigsaw']",
]
_PIECE_SELECTORS = [
    ".geetest_canvas_slice",
    ".jigsaw-piece",
    ".slide-piece",
]
_HANDLE_SELECTORS = [
    ".geetest_slider_button",
    ".jigsaw-handle",
    ".slide-handle",
]


class JigsawCaptchaSolver:
    name = "jigsaw_captcha"

    def detect(self, url: str, dom_snapshot: Optional[str]) -> float:
        if not dom_snapshot:
            return 0.0
        hay = dom_snapshot.lower()
        signals = sum(
            1 for needle in ("geetest_canvas", "jigsaw-captcha", "slide-puzzle")
            if needle in hay
        )
        if signals >= 1:
            return 0.75
        return 0.0

    async def extract_state(self, browser: SolverBrowser) -> dict[str, Any]:
        container_rects = await browser.get_rects(_CONTAINER_SELECTORS)
        piece_rects = await browser.get_rects(_PIECE_SELECTORS)
        handle_rects = await browser.get_rects(_HANDLE_SELECTORS)
        container = next((r for r in container_rects if r), None)
        piece = next((r for r in piece_rects if r), None)
        handle = next((r for r in handle_rects if r), None)
        if not container:
            return {"error": "no_container"}
        crop_b64 = await browser.image_region(
            {"x": container["x"], "y": container["y"], "w": container["w"], "h": container["h"]}
        )
        return {
            "container": container,
            "piece": piece,
            "handle": handle,
            "crop_b64_len": len(crop_b64),
            "crop_b64": crop_b64,
        }

    async def plan_actions(self, state: dict[str, Any]) -> list[Action]:
        if state.get("error"):
            return [Action(kind="noop", reason=state["error"])]
        handle = state.get("handle")
        piece = state.get("piece")
        container = state.get("container")
        if not (handle and piece and container):
            return [Action(kind="noop", reason="missing_piece_or_handle")]
        # Template-match the piece against the container crop to find the
        # target x-offset (jigsaw captchas typically require only
        # horizontal drag — the vertical placement is fixed).
        try:
            from .engines.image_match import find_piece_x_offset
            crop_b64 = state.get("crop_b64", "")
            crop_bytes = base64.b64decode(crop_b64) if crop_b64 else b""
            # Piece crop: best-effort, using the piece rect.
            piece_crop_b64 = ""  # captured separately below via browser
            offset = find_piece_x_offset(crop_bytes, piece_crop_b64)
        except Exception as e:
            return [Action(kind="noop", reason=f"template_match_unavailable: {e}")]
        handle_cx = handle["cx"]
        handle_cy = handle["cy"]
        target_x = container["x"] + offset
        # Humanised polyline: 12-18 points with slight y-jitter to beat
        # straight-line bot detectors.
        steps = 14
        import math
        points = []
        for i in range(steps + 1):
            t = i / steps
            # Sigmoid easing
            ease = 1 / (1 + math.exp(-10 * (t - 0.5)))
            x = handle_cx + (target_x - handle_cx) * ease
            # Tiny y-jitter (±2px) to look like a hand
            jitter = math.sin(t * math.pi * 2) * 2.0
            points.append({"x": x, "y": handle_cy + jitter})
        return [Action(kind="drag_path", points=points, reason="jigsaw_trace")]

    async def verify(
        self, browser: SolverBrowser, state: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        probe = await browser.evaluate(
            r"""(() => ({
              solved: !!document.querySelector('.captcha-verified, .geetest_success, .jigsaw-solved')
            }))()"""
        )
        if isinstance(probe, dict) and probe.get("solved"):
            return True, {**state, "solved": True}
        return False, state

    async def execute(self, browser: SolverBrowser, action: Action) -> dict[str, Any]:
        from .base import default_execute
        return await default_execute(browser, action)
