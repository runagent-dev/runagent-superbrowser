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

import hashlib
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


FILTER_FIELDS = {
    "level": lambda t: (t.level or "").lower(),
    "category": lambda t: str(t.extra.get("category") or "").lower(),
    "antibot": lambda t: str(t.extra.get("antibot_risk") or "").lower(),
    "attention": lambda t: str(t.extra.get("attention_level") or "").lower(),
    "website": lambda t: (t.domain or "").lower(),
}


def filter_tasks(tasks: list[Task], spec: str) -> list[Task]:
    """Select tasks by annotation, e.g. ``level=hard,antibot=low,n=10``.

    Every field accepts a ``|``-separated set (``level=easy|medium``). ``n``
    caps the count and ``stratify`` spreads that cap evenly across the values of
    a field (``stratify=level``). Selection is deterministic: tasks are ordered
    by id and shuffled with ``seed`` (default: derived from the filter text), so
    the same selector always yields the same task list on any machine. That
    matters because a task set must be fixed BEFORE any arm runs.
    """
    terms: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"bad filter term {part!r}; expected key=value (e.g. level=hard)")
        k, v = part.split("=", 1)
        terms[k.strip().lower()] = v.strip()

    unknown = set(terms) - set(FILTER_FIELDS) - {"n", "seed", "stratify"}
    if unknown:
        raise KeyError(f"unknown filter field(s) {sorted(unknown)}; have "
                       f"{sorted(FILTER_FIELDS)} plus n, seed, stratify")

    out = list(tasks)
    for key, getter in FILTER_FIELDS.items():
        if key not in terms:
            continue
        wanted = {w.strip().lower() for w in terms[key].split("|") if w.strip()}
        out = [t for t in out if getter(t) in wanted]

    n = int(terms["n"]) if "n" in terms else None
    if n is None:
        return sorted(out, key=lambda t: t.task_id)

    seed = int(terms["seed"]) if "seed" in terms else (
        int(hashlib.sha256(spec.replace(" ", "").encode()).hexdigest()[:8], 16))
    strat = terms.get("stratify")
    if strat:
        if strat not in FILTER_FIELDS:
            raise KeyError(f"cannot stratify by {strat!r}; have {sorted(FILTER_FIELDS)}")
        getter = FILTER_FIELDS[strat]
        groups: dict[str, list[Task]] = defaultdict(list)
        for t in sorted(out, key=lambda t: t.task_id):
            groups[getter(t)].append(t)
        for g in groups.values():
            random.Random(seed).shuffle(g)
        picked: list[Task] = []
        # round-robin so a short group never starves a long one
        for i in range(max((len(g) for g in groups.values()), default=0)):
            for key in sorted(groups):
                if i < len(groups[key]) and len(picked) < n:
                    picked.append(groups[key][i])
            if len(picked) >= n:
                break
        out = picked
    else:
        out = sorted(out, key=lambda t: t.task_id)
        random.Random(seed).shuffle(out)
        out = out[:n]
    return sorted(out, key=lambda t: t.task_id)


def resolve_tasks(spec: str, *, benchmark: str) -> list[Task]:
    """Turn a CLI task selector into Task objects.

    ``spec`` is ``all``, a named subset from ``subsets.json``, an annotation
    filter (any ``key=value`` term, see :func:`filter_tasks`), or a
    comma-separated list of task ids. Order follows the benchmark file (stable
    across runs) so paired arms see the same sequence.
    """
    tasks = load_benchmark(benchmark)
    by_id = {t.task_id: t for t in tasks}
    if spec in ("", "all"):
        return tasks
    if "=" in spec:                      # ids and subset names never contain '='
        picked = filter_tasks(tasks, spec)
        if not picked:
            have = sorted({(t.level or "?") for t in tasks})
            hint = ""
            if "level=" in spec and len(have) == 1:
                hint = (f" — {benchmark!r} contains only {have[0]!r} tasks; pass "
                        f"--benchmark online_mind2web_all to select across levels")
            raise KeyError(f"filter {spec!r} matched no task in benchmark {benchmark!r}{hint}")
        keep = {t.task_id for t in picked}
        return [t for t in tasks if t.task_id in keep]
    subsets = load_subsets()
    if spec in subsets:
        entry = subsets[spec]
        ids = entry.get("task_ids", [])
        # a subset carries the benchmark it was frozen from: honour it, so a
        # 10-task pilot drawn from the full split still resolves when the
        # experiment's default benchmark is the hard-only file
        source = entry.get("benchmark") or benchmark
        if source != benchmark:
            tasks = load_benchmark(source)
            by_id = {t.task_id: t for t in tasks}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise KeyError(f"subset {spec!r} (benchmark {source!r}) references unknown task ids: {missing[:5]}")
        keep = set(ids)
        return [t for t in tasks if t.task_id in keep]
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise KeyError(f"unknown task id(s) for benchmark {benchmark!r}: {missing[:5]}")
    keep = set(wanted)
    return [t for t in tasks if t.task_id in keep]


def _norm_text(s: str) -> str:
    return " ".join(str(s).split()).strip().lower()


def find_by_instruction(instruction: str, url: str | None = None, *, benchmarks: Iterable[str] | None = None) -> Task | None:
    """The frozen benchmark task whose instruction matches ``instruction``
    (whitespace/case-normalised; ``url``'s domain must match when given).

    Lets an SDK run be labelled with the benchmark task id when the caller typed
    a benchmark task verbatim (``examples/03_browser_mode.py``); anything else
    stays a custom task with a derived id, never a silently re-labelled one.
    """
    want = _norm_text(instruction)
    if not want:
        return None
    dom = None
    if url:
        host = urlparse(url).netloc.lower()
        dom = host[4:] if host.startswith("www.") else host
    for name in (list(benchmarks) if benchmarks is not None else available_benchmarks()):
        try:
            tasks = load_benchmark(name)
        except Exception:
            continue
        for t in tasks:
            if _norm_text(t.instruction) == want and (dom is None or not t.domain or t.domain == dom):
                return t
    return None


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
        python -m eval.core.tasks --benchmark online_mind2web_all --select "level=hard,n=10"
        python -m eval.core.tasks --benchmark online_mind2web_all \
            --filter "level=hard|medium,antibot=low,n=10" --make-subset pilot10 --note "first sweep"
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
    ap.add_argument("--select", metavar="FILTER", default=None,
                    help='preview an annotation filter, e.g. "level=hard,antibot=low,n=10"')
    ap.add_argument("--filter", metavar="FILTER", default=None,
                    help="same syntax; use as the source of --make-subset instead of site-type stratification")
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
    if args.select:
        chosen = resolve_tasks(args.select, benchmark=args.benchmark)
        from collections import Counter
        print(f"{len(chosen)} task(s) from {args.benchmark} matching {args.select!r}\n")
        for t in chosen:
            print(f"  {t.task_id}  [{(t.level or '-'):6s}] {str(t.extra.get('category') or '-'):22s} "
                  f"antibot={str(t.extra.get('antibot_risk') or '-'):7s} {t.domain:26s} {t.instruction[:58]}")
        for field in ("level", "category", "antibot_risk"):
            vals = Counter(str(t.extra.get(field) if field != "level" else t.level) for t in chosen)
            print(f"\n  {field}: {dict(sorted(vals.items()))}", end="")
        print(f"\n\nSelection is deterministic. Freeze it before running arms:\n"
              f"  python -m eval.core.tasks --benchmark {args.benchmark} "
              f'--filter "{args.select}" --make-subset <name>')
        return 0
    if args.make_subset:
        tasks = load_benchmark(args.benchmark)
        if args.ids:
            want = [s.strip() for s in args.ids.split(",") if s.strip()]
            chosen = [t for t in tasks if t.task_id in set(want)]
        elif args.filter:
            chosen = resolve_tasks(args.filter, benchmark=args.benchmark)
        else:
            chosen = stratified_subset(tasks, args.n, seed=args.seed)
        subsets = load_subsets()
        subsets[args.make_subset] = {
            "benchmark": args.benchmark,
            "n": len(chosen),
            "method": ("explicit ids" if args.ids else f"filter {args.filter!r}" if args.filter
                       else f"stratified by site_type, proportional allocation, seed={args.seed}"),
            "note": args.note,
            "strata": dict(sorted(__import__("collections").Counter(site_type(t) for t in chosen).items())),
            "level_counts": dict(sorted(__import__("collections").Counter(str(t.level) for t in chosen).items())),
            "antibot_counts": dict(sorted(__import__("collections").Counter(
                str(t.extra.get("antibot_risk") or "unknown") for t in chosen).items())),
            "task_ids": [t.task_id for t in chosen],
        }
        (BENCH_DIR / "subsets.json").write_text(json.dumps(subsets, indent=2) + "\n")
        print(f"wrote subset {args.make_subset!r}: {len(chosen)} tasks -> {BENCH_DIR / 'subsets.json'}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
