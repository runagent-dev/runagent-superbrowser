"""Token counts and the narrative read. Both are fixed by the tagged plan."""
from __future__ import annotations


def token_count(text: str) -> int:
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    return len(enc.encode(text or ""))


def length_band(target: int) -> tuple[int, int]:
    """floor(0.9 n) and ceil(1.1 n), in integer arithmetic.

    Binary floats make ``ceil(1.1 * 100)`` equal 111, which is not 10%.
    """
    return (target * 9) // 10, (target * 11 + 9) // 10


def in_band(n: int, target: int) -> bool:
    lo, hi = length_band(target)
    return lo <= n <= hi


def narrative_read(assert_pct: float, disclose_pct: float, verbose_pct: float, margin: float = 10.0) -> str:
    """First matching rule, in the order written in the plan."""
    a, d, v = assert_pct, disclose_pct, verbose_pct
    if abs(v - a) <= margin and abs(v - d) > margin and d < a:
        return "disclosure"
    lo, hi = min(a, d), max(a, d)
    if lo < v < hi:
        return "both"
    if abs(v - d) <= margin and abs(v - a) > margin:
        return "length"
    return "indeterminate"


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values, capped at 1."""
    ordered = sorted(pvalues.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, p * (m - i)))
        adjusted[name] = running
    return adjusted
