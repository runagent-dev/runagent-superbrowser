"""In-session help advisor for `browser_request_help`.

Background
----------
Arch v2: when a worker called `browser_request_help`, the orchestrator
treated it as a death signal and spawned a fresh worker with enriched
instructions. Every help call meant fresh conversation history, fresh
loop-detector state, fresh budget — token-expensive.

Arch v3: the first 3 help calls per task become *synchronous advisor*
calls that return tactical advice as a tool result and let the worker
continue in the same conversation. Only after 3 advisor calls do we
fall back to spawning a successor.

The advisor is a single Gemini Flash call. It takes the brief, last
~5 step_history entries, current vision_state, and failed_tactics, and
returns 1-2 paragraphs of next-step advice — what to try, what NOT to
try (because it already failed), and which constraint to focus on.

Failure mode
------------
If the LLM call fails (network/quota/parse), the advisor returns a
deterministic fallback message that the brain can still act on. The
help call is NEVER a hard error.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Iterable, Optional


_ADVISOR_SYSTEM_PROMPT = """\
You advise a browser automation agent that just got stuck. The agent is
operating a browser via tools (browser_screenshot, browser_click_at,
browser_type_at, browser_navigate, etc.) to satisfy a user task. You are
NOT the agent — you are a tactical advisor giving the agent its next
move in plain text.

Input shape:
  - TASK_BRIEF: the original user query, structured constraints with
    status, plan_of_attack
  - CURRENT URL + brief PAGE STATE
  - LAST STEPS: compressed log of the last few tool calls and outcomes
  - FAILED TACTICS: short labels of approaches the agent already tried
    and that did not work — DO NOT recommend any of these
  - REASON: the agent's stated reason for asking for help
  - FAILED_BBOXES: V_n indexes the agent referenced that did not produce
    progress

Output (plain text, NOT JSON, 1-2 paragraphs):
  - Name the SINGLE next concrete action you recommend (e.g. "scroll
    down 800px to find the price filter", "click the visible 'Apply
    filters' button", "navigate to /search?q=...").
  - State which constraint this advances and why.
  - If a different observation tool would help first
    (browser_state_check, browser_get_markdown, browser_eval), say so
    and what to look for.
  - If the agent should give up on a constraint (mark not_applicable),
    say so explicitly with the canonical_value.

Be terse. No filler ("Sure!", "I'd suggest"). No headers or bullets.
Imperative voice. Under 180 words.
"""


def _format_advisor_user(payload: dict[str, Any]) -> str:
    """Compose the user prompt from the worker's snapshot dict."""
    parts: list[str] = []
    brief = payload.get("brief")
    if isinstance(brief, dict):
        oq = (brief.get("original_query") or "")[:1000]
        if oq:
            parts.append(f"TASK_BRIEF.original_query:\n  {oq}")
        plan = (brief.get("plan_of_attack") or "")[:400]
        if plan:
            parts.append(f"TASK_BRIEF.plan_of_attack:\n  {plan}")
        constraints = brief.get("constraints") or []
        if constraints:
            lines = ["TASK_BRIEF.constraints:"]
            for c in constraints:
                if not isinstance(c, dict):
                    continue
                marker = c.get("status", "unverified")
                cv = c.get("canonical_value") or c.get("text") or ""
                kind = c.get("kind", "filter")
                op = c.get("operator", "")
                th = c.get("threshold", "")
                ev = c.get("evidence", "")
                bit = f"  [{marker}] ({kind}) {cv}"
                if op:
                    bit += f" {op}"
                    if th:
                        bit += f" {th}"
                if ev:
                    bit += f"  evidence={ev[:80]!r}"
                lines.append(bit)
            parts.append("\n".join(lines))
    url = payload.get("current_url")
    if url:
        parts.append(f"CURRENT URL: {url}")
    page_state = payload.get("page_state")
    if isinstance(page_state, dict):
        ps_compact = json.dumps(page_state, default=str)[:1200]
        parts.append(f"PAGE STATE: {ps_compact}")
    last_steps = payload.get("last_steps") or []
    if last_steps:
        lines = ["LAST STEPS (most recent last):"]
        for s in last_steps[-6:]:
            if not isinstance(s, dict):
                continue
            tool = s.get("tool", "?")
            args_summary = (s.get("args_summary") or "")[:80]
            outcome = s.get("result_outcome", "")
            lines.append(f"  - {tool}({args_summary}) -> {outcome}")
        parts.append("\n".join(lines))
    failed_tactics = payload.get("failed_tactics") or []
    if failed_tactics:
        parts.append(
            "FAILED TACTICS (already tried, do NOT suggest):\n  "
            + "\n  ".join(f"- {t}" for t in failed_tactics[:12])
        )
    failed_bboxes = payload.get("failed_bboxes") or []
    if failed_bboxes:
        parts.append(
            "FAILED_BBOXES (V_n that did not work): "
            + ", ".join(str(b) for b in failed_bboxes[:8])
        )
    reason = payload.get("reason") or ""
    if reason:
        parts.append(f"REASON: {reason[:600]}")
    return "\n\n".join(parts)


async def advise(payload: dict[str, Any]) -> str:
    """Return tactical advice for the agent. Always returns a string;
    the caller injects it as the help-tool result.
    """
    user = _format_advisor_user(payload)
    if not user:
        return _fallback_advice(payload)

    api_key = (
        os.environ.get("VISION_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    )
    if not api_key:
        return _fallback_advice(payload)

    model = os.environ.get("HELP_ADVISOR_MODEL") or "gemini-2.5-flash"
    base_url = os.environ.get(
        "HELP_ADVISOR_BASE_URL"
    ) or "https://generativelanguage.googleapis.com/v1beta/openai"

    try:
        from openai import AsyncOpenAI  # type: ignore
    except Exception:
        return _fallback_advice(payload)

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=12.0)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _ADVISOR_SYSTEM_PROMPT},
                {"role": "user", "content": user[:6000]},
            ],
            max_tokens=400,
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return _fallback_advice(payload)
        return text[:2000]
    except Exception as exc:
        print(f"[help_advisor] LLM call failed: {exc}")
        return _fallback_advice(payload)
    finally:
        try:
            await client.close()
        except Exception:
            pass


def advise_sync(payload: dict[str, Any]) -> str:
    """Sync wrapper. Used by the request_help tool which runs in a
    nanobot-sync tool handler.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(advise(payload))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(asyncio.run, advise(payload))
        return fut.result(timeout=15.0)


def _fallback_advice(payload: dict[str, Any]) -> str:
    """Deterministic fallback when the LLM is unreachable.

    Reads the brief and points the agent at the highest-priority
    unverified constraint, with a generic action recipe.
    """
    brief = payload.get("brief") or {}
    constraints = (
        brief.get("constraints") if isinstance(brief, dict) else []
    ) or []
    unverified = [
        c for c in constraints
        if isinstance(c, dict) and c.get("status") == "unverified"
    ]
    if not unverified:
        return (
            "Advisor unavailable. All constraints look verified or "
            "failed — call browser_state_check to confirm, then call "
            "done() with a final_answer summarising what's known."
        )
    target = unverified[0]
    cv = target.get("canonical_value") or target.get("text") or ""
    kind = target.get("kind", "filter")
    if kind == "filter":
        return (
            f"Advisor unavailable. Focus next: verify constraint "
            f"{cv!r}. Call browser_state_check first to see if it's "
            f"already applied. If not, locate the filter panel "
            f"(scroll if needed) and apply the matching toggle. "
            f"Call browser_verify_action after the click."
        )
    if kind == "numeric":
        op = target.get("operator", "lte")
        th = target.get("threshold", "")
        return (
            f"Advisor unavailable. Focus next: verify the numeric "
            f"constraint ({op} {th}) on {cv!r}. Use the visible price/"
            f"range filter if any; otherwise read the result list and "
            f"manually filter. Mark not_applicable if the site doesn't "
            f"expose this filter."
        )
    if kind == "negative":
        return (
            f"Advisor unavailable. Verify the negative constraint "
            f"(NOT {cv!r}). Read the result detail page or filter chip "
            f"to confirm absence. Mark satisfied when confirmed."
        )
    return (
        f"Advisor unavailable. Focus next: address constraint "
        f"{cv!r} ({kind}). If you cannot make progress in 3 turns, "
        f"call browser_request_help again with REASON='advisor_failed'."
    )


__all__ = ["advise", "advise_sync"]
