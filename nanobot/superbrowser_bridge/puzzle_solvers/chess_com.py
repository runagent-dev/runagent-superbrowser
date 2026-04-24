"""chess.com puzzle solver.

**Strategy**
1. *Detect*: URL contains "chess.com/puzzle" or "chess.com/play" + page exposes `wc-chess-board`.
2. *Extract state*: prefer `window.chessboard.game.getFEN()` when exposed; otherwise parse
   the piece DOM (`.piece.wp.square-52`) into a FEN. Also determine side-to-move from the
   puzzle side indicator.
3. *Plan*: Stockfish → best UCI move.
4. *Execute*: convert source/target squares to pixel centres using the `wc-chess-board`
   bounding rect + file/rank math (most reliable — no reliance on selectors that may or
   may not exist for empty squares). Drag piece via two raw clicks (click-to-move, which
   chess.com supports and which bypasses react-dnd quirks).
5. *Verify*: success is either a "Puzzle solved" banner (`.puzzle-correct-modal`, etc.)
   or the FEN advancing beyond the solver's planned line.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .base import Action, SolverBrowser

logger = logging.getLogger(__name__)


# chess.com encodes squares as class `.square-<file><rank>` where both are
# single digits 1-8 (file a=1 … h=8, rank 1..8). Examples:
#   e2 → square-52 ; h8 → square-88 ; a1 → square-11
_FILE_TO_DIGIT = {f: i + 1 for i, f in enumerate("abcdefgh")}
_DIGIT_TO_FILE = {v: k for k, v in _FILE_TO_DIGIT.items()}


def square_code(sq: str) -> str:
    """'e2' → '52'."""
    if len(sq) != 2 or sq[0] not in _FILE_TO_DIGIT or sq[1] not in "12345678":
        raise ValueError(f"bad square: {sq!r}")
    return f"{_FILE_TO_DIGIT[sq[0]]}{sq[1]}"


def square_from_code(code: str) -> str:
    """'52' → 'e2'."""
    if len(code) != 2:
        raise ValueError(f"bad square code: {code!r}")
    return f"{_DIGIT_TO_FILE[int(code[0])]}{code[1]}"


# Chess.com's piece class encoding:
#   `w`|`b`  (colour) + `p|n|b|r|q|k` (role) — e.g. "wp" white pawn, "bk" black king.
_PIECE_TO_FEN = {
    "wp": "P", "wn": "N", "wb": "B", "wr": "R", "wq": "Q", "wk": "K",
    "bp": "p", "bn": "n", "bb": "b", "br": "r", "bq": "q", "bk": "k",
}


def board_from_piece_dom(pieces: list[dict[str, str]]) -> dict[str, str]:
    """Convert a list of {'piece': 'wp', 'square': '52'} → {'e2': 'P'} map.

    This is a side-effect-free helper so it's easy to unit-test with
    fixtures. `pieces` is what we expect the in-page evaluator to return.
    """
    board: dict[str, str] = {}
    for entry in pieces:
        piece_cls = entry.get("piece", "")
        sq_code = entry.get("square", "")
        symbol = _PIECE_TO_FEN.get(piece_cls)
        if not symbol or not sq_code:
            continue
        try:
            sq = square_from_code(sq_code)
        except Exception:
            continue
        board[sq] = symbol
    return board


def board_map_to_fen_placement(board: dict[str, str]) -> str:
    """{'e2': 'P', ...} → rank-8/.../rank-1 FEN placement string."""
    rows: list[str] = []
    for rank in range(8, 0, -1):
        row = ""
        empty = 0
        for file in "abcdefgh":
            sq = f"{file}{rank}"
            if sq in board:
                if empty:
                    row += str(empty)
                    empty = 0
                row += board[sq]
            else:
                empty += 1
        if empty:
            row += str(empty)
        rows.append(row)
    return "/".join(rows)


# Page-side script to pull the board state. Prefer the game API when
# available (chess.com exposes `window.chessboardElement?.game.getFEN()`
# in several recent versions); fall back to DOM-parsing.
_EXTRACT_SCRIPT = r"""
(() => {
  const out = { source: 'unknown', fen: null, pieces: [], orientation: 'white', solved: null, turn: null, boardRect: null };
  // Board rect (bounding client rect for whichever custom element hosts the board).
  const board = document.querySelector('wc-chess-board, chess-board, .board-vs-personalities-wrapper, .board');
  if (board) {
    const r = board.getBoundingClientRect();
    out.boardRect = { x: r.x, y: r.y, w: r.width, h: r.height };
    // Orientation: chess.com flips via `.flipped` class on the board wrapper
    out.orientation = board.classList.contains('flipped') ? 'black' : 'white';
  }
  // Puzzle-solved indicator (covers multiple versions of the UI).
  const solved =
    document.querySelector('.puzzle-correct-modal, .correct-move-text, .puzzle-solved, .correct') ||
    [...document.querySelectorAll('*')].find(el => /puzzle solved/i.test((el.innerText||'').slice(0, 60)));
  if (solved) out.solved = true;
  // Side-to-move: "Your Turn" badge or similar
  const yt = [...document.querySelectorAll('*')].find(el => /your turn/i.test((el.innerText||'').slice(0, 30)));
  out.turn = yt ? 'mine' : null;
  // Preferred path: direct game API.
  try {
    const api = (board && (board.game || (board.gameObj && board.gameObj)))
              || window.chess
              || (window.chessboard && window.chessboard.game);
    if (api && typeof api.getFEN === 'function') {
      out.fen = api.getFEN();
      out.source = 'api';
      return out;
    }
  } catch (e) { /* fall through */ }
  // DOM parse: pieces carry both colour-role and square-code classes.
  const pieceNodes = document.querySelectorAll(
    'wc-chess-board .piece, chess-board .piece, .piece'
  );
  for (const node of pieceNodes) {
    const classes = (node.className || '').split(/\s+/);
    let piece = null, square = null;
    for (const c of classes) {
      // Two-char role like wp, bk, wq
      if (/^[wb][pnbrqk]$/.test(c)) piece = c;
      const m = c.match(/^square-(\d\d)$/);
      if (m) square = m[1];
    }
    if (piece && square) out.pieces.push({ piece, square });
  }
  if (out.pieces.length) out.source = 'dom';
  return out;
})();
"""


@dataclass
class _BoardView:
    rect: dict[str, float]
    orientation: str  # 'white' | 'black'

    def square_point(self, sq: str) -> tuple[float, float]:
        """CSS pixel centre of square `sq` ('e4') given the board rect + orientation."""
        file = _FILE_TO_DIGIT[sq[0]]  # 1..8
        rank = int(sq[1])             # 1..8
        rect = self.rect
        sq_w = rect["w"] / 8.0
        sq_h = rect["h"] / 8.0
        if self.orientation == "white":
            cx = rect["x"] + (file - 0.5) * sq_w
            cy = rect["y"] + (8 - rank + 0.5) * sq_h
        else:  # black orientation: a1 is top-right
            cx = rect["x"] + (8 - file + 0.5) * sq_w
            cy = rect["y"] + (rank - 0.5) * sq_h
        return cx, cy


class ChessComSolver:
    name = "chess_com"

    def detect(self, url: str, dom_snapshot: Optional[str]) -> float:
        u = (url or "").lower()
        if "chess.com" not in u:
            return 0.0
        if any(seg in u for seg in ("/puzzle", "/puzzles", "/play", "/analysis", "/live")):
            return 0.9
        if dom_snapshot and "wc-chess-board" in dom_snapshot:
            return 0.7
        return 0.3

    async def extract_state(self, browser: SolverBrowser) -> dict[str, Any]:
        # `evaluateScript` returns whatever `page.evaluate` returns. The
        # IIFE returns a plain object; httpx will give us a dict.
        raw = await browser.evaluate(_EXTRACT_SCRIPT)
        if not isinstance(raw, dict):
            return {
                "fen": None, "boardRect": None, "orientation": "white",
                "solved": False, "turn": None, "source": "error",
                "error": f"unexpected evaluate result: {type(raw).__name__}",
            }
        board_rect = raw.get("boardRect")
        orientation = raw.get("orientation", "white")
        fen = raw.get("fen")
        if not fen and raw.get("pieces"):
            placement = board_map_to_fen_placement(
                board_from_piece_dom(raw.get("pieces") or [])
            )
            # Minimal FEN: side-to-move, castling, en-passant, halfmove, fullmove unknown.
            # Stockfish will accept "w" or "b" — we default to "w" and rely on the
            # engine's best-move being side-insensitive-enough. For puzzles,
            # chess.com usually orients the board for the side to move, so
            # orientation is a decent proxy.
            fen = f"{placement} {'w' if orientation == 'white' else 'b'} - - 0 1"
        return {
            "fen": fen,
            "boardRect": board_rect,
            "orientation": orientation,
            "solved": bool(raw.get("solved")),
            "turn": raw.get("turn"),
            "source": raw.get("source", "unknown"),
        }

    async def plan_actions(self, state: dict[str, Any]) -> list[Action]:
        fen = state.get("fen")
        rect = state.get("boardRect")
        if not fen:
            return [Action(kind="noop", reason="no_fen_extracted")]
        if not rect:
            return [Action(kind="noop", reason="no_board_rect")]
        try:
            from .engines.chess_stockfish import best_move
        except Exception as e:
            return [Action(kind="noop", reason=f"engine_unavailable: {e}")]
        try:
            uci = best_move(fen, time_s=0.3)
        except Exception as e:
            return [Action(kind="noop", reason=f"engine_error: {e}")]
        from_sq, to_sq = uci[:2], uci[2:4]
        promo = uci[4:] if len(uci) > 4 else None

        view = _BoardView(rect=rect, orientation=state.get("orientation", "white"))
        fx, fy = view.square_point(from_sq)
        tx, ty = view.square_point(to_sq)

        # Click-click (source, then destination) is the canonical chess.com
        # move input — safer than drag across all versions of the board.
        actions: list[Action] = [
            Action(
                kind="drag_path",
                points=[{"x": fx, "y": fy}, {"x": fx, "y": fy}],
                reason=f"source {from_sq}",
            ),
            Action(kind="wait", wait_ms=120, reason="click_click spacing"),
            Action(
                kind="drag_path",
                points=[{"x": tx, "y": ty}, {"x": tx, "y": ty}],
                reason=f"dest {to_sq}{promo or ''}",
            ),
        ]
        # TODO: handle promotion dialog — appears as a small popover over
        # the destination square. For now the default is queen on most
        # chess.com UIs (first option auto-clicks via Enter or first icon).
        if promo:
            actions.append(Action(kind="wait", wait_ms=250, reason="promo_dialog_settle"))
        return actions

    async def execute(
        self, browser: SolverBrowser, action: Action,
    ) -> dict[str, Any]:
        # Chess moves are coordinate-based, so delegate to default_execute
        # for wait/noop and extend with drag_path that uses identical points
        # as a click-at-xy.
        from .base import default_execute
        if action.kind == "drag_path":
            assert action.points
            return await browser.drag_path(action.points)
        return await default_execute(browser, action)

    async def verify(
        self, browser: SolverBrowser, state: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        if state.get("solved"):
            return True, state
        return False, state
