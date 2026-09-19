"""The sweep audit must catch the provenance problems that were found by hand
in the reported sweep: a mixed host model on one task, an as-run instruction
that differs from the frozen benchmark, code drift between arms, and the
infrastructure-error marker count."""
import json

from eval.core import audit_sweep
from eval.core.harvest import build_record
from eval.tests.helpers import make_run_dir


def _sweep(tmp_path, *, mixed_model_task=None, deviating_task=None, models=("m/a",)):
    recs = []
    for arm in ("ledger", "fifo"):
        for t in ("t1", "t2", "t3"):
            d = make_run_dir(tmp_path, experiment="sw", arm=arm, task_id=t, judges={"webjudge": arm == "ledger"})
            spec = json.loads((d / "spec.json").read_text())
            spec["protocol"]["benchmark"] = "synthetic"
            if t == deviating_task:
                spec["task"]["instruction"] = "a rewritten instruction"
            (d / "spec.json").write_text(json.dumps(spec))
            meta = json.loads((d / "meta.json").read_text())
            meta["environment"] = {"git_sha": "aaa111" if arm == "ledger" else "aaa111", "git_dirty": False}
            if t == mixed_model_task and arm == "fifo":
                meta["effective_defaults"]["model"] = "m/other"
            (d / "meta.json").write_text(json.dumps(meta))
            rec = build_record(d)
            rec.protocol["context_window_tokens"], rec.protocol["max_tokens"] = 200000, 16384
            rec.write(d)
            recs.append(rec)
    return recs


def test_audit_passes_on_a_clean_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_sweep, "load_benchmark", lambda name, annotate=True: [])
    monkeypatch.setattr(audit_sweep, "load_exclusions", lambda: {"rules": [], "task_exclusions": [], "instruction_deviations": []})
    recs = _sweep(tmp_path)
    a = audit_sweep.audit(recs, experiment="sw", runs_root=tmp_path)
    assert a["shape"]["records"] == 6 and a["shape"]["records_per_task"] == {2: 3}
    assert a["checks"]["one_record_per_task_arm"] and a["checks"]["single_protocol_hash"]
    assert a["checks"]["no_non_browser_tool_executed"] and a["checks"]["code_drift_balanced_across_arms_excluding_multi_sha_tasks"]
    assert a["toolset"]["non_browser_executed_total"] == 0
    assert a["context"]["runs_over_snip_threshold"] == 0
    assert "ERR_NO_SUPPORTED_PROXIES" in a["infra_errors"]["markers"]


def test_audit_flags_mixed_model_and_undeclared_instruction(tmp_path, monkeypatch):
    from eval.core.tasks import Task

    monkeypatch.setattr(audit_sweep, "load_benchmark",
                        lambda name, annotate=True: [Task(task_id=t, instruction="Find a red Toyota Corolla from 2018 to 2023.") for t in ("t1", "t2", "t3")])
    monkeypatch.setattr(audit_sweep, "load_exclusions", lambda: {"rules": [], "task_exclusions": [], "instruction_deviations": []})
    recs = _sweep(tmp_path, mixed_model_task="t2", deviating_task="t3")
    a = audit_sweep.audit(recs, experiment="sw", runs_root=tmp_path)
    assert list(a["host_model"]["mixed_model_tasks"]) == ["t2"]
    assert a["checks"]["mixed_model_tasks_all_excluded"] is False
    assert a["instructions"]["undeclared_mismatches"] == ["t3"]
    assert a["checks"]["all_instruction_mismatches_declared"] is False
    # declaring both makes the audit pass again
    monkeypatch.setattr(audit_sweep, "load_exclusions", lambda: {
        "rules": [{"id": "host_model_mismatch"}],
        "task_exclusions": [{"task_id": "t2", "rule": "host_model_mismatch", "reason": "x", "recorded": "today"}],
        "instruction_deviations": [{"task_id": "t3", "upstream_instruction": "a", "as_run_instruction": "b"}]})
    a2 = audit_sweep.audit(recs, experiment="sw", runs_root=tmp_path)
    assert a2["checks"]["mixed_model_tasks_all_excluded"] and a2["checks"]["all_instruction_mismatches_declared"]


def test_infra_marker_count_uses_one_fixed_regex(tmp_path):
    d = make_run_dir(tmp_path, experiment="sw", arm="ledger", task_id="t9",
                     turns=[("browser_open", {"url": "https://x/"}, "Chrome error page ERR_NO_SUPPORTED_PROXIES"),
                            ("browser_screenshot", {}, "This site can't be reached due to a proxy error"),
                            ("browser_close", {}, "Session closed. Vision: 5")])
    assert audit_sweep.infra_marker_count(d) == 2, "the normal 'Session closed' message must not count"
