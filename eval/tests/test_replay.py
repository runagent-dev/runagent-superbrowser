"""The legacy-run replay adapter is offline and idempotent."""
import json

from eval.core import replay
from eval.core.records import RunRecord
from eval.tests.helpers import make_run_dir


def _legacy_run(root, label, task_id, success):
    """A run dir in the OLD §7.4 shape (label meta, no spec.json)."""
    d = make_run_dir(root, experiment="__tmp__", arm=label, task_id=task_id, judges=None)
    # rewrite meta.json into the legacy schema and drop spec.json
    (d / "spec.json").unlink()
    (d / "meta.json").write_text(json.dumps({
        "label": label, "task_id": task_id, "seed": 0, "model": {"model": "test/model", "provider": "openrouter"},
        "vision_model": "vision/x", "instruction": "Find X", "url": "https://x.com/", "final_answer": "done" if success else "no",
        "stop_reason": "ok", "error": None, "duration_sec": 100.0, "timestamp": 1000.0, "harness_git_sha": "abc",
        "orch_task_id": "orch-abc", "worker_ids": ["w" + task_id[:6]],
        "judge": {"success": success, "rationale": "legacy", "judge_model": "gpt-5.5"}, "heuristic_success": success,
        "api_error": False,
    }, indent=2))
    # relocate to the legacy layout <root>/<label>/<task>/seed0
    dest = root / label / task_id / "seed0"
    dest.parent.mkdir(parents=True, exist_ok=True)
    d.rename(dest)
    return dest


def test_replay_adapts_and_is_idempotent(tmp_path):
    _legacy_run(tmp_path, "claude", "petfinder_rabbits", True)
    _legacy_run(tmp_path, "kimi", "petfinder_rabbits", False)
    n = replay.replay_experiment(tmp_path, experiment="rep")
    assert n == 2
    recs = {r.ids["arm"]: r for r in replay.load_replayed(tmp_path, "rep")}
    assert recs["claude"].outcome["success"] is True and recs["claude"].outcome["decided_by"] == "webjudge"
    assert recs["kimi"].outcome["success"] is False
    assert recs["claude"].counts["tool_calls_executed"] == 5  # from the synthetic transcript
    # the ORIGINAL run dir is untouched (legacy analyzer keeps working) ...
    d = tmp_path / "claude" / "petfinder_rabbits" / "seed0"
    assert sorted(p.name for p in d.iterdir()) == ["ledgers", "meta.json", "result.txt", "screenshots", "usage.json", "workers"]
    assert "label" in json.loads((d / "meta.json").read_text())
    # ... the adapted copy lives under the experiment tree and points back at its source
    copy = tmp_path / "rep" / "claude" / "petfinder_rabbits" / "seed0"
    assert (copy / "spec.json").exists() and (copy / "run_record.json").exists() and (copy / "judges" / "webjudge.json").exists()
    assert (copy / "source_run_dir").read_text().strip() == str(d.resolve())
    # re-running rebuilds the copies from scratch (idempotent)
    assert replay.replay_experiment(tmp_path, experiment="rep") == 2
    assert len(replay.load_replayed(tmp_path, "rep")) == 2
