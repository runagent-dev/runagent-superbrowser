"""Benchmark task loading.

Tasks live as frozen JSONL under ``eval/benchmarks/`` (see ``manifest.json``
for provenance). A task row carries the benchmark id, the verbatim
instruction, the start URL, the difficulty level and, optionally, per-task
annotations that other files attach by ``task_id``:

* ``critical_state.json`` — exact strings that must survive until reuse (CSD)
* ``checks.json``         — deterministic success checks (URL / answer regexes)
* ``subsets.json``        — named, pre-registered task subsets (e.g. the
  stratified ablation subset), so every experiment names its subset instead
  of hand-picking tasks
* ``exclusions.json``     — pre-registered exclusion rules + hand exclusions
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from eval._bootstrap import REPO_ROOT

BENCH_DIR = REPO_ROOT / "eval" / "benchmarks"


@dataclass
class Task:
    task_id: str
    instruction: str
    start_url: str | None = None
    benchmark: str = "custom"
    level: str | None = None
    website: str | None = None
    reference: str | None = None            # optional rubric for the answer judge
    reference_length: int | None = None
    critical_state: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        host = urlparse(self.start_url or "").netloc.lower()
        return host[4:] if host.startswith("www.") else host

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "benchmark": self.benchmark,
            "level": self.level,
            "website": self.website,
            "start_url": self.start_url,
            "instruction": self.instruction,
            "reference": self.reference,
            "reference_length": self.reference_length,
            "critical_state": list(self.critical_state),
            "checks": list(self.checks),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Task":
        known = {"task_id", "instruction", "start_url", "benchmark", "level", "website",
                 "reference", "reference_length", "critical_state", "checks", "id", "url"}
        return cls(
            task_id=str(row.get("task_id") or row.get("id")),
            instruction=str(row["instruction"]),
            start_url=row.get("start_url") or row.get("url"),
            benchmark=row.get("benchmark", "custom"),
            level=row.get("level"),
            website=row.get("website"),
            reference=row.get("reference"),
            reference_length=row.get("reference_length"),
            critical_state=list(row.get("critical_state") or []),
            checks=list(row.get("checks") or []),
            extra={k: v for k, v in row.items() if k not in known},
        )


# --------------------------------------------------------------------------- io
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def available_benchmarks() -> list[str]:
    return sorted(p.stem for p in BENCH_DIR.glob("*.jsonl"))


def load_benchmark(name: str, *, annotate: bool = True) -> list[Task]:
    """Load ``eval/benchmarks/<name>.jsonl`` (+ per-task annotations)."""
    path = BENCH_DIR / f"{name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"unknown benchmark {name!r}; have {available_benchmarks()}")
    tasks = [Task.from_row(r) for r in _read_jsonl(path)]
    if annotate:
        crit = _read_json(BENCH_DIR / "critical_state.json", {})
        checks = _read_json(BENCH_DIR / "checks.json", {})
        for t in tasks:
            entry = crit.get(t.task_id)
            if isinstance(entry, dict):
                t.critical_state = list(entry.get("items") or [])
            elif isinstance(entry, list):
                t.critical_state = list(entry)
            c = checks.get(t.task_id)
            if isinstance(c, dict):
                t.checks = list(c.get("checks") or [])
            elif isinstance(c, list):
                t.checks = list(c)
    return tasks


def load_subsets() -> dict[str, dict[str, Any]]:
    return _read_json(BENCH_DIR / "subsets.json", {})


def resolve_tasks(spec: str, *, benchmark: str) -> list[Task]:
    """Turn a CLI task selector into Task objects.

    ``spec`` is ``all``, a named subset from ``subsets.json``, or a
    comma-separated list of task ids. Order follows the benchmark file (stable
    across runs) so paired arms see the same sequence.
    """
    tasks = load_benchmark(benchmark)
    by_id = {t.task_id: t for t in tasks}
    if spec in ("", "all"):
        return tasks
    subsets = load_subsets()
    if spec in subsets:
        ids = subsets[spec].get("task_ids", [])
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise KeyError(f"subset {spec!r} references unknown task ids: {missing[:5]}")
        keep = set(ids)
        return [t for t in tasks if t.task_id in keep]
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise KeyError(f"unknown task id(s) for benchmark {benchmark!r}: {missing[:5]}")
    keep = set(wanted)
    return [t for t in tasks if t.task_id in keep]


# ------------------------------------------------------------------ exclusions
def load_exclusions() -> dict[str, Any]:
    return _read_json(BENCH_DIR / "exclusions.json", {"rules": [], "task_exclusions": []})


def excluded_task_ids() -> dict[str, str]:
    """task_id -> reason for the hand exclusions pre-registered in exclusions.json."""
    out: dict[str, str] = {}
    for e in load_exclusions().get("task_exclusions", []) or []:
        if isinstance(e, dict) and e.get("task_id"):
            out[str(e["task_id"])] = str(e.get("reason", "pre-registered exclusion"))
    return out


# -------------------------------------------------------------- stratification
SITE_TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Coarse, hand-assigned site families for stratifying the frozen hard
    # split (74 tasks / 59 domains). Rules are substring matches on the start
    # domain; first match wins; anything unmatched is "info_media".
    ("shopping", ("bestbuy", "target.com", "kohls", "uniqlo", "gamestop", "macys", "ikea", "samsung",
                  "cvs.com", "store.steampowered", "vivino", "macyswineshop", "wineaccess", "kfc.com",
                  "porsche", "nvidia")),
    ("travel_booking", ("united.com", "airbnb", "booking.com", "expedia", "ryanair", "flightaware",
                        "stubhub", "spothero", "student.com", "trip.com")),
    ("vehicles", ("cars.com", "cargurus", "carmax", "kbb.com")),
    ("housing_jobs", ("apartments.com", "redfin", "hiring.amazon", "ohiomeansjobs", "ycombinator")),
    ("government_health", (".gov", "gov.uk", "americashealthrankings", "bbb.org", "webmd", "healthgrades",
                           "healthline", "babycenter", "chase.com", "akc.org", "petfinder")),
    ("info_media", ()),
]


def site_type(task: Task) -> str:
    dom = task.domain
    for label, needles in SITE_TYPE_RULES:
        if any(n in dom for n in needles):
            return label
    return "info_media"


def stratified_subset(tasks: Iterable[Task], n: int, *, seed: int = 20260906) -> list[Task]:
    """Deterministic stratified sample (by site type) used to pre-register the
    ablation subset. Proportional allocation with largest-remainder rounding;
    within a stratum the choice is a seeded shuffle."""
    tasks = list(tasks)
    strata: dict[str, list[Task]] = defaultdict(list)
    for t in tasks:
        strata[site_type(t)].append(t)
    total = len(tasks)
    if n >= total:
        return tasks
    rng = random.Random(seed)
    quotas: dict[str, float] = {k: n * len(v) / total for k, v in strata.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    short = n - sum(alloc.values())
    for k in sorted(quotas, key=lambda k: -(quotas[k] - alloc[k]))[:short]:
        alloc[k] += 1
    chosen: list[Task] = []
    for k in sorted(strata):
        pool = list(strata[k])
        rng.shuffle(pool)
        chosen.extend(pool[: alloc[k]])
    order = {t.task_id: i for i, t in enumerate(tasks)}
    chosen.sort(key=lambda t: order[t.task_id])
    return chosen


# ------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    """Utilities: list benchmarks, print tasks, pre-register a stratified subset.

        python -m eval.core.tasks --list
        python -m eval.core.tasks --show online_mind2web_hard
        python -m eval.core.tasks --make-subset ablation24 --n 24 [--seed 20260906]
    """
    import argparse

    ap = argparse.ArgumentParser(description="Benchmark task utilities")
    ap.add_argument("--benchmark", default="online_mind2web_hard")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", metavar="NAME")
    ap.add_argument("--make-subset", metavar="SUBSET_NAME")
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--seed", type=int, default=20260906)
    ap.add_argument("--ids", default=None, help="explicit comma-separated ids for --make-subset")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    if args.list:
        for b in available_benchmarks():
            print(f"{b:28s} {len(load_benchmark(b, annotate=False)):4d} tasks")
        print("subsets:", ", ".join(f"{k} ({len(v.get('task_ids', []))})" for k, v in load_subsets().items()) or "-")
        return 0
    if args.show:
        for t in load_benchmark(args.show):
            print(f"{t.task_id}  [{t.level or '-':6s}] {site_type(t):17s} {t.domain:28s} {t.instruction[:70]}")
        return 0
    if args.make_subset:
        tasks = load_benchmark(args.benchmark)
        if args.ids:
            want = [s.strip() for s in args.ids.split(",") if s.strip()]
            chosen = [t for t in tasks if t.task_id in set(want)]
        else:
            chosen = stratified_subset(tasks, args.n, seed=args.seed)
        subsets = load_subsets()
        subsets[args.make_subset] = {
            "benchmark": args.benchmark,
            "n": len(chosen),
            "method": ("explicit ids" if args.ids else f"stratified by site_type, proportional allocation, seed={args.seed}"),
            "note": args.note,
            "strata": dict(sorted(__import__("collections").Counter(site_type(t) for t in chosen).items())),
            "task_ids": [t.task_id for t in chosen],
        }
        (BENCH_DIR / "subsets.json").write_text(json.dumps(subsets, indent=2) + "\n")
        print(f"wrote subset {args.make_subset!r}: {len(chosen)} tasks -> {BENCH_DIR / 'subsets.json'}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
