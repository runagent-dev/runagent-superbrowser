"""25% stratified sample for the human content check. Does not label it."""
from __future__ import annotations

import math
import random
from collections import defaultdict


def stratified_quarter(items: list[dict], seed: int = 20260922) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in sorted(items, key=lambda r: (r["source_arm"], r["condition"], r["unit_id"])):
        groups[(item["source_arm"], item["condition"])].append(item)
    raw = {k: len(v) * 0.25 for k, v in groups.items()}
    take = {k: math.floor(n) for k, n in raw.items()}
    target = round(0.25 * len(items))
    leftover = target - sum(take.values())
    order = sorted(groups, key=lambda k: (-(raw[k] - take[k]), k[0], k[1]))
    for key in order:
        if leftover <= 0:
            break
        if take[key] < len(groups[key]):
            take[key] += 1
            leftover -= 1
    rng = random.Random(seed)
    chosen: list[dict] = []
    for key, rows in groups.items():
        pool = list(rows)
        rng.shuffle(pool)
        chosen.extend(pool[:take[key]])
    return chosen
