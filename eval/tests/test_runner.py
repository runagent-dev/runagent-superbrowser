import json

from eval.core import arms
from eval.core.protocol import DEFAULT_PROTOCOL
from eval.core.runner import RunSpec, build_schedule, execute
from eval.core.tasks import Task


def _tasks(n=2):
    return [Task(task_id=f"t{i}", instruction=f"do {i}", start_url="https://x.com/", benchmark="b") for i in range(n)]


def test_schedule_interleaves_arms_per_task_and_groups_ts(tmp_path):
    chosen = arms.resolve("snap_center,ledger,fifo,no_ladder")
    specs = build_schedule(experiment="e", arms=chosen, tasks=_tasks(2), seeds=[0, 1], protocol=DEFAULT_PROTOCOL,
                           model="m", runs_root=tmp_path)
    assert len(specs) == 2 * 2 * 4
    first_task = [s for s in specs if s.seed == 0 and s.task.task_id == "t0"]
    # python-side arms first in the given order, then TS-side groups
    assert [s.arm.name for s in first_task] == ["ledger", "fifo", "no_ladder", "snap_center"]
    assert specs[0].run_dir == tmp_path / "e" / "ledger" / "t0" / "seed0"
    assert specs[-1].seed == 1 and specs[-1].task.task_id == "t1"


def test_runspec_env_and_json(tmp_path):
    spec = build_schedule(experiment="e", arms=arms.resolve("fifo"), tasks=_tasks(1), seeds=[3],
                          protocol=DEFAULT_PROTOCOL, model="m", runs_root=tmp_path)[0]
    env = spec.env()
    assert env["SUPERBROWSER_MEMORY_POLICY"] == "fifo"
    assert env["SUPERBROWSER_CROSS_TASK_MEMORY"] == "0"
    assert env["SUPERBROWSER_EVAL_CAPTURE_DIR"].endswith("/e/fifo/t0/seed3/workers")
    assert env["SUPERBROWSER_SCREENSHOT_DIR"].endswith("/screenshots")
    assert env["SUPERBROWSER_EVAL_SEED"] == "3"
    j = spec.to_json()
    assert j["run_id"] == "e:fifo:t0:s3" and j["protocol"]["hash"] == DEFAULT_PROTOCOL.hash()
    assert j["nanobot_overrides"]["maxToolIterations"] == DEFAULT_PROTOCOL.max_iterations
    json.dumps(j)  # serialisable


def test_dry_run_launches_nothing(tmp_path, capsys):
    specs = build_schedule(experiment="e", arms=arms.resolve("ledger"), tasks=_tasks(1), seeds=[0],
                           protocol=DEFAULT_PROTOCOL, model="m", runs_root=tmp_path)
    out = execute(specs, manage_server=False, assumed_server_env=None, resume=False, no_judge=True, judges=[],
                  dry_run=True, log_dir=tmp_path / "logs")
    assert out == []
    assert "1 runs" in capsys.readouterr().out
    assert not (tmp_path / "e").exists()


def test_execute_records_and_resumes_with_stubbed_launch(tmp_path, monkeypatch):
    """Parent-side glue: launch (stubbed) -> harvest -> record -> results.jsonl; --resume skips."""
    from eval.core import runner as R
    from eval.core.records import read_results, results_path
    from eval.tests.helpers import make_run_dir

    calls = []

    def fake_launch(spec, *, log_to):
        calls.append(spec.run_id)
        make_run_dir(tmp_path, experiment=spec.experiment, arm=spec.arm.name, task_id=spec.task.task_id,
                     seed=spec.seed, judges={"webjudge": spec.arm.name == "ledger"})
        return 0, "ok"

    from eval.core import server as S
    monkeypatch.setattr(R, "launch_run", fake_launch)
    monkeypatch.setattr(S, "http_ok", lambda *a, **k: True)   # pretend the default server is up
    specs = build_schedule(experiment="e", arms=arms.resolve("ledger,fifo"), tasks=_tasks(2), seeds=[0],
                           protocol=DEFAULT_PROTOCOL, model="m", runs_root=tmp_path)
    out = execute(specs, manage_server=False, assumed_server_env=None, resume=False, no_judge=True, judges=[],
                  dry_run=False, log_dir=tmp_path / "logs")
    assert len(out) == 4 and len(calls) == 4
    rows = read_results(results_path(tmp_path, "e"))
    assert len(rows) == 4 and sum(1 for r in rows if r.success) == 2
    assert all(r.protocol.get("model") == "test/model" for r in rows)   # from the synthetic meta
    # resume: nothing relaunched, summaries come from the stored records
    calls.clear()
    out2 = execute(specs, manage_server=False, assumed_server_env=None, resume=True, no_judge=True, judges=[],
                   dry_run=False, log_dir=tmp_path / "logs")
    assert calls == [] and all(o.status == "skipped" for o in out2)


def test_execute_refuses_ts_arm_on_unmanaged_server_with_wrong_env(tmp_path, monkeypatch):
    import pytest
    from eval.core import server as S

    monkeypatch.setattr(S, "http_ok", lambda *a, **k: True)
    specs = build_schedule(experiment="e", arms=arms.resolve("snap_center"), tasks=_tasks(1), seeds=[0],
                           protocol=DEFAULT_PROTOCOL, model="m", runs_root=tmp_path)
    with pytest.raises(RuntimeError, match="needs the browser server started with"):
        execute(specs, manage_server=False, assumed_server_env=None, resume=False, no_judge=True, judges=[],
                dry_run=False, log_dir=tmp_path / "logs")
