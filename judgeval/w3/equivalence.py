"""Equivalence bounds for paired binary rates.

A pair of success counts identifies the difference of rates and nothing else
about the 2x2 table. This module enumerates every table consistent with those
counts and refuses to collapse them into one result.
"""
from __future__ import annotations

import math
import random
from collections.abc import Sequence

MARGIN = 0.05
N_BOOT = 10_000
SEED = 20260922
# z_{0.975} + z_{0.80} for a two-sided 5% test at 80% power.
_Z_MDE = 1.959963984540054 + 0.8416212335729143


def feasible_tables(k_a: int, k_b: int, n: int) -> list[tuple[int, int, int, int]]:
    """Every (both_success, a_only, b_only, both_fail) with those margins."""
    if not (0 <= k_a <= n and 0 <= k_b <= n):
        raise ValueError("counts must lie in 0..n")
    out = []
    for b_only in range(n + 1):
        a_only = b_only + (k_a - k_b)
        both = k_a - a_only
        neither = n - both - a_only - b_only
        if min(a_only, b_only, both, neither) >= 0:
            out.append((both, a_only, b_only, neither))
    if not out:
        raise ValueError("no table matches these margins")
    return out


def table_from_pairs(a: Sequence[bool], b: Sequence[bool]) -> tuple[int, int, int, int]:
    if len(a) != len(b):
        raise ValueError("paired sequences must have equal length")
    both = a_only = b_only = 0
    for x, y in zip(a, b):
        if x and y:
            both += 1
        elif x and not y:
            a_only += 1
        elif y and not x:
            b_only += 1
    n = len(a)
    return both, a_only, b_only, n - both - a_only - b_only


def _percentile(sorted_vals: list[float], alpha: float) -> tuple[float, float]:
    n = len(sorted_vals)
    lo = sorted_vals[int((alpha / 2) * n)]
    hi = sorted_vals[min(n - 1, int((1 - alpha / 2) * n))]
    return lo, hi


def bootstrap_cis(both: int, a_only: int, b_only: int, neither: int, *,
                  seed: int = SEED, n_boot: int = N_BOOT) -> dict[str, tuple[float, float]]:
    """Percentile bootstrap CIs for the paired difference, from the multiset of pairs."""
    n = both + a_only + b_only + neither
    diffs = [1.0] * a_only + [-1.0] * b_only + [0.0] * (both + neither)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        total = 0.0
        for _i in range(n):
            total += diffs[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    return {"ci95": _percentile(means, 0.05), "ci90": _percentile(means, 0.10)}


def _binom_cdf_term(k: int, n: int) -> float:
    return math.comb(n, k) / (2.0 ** n)


def mcnemar_p(a_only: int, b_only: int) -> float:
    """Two-sided exact McNemar p, conditional on the discordant pairs."""
    disc = a_only + b_only
    if disc == 0:
        return 1.0
    k = min(a_only, b_only)
    pk = _binom_cdf_term(k, disc)
    p = sum(_binom_cdf_term(i, disc) for i in range(disc + 1) if _binom_cdf_term(i, disc) <= pk + 1e-15)
    return min(1.0, p)


def _beta_inv_approx_clopper(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson interval via the beta quantile, with a normal fallback.

    scipy is used when it imports. The fallback is the Wilson interval, which
    is then labeled by the caller only if scipy is missing; this function
    prefers the exact beta quantile.
    """
    if n == 0:
        return (float("nan"), float("nan"))
    try:
        from scipy.stats import beta
        lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
        hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
        return lo, hi
    except Exception:
        # Wilson, used only if scipy is absent. Callers record which one ran.
        z = 1.959963984540054
        p = k / n
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return max(0.0, centre - half), min(1.0, centre + half)


def conditional_exact_ci(a_only: int, b_only: int, n: int) -> tuple[float, float]:
    """CI for the paired difference conditional on the discordant count.

    Among D discordant pairs, the number favoring A is binomial. The difference
    of rates equals (2X - D) / n. The Clopper-Pearson interval on X/D is mapped
    through that function. It does not cover the unconditional difference.
    """
    disc = a_only + b_only
    if disc == 0:
        return 0.0, 0.0
    lo, hi = _beta_inv_approx_clopper(a_only, disc)
    return (2 * lo - 1) * disc / n, (2 * hi - 1) * disc / n


def tost_passes(ci90: tuple[float, float], margin: float = MARGIN) -> bool:
    """α=0.05 TOST: the 90% interval lies strictly inside (−margin, +margin)."""
    return ci90[0] > -margin and ci90[1] < margin


def mde(discordant_rate: float, n: int) -> float:
    """Approximate minimum detectable |difference| at 80% power, two-sided 5%.

    Uses SE ≈ sqrt(ψ / n) near a zero difference, ψ = discordant-pair rate.
    """
    if discordant_rate < 0 or n <= 0:
        return float("nan")
    return _Z_MDE * math.sqrt(discordant_rate / n)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvalues.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, p * (m - i)))
        adjusted[name] = running
    return adjusted


def summarize_margins(k_a: int, k_b: int, n: int, *, margin: float = MARGIN) -> dict:
    """What the two totals identify, and what they leave open."""
    tables = feasible_tables(k_a, k_b, n)
    point = (k_a - k_b) / n
    rows = []
    for both, a_only, b_only, neither in tables:
        cis = bootstrap_cis(both, a_only, b_only, neither)
        rows.append({
            "both_success": both,
            "a_only": a_only,
            "b_only": b_only,
            "both_fail": neither,
            "discordant": a_only + b_only,
            "mcnemar_p": mcnemar_p(a_only, b_only),
            "ci95": cis["ci95"],
            "ci90": cis["ci90"],
            "exact_conditional_ci95": conditional_exact_ci(a_only, b_only, n),
            "tost": tost_passes(cis["ci90"], margin),
        })
    ps = [r["mcnemar_p"] for r in rows]
    discs = [r["discordant"] / n for r in rows]
    return {
        "k_a": k_a,
        "k_b": k_b,
        "n": n,
        "point_diff": point,
        "point_outside_margin": abs(point) > margin,
        "n_feasible_tables": len(rows),
        "mcnemar_p_min": min(ps),
        "mcnemar_p_max": max(ps),
        "all_nonsignificant_unadjusted": all(p >= 0.05 for p in ps),
        "tost_all_tables": all(r["tost"] for r in rows),
        "tost_any_table": any(r["tost"] for r in rows),
        "mde_at_min_discordant": mde(min(discs), n),
        "mde_at_max_discordant": mde(max(discs), n),
        "tables": rows,
    }


def summarize_pairs(a: Sequence[bool], b: Sequence[bool], *, margin: float = MARGIN) -> dict:
    both, a_only, b_only, neither = table_from_pairs(a, b)
    n = len(a)
    base = summarize_margins(sum(a), sum(b), n, margin=margin)
    # The observed table is one element of `tables`; keep it as the estimate.
    observed = next(r for r in base["tables"]
                    if (r["both_success"], r["a_only"], r["b_only"], r["both_fail"])
                    == (both, a_only, b_only, neither))
    base["observed"] = observed
    base["identified"] = True
    return base
