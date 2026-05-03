"""Tier-agnostic evaluate / state helpers.

Both `action_planner.py` and `verify_action.py` (and the t3 escalation in
`session_tools.py`) historically called `t3manager.evaluate(sid, js, arg)`
and `t3manager.state(sid)` directly. That gated those layers to t3-only
sessions even though the underlying JS probes are pure DOM and the TS
server already exposes a `/session/:id/evaluate` endpoint that works for
t1 sessions.

This module returns a small backend object whose `.evaluate(sid, js, arg)`
and `.state(sid)` mirror the T3SessionManager methods. Callers don't
change shape — they swap `t3manager` for `get_backend(session_id)`.

For `t3-` session IDs the returned backend IS the in-process
T3SessionManager. For other session IDs the backend is a thin httpx
wrapper that POSTs to `/session/{sid}/evaluate` and GETs
`/session/{sid}/state`.

When passing an `arg` to a function-literal script through the t1 path,
the script string and the arg are merged into a single self-contained
expression on the Python side, since the TS `/evaluate` route accepts
`{script}` only (no separate arg parameter).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


SUPERBROWSER_URL = os.environ.get("SUPERBROWSER_URL", "http://localhost:3100")


def _auth_headers() -> dict[str, str]:
    tok = os.environ.get("SUPERBROWSER_TOKEN") or os.environ.get("TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _is_function_literal(script: str) -> bool:
    """Heuristic: does this script start like a function literal?

    Mirrors the same family of shapes the t3 evaluate handles
    (interactive_session.py:2061): arrow fns, async arrows, named
    function decls, IIFEs, block bodies. If yes, we can wrap-and-call
    it with the arg inlined; if no, the caller's script is presumed
    self-contained and we leave it alone.
    """
    body = script.strip()
    return body.startswith(("(", "async ", "async(", "function", "=>", "{"))


def _wrap_statement_body_for_t1(script: str) -> str:
    """If script is a statement body (top-level `return`, multi-statement),
    wrap it in `(async () => { <body> })()` so Puppeteer's
    `page.evaluate(string)` can evaluate it. Mirrors the auto-wrap in
    `T3SessionManager.evaluate` at interactive_session.py:2061-2076.
    Function literals and bare expressions pass through unchanged.
    """
    body = script.strip()
    if not body or _is_function_literal(body):
        return script
    import re as _re
    has_top_return = _re.search(r"(?:^|[\s;{])return(?:$|[\s(;])", body) is not None
    has_multi_stmt = ";" in body.rstrip(" \t\n;")
    if has_top_return or has_multi_stmt:
        return f"(async () => {{ {body} }})()"
    return script


def _inline_arg_into_script(script: str, arg: Any) -> str:
    """Rewrite `(arg) => body` + arg → `((script))(<json>)` for t1 transport.

    Only the function-literal shapes get rewritten. Statement bodies and
    bare expressions that don't take an arg are returned unchanged — the
    arg is silently dropped, matching t3's behavior in interactive_session
    when a wrap was needed (interactive_session.py:2073).
    """
    if not _is_function_literal(script):
        return script
    arg_json = json.dumps(arg)
    return f"(({script}))({arg_json})"


class _T1Backend:
    """Routes evaluate / state to the TS server for non-t3 sessions.

    Exposes `.evaluate(sid, script, arg=None)` and `.state(sid)` so it's
    drop-in compatible with `T3SessionManager`. Callers shouldn't need
    to know which backend they got.
    """

    async def evaluate(self, sid: str, script: str, arg: Any = None) -> Any:
        if arg is not None:
            payload_script = _inline_arg_into_script(script, arg)
        else:
            payload_script = script
        # Wrap statement bodies into an IIFE so Puppeteer's
        # page.evaluate(string) doesn't trip on top-level `return`.
        payload_script = _wrap_statement_body_for_t1(payload_script)
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{sid}/evaluate",
                json={"script": payload_script},
                headers=_auth_headers(),
            )
            r.raise_for_status()
            body = r.json()
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        return body

    async def state(self, sid: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{sid}/state",
                headers=_auth_headers(),
            )
            r.raise_for_status()
            body = r.json()
        return body if isinstance(body, dict) else {}


def get_backend(session_id: str) -> Any:
    """Return a backend that exposes `.evaluate(sid, js, arg)` + `.state(sid)`.

    For `t3-` prefixed session IDs the backend is the in-process
    T3SessionManager singleton. For all other session IDs (t1, future
    tiers) the backend is a `_T1Backend` that hits the TS server's
    HTTP routes. The returned object's signature matches T3SessionManager
    so existing call sites work unchanged.
    """
    if session_id and session_id.startswith("t3-"):
        from superbrowser_bridge.antibot import interactive_session as _t3
        return _t3.default()
    return _T1Backend()


__all__ = ["get_backend"]
