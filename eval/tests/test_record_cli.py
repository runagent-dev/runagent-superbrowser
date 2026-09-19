"""``eval.core.record`` builds records for run directories the runner never
recorded (SDK audit-trail runs, crashed runs) and freezes results.jsonl WITH the
analyzers' metrics."""
import json

from eval.core import record as rec_cli
from eval.core.records import read_results, results_path
from eval.tests.helpers import make_run_dir


def test_record_experiment_builds_missing_records_and_appends(tmp_path):
    d = make_run_dir(tmp_path, experiment="sdk", arm="ledger", task_id="t1", judges={"webjudge": True})
    assert not (d / "run_record.json").exists()
    done = rec_cli.record_experiment(tmp_path, "sdk")
    assert done == [d] and (d / "run_record.json").exists()
    rows = read_results(results_path(tmp_path, "sdk"))
    assert len(rows) == 1 and rows[0].success is True
    # idempotent: a second pass records nothing new
    assert rec_cli.record_experiment(tmp_path, "sdk") == []
    assert rec_cli.record_experiment(tmp_path, "sdk", force=True) == [d]


def test_rescue_marks_a_dead_run_as_harness_error_not_failure(tmp_path):
    d = make_run_dir(tmp_path, experiment="sdk", arm="ledger", task_id="t2")
    meta = json.loads((d / "meta.json").read_text())
    meta["stop_reason"] = "running"
    (d / "meta.json").write_text(json.dumps(meta))
    assert rec_cli.record_experiment(tmp_path, "sdk") == [], "a run still marked running is skipped without --rescue"
    assert rec_cli.rescue_run(d) is True
    assert json.loads((d / "meta.json").read_text())["stop_reason"] == "harness_error"
    done = rec_cli.record_experiment(tmp_path, "sdk")
    rows = read_results(results_path(tmp_path, "sdk"))
    assert done == [d] and rows[0].outcome["failure_reason"] == "harness_error"
    assert rows[0].outcome["exclusion_label"] == "harness_error"


def test_rebuild_results_writes_metrics_to_a_frozen_copy(tmp_path):
    from eval.core.harvest import build_record

    d = make_run_dir(tmp_path, experiment="x", arm="ledger", task_id="t1", judges={"webjudge": True})
    r = build_record(d)
    r.metrics["csd"] = {"csd_observed": 1.0}
    r.write(d)
    out = tmp_path / "frozen" / "results.jsonl"
    assert rec_cli.main(["--rebuild-results", "--experiment", "x", "--out", str(tmp_path), "--to", str(out)]) == 0
    rows = read_results(out)
    assert rows[0].metrics["csd"]["csd_observed"] == 1.0
    assert not results_path(tmp_path, "x").exists(), "--to must not touch the experiment's own results.jsonl"
