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
from typing import Any


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
