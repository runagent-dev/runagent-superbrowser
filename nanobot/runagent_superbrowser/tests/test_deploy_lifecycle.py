"""Tests for deploy/main.py's cooperative-cancel path and new entrypoints.

deploy/main.py is loaded from its file path (it is not a package module) and
its module globals are faked so no engine/LLM is needed. These tests guard the
SYNCED file's contract: byte-identical copies ship in the runagent template
and runagent-serverless.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest

from runagent_superbrowser import lifecycle

_DEPLOY_MAIN = Path(__file__).resolve().parents[3] / "deploy" / "main.py"


@pytest.fixture()
def deploy_main(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location("deploy_main_under_test", _DEPLOY_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_has_llm_credentials", lambda: True)
    lifecycle._records.clear()
    yield module
    lifecycle._records.clear()


class FakeSB:
    """Stands in for the in-process SuperBrowser inside deploy/main.py."""

    def __init__(self, events, delay: float = 0.0):
        self.events = events
        self.delay = delay
        self.stream_kwargs = None
        self.closed = False

    def stream(self, task, **kwargs):
        self.stream_kwargs = {"task": task, **kwargs}

        def gen():
            try:
                for event in self.events:
                    if self.delay:
                        time.sleep(self.delay)
                    yield event
            finally:
                self.closed = True

        return gen()

    def run(self, task, **kwargs):  # classic path
        return type("R", (), {"text": "classic", "success": True, "data": None,
                              "error": None, "task_id": "orch-1", "mode": kwargs.get("mode", "auto"),
                              "classification": None})()


def test_run_without_client_task_id_uses_classic_path(deploy_main, monkeypatch) -> None:
    fake = FakeSB([])
    monkeypatch.setattr(deploy_main, "_sb", fake, raising=False)
    result = deploy_main.run("do it", mode="auto", timeout=5)
    assert result["text"] == "classic" and result["success"] is True
    assert fake.stream_kwargs is None  # streaming path untouched
    assert lifecycle.list_tasks() == []  # nothing registered


def test_run_with_client_task_id_aggregates_stream(deploy_main, monkeypatch) -> None:
    fake = FakeSB([
        {"type": "status", "message": "working"},
        {"type": "result", "text": "done!", "success": True, "task_id": "orch-9",
         "mode": "auto", "data": None, "error": None, "classification": None},
    ])
    monkeypatch.setattr(deploy_main, "_sb", fake, raising=False)
    result = deploy_main.run("do it", mode="auto", timeout=5, client_task_id="task-d1")
    assert result["success"] is True and result["text"] == "done!"
    assert result["task_handle"] == "task-d1"
    assert "type" not in result
    assert fake.stream_kwargs["timeout"] == 5
    assert fake.closed
    assert lifecycle.get("task-d1").state == "done"


def test_run_cancel_mid_task(deploy_main, monkeypatch) -> None:
    # An endless stream of status events; the cancel must break the loop.
    endless = ({"type": "status", "n": n} for n in range(10_000))
    fake = FakeSB(endless, delay=0.01)
    monkeypatch.setattr(deploy_main, "_sb", fake, raising=False)

    results: list[dict] = []
    runner = threading.Thread(
        target=lambda: results.append(deploy_main.run("long task", client_task_id="task-c1")),
        daemon=True,  # a failed cancel must not wedge the pytest process
    )
    runner.start()
    deadline = time.time() + 5
    while time.time() < deadline and lifecycle.get("task-c1") is None:
        time.sleep(0.01)

    payload = deploy_main.cancel("task-c1")
    assert payload == {"supported": True, "cancelled": True}

    runner.join(timeout=5)
    assert not runner.is_alive()
    assert results and results[0]["cancelled"] is True
    assert results[0]["error"] == "cancelled by client"
    assert fake.closed  # generator unwound -> underlying run cancelled
    assert lifecycle.get("task-c1").state == "cancelled"


def test_cancel_unknown_task(deploy_main) -> None:
    assert deploy_main.cancel("task-nope") == {"supported": True, "cancelled": False}


def test_tasks_entrypoint_lists(deploy_main) -> None:
    lifecycle.register("task-t1", "listed task")
    payload = deploy_main.tasks()
    assert payload["supported"] is True
    assert any(t["handle"] == "task-t1" for t in payload["tasks"])
