"""``SuperBrowser(audit_dir=...)`` must leave the harness's run directory for an
in-process run, restore the process env afterwards, and write nothing when off."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from runagent_superbrowser import lifecycle
from runagent_superbrowser import client as client_mod
from runagent_superbrowser.client import SuperBrowser


@pytest.fixture(autouse=True)
def _clean_registry():
    lifecycle._records.clear()
    yield
    lifecycle._records.clear()


class _FakeOrch:
    """Stands in for _runtime.Orchestrator with a REAL orchestrator Memory so
    write_task_summary / events.path behave as in production."""

    def __init__(self, task: str):
        from superbrowser_bridge.memory import Memory

        short = uuid.uuid4().hex[:8]
        self.task_id, self.session_key = f"orch-{short}", f"orchestrator:{short}"
        self.memory = Memory(self.task_id, session_key=self.session_key, role="orchestrator")
        self.memory.set_goal(task[:300])
        self.hook = self.memory.attach(None)
        self.bot = object()
        self.directive = ""


def _install_fakes(monkeypatch, *, behaviour: str = "ok"):
    made: dict = {}

    def fake_build(*, mode, task, model=None, provision_force=False):
        made["orch"] = _FakeOrch(task)
        return made["orch"]

    async def fake_run(bot, framed, session_key, *, hooks=None, timeout=None):
        # what the bridge would do during a run: a screenshot in the frozen dir,
        # a worker transcript in the capture dir, a worker memory dir
        shot_dir = Path(os.environ.get("SUPERBROWSER_SCREENSHOT_DIR") or "/tmp/superbrowser/screenshots")
        shot_dir.mkdir(parents=True, exist_ok=True)
        (shot_dir / "001-open.jpg").write_bytes(b"jpg")
        wid = uuid.uuid4().hex[:8]
        cap_env = os.environ.get("SUPERBROWSER_EVAL_CAPTURE_DIR")   # only set while an audit run is active
        if cap_env:
            cap = Path(cap_env)
            cap.mkdir(parents=True, exist_ok=True)
            (cap / f"{wid}.json").write_text(json.dumps({"task_id": wid, "messages": [], "meta": {"vision_calls": 1}}))
        wmem = Path("/tmp/superbrowser") / wid / "memory"
        wmem.mkdir(parents=True, exist_ok=True)
        (wmem / "events.jsonl").write_text('{"type": "memory_attach"}\n')
        made["wid"] = wid
        for h in hooks or []:
            if hasattr(h, "before_run"):
                await h.before_run(type("C", (), {"messages": []})())
        if behaviour == "timeout":
            raise asyncio.TimeoutError()
        if behaviour == "boom":
            raise RuntimeError("engine exploded")
        return "the answer", "the answer"

    monkeypatch.setattr(client_mod, "build_orchestrator", fake_build)
    monkeypatch.setattr(client_mod, "run_and_capture", fake_run)
    return made


def _cleanup(made: dict):
    for tid in (getattr(made.get("orch"), "task_id", None), made.get("wid")):
        if tid:
            shutil.rmtree(Path("/tmp/superbrowser") / tid, ignore_errors=True)


def test_audit_run_leaves_the_harness_layout(tmp_path, monkeypatch):
    for k in ("SUPERBROWSER_TRACE_VISION", "SUPERBROWSER_EVAL_CAPTURE_DIR", "SUPERBROWSER_AUDIT_ITERATIONS"):
        monkeypatch.delenv(k, raising=False)
    made = _install_fakes(monkeypatch)
    try:
        sb = SuperBrowser(audit_dir=tmp_path / "runs", model="test/model")
        assert Path(os.environ["SUPERBROWSER_SCREENSHOT_DIR"]).is_dir(), "the inbox must exist before any bridge import"
        res = sb.run("Find a thing", url="https://www.example.com/", mode="fetch",
                     audit_task={"task_id": "t-bench", "benchmark": "online_mind2web", "level": "easy"})
        assert res.success and res.audit_dir
        d = Path(res.audit_dir)
        assert d == tmp_path / "runs" / "sdk" / "ledger" / "t-bench" / "seed0"
        expected = {"spec.json", "meta.json", "manifest.json", "result.txt", "usage.json", "config.redacted.json",
                    "workers", "screenshots", "ledgers"}
        assert expected <= {p.name for p in d.iterdir()}, sorted(p.name for p in d.iterdir())
        meta = json.loads((d / "meta.json").read_text())
        assert meta["stop_reason"] == "ok" and meta["final_answer"] == "the answer" and meta["orch_task_id"] == made["orch"].task_id
        assert set(meta["role_task_ids"]) == {made["orch"].task_id, made["wid"]}
        assert (d / "ledgers" / made["orch"].task_id / "events.jsonl").exists()
        assert (d / "ledgers" / made["wid"] / "events.jsonl").exists()
        assert (d / "screenshots" / "001-open.jpg").exists() and (d / "screenshots" / "index.jsonl").exists()
        assert (d / "workers" / f"{made['wid']}.json").exists()
        roster = (d / "workers" / "_roster.jsonl").read_text()
        assert '"role": "orchestrator"' in roster
        spec = json.loads((d / "spec.json").read_text())
        assert spec["task"]["instruction"] == "Find a thing" and spec["task"]["start_url"] == "https://www.example.com/"
        assert spec["model"] == "test/model" and spec["mode"] == "fetch"
        manifest = json.loads((d / "manifest.json").read_text())
        assert manifest["effective_nanobot_defaults"]["model"] == "test/model" and manifest["sdk"]["mode"] == "fetch"
        # process env restored: the next non-audit run must not keep tracing into this dir
        assert "SUPERBROWSER_EVAL_CAPTURE_DIR" not in os.environ and "SUPERBROWSER_AUDIT_ITERATIONS" not in os.environ
        # the orchestrator ledger got the per-iteration bank file location (hook attached)
        assert (d / "ledgers" / made["orch"].task_id).is_dir()
    finally:
        _cleanup(made)


def test_timeout_still_leaves_a_complete_trail(tmp_path, monkeypatch):
    made = _install_fakes(monkeypatch, behaviour="timeout")
    try:
        sb = SuperBrowser(audit_dir=tmp_path / "runs")
        res = sb.run("Find a thing", mode="fetch", timeout=1)
        assert not res.success and "timed out" in (res.error or "")
        d = Path(res.audit_dir)
        meta = json.loads((d / "meta.json").read_text())
        assert meta["stop_reason"] == "timeout" and (d / "result.txt").read_text() == ""
        assert (d / "screenshots" / "001-open.jpg").exists(), "frames taken before the timeout are kept"
        assert (d / "ledgers" / made["wid"]).is_dir(), "a worker that never dumped a transcript is harvested via the roster"
    finally:
        _cleanup(made)


def test_exception_is_recorded_as_error(tmp_path, monkeypatch):
    made = _install_fakes(monkeypatch, behaviour="boom")
    try:
        sb = SuperBrowser(audit_dir=tmp_path / "runs")
        res = sb.run("Find a thing", mode="fetch")
        assert not res.success and "engine exploded" in (res.error or "")
        assert json.loads((Path(res.audit_dir) / "meta.json").read_text())["stop_reason"] == "error"
    finally:
        _cleanup(made)


def test_no_audit_dir_writes_nothing_and_sets_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERBROWSER_AUDIT_DIR", raising=False)
    monkeypatch.delenv("SUPERBROWSER_EVAL_CAPTURE_DIR", raising=False)
    monkeypatch.setenv("SUPERBROWSER_SCREENSHOT_DIR", str(tmp_path / "shots"))
    made = _install_fakes(monkeypatch)
    try:
        sb = SuperBrowser()
        assert sb._audit is None
        res = sb.run("Find a thing", mode="fetch")
        assert res.success and res.audit_dir is None
        assert not (tmp_path / "runs").exists()
        assert os.environ["SUPERBROWSER_SCREENSHOT_DIR"] == str(tmp_path / "shots")
    finally:
        _cleanup(made)


def test_repeated_runs_get_fresh_seeds_only_when_judged(tmp_path, monkeypatch):
    made = _install_fakes(monkeypatch)
    try:
        sb = SuperBrowser(audit_dir=tmp_path / "runs")
        r1 = sb.run("Find a thing", mode="fetch")
        _cleanup(made)
        (Path(r1.audit_dir) / "run_record.json").write_text("{}")   # judged
        r2 = sb.run("Find a thing", mode="fetch")
        assert Path(r2.audit_dir).name == "seed1" and Path(r1.audit_dir).exists()
    finally:
        _cleanup(made)
