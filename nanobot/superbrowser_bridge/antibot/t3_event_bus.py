"""Per-session pub/sub for the T3 live viewer.

Mirrors the TS side's `inputEventBus` (`src/server/input-event-bus.ts` +
the WS fan-out in `src/server/websocket.ts`) so the Python viewer can
show the same live cursor / click / bbox / keystroke / screencast UX
without going through a TS-server round trip.

Event shape (pushed into every subscriber queue for the session):

    {"type": <str>, "session_id": <str>, "payload": <dict>}

`type` values: cursor_move, click_target, vision_bboxes, keystroke,
drag, navigation, screencast. Payload shape varies per type and is
documented on each emit_* method. The viewer's JS switches on `type`.

Subscriptions are session-scoped. A WS handler typically calls
`subscribe(sid)` on connect and iterates `queue.get()` until the
client disconnects, at which point `unsubscribe(sid, queue)` clears
its slot. Multiple viewers on the same session work — each gets its
own queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Minimum gap (seconds) between consecutive cursor-move events per
# session. Mouse humanization can fire 50+ move events per second;
# throttling at ~30 FPS keeps WS bandwidth + client render cost
# bounded without losing the "cursor moving" feel.
_CURSOR_THROTTLE_S = 0.033

# Per-subscriber queue bound — if a client is slow (network lag,
# backgrounded tab), events back up. Drop oldest rather than block
# producers. 256 fits ~8 seconds of 30 FPS cursor + room for other
# event types.
_QUEUE_MAXSIZE = 256


class T3EventBus:
    """Singleton-style bus. Subscribers register per session; emits
    fan out to every queue for that session.

    Thread-safety: designed for a single asyncio event loop (the one
    running the aiohttp server + patchright). Not re-entrant across
    loops. Adequate for the current single-process deployment.
    """

    def __init__(self) -> None:
        # session_id -> list of subscriber queues. List order is not
        # load-bearing; removal walks by identity so duplicate queues
        # are tolerated.
        self._subs: dict[str, list[asyncio.Queue]] = {}
        # session_id -> monotonic-seconds timestamp of last cursor_move
        # emit. Throttles mouse humanization spam.
        self._last_cursor_ts: dict[str, float] = {}

    # --- subscription surface ------------------------------------------

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Register a new subscriber for `session_id` and return its
        queue. Caller owns the queue and must call `unsubscribe` when
        done.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subs.setdefault(session_id, []).append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        """Remove a previously registered queue. No-op if it's not
        registered or the session has no subscribers.
        """
        lst = self._subs.get(session_id)
        if not lst:
            return
        try:
            lst.remove(q)
        except ValueError:
            pass
        if not lst:
            self._subs.pop(session_id, None)
            # Clear throttle bookkeeping too — stale entry would leak.
            self._last_cursor_ts.pop(session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        """Used by callers (screencast bootstrap) that only want to
        produce events when someone's watching.
        """
        return len(self._subs.get(session_id, []))

    # --- emits ---------------------------------------------------------

    def _push(self, session_id: str, event_type: str, payload: dict) -> None:
        """Fan out a single event to every queue for the session.

        When a queue is full, discard the OLDEST event rather than the
        new one — a slow viewer missing a cursor-move tick is fine; a
        slow viewer missing a screencast key-frame or click-target is
        much worse visually. drop-oldest semantics preserve recency.
        """
        lst = self._subs.get(session_id)
        if not lst:
            return
        event = {"type": event_type, "session_id": session_id, "payload": payload}
        for q in lst:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Still full after dropping one — a truly stuck
                    # consumer. Give up silently; the producer path
                    # must never raise into the caller.
                    logger.debug(
                        "event queue stuck for session=%s type=%s",
                        session_id, event_type,
                    )

    def emit_cursor_move(self, session_id: str, x: float, y: float) -> None:
        """Emit a cursor-move event, throttled per-session to roughly
        30 FPS. Caller emits unconditionally; the throttle lives here.
        """
        if not self._subs.get(session_id):
            return
        now = time.monotonic()
        last = self._last_cursor_ts.get(session_id, 0.0)
        if now - last < _CURSOR_THROTTLE_S:
            return
        self._last_cursor_ts[session_id] = now
        self._push(session_id, "cursor_move", {"x": float(x), "y": float(y)})

    def emit_click_target(
        self,
        session_id: str,
        *,
        x: float,
        y: float,
        snapped: bool = False,
        bbox: Optional[dict] = None,
        target: Optional[str] = None,
    ) -> None:
        """A click has been dispatched. Viewer pulses at (x, y) and
        briefly draws the bbox if provided.
        """
        payload: dict[str, Any] = {
            "x": float(x), "y": float(y), "snapped": bool(snapped),
        }
        if bbox is not None:
            payload["bbox"] = bbox
        if target is not None:
            payload["target"] = target
        self._push(session_id, "click_target", payload)

    def emit_drag(
        self,
        session_id: str,
        start_x: float, start_y: float,
        end_x: float, end_y: float,
    ) -> None:
        """Drag dispatched. Viewer can draw a line or animate cursor
        between the endpoints.
        """
        self._push(session_id, "drag", {
            "startX": float(start_x), "startY": float(start_y),
            "endX": float(end_x), "endY": float(end_y),
        })

    def emit_keystroke(self, session_id: str, key: str) -> None:
        """One keystroke (or a chunk of typed text). Viewer maintains
        a short trailing text buffer that fades.
        """
        self._push(session_id, "keystroke", {"key": str(key)})

    def emit_vision_bboxes(
        self,
        session_id: str,
        bboxes: list[dict],
        image_w: int,
        image_h: int,
    ) -> None:
        """Full vision-agent bbox set for the current page. Viewer
        replaces any prior overlay and auto-dims old boxes after a
        few seconds.
        """
        self._push(session_id, "vision_bboxes", {
            "bboxes": bboxes,
            "imageWidth": int(image_w),
            "imageHeight": int(image_h),
        })

    def emit_navigation(
        self,
        session_id: str,
        url: str,
        title: str = "",
    ) -> None:
        """A navigation completed. Viewer updates its banner."""
        self._push(session_id, "navigation", {
            "url": str(url), "title": str(title),
        })

    def emit_screencast_frame(
        self,
        session_id: str,
        jpeg_base64: str,
        *,
        width: int = 0,
        height: int = 0,
        timestamp: float = 0.0,
    ) -> None:
        """A CDP screencast frame. Viewer sets <img> src to the data
        URL and acks the frame. Phase B emitter; unused if CDP isn't
        running for the session.
        """
        self._push(session_id, "screencast", {
            "data": jpeg_base64,
            "width": int(width),
            "height": int(height),
            "timestamp": float(timestamp),
        })


# Module-level singleton. Mirrors `proxy_tiers.default()` pattern so
# callers don't have to thread the bus through initialisation.
_BUS_SINGLETON: Optional[T3EventBus] = None


def default() -> T3EventBus:
    global _BUS_SINGLETON
    if _BUS_SINGLETON is None:
        _BUS_SINGLETON = T3EventBus()
    return _BUS_SINGLETON


def reset_for_test() -> None:
    """Test helper: discard the current singleton so each test starts
    with a fresh bus. Not for production use.
    """
    global _BUS_SINGLETON
    _BUS_SINGLETON = None
