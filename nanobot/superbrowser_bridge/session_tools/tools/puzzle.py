"""Puzzle-solver tool — slider/chess/etc. via puzzle_solvers package."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403

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


