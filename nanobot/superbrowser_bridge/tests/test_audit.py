"""The per-run audit trail must write the harness's run-directory layout, and
must never fail silently: every assertion below checks that a directory
resolves BEFORE it checks that a file landed (the tracer bug of 270b880)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from nanobot.agent.hook import AgentHookContext, AgentRunHookContext  # noqa: E402

from superbrowser_bridge import audit  # noqa: E402


@pytest.fixture
def memory_base(tmp_path):
    base = tmp_path / "superbrowser"
    base.mkdir()
    return base


def _fake_role(base: Path, tid: str, *, with_steps: bool = True) -> Path:
    mem = base / tid / "memory"
    mem.mkdir(parents=True)
    (mem / "events.jsonl").write_text('{"ts": 1, "type": "memory_attach"}\n')
    (mem / "ledger.json").write_text('{"goal": "g"}')
    if with_steps:
        (mem / "steps.jsonl").write_text('{"tool": "browser_open"}\n')
    (base / tid / "task_summary.json").write_text("{}")
    return mem


def test_begin_writes_harness_shaped_spec_and_derives_task_id(tmp_path):
    rec = audit.AuditRecorder(tmp_path / "runs", experiment="sdk", arm="ledger")
    run = rec.begin(task={"instruction": "Find the 5-day price chart for Bitcoin.", "start_url": "https://www.google.com/finance/"},
                    seed=0, model="m/x", timeout=120, server_url="http://localhost:3100")
    tid = audit.derived_task_id("Find the 5-day price chart for Bitcoin.", "https://www.google.com/finance/")
    assert len(tid) == 32 and run.run_dir == tmp_path / "runs" / "sdk" / "ledger" / tid / "seed0"
    assert run.run_id == f"sdk:ledger:{tid}:s0"
    spec = json.loads((run.run_dir / "spec.json").read_text())
    for key in ("run_id", "experiment", "seed", "arm", "task", "benchmark", "run_dir", "model", "topology", "protocol",
                "nanobot_overrides", "internal_timeout_s", "server_url"):
        assert key in spec, key
    assert spec["task"]["benchmark"] == "custom" and spec["task"]["task_id"] == tid
    assert spec["arm"] == {"name": "ledger", "env": {}, "side": "python", "family": "system",
                           "description": spec["arm"]["description"]}
    assert spec["protocol"]["env_pins"]["SUPERBROWSER_TRACE_VISION"] == "1"
    assert (run.run_dir / "workers").is_dir() and (run.run_dir / "screenshots").is_dir()
    # an explicit benchmark task keeps its id and labels
    run2 = rec.begin(task={"task_id": "abc", "benchmark": "online_mind2web", "level": "easy", "instruction": "x", "start_url": "https://a/"})
    assert run2.run_dir.parent.name == "abc" and json.loads((run2.run_dir / "spec.json").read_text())["task"]["level"] == "easy"


def test_begin_never_clobbers_a_judged_run_and_archives_a_stale_one(tmp_path):
    rec = audit.AuditRecorder(tmp_path / "runs", experiment="sdk")
    task = {"task_id": "t1", "instruction": "x"}
    run = rec.begin(task=task, seed=0)
    (run.run_dir / "run_record.json").write_text("{}")
    run_b = rec.begin(task=task, seed=0)
    assert run_b.run_dir.name == "seed1", "a judged seed0 must be left alone"
    # an unjudged, non-empty attempt is moved to _failed_attempts, not deleted
    (run_b.run_dir / "result.txt").write_text("partial")
    run_c = rec.begin(task=task, seed=1)
    assert run_c.run_dir == run_b.run_dir and not (run_c.run_dir / "result.txt").exists()
    archived = list((tmp_path / "runs" / "sdk" / "_failed_attempts").glob("ledger__t1__seed1__*"))
    assert len(archived) == 1 and (archived[0] / "result.txt").read_text() == "partial"


def test_env_apply_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERBROWSER_TRACE_VISION", "0")
    monkeypatch.delenv("SUPERBROWSER_EVAL_CAPTURE_DIR", raising=False)
    rec = audit.AuditRecorder(tmp_path / "runs")
    run = rec.begin(task={"instruction": "x"})
    run.apply_env()
    assert os.environ["SUPERBROWSER_TRACE_VISION"] == "1"
    assert os.environ["SUPERBROWSER_EVAL_CAPTURE_DIR"] == str(run.run_dir / "workers")
    assert os.environ["SUPERBROWSER_AUDIT_ITERATIONS"] == "1"
    assert "SUPERBROWSER_SCREENSHOT_DIR" not in run.env(), "the frozen screenshot dir is the client's job, not the run's"
    run.restore_env()
    assert os.environ["SUPERBROWSER_TRACE_VISION"] == "0"
    assert "SUPERBROWSER_EVAL_CAPTURE_DIR" not in os.environ


def test_finish_harvests_moves_screenshots_and_writes_meta(tmp_path, memory_base):
    rec = audit.AuditRecorder(tmp_path / "runs", memory_base=memory_base)
    run = rec.begin(task={"instruction": "x", "start_url": "https://a/"})
    orch, wid, wid_killed = f"orch-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex[:8], uuid.uuid4().hex[:8]
    assert _fake_role(memory_base, orch, with_steps=False).exists()
    assert _fake_role(memory_base, wid).exists()
    assert _fake_role(memory_base, wid_killed).exists()          # never dumped a transcript, only in the roster
    (run.run_dir / "workers" / f"{wid}.json").write_text('{"messages": []}')
    (run.roster_path).write_text(json.dumps({"ts": 1, "role": "worker", "task_id": wid_killed}) + "\n")
    inbox = rec.inbox_screenshot_dir()
    assert inbox.is_dir()
    (inbox / "001-open.jpg").write_bytes(b"jpg")
    (inbox / "index.jsonl").write_text('{"idx": 0, "file": "001-open.jpg"}\n')
    run.write_preliminary_meta(orch, "framed")
    assert json.loads((run.run_dir / "meta.json").read_text())["stop_reason"] == "running"
    ids = run.finish(final_answer="done", raw_content="done", stop_reason="ok", error=None,
                     usage={"input_tokens": 1}, orch_task_id=orch, framed_task="framed",
                     effective_defaults={"model": "m"}, screenshot_src=inbox)
    assert set(ids) == {orch, wid, wid_killed}
    for tid in (orch, wid, wid_killed):
        d = run.run_dir / "ledgers" / tid
        assert d.is_dir(), f"ledger dir for {tid} was not harvested"
        assert (d / "events.jsonl").exists() and (d / "ledger.json").exists() and (d / "task_summary.json").exists()
    assert (run.run_dir / "screenshots" / "001-open.jpg").read_bytes() == b"jpg"
    assert not (inbox / "001-open.jpg").exists(), "screenshots are MOVED so the next run cannot inherit them"
    assert (run.run_dir / "screenshots" / "index.jsonl").exists()
    meta = json.loads((run.run_dir / "meta.json").read_text())
    assert meta["stop_reason"] == "ok" and meta["role_task_ids"] == ids and meta["n_screenshots"] == 1
    assert meta["final_answer"] == "done" and meta["environment"]["git_sha"] and meta["effective_defaults"] == {"model": "m"}
    assert (run.run_dir / "result.txt").read_text() == "done"
    assert json.loads((run.run_dir / "usage.json").read_text()) == {"input_tokens": 1}
    # idempotent
    assert run.finish() == ids


def test_manifest_records_provenance_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("VISION_API_KEY", "sk-secret")
    monkeypatch.setenv("ABLATE_VISION_REUSE", "1")
    rec = audit.AuditRecorder(tmp_path / "runs")
    run = rec.begin(task={"instruction": "x"})
    run.write_manifest(server_url="http://x", health={"ok": True}, effective_defaults={"model": "m"}, extra={"sdk": {"mode": "browser"}})
    m = json.loads((run.run_dir / "manifest.json").read_text())
    assert m["git"]["short_sha"] and "diff_sha256" in m["git"]
    assert m["env"]["VISION_API_KEY"] == "***" and m["env"]["ABLATE_VISION_REUSE"] == "1"
    assert "orchestrator" in m["soul"] and m["soul"]["orchestrator"]["sha256"]
    assert m["server"] == {"url": "http://x", "health": {"ok": True}} and m["sdk"]["mode"] == "browser"
    assert "sk-secret" not in (run.run_dir / "manifest.json").read_text()


def test_redact_config_without_overrides_writes_only_the_redacted_copy(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"model": "m", "apiKey": "sk-1"}}, "providers": {"x": {"api_key": "sk-2"}}}))
    tmp, eff = audit.redact_config(tmp_path / "run", config_path=cfg)
    assert tmp is None and eff == {"model": "m", "apiKey": "***"}
    text = (tmp_path / "run" / "config.redacted.json").read_text()
    assert "sk-1" not in text and "sk-2" not in text
    tmp2, eff2 = audit.redact_config(tmp_path / "run2", overrides={"maxTokens": 5}, model="m2", config_path=cfg)
    assert tmp2 is not None and tmp2.exists() and eff2["model"] == "m2" and eff2["maxTokens"] == 5
    assert oct(tmp2.stat().st_mode & 0o777) == "0o600"
    tmp2.unlink()


def test_audit_hook_banks_per_iteration_and_terminal_row_without_after_run(tmp_path):
    mem = tmp_path / "mem"
    hook = audit.AuditHook("worker", memory_dir=mem, task_id="w1", roster_path=tmp_path / "workers" / audit.ROSTER_NAME)

    async def go():
        await hook.before_run(AgentRunHookContext(messages=[]))
        for i in range(3):
            ctx = AgentHookContext(iteration=i, messages=[{"role": "user", "content": "x"}] * (i + 1),
                                   usage={"input_tokens": 100 + i, "output_tokens": 5})
            await hook.after_iteration(ctx)
        # cancelled/timed-out runs never reach after_run; on_finally always runs
        await hook.on_finally(AgentRunHookContext(messages=[], stop_reason="cancelled", exception=asyncio.CancelledError()))

    asyncio.run(go())
    assert mem.is_dir(), "the hook must create the memory dir it banks into"
    rows = [json.loads(l) for l in (mem / "iterations.jsonl").read_text().splitlines()]
    assert [r.get("iter") for r in rows[:3]] == [0, 1, 2] and rows[1]["tokens_in"] == 101 and rows[2]["n_messages"] == 3
    assert rows[-1]["final"] is True and rows[-1]["stop_reason"] == "cancelled" and rows[-1]["exception"] == "CancelledError"
    roster = [json.loads(l) for l in (tmp_path / "workers" / audit.ROSTER_NAME).read_text().splitlines()]
    assert roster == [{**roster[0], "role": "worker", "task_id": "w1"}]


def test_worker_hook_factory_is_gated(tmp_path, monkeypatch):
    from superbrowser_bridge.memory.eval_instrumentation import eval_worker_hooks

    class _Mem:
        task_id = "w9"

        class events:
            path = tmp_path / "mem" / "events.jsonl"

    monkeypatch.delenv("SUPERBROWSER_AUDIT_ITERATIONS", raising=False)
    monkeypatch.delenv("SUPERBROWSER_EVAL_DISTRACTOR_TOKENS", raising=False)
    assert eval_worker_hooks(_Mem()) == []
    monkeypatch.setenv("SUPERBROWSER_AUDIT_ITERATIONS", "1")
    monkeypatch.setenv("SUPERBROWSER_EVAL_CAPTURE_DIR", str(tmp_path / "workers"))
    hooks = eval_worker_hooks(_Mem())
    assert len(hooks) == 1 and isinstance(hooks[0], audit.AuditHook)
    assert hooks[0].memory_dir == tmp_path / "mem" and hooks[0].roster_path == tmp_path / "workers" / audit.ROSTER_NAME


def test_repoint_screenshot_dir_and_effective_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERBROWSER_SCREENSHOT_DIR", str(tmp_path / "env"))
    saved = {m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("superbrowser_bridge.session_tools")}
    try:
        assert audit.effective_screenshot_dir() == tmp_path / "env"
        assert audit.repoint_screenshot_dir(tmp_path / "x") == [], "nothing to re-point before the bridge is imported"
    finally:
        sys.modules.update(saved)
