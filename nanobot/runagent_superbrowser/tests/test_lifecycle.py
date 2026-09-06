"""Unit tests for the task lifecycle registry."""

from __future__ import annotations

import threading
import time

import pytest

from runagent_superbrowser import lifecycle


@pytest.fixture(autouse=True)
def _clean_registry():
    lifecycle._records.clear()
    yield
    lifecycle._records.clear()


def test_register_and_finish_round_trip() -> None:
    record = lifecycle.register(None, "book a flight to BKK")
    assert record.handle.startswith("task-")
    assert record.state == "running"

    listed = lifecycle.list_tasks()
    assert len(listed) == 1
    assert listed[0]["handle"] == record.handle
    assert listed[0]["task"] == "book a flight to BKK"

    lifecycle.finish(record.handle, "done")
    assert lifecycle.get(record.handle).state == "done"
    # terminal finish is sticky
    lifecycle.finish(record.handle, "error")
    assert lifecycle.get(record.handle).state == "done"


def test_cancel_flow() -> None:
    record = lifecycle.register("task-cafe0001", "x")
    assert not lifecycle.cancelled("task-cafe0001")
    assert lifecycle.request_cancel("task-cafe0001") is True
    assert lifecycle.cancelled("task-cafe0001") is True
    assert lifecycle.get("task-cafe0001").state == "cancelling"
    # cancelling an unknown or finished task is False
    assert lifecycle.request_cancel("task-nope") is False
    lifecycle.finish("task-cafe0001", "cancelled")
    assert lifecycle.request_cancel("task-cafe0001") is False


def test_reregister_running_handle_keeps_cancel_event() -> None:
    first = lifecycle.register("task-h1", "x")
    lifecycle.request_cancel("task-h1")
    second = lifecycle.register("task-h1", "x again")
    assert second is first
    assert lifecycle.cancelled("task-h1")
    # a terminal record is replaced by a fresh one
    lifecycle.finish("task-h1", "cancelled")
    third = lifecycle.register("task-h1", "new run")
    assert third is not first
    assert not lifecycle.cancelled("task-h1")


def test_running_tasks_listed_first() -> None:
    done = lifecycle.register("task-done", "old")
    lifecycle.finish(done.handle, "done")
    time.sleep(0.01)
    lifecycle.register("task-live", "new")
    listed = lifecycle.list_tasks()
    assert listed[0]["handle"] == "task-live"
    assert listed[-1]["state"] == "done"


def test_prune_drops_stale_terminal_records(monkeypatch: pytest.MonkeyPatch) -> None:
    record = lifecycle.register("task-old", "x")
    lifecycle.finish(record.handle, "done")
    record.finished_at = time.time() - 7200  # two hours ago
    live = lifecycle.register("task-new", "y")
    handles = {r["handle"] for r in lifecycle.list_tasks()}
    assert handles == {live.handle}


def test_stream_and_capture_cancels_run_on_early_close() -> None:
    """The deterministic cancel point: closing the stream generator early must
    cancel the underlying run task (this is what unwinds a browser task when a
    client disconnects or cancels)."""
    import asyncio

    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus

    from runagent_superbrowser._capture import stream_and_capture

    async def main():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class FakeLoop:
            def __init__(self):
                self.bus = MessageBus()

        class FakeBot:
            def __init__(self):
                self._loop = FakeLoop()

            async def run(self, task, session_key, hooks):
                started.set()
                try:
                    await asyncio.sleep(3600)  # never completes on its own
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        bot = FakeBot()
        agen = stream_and_capture(bot, "task", "sess")
        # Publish one message so the generator yields once (and the run task starts).
        await bot._loop.bus.publish_outbound(OutboundMessage(channel="x", chat_id="y", content="hi"))
        ev = await asyncio.wait_for(agen.__anext__(), timeout=2)
        assert ev["type"] == "message"
        assert started.is_set()
        # Early close — the finally must cancel + await the run task.
        await agen.aclose()
        await asyncio.wait_for(cancelled.wait(), timeout=2)

    asyncio.run(main())


def test_thread_safety_under_contention() -> None:
    errors: list[Exception] = []

    def hammer(idx: int) -> None:
        try:
            for i in range(200):
                handle = f"task-{idx}-{i % 10}"
                lifecycle.register(handle, "t")
                lifecycle.request_cancel(handle)
                lifecycle.cancelled(handle)
                lifecycle.finish(handle, "cancelled")
                lifecycle.list_tasks()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
