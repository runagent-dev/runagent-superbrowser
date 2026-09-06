"""LLM history compressor for the ``summary`` memory policy.

Uses the same host model the agent runs on (``bot._loop.provider`` /
``bot._loop.model``) so the arm differs from the others only in HOW older
history is retained, not in which model reasons about it. Tokens are booked
under the usage role ``compressor`` and every call is logged as a
``compressor_call`` event; both feed the cost accounting.

The prompt is an ACON-style structured summary (fixed sections, verbatim
values only where still useful, merge-not-append with the previous summary)
capped at the arm's history budget.
"""
from __future__ import annotations

import time
from typing import Any

from loguru import logger

COMPRESSOR_INPUT_CAP_TOKENS = 24_000

COMPRESSOR_SYSTEM_PROMPT = """You maintain a compact, structured summary of a browser agent's earlier actions so the agent can continue the task after the raw history has been archived. You are given the PREVIOUS SUMMARY (may be empty) and the NEW HISTORY (oldest first). Produce ONE merged summary that REPLACES the previous one — merge, do not append.

Rules:
- Keep exact values verbatim when they may still be needed: URLs, IDs, prices, dates, filter values, form inputs, error messages, element labels. Never paraphrase an identifier.
- Record what was tried and FAILED so it is not retried.
- No speculation, no advice, no restating the task instruction.
- Hard limit: about {budget} tokens. Prefer dropping narration over dropping values.

Output exactly these sections (omit a section only if truly empty):
### TASK PROGRESS
### CURRENT STATE (url, page, login state, filters applied)
### KEY FACTS (verbatim values, ids, prices, dates)
### FAILED ATTEMPTS — DO NOT RETRY
### OPEN ITEMS"""


def _usage_dict(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None) or {}
    if not isinstance(u, dict):
        try:
            u = dict(u)
        except Exception:
            u = {}
    return {k: int(v) for k, v in u.items() if isinstance(v, (int, float))}


async def compress_history(bot: Any, memory: Any, prev_summary: str, chunks: list[str], *,
                           budget: int, iteration: int) -> str | None:
    """Return the new summary text, or None on failure (caller retries / falls back)."""
    loop = getattr(bot, "_loop", None)
    provider = getattr(loop, "provider", None)
    model = getattr(loop, "model", None)
    if provider is None:
        logger.debug("compressor: no provider bound (bot={})", type(bot).__name__ if bot else None)
        return None
    try:
        from nanobot.utils.helpers import truncate_text_to_tokens
    except Exception:  # pragma: no cover
        truncate_text_to_tokens = None  # type: ignore[assignment]

    user = (f"PREVIOUS SUMMARY:\n{prev_summary or '(none)'}\n\nNEW HISTORY (oldest first):\n"
            + "\n".join(chunks))
    if truncate_text_to_tokens is not None:
        try:
            user = truncate_text_to_tokens(user, COMPRESSOR_INPUT_CAP_TOKENS)
        except Exception:
            pass
    messages = [
        {"role": "system", "content": COMPRESSOR_SYSTEM_PROMPT.format(budget=budget)},
        {"role": "user", "content": user},
    ]
    t0 = time.monotonic()
    try:
        resp = await provider.chat_with_retry(
            messages=messages, tools=None, model=model, tool_choice=None,
            max_tokens=max(1024, min(4096, 2 * budget)),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("compressor call failed: {}", exc)
        try:
            memory.events.log("compressor_call", {"iter": iteration, "role": memory.role, "model": model,
                                                  "ok": False, "error": str(exc)[:200]})
        except Exception:
            pass
        return None
    usage = _usage_dict(resp)
    content = (getattr(resp, "content", None) or "").strip()
    finish = getattr(resp, "finish_reason", None)
    ok = bool(content) and finish != "error"
    try:
        from superbrowser_bridge.usage import record_brain

        record_brain("compressor", usage)
    except Exception:
        pass
    try:
        memory.events.log("compressor_call", {
            "iter": iteration, "role": memory.role, "model": model, "ok": ok, "finish_reason": finish,
            "tokens_in": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
            "tokens_out": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
            "chars_in": len(user), "chars_out": len(content), "ms": int((time.monotonic() - t0) * 1000),
        })
    except Exception:
        pass
    return content if ok else None
