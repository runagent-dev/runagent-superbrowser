"""Pixel-exact chess-move tool.

`browser_chess_move` moves a piece on a chess.com board by algebraic square
(e.g. "g1"->"f3"). It anchors on the board's own bounding rect and subdivides
it 8x8, so the source/target land dead-centre on the square instead of on a
vision-eyeballed guess that misses and gets the move rejected. Reuses the
chess_com solver's geometry (`_BoardView`, `square_code`) and auto-handles
board flip when you play Black.

Why DOM-rect (not vision) here: CDP mouse input is in CSS pixels — the same
space `getBoundingClientRect()` returns — so there's no device-pixel-ratio
rescale to drift on, and an empty target square has no visual feature for
vision to lock onto anyway. Subdividing one exact board box also divides any
residual error by 8.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState

_SQUARE = re.compile(r"^[a-h][1-8]$")

# Board rect + orientation + occupied square-codes read straight from the DOM,
# so move verification works regardless of whether the page exposes a game API.
# `.piece` elements carry a `square-<file><rank>` class (a knight on g1 ->
# "square-71"; file a=1..h=8, rank 1..8).
_BOARD_SCRIPT = r"""
(() => {
  const board = document.querySelector('wc-chess-board, chess-board, .board-vs-personalities-wrapper, .board');
  if (!board) return { boardRect: null };
  const r = board.getBoundingClientRect();
  const squares = [...document.querySelectorAll('.piece')].map((el) => {
    const m = String(el.className || '').match(/square-(\d\d)/);
    return m ? m[1] : null;
  }).filter(Boolean);
  return {
    boardRect: { x: r.x, y: r.y, w: r.width, h: r.height },
    orientation: board.classList.contains('flipped') ? 'black' : 'white',
    squares,
  };
})();
"""


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        from_square=StringSchema("Source square in algebraic notation, e.g. 'e2' or 'g1'."),
        to_square=StringSchema("Destination square in algebraic notation, e.g. 'e4' or 'f3'."),
        method=StringSchema(
            "'auto' (default): real drag, then fall back to click-to-move only "
            "if the piece provably didn't move. 'drag': mouse drag only. "
            "'click': click source then destination.",
            nullable=True,
        ),
        promo=StringSchema(
            "Promotion piece for a promoting pawn move: q|r|b|n. Best-effort — "
            "most chess.com UIs default to queen.",
            nullable=True,
        ),
        required=["session_id", "from_square", "to_square"],
    )
)
class BrowserChessMoveTool(Tool):
    name = "browser_chess_move"
    description = (
        "Move a chess piece on a chess.com board by algebraic squares "
        "(from_square='g1', to_square='f3'). Anchors on the board's bounding "
        "rect and subdivides 8x8 for pixel-EXACT square centres (auto-handles "
        "board flip when you play Black), then performs a real drag — no vision "
        "guessing. ALWAYS PREFER THIS over browser_drag / browser_drag_path / "
        "browser_drag_selectors for chess.com moves: eyeballed coordinates miss "
        "the square centre and the move silently fails. Reports whether the move "
        "actually landed."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def _read_board(self, session_id: str) -> Optional[dict[str, Any]]:
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": _BOARD_SCRIPT},
                timeout=15.0,
            )
            r.raise_for_status()
            res = r.json().get("result")
            return res if isinstance(res, dict) else None
        except Exception:
            return None

    async def _occupied(self, session_id: str) -> Optional[set[str]]:
        board = await self._read_board(session_id)
        if not board:
            return None
        sqs = board.get("squares")
        if not isinstance(sqs, list):
            return None
        return {s for s in sqs if isinstance(s, str)}

    async def _drag(self, session_id: str, fx: float, fy: float, tx: float, ty: float) -> None:
        # /drag -> page.dragTo -> humanDrag: smooth Bezier path, button held
        # the whole way (buttons:1), release at the EXACT target.
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag",
            json={"startX": fx, "startY": fy, "endX": tx, "endY": ty},
            timeout=30.0,
        )
        r.raise_for_status()

    async def _click_to_move(
        self, session_id: str, fx: float, fy: float, tx: float, ty: float,
    ) -> None:
        # Click source then destination (drag-path with identical points) —
        # chess.com's canonical click-to-move input.
        for (cx, cy) in ((fx, fy), (tx, ty)):
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/drag-path",
                json={"points": [{"x": cx, "y": cy}, {"x": cx, "y": cy}]},
                timeout=30.0,
            )
            r.raise_for_status()
            await asyncio.sleep(0.12)

    async def execute(
        self,
        session_id: str,
        from_square: str,
        to_square: str,
        method: str | None = None,
        promo: str | None = None,
        **kw: Any,
    ) -> str:
        fs = (from_square or "").strip().lower()
        ts = (to_square or "").strip().lower()
        if not _SQUARE.match(fs) or not _SQUARE.match(ts):
            return (
                "[chess_move_failed] squares must be algebraic like 'e2'->'e4'; "
                f"got {from_square!r}->{to_square!r}"
            )
        method = (method or "auto").lower()
        if method not in ("auto", "drag", "click"):
            return f"[chess_move_failed] method must be auto|drag|click, got {method!r}"

        print(f"\n>> browser_chess_move({fs}->{ts}, method={method})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        # Canonical board geometry lives in the chess_com solver — reuse it so
        # the move tool and the solver never disagree on square centres.
        from superbrowser_bridge.puzzle_solvers.chess_com import _BoardView, square_code

        board = await self._read_board(session_id)
        if not board or not board.get("boardRect"):
            return (
                "[chess_move_failed] no chess board found on screen (looked for "
                "wc-chess-board / .board). Open the board first."
            )
        rect = board["boardRect"]
        orientation = board.get("orientation", "white")
        before = {s for s in (board.get("squares") or []) if isinstance(s, str)}
        from_code, to_code = square_code(fs), square_code(ts)

        view = _BoardView(rect=rect, orientation=orientation)
        fx, fy = view.square_point(fs)
        tx, ty = view.square_point(ts)

        try:
            used = method
            if method == "click":
                await self._click_to_move(session_id, fx, fy, tx, ty)
            else:
                await self._drag(session_id, fx, fy, tx, ty)
                if method == "auto":
                    await asyncio.sleep(0.2)  # let chess.com's move animation settle
                    after = await self._occupied(session_id)
                    # Fall back to click-to-move only if the drag PROVABLY
                    # failed (source piece still on its square). Guards against
                    # a second, spurious move when the drag actually worked.
                    if after is not None and from_code in after:
                        used = "drag+click"
                        await self._click_to_move(session_id, fx, fy, tx, ty)
        except Exception as exc:
            return f"[chess_move_failed] move dispatch error: {exc}"

        await asyncio.sleep(0.2)
        after = await self._occupied(session_id)
        if after is None:
            landed: Optional[bool] = None
        else:
            # A move succeeded iff the source square emptied AND the target
            # filled — robust for quiet moves, captures, and en passant.
            landed = (from_code not in after) and (to_code in after)

        coord = f"({fx:.0f},{fy:.0f})->({tx:.0f},{ty:.0f})"
        self.s.record_step("browser_chess_move", f"{fs}->{ts} via {used} {coord}", "")

        if landed is True:
            status = "move registered OK"
        elif landed is False:
            status = (
                "move did NOT register - confirm it's your turn, re-screenshot, "
                "and retry (try method='click')"
            )
        else:
            status = "executed (could not auto-verify from DOM - confirm via screenshot)"
        warn = ""
        if before and from_code not in before:
            warn = " [warn: source square looked empty before the move]"
        promo_note = f" promo={promo}" if promo else ""
        return (
            f"Chess {fs}->{ts} [{orientation} board] via {used} at exact "
            f"{coord}{promo_note}: {status}.{warn}"
        )
