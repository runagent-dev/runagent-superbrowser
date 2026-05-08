"""Image-region capture + puzzle-solver dispatch tools.

`BrowserImageRegionTool` returns a base64 JPEG of a viewport region.
`BrowserSolvePuzzleTool` auto-detects the puzzle on the page and runs
a dedicated solver (chess, slider, jigsaw, rotation, grid-drag).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        bbox_json=StringSchema(
            "JSON object {x, y, w, h} describing the viewport region to crop, in CSS pixels."
        ),
        quality=IntegerSchema("JPEG quality 1–100, default 80.", nullable=True),
        required=["session_id", "bbox_json"],
    )
)
class BrowserImageRegionTool(Tool):
    name = "browser_image_region"
    description = (
        "Screenshot a bounded region of the viewport and return base64 JPEG. "
        "Cheaper than a full-page Gemini pass for solvers that need to "
        "template-match a captcha piece, OCR a small area, or run a tiny "
        "focused vision query."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        bbox_json: str,
        quality: int | None = None,
        **kw: Any,
    ) -> str:
        try:
            bbox = json.loads(bbox_json)
        except (TypeError, ValueError) as exc:
            return f"[image_region_failed] bbox_json is not valid JSON: {exc}"
        for k in ("x", "y", "w", "h"):
            if not isinstance(bbox.get(k), (int, float)):
                return f"[image_region_failed] bbox must have numeric {k}"

        payload: dict[str, Any] = {"bbox": bbox}
        if quality is not None:
            payload["quality"] = quality
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/image-region",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[image_region_failed] {err}"
        data = r.json()
        b64 = data.get("base64", "")
        return (
            f"image_region: {bbox['w']}x{bbox['h']} at ({bbox['x']},{bbox['y']}), "
            f"base64_len={len(b64)}\n{b64[:200]}..."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        hint=StringSchema(
            "Optional solver name to force (chess_com, slider_captcha, jigsaw_captcha, "
            "rotation_captcha, grid_drag). Skip for auto-detect.",
            nullable=True,
        ),
        max_steps=IntegerSchema(
            "Maximum solver iterations. Default 10 — enough for most chess puzzles "
            "and captchas; increase for long puzzle lines.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserSolvePuzzleTool(Tool):
    name = "browser_solve_puzzle"
    description = (
        "Auto-detect the puzzle on the current page (chess position, "
        "slider/jigsaw/rotation captcha, generic grid-drag) and run a "
        "dedicated solver through extract → plan → execute → verify. "
        "Uses selector- and coordinate-exact primitives under the hood "
        "(zero Gemini round-trips in the move loop). Use whenever the "
        "page presents a puzzle-like challenge."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        hint: str | None = None,
        max_steps: int | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_solve_puzzle(session={session_id}, hint={hint!r}, max_steps={max_steps})")
        from superbrowser_bridge.puzzle_solvers import detect as _detect, solve as _solve
        from superbrowser_bridge.puzzle_solvers.browser import HttpSolverBrowser

        # Pull a DOM snapshot + URL to feed the detector (cheap GET).
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                state_resp = await client.get(
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "false"},
                )
                state_resp.raise_for_status()
                state_data = state_resp.json()
        except Exception as exc:
            return f"[solve_puzzle_failed] cannot read session state: {exc}"

        url = state_data.get("url", "") or ""
        dom_snippet = state_data.get("elements") or ""

        solver, conf = _detect(url, dom_snippet, hint=hint)
        if solver is None:
            return (
                "[solve_puzzle_no_match] No solver matched the current page "
                f"(url={url!r}, confidence={conf:.2f}). Pass hint=<solver name> "
                "to force, or implement a new solver for this page type."
            )

        print(f">> selected solver: {solver.name} (confidence={conf:.2f})")
        async with HttpSolverBrowser(session_id, SUPERBROWSER_URL) as browser:
            result = await _solve(solver, browser, max_steps=max_steps or 10)

        self.s.record_step(
            "browser_solve_puzzle",
            f"{solver.name} success={result.success} steps={result.steps_taken}",
            url,
        )
        lines = [
            f"Puzzle solver: {result.solver}",
            f"Success: {result.success}",
            f"Steps taken: {result.steps_taken}",
        ]
        if result.error:
            lines.append(f"Error: {result.error}")
        if result.actions:
            lines.append(f"Actions ({len(result.actions)}):")
            for a in result.actions[:8]:
                lines.append(f"  - {a.kind}: {a.reason or ''}")
            if len(result.actions) > 8:
                lines.append(f"  … ({len(result.actions) - 8} more)")
        if result.final_state:
            # Strip large base64 payloads before logging.
            redacted = {
                k: (f"<{len(v)} bytes>" if isinstance(v, str) and len(v) > 200 else v)
                for k, v in result.final_state.items()
            }
            lines.append(f"Final state: {redacted}")
        return "\n".join(lines)
