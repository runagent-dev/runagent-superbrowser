"""Build the submission supplement: per-task evidence for every run of a sweep.

    python -m eval.supplement.build --experiment ablate10 --tier light          # < 100 MB, for the OpenReview upload
    python -m eval.supplement.build --experiment ablate10 --tier full --zip     # everything, for the hosted archive
    python -m eval.supplement.build --experiment ablate10 --also sdk --tier light

Output: ``eval/artifacts/supplement_<tier>_<date>/`` (or ``--out``) with

    README.md                 generated: protocol hash, benchmark digests, task index with per-arm
                              outcomes and the upstream-wording flag, as-run deviations (from
                              PROTOCOL.md), the sweep audit, the archived-run index, file layout
    records/                  results.jsonl (with metrics), per_run.csv, arm summaries, paired tests,
                              exclusions.json, subsets.json, benchmark manifest, PROTOCOL.md, audit.*
    tables/                   the paper's generated tables + numbers.tex
    tasks/<task_id>/<arm>/    spec.json, meta.json, result.txt, judges/*.json, ledgers/<role>/{steps,events,
                              iterations}.jsonl + ledger.json + task_summary.json, screenshots/
                              (light: JPEG q45 ≤800px; full: originals + live_context.jsonl.gz +
                              workers/*.json + run.log + config.redacted.json + manifest.json)
    archives/                 full tier only: spec/meta/result of every archived (discarded/orphaned/
                              failed-attempt) run, so nothing that was paid for is hidden
    SHA256SUMS                over every file

A secret scan runs over every text file before the bundle is declared complete;
a hit aborts the build and names the file. The light tier refuses to exceed the
size cap (``--max-mb``, default 100).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from eval._bootstrap import REPO_ROOT
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_records
from eval.core.records import RunRecord, read_results
from eval.core.tasks import BENCH_DIR, load_benchmark, load_exclusions, load_subsets

ARTIFACTS = REPO_ROOT / "eval" / "artifacts"
LIGHT_LEDGER_FILES = ("steps.jsonl", "events.jsonl", "iterations.jsonl", "ledger.json", "task_summary.json", "episodic.jsonl")
FULL_LEDGER_EXTRA = ("live_context.jsonl.gz", "vision_calls.jsonl", "clicks.jsonl", "facts.jsonl", "step_history.json",
                     "step_history.md", "checkpoint.json", "usage.json", "screenshots.jsonl")
LIGHT_RUN_FILES = ("spec.json", "meta.json", "result.txt", "usage.json")
FULL_RUN_EXTRA = ("run.log", "config.redacted.json", "manifest.json", "run_record.json")
SECRET_PATTERNS = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|Bearer [A-Za-z0-9._-]{20,}|AIza[0-9A-Za-z_-]{30,}|"
    r"ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{20,}|[A-Z_]*(?:API_KEY|SECRET|TOKEN)=[^\s\"'*]{12,}")
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".csv", ".tex", ".log", ".html"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return True


LIGHT_MAX_PX = 800
LIGHT_JPEG_QUALITY = 45


def downsample_screenshots(src_dir: Path, dest_dir: Path, *, max_px: int = LIGHT_MAX_PX, quality: int = LIGHT_JPEG_QUALITY) -> int:
    """JPEG-recompress the ordered screenshots (light tier). Falls back to a
    plain copy when Pillow is unavailable."""
    if not src_dir.exists():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        from PIL import Image
    except Exception:
        Image = None  # type: ignore[assignment]
    for p in sorted(src_dir.iterdir()):
        if p.name == "index.jsonl":
            shutil.copyfile(p, dest_dir / p.name)
            continue
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        out = dest_dir / (p.stem + ".jpg")
        if Image is None:
            shutil.copyfile(p, dest_dir / p.name)
        else:
            try:
                im = Image.open(p).convert("RGB")
                if max(im.size) > max_px:
                    scale = max_px / max(im.size)
                    im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
                im.save(out, "JPEG", quality=quality, optimize=True)
            except Exception:
                shutil.copyfile(p, dest_dir / p.name)
        n += 1
    return n


def copy_run(run_dir: Path, dest: Path, *, tier: str, max_px: int = LIGHT_MAX_PX, quality: int = LIGHT_JPEG_QUALITY) -> dict[str, Any]:
    """Copy one run directory's evidence into ``dest``; returns what landed."""
    got: dict[str, Any] = {"files": 0, "screenshots": 0, "ledgers": 0}
    for name in LIGHT_RUN_FILES + (FULL_RUN_EXTRA if tier == "full" else ()):
        got["files"] += _copy(run_dir / name, dest / name)
    for p in sorted((run_dir / "judges").glob("*.json")):
        got["files"] += _copy(p, dest / "judges" / p.name)
    for d in sorted((run_dir / "ledgers").glob("*")):
        if not d.is_dir():
            continue
        got["ledgers"] += 1
        for name in LIGHT_LEDGER_FILES + (FULL_LEDGER_EXTRA if tier == "full" else ()):
            got["files"] += _copy(d / name, dest / "ledgers" / d.name / name)
    if tier == "full":
        for p in sorted((run_dir / "workers").glob("*.json*")):
            got["files"] += _copy(p, dest / "workers" / p.name)
        if (run_dir / "screenshots").exists():
            shutil.copytree(run_dir / "screenshots", dest / "screenshots", dirs_exist_ok=True)
            got["screenshots"] = len([p for p in (dest / "screenshots").iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    else:
        got["screenshots"] = downsample_screenshots(run_dir / "screenshots", dest / "screenshots", max_px=max_px, quality=quality)
    return got


def secret_scan(root: Path) -> list[tuple[Path, str]]:
    hits: list[tuple[Path, str]] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in SECRET_PATTERNS.finditer(text):
            token = m.group(0)
            if token.endswith("=***") or "***" in token:
                continue
            hits.append((p.relative_to(root), token[:12] + "…"))
            break
    return hits


def dir_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _protocol_deviations_text() -> str:
    proto = REPO_ROOT / "eval" / "PROTOCOL.md"
    if not proto.exists():
        return ""
    text = proto.read_text(encoding="utf-8")
    i = text.find("## As-run deviations")
    return text[i:] if i >= 0 else ""


def build_readme(records: list[RunRecord], *, experiments: list[str], tier: str, audit: dict[str, Any] | None,
                 task_rows: list[dict[str, Any]], archives: dict[str, Any], size_mb: float,
                 per_run_counts: dict[str, int]) -> str:
    ex = load_exclusions()
    manifest = json.loads((BENCH_DIR / "manifest.json").read_text(encoding="utf-8")) if (BENCH_DIR / "manifest.json").exists() else {}
    arms = sorted({str(r.ids.get("arm")) for r in records})
    hashes = sorted({str((r.protocol or {}).get("hash")) for r in records})
    L = [f"# Supplementary material — per-run evidence ({tier} tier)", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} by `python -m eval.supplement.build` from the per-run "
         f"records of experiment(s) **{', '.join(experiments)}**: {len(records)} runs, {len(task_rows)} tasks, arms {', '.join(arms)}; "
         f"protocol hash {', '.join(hashes)}. Bundle size {size_mb:.1f} MB.", "",
         "## What is in here", "",
         "| Path | Contents |", "|---|---|",
         "| `records/results.jsonl` | one record per run (ids, protocol, timing, outcome, counts, tokens, cost, process metrics, artifacts) |",
         "| `records/per_run.csv`, `arm_summary*.csv`, `*paired_binary.json`, `paired_metrics.csv` | the analyzer outputs the paper's tables are generated from |",
         "| `records/exclusions.json`, `subsets.json`, `manifest.json`, `PROTOCOL.md` | pre-registered exclusions and instruction deviations; the task subset and its composition history; benchmark digests; the frozen protocol with its as-run deviations |",
         "| `records/audit.json`, `audit.md` | the sweep audit (host model per run, code drift per arm, non-browser tool executions, instruction wording vs the frozen benchmark, trace presence, peak context, judge input, infra-error markers) |",
         "| `tables/` | the paper's generated tables and `numbers.tex` (every number quoted in the prose) |",
         "| `tasks/<task_id>/<arm>/` | per run: `spec.json` (task + arm + protocol), `meta.json` (timing, stop reason, final answer), `result.txt`, `judges/*.json` (verdict, rationale, per-screenshot scores), `ledgers/<role>/` (executed steps, memory events, per-iteration bank, ledger snapshot), `screenshots/` (the frames the judge scored" + (", full resolution) plus `workers/*.json` transcripts, `live_context.jsonl.gz`, `run.log`, `config.redacted.json`, `manifest.json` |" if tier == "full" else ", JPEG-recompressed to ≤800 px) |"),
         "| `archives/` | " + ("spec/meta/result of every archived run (discarded, orphaned by a task swap, failed attempt) |" if tier == "full" else "not in the light tier; indexed below |"),
         "| `SHA256SUMS` | digest of every file |", ""]
    L += ["## Benchmark", ""]
    if manifest:
        L.append(f"Online-Mind2Web as vendored ({manifest.get('source', {}).get('upstream', '')}); frozen {manifest.get('frozen_at')}.")
        L.append("")
        L.append("| File | n | sha256 |\n|---|---|---|")
        for name, entry in (manifest.get("files") or {}).items():
            L.append(f"| `{name}` | {entry.get('n')} | `{entry.get('sha256')}` |")
        L.append("")
    L += ["## Tasks and outcomes", "",
          "`upstream` = the as-run instruction equals the frozen benchmark wording (a `no` is declared in `records/exclusions.json` → `instruction_deviations`). "
          "`excluded` = left every primary table under a pre-registered rule (`records/exclusions.json` → `task_exclusions`).", "",
          "| task_id | level | site | upstream | excluded | " + " | ".join(arms) + " | instruction |",
          "|" + "---|" * (6 + len(arms))]
    for row in task_rows:
        L.append(f"| `{row['task_id']}` | {row['level']} | {row['site']} | {'yes' if row['upstream'] else 'NO'} | {row['excluded'] or ''} | "
                 + " | ".join(row["outcomes"].get(a, "–") for a in arms) + f" | {row['instruction']} |")
    L.append("")
    if ex.get("instruction_deviations"):
        L += ["### Instruction deviations", ""]
        for d in ex["instruction_deviations"]:
            L += [f"- `{d['task_id']}` — as run: “{d['as_run_instruction']}”; benchmark: “{d['upstream_instruction']}”. {d.get('reason', '')} ({d.get('handling', '')})"]
        L.append("")
    if ex.get("task_exclusions"):
        L += ["### Pre-registered task exclusions", ""]
        for e in ex["task_exclusions"]:
            L += [f"- `{e['task_id']}` — rule `{e['rule']}`: {e['reason']}"]
        L.append("")
    L += ["## Archived runs (not counted)", ""]
    if archives:
        L.append("| directory | entries | size (MB) | why |\n|---|---|---|---|")
        for name, info in archives.items():
            L.append(f"| `{name}` | {info['entries']} | {info['size_mb']} | {info.get('why', '')} |")
    else:
        L.append("none")
    L.append("")
    dev = _protocol_deviations_text()
    if dev:
        L += ["## As-run deviations from the frozen protocol", "", dev.split("\n", 1)[1].strip(), ""]
    if audit:
        L += ["## Sweep audit (checks)", "", "| check | result |", "|---|---|"]
        for k, v in (audit.get("checks") or {}).items():
            L.append(f"| {k} | {'PASS' if v else 'FAIL'} |")
        L.append("")
        L.append("Full audit in `records/audit.md`.")
        L.append("")
    L += ["## Per-run file counts", "", f"files {per_run_counts.get('files', 0)}, ledgers {per_run_counts.get('ledgers', 0)}, screenshots {per_run_counts.get('screenshots', 0)}.", "",
          "## Integrity", "",
          "Every text file was scanned for credential patterns before packaging; `config.redacted.json` files carry `***` in place of secrets. "
          "Run `sha256sum -c SHA256SUMS` to verify the bundle. Nothing in the run directories was edited: the copies here are byte-identical to the "
          "harness's output except the light-tier screenshots (recompressed) and the generated README.", ""]
    return "\n".join(L)


def build(*, experiments: list[str], tier: str, out: Path, runs_root: Path = DEFAULT_RUNS_ROOT, max_mb: float = 100.0,
          make_zip: bool = False, skip_secret_scan: bool = False, max_px: int = LIGHT_MAX_PX,
          quality: int = LIGHT_JPEG_QUALITY) -> dict[str, Any]:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    records: list[RunRecord] = []
    audit: dict[str, Any] | None = None
    for exp in experiments:
        frozen = ARTIFACTS / f"{exp}_paper"
        recs = read_results(frozen / "results.jsonl") if (frozen / "results.jsonl").exists() else load_records(exp, runs_root=runs_root)
        records += recs
        # records dir: frozen analyzer outputs when present, else whatever the audit dir holds
        for src_dir in (frozen, ARTIFACTS / f"{exp}_audit"):
            if src_dir.exists():
                for p in sorted(src_dir.iterdir()):
                    if p.is_file() and p.suffix in (".jsonl", ".csv", ".json", ".md"):
                        _copy(p, out / "records" / (p.name if len(experiments) == 1 else f"{exp}__{p.name}"))
        if audit is None:
            for cand in (frozen / "audit.json", ARTIFACTS / f"{exp}_audit" / "audit.json"):
                if cand.exists():
                    audit = json.loads(cand.read_text(encoding="utf-8"))
                    break
    if not records:
        raise SystemExit("no records found")
    # the records the bundle was built from, verbatim (one row per run, last write wins)
    (out / "records").mkdir(parents=True, exist_ok=True)
    with (out / "records" / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_dict(), default=str, ensure_ascii=False) + "\n")
    for name in ("exclusions.json", "subsets.json", "manifest.json"):
        _copy(BENCH_DIR / name, out / "records" / name)
    _copy(REPO_ROOT / "eval" / "PROTOCOL.md", out / "records" / "PROTOCOL.md")
    for exp in experiments:
        tdir = ARTIFACTS / f"{exp}_paper" / "tables"
        if tdir.exists():
            for p in sorted(tdir.glob("*.tex")):
                _copy(p, out / "tables" / p.name)
    # per-run evidence
    ex = load_exclusions()
    excluded = {e["task_id"]: e["rule"] for e in ex.get("task_exclusions", [])}
    deviations = {d["task_id"] for d in ex.get("instruction_deviations", [])}
    bench_cache: dict[str, dict[str, str]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    counts = {"files": 0, "ledgers": 0, "screenshots": 0}
    for r in sorted(records, key=lambda r: (str(r.ids.get("task_id")), str(r.ids.get("arm")))):
        tid, arm = str(r.ids.get("task_id")), str(r.ids.get("arm"))
        run_dir = Path(r.ids.get("run_dir", ""))
        dest = out / "tasks" / tid / arm
        if run_dir.exists():
            got = copy_run(run_dir, dest, tier=tier, max_px=max_px, quality=quality)
            for k in counts:
                counts[k] += got[k]
        row = tasks.setdefault(tid, {"task_id": tid, "level": r.ids.get("level"), "site": "", "instruction": "", "upstream": True,
                                     "excluded": excluded.get(tid), "outcomes": {}})
        row["outcomes"][arm] = "✓" if r.outcome.get("success") else ("✗" if r.outcome.get("success") is False else "?")
        if not row["instruction"] and (run_dir / "spec.json").exists():
            try:
                spec = json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))
                instr = str(spec.get("task", {}).get("instruction") or "")
                row["instruction"] = instr.replace("|", "\\|")[:140]
                row["site"] = str(spec.get("task", {}).get("website") or spec.get("task", {}).get("start_url") or "").replace("https://", "").rstrip("/")
                bench = str(spec.get("protocol", {}).get("benchmark") or "")
                if bench and bench not in bench_cache:
                    try:
                        bench_cache[bench] = {t.task_id: t.instruction for t in load_benchmark(bench, annotate=False)}
                    except Exception:
                        bench_cache[bench] = {}
                frozen_instr = bench_cache.get(bench, {}).get(tid)
                row["upstream"] = (frozen_instr is None and tid not in deviations) or (frozen_instr is not None and frozen_instr.strip() == instr.strip())
            except Exception:
                pass
    # archives
    archives: dict[str, Any] = {}
    subset_note = ""
    for sub in load_subsets().values():
        if set(sub.get("task_ids", [])) >= {t for t in tasks if t not in excluded}:
            subset_note = sub.get("note") or ""
            break
    for exp in experiments:
        base = runs_root / exp
        if not base.exists():
            continue
        for d in sorted(base.glob("_*")):
            if not d.is_dir() or d.name in ("_inbox", "_logs"):
                continue
            entries = [p for p in d.iterdir()]
            why = {"_discarded_petfinder": "task swapped out of the subset after its arms ran (see subset note)",
                   "_discarded_student_com": "runs of the intermediate instruction variant (see instruction_deviations)",
                   "_failed_attempts": "attempts archived before a retry (Ctrl-C / timeout / crash); never judged",
                   "_orphaned_by_task_swaps": "completed runs of tasks later swapped out of the subset (see subset note)",
                   "_preproxy_discarded": "run made before the evaluation proxy was configured"}.get(d.name, "")
            archives[d.name] = {"entries": len(entries), "size_mb": round(sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1048576, 1), "why": why}
            if tier == "full":
                for run in sorted(d.rglob("spec.json")):
                    rd = run.parent
                    rel = rd.relative_to(base)
                    for name in ("spec.json", "meta.json", "result.txt", "run_record.json"):
                        _copy(rd / name, out / "archives" / rel / name)
                    for p in sorted((rd / "judges").glob("*.json")):
                        _copy(p, out / "archives" / rel / "judges" / p.name)
    if subset_note:
        (out / "records" / "subset_note.md").write_text(subset_note + "\n", encoding="utf-8")
    # secret scan
    hits = [] if skip_secret_scan else secret_scan(out)
    if hits:
        for p, tok in hits:
            print(f"SECRET? {p}: {tok}")
        raise SystemExit(f"secret scan hit {len(hits)} file(s); bundle NOT completed: {out}")
    size_mb = dir_size(out) / 1048576
    task_rows = [tasks[t] for t in sorted(tasks)]
    (out / "README.md").write_text(build_readme(records, experiments=experiments, tier=tier, audit=audit, task_rows=task_rows,
                                                archives=archives, size_mb=size_mb, per_run_counts=counts), encoding="utf-8")
    size_mb = dir_size(out) / 1048576
    if tier == "light" and size_mb > max_mb:
        shots_mb = sum(p.stat().st_size for p in (out / "tasks").rglob("screenshots/*") if p.is_file()) / 1048576 if (out / "tasks").exists() else 0.0
        raise SystemExit(f"light bundle is {size_mb:.1f} MB > {max_mb} MB cap ({shots_mb:.1f} MB of screenshots at "
                         f"{max_px}px/q{quality}); pass --max-px/--quality lower or drop --also experiments")
    # checksums
    with (out / "SHA256SUMS").open("w", encoding="utf-8") as f:
        for p in sorted(out.rglob("*")):
            if p.is_file() and p.name != "SHA256SUMS":
                f.write(f"{sha256_file(p)}  {p.relative_to(out).as_posix()}\n")
    zip_path = None
    if make_zip:
        zip_path = shutil.make_archive(str(out), "zip", root_dir=out.parent, base_dir=out.name)
    summary = {"out": str(out), "tier": tier, "records": len(records), "tasks": len(tasks), "size_mb": round(size_mb, 1),
               "files": counts, "archives": archives, "zip": zip_path}
    print(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the per-run evidence supplement")
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--also", action="append", default=[], help="additional experiment(s) to include (e.g. sdk)")
    ap.add_argument("--tier", choices=("light", "full"), default="light")
    ap.add_argument("--out", default=None)
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    ap.add_argument("--max-mb", type=float, default=100.0)
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--max-px", type=int, default=LIGHT_MAX_PX, help="light tier: longest screenshot side in px")
    ap.add_argument("--quality", type=int, default=LIGHT_JPEG_QUALITY, help="light tier: JPEG quality")
    ap.add_argument("--skip-secret-scan", action="store_true", help="tests only")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else ARTIFACTS / f"supplement_{args.tier}_{time.strftime('%Y%m%d')}"
    build(experiments=[args.experiment, *args.also], tier=args.tier, out=out, runs_root=Path(args.runs), max_mb=args.max_mb,
          make_zip=args.zip, skip_secret_scan=args.skip_secret_scan, max_px=args.max_px, quality=args.quality)
    return 0


if __name__ == "__main__":
    sys.exit(main())
