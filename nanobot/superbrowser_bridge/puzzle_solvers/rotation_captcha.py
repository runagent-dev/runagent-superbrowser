"""Rotation CAPTCHA solver.

A rotation captcha shows an image that has been rotated by an unknown
angle; the user rotates it (via a slider or arrow buttons) until it's
upright. We detect via DOM markers, crop the image, and call an
orientation estimator (CNN-lite or edge-histogram heuristic) to decide
the correction needed.

v1 scaffolds the flow. The estimator lives in `engines/image_match.py`
and emits a noop-with-reason if OpenCV/torch aren't installed.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import Action, SolverBrowser


_CONTAINER_SELECTORS = [
    ".rotation-captcha",
    ".captcha-rotate",
    "[data-captcha='rotate']",
    ".rotate-puzzle",
]
_CW_BUTTON_SELECTORS = [
    ".rotate-cw",
    ".captcha-rotate-cw",
    "[aria-label*='clockwise' i]",
]
_CCW_BUTTON_SELECTORS = [
    ".rotate-ccw",
    ".captcha-rotate-ccw",
    "[aria-label*='counter' i]",
]


class RotationCaptchaSolver:
    name = "rotation_captcha"

    def detect(self, url: str, dom_snapshot: Optional[str]) -> float:
        if not dom_snapshot:
            return 0.0
        hay = dom_snapshot.lower()
        if any(n in hay for n in ("rotation-captcha", "captcha-rotate", "rotate-puzzle")):
            return 0.75
        return 0.0

    async def extract_state(self, browser: SolverBrowser) -> dict[str, Any]:
        container_rects = await browser.get_rects(_CONTAINER_SELECTORS)
        cw_rects = await browser.get_rects(_CW_BUTTON_SELECTORS)
        ccw_rects = await browser.get_rects(_CCW_BUTTON_SELECTORS)
        container = next((r for r in container_rects if r), None)
        cw = next((r for r in cw_rects if r), None)
        ccw = next((r for r in ccw_rects if r), None)
        if not container:
            return {"error": "no_container"}
        crop_b64 = await browser.image_region(
            {"x": container["x"], "y": container["y"], "w": container["w"], "h": container["h"]}
        )
        return {"container": container, "cw": cw, "ccw": ccw, "crop_b64": crop_b64}

    async def plan_actions(self, state: dict[str, Any]) -> list[Action]:
        if state.get("error"):
            return [Action(kind="noop", reason=state["error"])]
        try:
            from .engines.image_match import estimate_upright_angle_delta
            crop_b64 = state.get("crop_b64", "")
            import base64
            angle = estimate_upright_angle_delta(base64.b64decode(crop_b64)) if crop_b64 else 0
        except Exception as e:
            return [Action(kind="noop", reason=f"orient_estimator_unavailable: {e}")]
        # Each button press typically rotates ±15°. Round to nearest.
        steps = int(round(angle / 15))
        if steps == 0:
            return [Action(kind="noop", reason="already_upright")]
        actions: list[Action] = []
        button = state.get("cw") if steps > 0 else state.get("ccw")
        if not button:
            return [Action(kind="noop", reason="no_rotation_button")]
        for _ in range(abs(steps)):
            actions.append(
                Action(
                    kind="drag_path",
                    points=[{"x": button["cx"], "y": button["cy"]},
                            {"x": button["cx"], "y": button["cy"]}],
                    reason=f"rotate_{'cw' if steps > 0 else 'ccw'}",
                )
            )
            actions.append(Action(kind="wait", wait_ms=150, reason="post_rotate_settle"))
        return actions

    async def verify(
        self, browser: SolverBrowser, state: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        probe = await browser.evaluate(
            r"""(() => ({
              solved: !!document.querySelector('.captcha-verified, .rotation-solved')
            }))()"""
        )
        if isinstance(probe, dict) and probe.get("solved"):
            return True, {**state, "solved": True}
        return False, state

    async def execute(self, browser: SolverBrowser, action: Action) -> dict[str, Any]:
        from .base import default_execute
        return await default_execute(browser, action)
