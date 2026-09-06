"""Task-critical exact values for Critical State Durability (CSD).

A task-critical item is an exact string the acting agent must still have
available at the step where it is needed: a filter value ("English Spot"),
a bound ("$25,000"), a date ("July 3"), a place ("Chicago, IL"), an id. For
benchmark tasks these are given by the instruction itself; observation-derived
items (a listing id first seen on a page and reused later) are detected
automatically by the CSD metric from the trajectory.

``draft_items`` proposes items from the instruction text with simple, explicit
rules; the proposals were then reviewed by hand and frozen in
``eval/benchmarks/critical_state.json`` (``"reviewed": true``). Re-drafting
never overwrites a reviewed entry.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from eval.core.tasks import BENCH_DIR, load_benchmark

_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|K|million|M))?")
_RANGE = re.compile(r"\b\d[\d,]*\s?(?:-|–|to)\s?\d[\d,]*\b")
_NUM_UNIT = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|inch(?:es)?|\"|in\b|miles?|mi\b|km|nights?|days?|hours?|hrs?|"
                       r"minutes?|min\b|years?|yr|lbs?|kg|oz|mph|hz|Hz|GB|TB|MB|stars?|beds?|baths?|"
                       r"bedrooms?|bathrooms?|people|guests?|adults?|children|passengers?|seats?|"
                       r"people|pm|am|PM|AM|k\b)\b")
_DATE = re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
                   r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?"
                   r"(?:,?\s+\d{4})?\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(?:19|20)\d{2}\b")
_QUOTED = re.compile(r"[\"“']([^\"”']{2,60})[\"”']")
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm|AM|PM)\b|\b\d{1,2}:\d{2}\b")
_PLACE = re.compile(r"\b([A-Z][a-zA-Z.]+(?:\s[A-Z][a-zA-Z.]+)*),\s([A-Z]{2})\b")
_CAP_SEQ = re.compile(r"\b((?:[A-Z][a-zA-Z0-9&'\-]+)(?:\s+(?:of|and|de|the|for)?\s*[A-Z][a-zA-Z0-9&'\-]+){0,3})\b")
_STOP = {"Find", "Show", "Search", "Compare", "Get", "Open", "Browse", "Check", "Look", "Add", "Book", "Select",
         "Go", "Use", "Set", "See", "View", "List", "Sort", "Filter", "Buy", "Locate", "Identify", "Explore",
         "The", "A", "An", "On", "In", "Then", "I", "My", "Me", "Please", "Also", "What", "Which", "Where",
         "How", "When", "Can", "Do", "Does", "Is", "Are", "To", "For", "With", "From", "At", "By", "Of", "And",
         "But", "Or", "If", "Under", "Over", "Between", "Near", "Within", "Using", "Give", "Provide", "Display",
         "Retrieve", "Choose", "Determine", "Navigate", "Access", "Read", "Review", "Apply", "Save", "Create"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" .,;:()")


def draft_items(instruction: str, *, website: str | None = None) -> list[str]:
    """Rule-based proposals; order = order of first appearance."""
    found: list[tuple[int, str]] = []

    def add(m: re.Match[str], text: str | None = None) -> None:
        val = _clean(text if text is not None else m.group(0))
        if len(val) >= 2:
            found.append((m.start(), val))

    for rx in (_MONEY, _RANGE, _NUM_UNIT, _DATE, _TIME, _ZIP):
        for m in rx.finditer(instruction):
            add(m)
    for m in _QUOTED.finditer(instruction):
        add(m, m.group(1))
    for m in _PLACE.finditer(instruction):
        add(m)
    site = (website or "").lower()
    for m in _CAP_SEQ.finditer(instruction):
        val = _clean(m.group(1))
        first = val.split(" ")[0]
        if first in _STOP or len(val) < 3:
            continue
        if site and val.lower().split(" ")[0] in site:   # the site's own brand is not task state
            continue
        found.append((m.start(), val))
    seen: set[str] = set()
    out: list[str] = []
    for _pos, val in sorted(found):
        key = val.lower()
        if key in seen or any(key != o.lower() and key in o.lower() for o in out):
            continue
        seen.add(key)
        out.append(val)
    return out


def load() -> dict[str, Any]:
    p = BENCH_DIR / "critical_state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save(data: dict[str, Any]) -> None:
    (BENCH_DIR / "critical_state.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def draft_benchmark(benchmark: str, *, overwrite_unreviewed: bool = True) -> dict[str, Any]:
    data = load()
    for t in load_benchmark(benchmark, annotate=False):
        cur = data.get(t.task_id)
        if cur and cur.get("reviewed") and not overwrite_unreviewed:
            continue
        if cur and cur.get("reviewed"):
            continue
        data[t.task_id] = {"benchmark": benchmark, "instruction": t.instruction,
                           "items": draft_items(t.instruction, website=t.website), "reviewed": False}
    save(data)
    return data


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Draft / show critical-state items")
    ap.add_argument("--benchmark", default="online_mind2web_hard")
    ap.add_argument("--draft", action="store_true", help="(re)draft unreviewed entries")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args(argv)
    if args.draft:
        data = draft_benchmark(args.benchmark)
        print(f"{sum(1 for v in data.values() if v.get('benchmark') == args.benchmark)} entries")
    if args.show or args.draft:
        for tid, v in load().items():
            if v.get("benchmark") != args.benchmark:
                continue
            flag = "R" if v.get("reviewed") else "d"
            print(f"[{flag}] {tid[:10]} {v['instruction'][:88]}\n     -> {v['items']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
