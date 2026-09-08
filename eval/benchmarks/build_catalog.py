"""Build ``task_catalog.json`` + the full-split JSONL from the frozen benchmark
and the TinyFish annotation workbook.

The frozen Online-Mind2Web file gives the authoritative task text, start URL and
difficulty. The workbook (``assets/Copy of TinyFish-Mind2Web Agent Runs.xlsx``)
adds per-task annotations we cannot derive from the task text: an instruction
category, an anti-bot risk rating, an attention level, plus one external
system's pass/fail and several human labels of THAT system's runs.

Nothing here judges SuperBrowser. The external columns are recorded under
``external`` so they can never be mistaken for our own results; they are useful
for choosing tasks (avoid high anti-bot sites for a first pilot) and as prior
art in the paper, not as a baseline we ran.

    python -m eval.benchmarks.build_catalog --xlsx <path> [--out eval/benchmarks]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from eval._bootstrap import REPO_ROOT

BENCH_DIR = REPO_ROOT / "eval" / "benchmarks"
SOURCE_JSONL = REPO_ROOT.parent / "BrowserOS" / "packages" / "browseros-agent" / "apps" / "eval" / "data" / "mind2web.jsonl"
DEFAULT_XLSX = REPO_ROOT.parent / "assets" / "Copy of TinyFish-Mind2Web Agent Runs.xlsx"

# sheet -> the team whose annotators labelled it
ANNOTATION_SHEETS = {
    "Team 1_annotation -- April": "april",
    "Team 1_annotation -- Morris": "morris",
    "Team 1_annotation-- Mia": "mia",
    "Team 2_annotation -- Dianne": "dianne",
    "Team 2_annotation -- Judy": "judy",
    "Team 2_annotation -- Arvin": "arvin",
}
CATEGORY_SLUGS = {
    "General info lookup / navigation": "info_lookup",
    "Geo/location-dependent task": "geo_dependent",
    "Time-sensitive lookup": "time_sensitive",
    "Other info/product search": "product_search",
    "Document / official source verification": "document_verification",
}


def canonical_id(task_id: Any) -> str:
    """The workbook re-versions some ids with a date suffix (``<id>_110325``)."""
    return str(task_id or "").strip().split("_")[0]


def _sheet_rows(wb: Any, name: str) -> list[dict[str, Any]]:
    ws = wb[name]
    it = ws.iter_rows(values_only=True)
    header = [("" if c is None else str(c)).strip() for c in next(it)]
    out = []
    for raw in it:
        row = {header[i]: raw[i] for i in range(min(len(header), len(raw))) if header[i]}
        if row.get("task_id"):
            out.append(row)
    return out


def _label(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _norm(v: Any) -> str | None:
    s = ("" if v is None else str(v)).strip()
    return s or None


def load_source() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in SOURCE_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        meta = r.get("metadata") or {}
        add = meta.get("additional") or {}
        out[r["query_id"]] = {
            "task_id": r["query_id"],
            "instruction": r["query"],
            "start_url": r.get("start_url"),
            "website": meta.get("website"),
            "level": add.get("level"),
            "reference_length": add.get("reference_length"),
        }
    return out


def build(xlsx: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import openpyxl

    tasks = load_source()
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)

    # instruction category / anti-bot risk / attention level (identical on every
    # annotation sheet; the unlabelled team sheets are the clean source)
    for sheet in ("Team 1_annotation", "Team 2_annotation"):
        for row in _sheet_rows(wb, sheet):
            t = tasks.get(canonical_id(row["task_id"]))
            if t is None:
                continue
            cat = _norm(row.get("Instruction Classification") or row.get("Classification"))
            t["category"] = CATEGORY_SLUGS.get(cat or "", "uncategorised")
            t["category_label"] = cat
            t["antibot_risk"] = (_norm(row.get("antibot_risk")) or "unknown").lower()
            t["attention_level"] = (_norm(row.get("attention_level")) or "unknown").lower()

    # one external system's result, and human labels OF THAT SYSTEM's runs
    for row in _sheet_rows(wb, "021026 Run Results"):
        t = tasks.get(canonical_id(row["task_id"]))
        if t is None:
            continue
        ext = t.setdefault("external", {})
        ext["tinyfish_result"] = _label(row.get("run_result"))
        ext["tinyfish_failure_note"] = _norm(row.get(" Failure Reasoning ") or row.get("Failure Reasoning"))
        if _norm(row.get("Level")) and _norm(row.get("Level")).lower() != t.get("level"):
            ext["workbook_level_disagreement"] = _norm(row.get("Level")).lower()

    for sheet, who in ANNOTATION_SHEETS.items():
        if sheet not in wb.sheetnames:
            continue
        for row in _sheet_rows(wb, sheet):
            t = tasks.get(canonical_id(row["task_id"]))
            if t is None:
                continue
            lab = _label(row.get("human_label\n0 or 1\n(failure or success)")
                         or next((v for k, v in row.items() if k.startswith("human_label")), None))
            if lab is None:
                continue
            labels = t.setdefault("external", {}).setdefault("human_labels", {})
            labels[who] = {"label": lab, "error_category": _norm(row.get("error_category"))}

    for t in tasks.values():
        t.setdefault("category", "uncategorised")
        t.setdefault("antibot_risk", "unknown")
        t.setdefault("attention_level", "unknown")

    catalog = {
        "benchmark": "online_mind2web",
        "n": len(tasks),
        "source": {
            "tasks": str(SOURCE_JSONL.relative_to(REPO_ROOT.parent)),
            "annotations": str(Path(xlsx).name),
            "annotations_sha256": hashlib.sha256(Path(xlsx).read_bytes()).hexdigest(),
        },
        "note": ("level/instruction/start_url come from the frozen benchmark; category, antibot_risk and "
                 "attention_level are human annotations from the workbook; everything under 'external' "
                 "describes a THIRD-PARTY system's runs and their human labels, never SuperBrowser's."),
        "counts": {
            "level": dict(Counter(t.get("level") for t in tasks.values())),
            "category": dict(Counter(t.get("category") for t in tasks.values())),
            "antibot_risk": dict(Counter(t.get("antibot_risk") for t in tasks.values())),
        },
        "tasks": dict(sorted(tasks.items())),
    }
    # Row shape must stay compatible with any existing online_mind2web_all.jsonl:
    # keep every field it already carried and only ADD the annotations.
    rows = [
        {"task_id": t["task_id"], "benchmark": "online_mind2web", "level": t.get("level"),
         "website": t.get("website"), "start_url": t.get("start_url"), "instruction": t["instruction"],
         "reference_length": t.get("reference_length"), "graders": ["mind2web_judge"],
         "category": t.get("category"), "antibot_risk": t.get("antibot_risk"),
         "attention_level": t.get("attention_level")}
        for t in sorted(tasks.values(), key=lambda x: x["task_id"])
    ]
    return catalog, rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the annotated task catalog")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--out", default=str(BENCH_DIR))
    args = ap.parse_args(argv)

    catalog, rows = build(Path(args.xlsx))
    out = Path(args.out)
    (out / "task_catalog.json").write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    jsonl = out / "online_mind2web_all.jsonl"
    jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"wrote {out / 'task_catalog.json'} ({catalog['n']} tasks)")
    print(f"wrote {jsonl} ({len(rows)} rows)")
    print("  levels:    ", catalog["counts"]["level"])
    print("  categories:", catalog["counts"]["category"])
    print("  antibot:   ", catalog["counts"]["antibot_risk"])
    labelled = sum(1 for t in catalog["tasks"].values() if (t.get("external") or {}).get("human_labels"))
    print(f"  external human-labelled tasks: {labelled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
