"""Client-side tests for the local-agent transports, cancel API, and probe."""

from __future__ import annotations

import io
import itertools
import json
from typing import Any

import pytest

from runagent_superbrowser import lifecycle
from runagent_superbrowser.client import SuperBrowser


@pytest.fixture(autouse=True)
def _clean_registry():
    lifecycle._records.clear()
    yield
    lifecycle._records.clear()


class FakeStreamClient:
    def __init__(self, events: list[dict]):
        self.events = events
        self.kwargs: dict[str, Any] | None = None
        self.closed = False

    def run_stream(self, **kwargs):
        self.kwargs = kwargs

        def gen():
            try:
                yield from self.events
            finally:
                self.closed = True

        return gen()


class FakeRestClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.kwargs: dict[str, Any] | None = None

    def run(self, **kwargs):
        self.kwargs = kwargs
        return self.payload


def _make_sb(monkeypatch: pytest.MonkeyPatch, *, entrypoints: set[str]) -> SuperBrowser:
    sb = SuperBrowser(local_agent_url="http://localhost:18450")
    monkeypatch.setattr(sb, "_server_entrypoints", lambda: entrypoints)
    return sb


RESULT_EVENT = {
    "type": "result",
    "text": "the answer",
    "success": True,
    "task_id": "orch-cafe1234",
    "mode": "auto",
    "data": None,
    "error": None,
    "classification": None,
    "input_tokens": 10,
    "output_tokens": 5,
    "total_tokens": 15,
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
}


def test_stream_transport_aggregates_result(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeStreamClient([{"type": "status", "message": "working"}, dict(RESULT_EVENT)])
    sb = _make_sb(monkeypatch, entrypoints={"run", "run_stream", "cancel", "tasks"})
    monkeypatch.setattr(sb, "_local_stream_client", lambda: fake)

    seen: list[dict] = []
    res = sb.run("do the thing", timeout=30, task_handle="task-abc12345", on_event=seen.append)

    assert res.success and res.text == "the answer"
    assert res.task_id == "orch-cafe1234"
    assert res.task_handle == "task-abc12345"
    assert res.total_tokens == 15
    assert fake.closed  # iterator always closed
    assert fake.kwargs == {
        "task": "do the thing",
        "mode": "auto",
        "timeout": 30,
        "client_task_id": "task-abc12345",
    }
    assert seen == [{"type": "status", "message": "working"}]
    assert lifecycle.get("task-abc12345").state == "done"


def test_stream_transport_omits_handle_for_old_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeStreamClient([dict(RESULT_EVENT)])
    sb = _make_sb(monkeypatch, entrypoints={"run", "run_stream"})  # old container
    monkeypatch.setattr(sb, "_local_stream_client", lambda: fake)

    res = sb.run("do it")
    assert res.success
    assert "client_task_id" not in (fake.kwargs or {})
    assert res.task_handle.startswith("task-")  # client-side handle still returned


def test_stream_transport_no_result_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeStreamClient([{"type": "status"}])
    sb = _make_sb(monkeypatch, entrypoints=set())
    monkeypatch.setattr(sb, "_local_stream_client", lambda: fake)

    res = sb.run("do it")
    assert not res.success
    assert "no result" in (res.error or "")


def test_stream_transport_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    endless = ({"type": "status", "n": n} for n in itertools.count())

    class EndlessClient(FakeStreamClient):
        def run_stream(self, **kwargs):
            self.kwargs = kwargs
            return endless

    fake = EndlessClient([])
    sb = _make_sb(monkeypatch, entrypoints=set())
    monkeypatch.setattr(sb, "_local_stream_client", lambda: fake)
    # A clock that jumps 100s per call: the deadline (timeout+30s) is exceeded
    # right after the first event.
    ticks = itertools.count(start=0, step=100)
    monkeypatch.setattr("runagent_superbrowser.client.time.monotonic", lambda: float(next(ticks)))

    res = sb.run("do it", timeout=1)
    assert not res.success
    assert "watchdog" in (res.error or "")


def test_stream_transport_server_cancel_marks_result(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled_event = {
        "type": "result", "text": "", "success": False,
        "error": "cancelled by client", "cancelled": True,
        "task_id": None, "task_handle": "task-abc12345", "mode": "auto", "classification": None,
    }
    fake = FakeStreamClient([cancelled_event])
    sb = _make_sb(monkeypatch, entrypoints={"cancel", "tasks", "run", "run_stream"})
    monkeypatch.setattr(sb, "_local_stream_client", lambda: fake)

    res = sb.run("do it", task_handle="task-abc12345")
    assert res.cancelled and not res.success
    assert res.error == "cancelled by client"
    assert lifecycle.get("task-abc12345").state == "cancelled"


def test_rest_fallback_forwards_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERBROWSER_RUN_TRANSPORT", "rest")
    from runagent_superbrowser import _reaper

    monkeypatch.setattr(_reaper, "install", lambda client: None)

    fake = FakeRestClient({"text": "ok", "success": True, "task_id": "orch-1", "mode": "auto"})
    sb = _make_sb(monkeypatch, entrypoints={"run", "run_stream", "cancel", "tasks"})
    monkeypatch.setattr(sb, "_local_runagent_client", lambda: fake)

    res = sb.run("do it", timeout=12, task_handle="task-r1")
    assert res.success and res.task_handle == "task-r1"
    assert fake.kwargs["timeout"] == 12
    assert fake.kwargs["client_task_id"] == "task-r1"


def test_rest_fallback_old_server_sends_no_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERBROWSER_RUN_TRANSPORT", "rest")
    fake = FakeRestClient({"text": "ok", "success": True, "task_id": "orch-1", "mode": "auto"})
    sb = _make_sb(monkeypatch, entrypoints=set())
    monkeypatch.setattr(sb, "_local_runagent_client", lambda: fake)

    res = sb.run("do it", timeout=12)
    assert res.success
    assert "client_task_id" not in fake.kwargs
    assert fake.kwargs["timeout"] == 12  # timeout is always safe to send


def test_cancel_and_tasks_via_entrypoints(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = _make_sb(monkeypatch, entrypoints={"cancel", "tasks"})
    calls: list[tuple[str, dict]] = []

    class FakeAux:
        def __init__(self, tag: str, payload: dict):
            self.tag, self.payload = tag, payload

        def run(self, **kwargs):
            calls.append((self.tag, kwargs))
            return self.payload

    aux = {
        "cancel": FakeAux("cancel", {"supported": True, "cancelled": True}),
        "tasks": FakeAux("tasks", {"supported": True, "tasks": [{"handle": "task-x", "state": "running"}]}),
    }
    monkeypatch.setattr(sb, "_entrypoint_client", lambda tag: aux[tag])

    assert sb.cancel("task-x") is True
    assert sb.tasks() == [{"handle": "task-x", "state": "running"}]
    assert calls == [("cancel", {"client_task_id": "task-x"}), ("tasks", {})]


def test_cancel_unsupported_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = _make_sb(monkeypatch, entrypoints=set())
    assert sb.cancel("task-x") is False  # old server
    assert sb.cancel("") is False

    plain = SuperBrowser()  # in-process mode
    lifecycle.register("task-inproc", "x")
    assert plain.cancel("task-inproc") is True
    assert lifecycle.cancelled("task-inproc")


def test_server_entrypoints_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = SuperBrowser(local_agent_url="http://localhost:18450")
    body = json.dumps(
        {"success": True, "data": {"entrypoints": [{"tag": "run"}, {"tag": "cancel"}, {"tag": "tasks"}]}}
    ).encode()

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=5: FakeResponse(body)
    )
    assert sb._server_entrypoints() == {"run", "cancel", "tasks"}
    # cached — a later network failure doesn't matter
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError))
    assert sb._server_entrypoints() == {"run", "cancel", "tasks"}


def test_server_entrypoints_probe_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = SuperBrowser(local_agent_url="http://localhost:18450")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    )
    assert sb._server_entrypoints() == set()
