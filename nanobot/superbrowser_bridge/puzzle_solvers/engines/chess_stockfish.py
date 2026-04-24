"""Stockfish wrapper for the chess solver.

Requires:
  - `python-chess` (pip install python-chess)
  - Stockfish binary (apt install stockfish — expected at /usr/games/stockfish)

The engine is opened lazily on the first call and cached per-process. It's
closed at interpreter shutdown via an atexit hook so we don't leak the
child process between test runs.
"""

from __future__ import annotations

import os
from typing import Optional

import chess
import chess.engine


_DEFAULT_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")


def _ensure_path(path: str) -> None:
    if not os.path.exists(path):
        raise RuntimeError(
            f"Stockfish binary not found at {path}. "
            "Install with `apt install stockfish` or set $STOCKFISH_PATH."
        )


def best_move(
    fen: str,
    *,
    time_s: float = 0.3,
    depth: Optional[int] = None,
    path: str = _DEFAULT_PATH,
) -> str:
    """Return the best move in UCI notation (e.g. 'e2e4', 'g7g8q' for promotion).

    The engine subprocess is started and closed per call. Stockfish
    startup is ~30ms so this is cheap in absolute terms, and it dodges
    python-chess's shutdown deadlock at interpreter exit (a cached engine
    leaves a non-daemon thread running during teardown).
    """
    _ensure_path(path)
    board = chess.Board(fen)
    limit = chess.engine.Limit(depth=depth) if depth else chess.engine.Limit(time=time_s)
    with chess.engine.SimpleEngine.popen_uci(path) as eng:
        result = eng.play(board, limit)
    if result.move is None:
        raise RuntimeError(f"Stockfish returned no move for FEN {fen!r}")
    return result.move.uci()


def legal_moves(fen: str) -> list[str]:
    """List legal UCI moves from a FEN. Useful for tests + sanity-checking."""
    board = chess.Board(fen)
    return [m.uci() for m in board.legal_moves]


def apply_move(fen: str, uci: str) -> str:
    """Return FEN after applying a UCI move. Raises on illegal moves."""
    board = chess.Board(fen)
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        raise ValueError(f"Illegal move {uci} in position {fen}")
    board.push(move)
    return board.fen()
