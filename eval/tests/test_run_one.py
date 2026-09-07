"""Offline checks of the per-run helpers in eval.core.run_one (no browser, no LLM)."""
import json
import os
import stat

from eval.core import run_one


def test_prepare_nanobot_config_patches_pins_and_redacts(tmp_path, monkeypatch):
    src = tmp_path / "config.json"
    src.write_text(json.dumps({"agents": {"defaults": {"model": "old/model", "maxTokens": 100000, "contextWindowTokens": 65536}},
                               "providers": {"openai": {"apiKey": "sk-secret-123"}}}))
    monkeypatch.setenv("NANOBOT_CONFIG", str(src))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path, effective = run_one.prepare_nanobot_config(
        run_dir, overrides={"contextWindowTokens": 200000, "maxTokens": 16384, "maxToolIterations": 50}, model="new/model")
    try:
        data = json.loads(path.read_text())
        d = data["agents"]["defaults"]
        assert d["model"] == "new/model" and d["contextWindowTokens"] == 200000 and d["maxToolIterations"] == 50
        assert data["providers"]["openai"]["apiKey"] == "sk-secret-123"          # the live copy keeps the key
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        red = json.loads((run_dir / "config.redacted.json").read_text())
        assert red["providers"]["openai"]["apiKey"] == "***"                     # the committed-able copy does not
        assert effective["model"] == "new/model" and "apiKey" not in json.dumps(effective)
    finally:
        path.unlink()


def test_frame_task_adds_start_url_once():
    assert run_one.frame_task({"instruction": "Do x", "start_url": "https://a.com/"}).endswith("Start URL: https://a.com/")
    assert run_one.frame_task({"instruction": "Go to https://a.com/ and do x", "start_url": "https://a.com/"}) == "Go to https://a.com/ and do x"


def test_harvest_and_screenshot_index(tmp_path, monkeypatch):
    base = tmp_path / "superbrowser"
    (base / "orch-1" / "memory").mkdir(parents=True)
    (base / "orch-1" / "memory" / "events.jsonl").write_text('{"type":"memory_attach"}\n')
    (base / "w1" / "memory").mkdir(parents=True)
    for name in ("events.jsonl", "steps.jsonl", "vision_calls.jsonl", "live_context.jsonl.gz"):
        (base / "w1" / "memory" / name).write_bytes(b"x")
    (base / "w1" / "step_history.json").write_text("{}")
    monkeypatch.setattr(run_one, "MEMORY_BASE", base)
    run_dir = tmp_path / "run"
    (run_dir / "workers").mkdir(parents=True)
    (run_dir / "workers" / "w1.json").write_text("{}")
    ids = run_one.harvest_memory_dirs(run_dir, ["orch-1"])
    assert ids == ["orch-1", "w1"]
    assert (run_dir / "ledgers" / "w1" / "live_context.jsonl.gz").exists()
    assert (run_dir / "ledgers" / "w1" / "step_history.json").exists()
    assert (run_dir / "ledgers" / "orch-1" / "events.jsonl").exists()
    shots = run_dir / "screenshots"
    shots.mkdir()
    (shots / "001-a.jpg").write_bytes(b"1")
    (shots / "002-b.jpg").write_bytes(b"2")
    assert run_one.index_screenshots(run_dir) == 2
    rows = [json.loads(l) for l in (shots / "index.jsonl").read_text().splitlines()]
    assert [r["file"] for r in rows] == ["001-a.jpg", "002-b.jpg"] and rows[0]["source"] == "unknown"
    # a bridge-written index is kept, not overwritten
    (shots / "index.jsonl").write_text('{"idx":1,"file":"001-a.jpg","source":"sync"}\n')
    run_one.index_screenshots(run_dir)
    assert json.loads((shots / "index.jsonl").read_text().splitlines()[0])["source"] == "sync"


def test_main_glue_with_stubbed_run(tmp_path, monkeypatch):
    """Exercise run_one._main end to end with the LLM/browser call stubbed out."""
    import asyncio

    src = tmp_path / "config.json"
    src.write_text(json.dumps({"agents": {"defaults": {"model": "old/model"}}}))
    monkeypatch.setenv("NANOBOT_CONFIG", str(src))
    base = tmp_path / "superbrowser"
    (base / "orch-x" / "memory").mkdir(parents=True)
    (base / "orch-x" / "memory" / "events.jsonl").write_text('{"type":"memory_attach"}\n')
    monkeypatch.setattr(run_one, "MEMORY_BASE", base)
    seen = {}

    async def fake_orchestrator(spec, framed_task, timeout):
        seen["framed"] = framed_task
        seen["timeout"] = timeout
        from nanobot.config.loader import get_config_path
        seen["config_path"] = str(get_config_path())
        return {"orch_task_id": "orch-x", "stop_reason": "ok", "error": None, "final_answer": "answer!",
                "raw_content": "answer!", "role_task_ids": ["orch-x"], "duration_s": 1.5,
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "by_role": {}}}

    monkeypatch.setattr(run_one, "_run_orchestrator", fake_orchestrator)
    run_dir = tmp_path / "runs" / "e" / "ledger" / "t1" / "seed0"
    spec = {"run_id": "e:ledger:t1:s0", "experiment": "e", "seed": 0, "arm": {"name": "ledger", "env": {}},
            "task": {"task_id": "t1", "instruction": "Do x", "start_url": "https://a.com/"},
            "run_dir": str(run_dir), "model": "new/model", "topology": "orchestrator",
            "nanobot_overrides": {"maxToolIterations": 50}, "internal_timeout_s": 1710}
    rc = asyncio.run(run_one._main(spec))
    assert rc == 0
    assert seen["framed"].endswith("Start URL: https://a.com/") and seen["timeout"] == 1710
    assert seen["config_path"].startswith("/tmp") and "sb-eval-config-" in seen["config_path"]
    assert not os.path.exists(seen["config_path"])                       # temp config removed after the run
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["stop_reason"] == "ok" and meta["final_answer"] == "answer!" and meta["role_task_ids"] == ["orch-x"]
    assert meta["effective_defaults"]["model"] == "new/model" and meta["effective_defaults"]["maxToolIterations"] == 50
    assert (run_dir / "result.txt").read_text() == "answer!"
    assert json.loads((run_dir / "usage.json").read_text())["total_tokens"] == 12
    assert (run_dir / "ledgers" / "orch-x" / "events.jsonl").exists()
    assert (run_dir / "config.redacted.json").exists() and "environment" in meta
