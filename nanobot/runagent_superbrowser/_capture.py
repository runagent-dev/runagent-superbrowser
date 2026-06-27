"""Run the orchestrator and reliably capture its user-visible answer.

Ported from ``nanobot/test_superbrowser.py::_run_and_capture``. ``bot.run()``
returns ``result.content`` — the LLM's direct text. But when the orchestrator
(or a nested agent) delivers its answer through the ``message()`` tool instead
of direct text, ``result.content`` is an empty string and the answer goes out
over the bus. So we subscribe to the outbound bus for the duration of the turn,
collect non-progress / non-streaming messages, and fall back to the
bus-captured content when ``result.content`` is empty.

This is the single chokepoint both ``run`` and ``arun`` go through.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator


async def run_and_capture(
    bot: Any,
    framed_task: str,
    session_key: str,
    *,
    hooks: list[Any] | None = None,
    timeout: float | None = None,
) -> tuple[str, str]:
    """Return ``(final_text, raw_content)``.

    ``final_text`` prefers the direct ``result.content`` and falls back to the
    last message captured off the bus. ``raw_content`` is the direct content
    (may be empty). Raises ``asyncio.TimeoutError`` if ``timeout`` elapses.
    """
    bus = bot._loop.bus
    captured: list[str] = []
    stop = asyncio.Event()

    async def _pump() -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - bus closed/errored; stop pumping
                return
            md = msg.metadata or {}
            # Skip progress pings, streaming deltas/ends — not the final answer.
            if md.get("_progress") or md.get("_stream_delta") or md.get("_stream_end"):
                continue
            if msg.content:
                captured.append(msg.content)

    pump = asyncio.create_task(_pump())
    try:
        coro = bot.run(framed_task, session_key=session_key, hooks=hooks or [])
        if timeout is not None:
            result = await asyncio.wait_for(coro, timeout=timeout)
        else:
            result = await coro
    finally:
        # Give the pump a beat to drain any still-in-flight message.
        await asyncio.sleep(0.05)
        stop.set()
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    raw = (result.content or "").strip() if result else ""
    if raw:
        return raw, raw
    # Last capture wins — the final message() call is usually the definitive
    # answer (earlier ones may be progress breadcrumbs).
    return (captured[-1] if captured else ""), raw


def _progress_event(content: str, md: dict) -> dict | None:
    """Map a nanobot progress bus message to a structured stream event.

    nanobot publishes progress as outbound messages tagged ``_progress`` with,
    optionally, ``_tool_events`` (structured tool calls), ``_tool_hint``,
    ``_reasoning_delta`` (thinking text), ``_reasoning_end``, or
    ``_file_edit_events`` (see nanobot/bus/progress.py). We surface the
    step-level ones and drop empty pings / segment boundaries.
    """
    content = content or ""
    if md.get("_tool_events"):
        return {"type": "tool", "tools": md["_tool_events"], "message": content}
    if md.get("_file_edit_events"):
        return {"type": "file_edit", "edits": md["_file_edit_events"], "message": content}
    if md.get("_reasoning_delta"):
        return {"type": "thinking", "text": content} if content else None
    if md.get("_reasoning_end"):
        return None
    if md.get("_tool_hint"):
        return {"type": "tool_hint", "message": content} if content else None
    if content:
        return {"type": "status", "message": content}
    return None


async def stream_and_capture(
    bot: Any,
    framed_task: str,
    session_key: str,
    *,
    hooks: list[Any] | None = None,
    timeout: float | None = None,
) -> AsyncIterator[dict]:
    """Run the orchestrator and yield step-level progress events as they happen.

    The streaming counterpart of :func:`run_and_capture`. It owns the outbound
    bus for the turn, turning nanobot's progress pings / tool events into
    structured ``{"type": ...}`` dicts, and finishes with a single
    ``{"type": "result", ...}`` event carrying the final answer. Token-level
    deltas (``_stream_delta`` / ``_stream_end``) are skipped — this is a
    step-level stream; the full answer arrives in the result event.
    """
    bus = bot._loop.bus
    captured: list[str] = []
    run = asyncio.create_task(
        bot.run(framed_task, session_key=session_key, hooks=hooks or [])
    )
    result = None
    error: str | None = None
    timed_out = False
    deadline = (time.monotonic() + timeout) if timeout else None
    try:
        # Single consumer of the bus: consume_outbound() returns pending messages
        # immediately and only times out once the bus is empty, so we drain every
        # message the run published before breaking — no lost-event race.
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)
            except asyncio.TimeoutError:
                if run.done():
                    break
                if deadline and time.monotonic() > deadline:
                    timed_out = True
                    break
                continue
            except Exception:  # noqa: BLE001 - bus closed/errored; stop consuming
                if run.done():
                    break
                continue
            md = msg.metadata or {}
            if md.get("_stream_delta") or md.get("_stream_end"):
                continue
            if md.get("_progress"):
                ev = _progress_event(msg.content, md)
                if ev is not None:
                    yield ev
                continue
            if msg.content:
                captured.append(msg.content)
                yield {"type": "message", "text": msg.content}

        if timed_out:
            run.cancel()
        elif run.done():
            exc = run.exception()
            if exc is not None:
                error = f"{type(exc).__name__}: {exc}"
            else:
                result = run.result()

        raw = (result.content or "").strip() if result else ""
        final_text = raw or (captured[-1] if captured else "")
        if timed_out:
            error = f"task timed out after {timeout}s"
        yield {
            "type": "result",
            "text": final_text,
            "raw_content": raw,
            "success": bool(final_text) and not timed_out and error is None,
            "error": error if error else (None if final_text else "the agent returned no answer"),
        }
    finally:
        if not run.done():
            run.cancel()
        try:
            await run
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
