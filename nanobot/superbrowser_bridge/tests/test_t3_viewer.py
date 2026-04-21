"""Unit + integration tests for the T3 live viewer stack.

Covers the pieces that are logic-only (event bus pubsub, throttling,
WS handler pumping JSON events) — does NOT launch a real browser.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from superbrowser_bridge.antibot import t3_event_bus  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --- Event bus core -------------------------------------------------------


class EventBusPubsubTests(unittest.TestCase):
    def setUp(self) -> None:
        t3_event_bus.reset_for_test()
        self.bus = t3_event_bus.default()

    def test_subscriber_count_tracks_adds_and_removes(self) -> None:
        self.assertEqual(self.bus.subscriber_count("sid1"), 0)
        q = self.bus.subscribe("sid1")
        self.assertEqual(self.bus.subscriber_count("sid1"), 1)
        self.bus.unsubscribe("sid1", q)
        self.assertEqual(self.bus.subscriber_count("sid1"), 0)

    def test_emit_reaches_every_subscriber_of_that_session(self) -> None:
        q1 = self.bus.subscribe("sid1")
        q2 = self.bus.subscribe("sid1")
        q_other = self.bus.subscribe("sid2")
        self.bus.emit_click_target("sid1", x=10, y=20, snapped=True)
        self.assertEqual(q1.qsize(), 1)
        self.assertEqual(q2.qsize(), 1)
        self.assertEqual(q_other.qsize(), 0)

    def test_emit_without_subscribers_is_noop(self) -> None:
        # Should not raise, should not buffer anywhere.
        self.bus.emit_cursor_move("ghost", 1, 2)
        self.bus.emit_navigation("ghost", "https://example.test", "x")
        self.assertEqual(self.bus.subscriber_count("ghost"), 0)

    def test_unsubscribe_unknown_queue_is_noop(self) -> None:
        foreign = asyncio.Queue()
        # Doesn't raise.
        self.bus.unsubscribe("sid1", foreign)

    def test_event_shape_has_type_session_payload(self) -> None:
        q = self.bus.subscribe("sid1")
        self.bus.emit_vision_bboxes(
            "sid1",
            bboxes=[{"x0": 10, "y0": 20, "x1": 50, "y1": 80, "label": "btn", "role": "button"}],
            image_w=1024, image_h=768,
        )
        event = q.get_nowait()
        self.assertEqual(event["type"], "vision_bboxes")
        self.assertEqual(event["session_id"], "sid1")
        self.assertEqual(event["payload"]["imageWidth"], 1024)
        self.assertEqual(event["payload"]["imageHeight"], 768)
        self.assertEqual(len(event["payload"]["bboxes"]), 1)


class CursorThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        t3_event_bus.reset_for_test()
        self.bus = t3_event_bus.default()

    def test_burst_of_moves_throttled_to_first(self) -> None:
        q = self.bus.subscribe("sid1")
        # 10 moves in a tight loop — only the first should land (no 33ms
        # elapsed between them).
        for i in range(10):
            self.bus.emit_cursor_move("sid1", i, i)
        self.assertEqual(q.qsize(), 1)

    def test_separated_moves_both_land(self) -> None:
        q = self.bus.subscribe("sid1")
        self.bus.emit_cursor_move("sid1", 1, 1)
        time.sleep(0.050)  # > 33ms throttle window
        self.bus.emit_cursor_move("sid1", 2, 2)
        self.assertEqual(q.qsize(), 2)


class ClickTargetPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        t3_event_bus.reset_for_test()
        self.bus = t3_event_bus.default()

    def test_minimal_click(self) -> None:
        q = self.bus.subscribe("sid1")
        self.bus.emit_click_target("sid1", x=100, y=200)
        ev = q.get_nowait()
        self.assertEqual(ev["payload"], {"x": 100.0, "y": 200.0, "snapped": False})

    def test_click_with_bbox_and_target(self) -> None:
        q = self.bus.subscribe("sid1")
        self.bus.emit_click_target(
            "sid1", x=100, y=200, snapped=True,
            bbox={"x0": 80, "y0": 180, "x1": 120, "y1": 220},
            target="button:Submit",
        )
        ev = q.get_nowait()
        self.assertTrue(ev["payload"]["snapped"])
        self.assertEqual(ev["payload"]["bbox"]["x0"], 80)
        self.assertEqual(ev["payload"]["target"], "button:Submit")


class QueueBackpressureTests(unittest.TestCase):
    def setUp(self) -> None:
        t3_event_bus.reset_for_test()
        self.bus = t3_event_bus.default()

    def test_full_queue_drops_oldest_not_newest(self) -> None:
        """When a subscriber is slow and their queue fills, the bus
        must drop the OLDEST event so recent state (click pulses,
        latest screencast frame) still reaches them."""
        # Use a tiny maxsize via the real bus mechanism — inject by
        # monkeypatching the constant for this test.
        from superbrowser_bridge.antibot import t3_event_bus as mod
        orig = mod._QUEUE_MAXSIZE
        mod._QUEUE_MAXSIZE = 3
        try:
            t3_event_bus.reset_for_test()
            bus = t3_event_bus.default()
            q = bus.subscribe("sid1")
            for i in range(5):
                bus.emit_click_target("sid1", x=i, y=i)
            drained = []
            while not q.empty():
                drained.append(q.get_nowait())
            # Queue is size 3; first 2 events got dropped.
            self.assertEqual(len(drained), 3)
            xs = [d["payload"]["x"] for d in drained]
            # Most recent emits retained.
            self.assertEqual(xs, [2.0, 3.0, 4.0])
        finally:
            mod._QUEUE_MAXSIZE = orig


# --- WS handler integration (uses aiohttp test client) -------------------


class WSHandlerTests(unittest.TestCase):
    """Boot the _Server, connect a WS client, emit events through the
    bus, and assert the client receives JSON frames with the expected
    shape.
    """

    def setUp(self) -> None:
        t3_event_bus.reset_for_test()

    def test_ws_client_receives_emitted_events(self) -> None:
        from aiohttp import WSMsgType, web
        from superbrowser_bridge.antibot import t3_viewer

        async def run() -> None:
            # Build the viewer's app manually (same routes _Server adds)
            # so we can point aiohttp's TestServer at it.
            srv = t3_viewer._Server()
            app = web.Application()
            app.router.add_get("/t3/session/{sid}/view", srv._view)
            app.router.add_get("/t3/session/{sid}/ws", srv._ws)
            # Don't mount /screenshot or /click — they touch the T3
            # manager which isn't available in tests.

            from aiohttp.test_utils import TestServer, TestClient
            test_srv = TestServer(app)
            await test_srv.start_server()
            try:
                async with TestClient(test_srv) as client:
                    ws = await client.ws_connect("/t3/session/test-sid/ws")
                    # Give the handler a tick to subscribe to the bus.
                    await asyncio.sleep(0.05)
                    bus = t3_event_bus.default()
                    self.assertEqual(bus.subscriber_count("test-sid"), 1)
                    bus.emit_click_target("test-sid", x=5, y=10, snapped=True)
                    bus.emit_navigation("test-sid", "https://example.test", "Ex")
                    received = []
                    for _ in range(2):
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        if msg.type == WSMsgType.TEXT:
                            received.append(json.loads(msg.data))
                    self.assertEqual(received[0]["type"], "click_target")
                    self.assertEqual(received[0]["payload"]["x"], 5.0)
                    self.assertEqual(received[1]["type"], "navigation")
                    self.assertEqual(received[1]["payload"]["url"], "https://example.test")
                    await ws.close()
                    # Give the handler a tick to unsubscribe.
                    await asyncio.sleep(0.05)
                    self.assertEqual(bus.subscriber_count("test-sid"), 0)
            finally:
                await test_srv.close()

        _run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
