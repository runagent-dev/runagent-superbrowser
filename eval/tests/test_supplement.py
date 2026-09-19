"""The supplement must carry per-run evidence for every record, refuse to ship
a credential, and index (never hide) the archived runs."""
import json

from eval.core.harvest import build_record
from eval.core.records import append_result, results_path
from eval.supplement import build as sup
from eval.tests.helpers import make_run_dir


def _sweep(tmp_path):
    runs = tmp_path / "runs"
    for arm in ("ledger", "fifo"):
        for t in ("t1", "t2"):
            d = make_run_dir(runs, experiment="exp", arm=arm, task_id=t, judges={"webjudge": arm == "ledger"})
            rec = build_record(d)
            rec.write(d)
            append_result(results_path(runs, "exp"), rec)
    # an archived attempt that must be indexed, not hidden
    arch = runs / "exp" / "_failed_attempts" / "ledger__t9__seed0__20260101T000000"
    arch.mkdir(parents=True)
    (arch / "spec.json").write_text("{}")
    (arch / "result.txt").write_text("partial")
    return runs


def test_light_bundle_has_evidence_readme_and_checksums(tmp_path, monkeypatch):
    runs = _sweep(tmp_path)
    monkeypatch.setattr(sup, "ARTIFACTS", tmp_path / "artifacts")
    out = tmp_path / "bundle"
    s = sup.build(experiments=["exp"], tier="light", out=out, runs_root=runs)
    assert s["records"] == 4 and s["tasks"] == 2
    for rel in ("README.md", "SHA256SUMS", "records/results.jsonl", "records/exclusions.json", "records/PROTOCOL.md",
                "tasks/t1/ledger/spec.json", "tasks/t1/ledger/judges/webjudge.json", "tasks/t1/ledger/ledgers/wt1/steps.jsonl",
                "tasks/t2/fifo/result.txt", "tasks/t1/ledger/screenshots/001-shot.jpg"):
        assert (out / rel).exists(), rel
    assert not (out / "tasks/t1/ledger/workers").exists(), "transcripts are full-tier only"
    assert not (out / "archives").exists()
    readme = (out / "README.md").read_text()
    assert "_failed_attempts" in readme and "| `t1` |" in readme and "✓" in readme
    sums = (out / "SHA256SUMS").read_text().splitlines()
    assert any(line.endswith("records/results.jsonl") for line in sums)


def test_full_bundle_includes_transcripts_context_and_archives(tmp_path, monkeypatch):
    runs = _sweep(tmp_path)
    monkeypatch.setattr(sup, "ARTIFACTS", tmp_path / "artifacts")
    out = tmp_path / "bundle_full"
    sup.build(experiments=["exp"], tier="full", out=out, runs_root=runs, make_zip=True)
    assert (out / "tasks/t1/ledger/workers/wt1.json").exists()
    assert (out / "tasks/t1/ledger/screenshots/001-shot.png").exists()
    assert (out / "archives/_failed_attempts/ledger__t9__seed0__20260101T000000/result.txt").read_text() == "partial"
    assert (tmp_path / "bundle_full.zip").exists()


def test_secret_scan_aborts_the_bundle(tmp_path, monkeypatch):
    runs = _sweep(tmp_path)
    d = runs / "exp" / "ledger" / "t1" / "seed0"
    (d / "result.txt").write_text("here is my key sk-ant-abcdefghijklmnopqrstuvwxyz0123456789")
    monkeypatch.setattr(sup, "ARTIFACTS", tmp_path / "artifacts")
    import pytest

    with pytest.raises(SystemExit, match="secret scan"):
        sup.build(experiments=["exp"], tier="light", out=tmp_path / "b", runs_root=runs)


def test_light_tier_respects_size_cap(tmp_path, monkeypatch):
    runs = _sweep(tmp_path)
    monkeypatch.setattr(sup, "ARTIFACTS", tmp_path / "artifacts")
    import pytest

    with pytest.raises(SystemExit, match="cap"):
        sup.build(experiments=["exp"], tier="light", out=tmp_path / "b", runs_root=runs, max_mb=0.0001)
