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
