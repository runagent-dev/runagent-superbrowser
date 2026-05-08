"""Slider CAPTCHA solver.

Thin wrapper around the existing slider drag logic in
`superbrowser_bridge/antibot/`. The repo already has a dedicated Bezier
drag solver used by the antibot subsystem; this solver just adapts it to
the `PuzzleSolver` interface so the orchestrator's `solve_puzzle` tool
can route sliders through the same pipeline as everything else.

Detection signals:
 - Common class markers: `.slider-captcha`, `.geetest_slider_button`,
   `[data-captcha="slider"]`, `.verify-slider`.
 - Visible text: "Drag the slider" / "Slide to verify".

Execution: find the handle selector, read the target offset (either from
a companion gap-indicator element or by template-matching the gap on the
rendered image) and drag.

Keep implementation minimal in v1 — falls back to a `noop + wait` plan
plus a call to the existing solver via `evaluate` when it's hot-loaded.
Full template-match is implemented in `jigsaw_captcha.py` since sliders
are a special case (1-D drag) of the jigsaw pattern.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import Action, SolverBrowser

# Selectors to probe for a slider handle. First match wins.
_HANDLE_SELECTORS = [
    "[data-captcha='slider'] .handle",
    ".geetest_slider_button",
    ".slider-captcha .handle",
    ".verify-slider .handle",
    "[aria-label*='slider' i]",
    ".captcha-slider .slider-button",
]

# Selectors for the container / track that defines the full drag distance.
_TRACK_SELECTORS = [
    "[data-captcha='slider']",
    ".geetest_slider_track",
    ".slider-captcha",
    ".verify-slider",
    ".captcha-slider",
]


class SliderCaptchaSolver:
    name = "slider_captcha"

    def detect(self, url: str, dom_snapshot: Optional[str]) -> float:
        if not dom_snapshot:
            return 0.0
        hay = dom_snapshot.lower()
        signals = 0
        for needle in (
            "slider-captcha", "geetest_slider_button", "verify-slider",
            "captcha-slider", "drag the slider", "slide to verify",
            "data-captcha=\"slider\"",
        ):
            if needle in hay:
                signals += 1
        if signals >= 2:
            return 0.85
        if signals == 1:
            return 0.55
        return 0.0

    async def extract_state(self, browser: SolverBrowser) -> dict[str, Any]:
        rects = await browser.get_rects(_HANDLE_SELECTORS + _TRACK_SELECTORS)
        handle = next(
            (r for r, sel in zip(rects[: len(_HANDLE_SELECTORS)], _HANDLE_SELECTORS) if r),
            None,
        )
        track = next(
            (r for r, sel in zip(rects[len(_HANDLE_SELECTORS):], _TRACK_SELECTORS) if r),
            None,
        )
        return {"handle": handle, "track": track}

    async def plan_actions(self, state: dict[str, Any]) -> list[Action]:
        handle = state.get("handle")
        track = state.get("track")
        if not handle or not track:
            return [Action(kind="noop", reason="no_slider_found")]
        # Default target: end of the track. The antibot subsystem's image-
        # matching solver would override this with the actual gap offset;
        # hook that in once migrated here. For now, drag to the far edge
        # which triggers the captcha's own gap-detection feedback.
        target_x = track["x"] + track["w"] - (handle["w"] / 2)
        target_y = handle["cy"]
        return [
            Action(
                kind="drag_path",
                points=[
                    {"x": handle["cx"], "y": handle["cy"]},
                    {"x": target_x, "y": target_y},
                ],
                reason="slider_drag_to_end",
            ),
        ]

    async def execute(self, browser: SolverBrowser, action: Action) -> dict[str, Any]:
        from .base import default_execute
        return await default_execute(browser, action)

    async def verify(
        self, browser: SolverBrowser, state: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        # Verify by re-probing for the slider: if it's gone, or if a
        # "verified" indicator is present, we're done.
        probe = await browser.evaluate(
            r"""(() => {
              const ok = document.querySelector('.captcha-verified, .geetest_success');
              const stillSlider = document.querySelector('.slider-captcha, .geetest_slider_button, .verify-slider');
              return { solved: !!ok || !stillSlider };
            })()"""
        )
        if isinstance(probe, dict) and probe.get("solved"):
            return True, {**state, "solved": True}
        return False, state
