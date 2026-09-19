"""Paired statistics used by the analyzers (pre-registered in PROTOCOL.md).

Everything here is deliberately plain: exact tests, closed-form intervals and
bootstrap resampling, so a reader can recompute a number from the released
records without a statistics package. scipy is used only where it is the
reference implementation (Wilcoxon signed-rank, Spearman), with fallbacks.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Sequence


# ------------------------------------------------------------- proportions
def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k/n (95% by default)."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo, hi = max(0.0, centre - half), min(1.0, centre + half)
    # k=0 and k=n are common on a small task set; the closed form leaves a ~1e-17
    # residue there, which puts the point outside its own interval downstream.
    eps = 1e-12
    return (0.0 if lo < eps else lo, 1.0 if hi > 1 - eps else hi)


def _binom_two_sided_p(k: int, n: int) -> float:
    """Two-sided exact binomial test p-value for k successes in n trials at p=0.5."""
    if n == 0:
        return 1.0
    total = 2.0 ** n
    pk = math.comb(n, k) / total
    p = sum(math.comb(n, i) / total for i in range(n + 1) if math.comb(n, i) / total <= pk + 1e-15)
    return min(1.0, p)


@dataclass
class McNemarResult:
    n_pairs: int
    both_success: int
    a_only: int          # discordant: A succeeded, B failed
    b_only: int          # discordant: B succeeded, A failed
    both_fail: int
    p_value: float       # exact (binomial on discordant pairs), two-sided
    diff: float          # TSR(A) - TSR(B)
    diff_ci: tuple[float, float]

    def to_dict(self) -> dict:
        return {"n_pairs": self.n_pairs, "both_success": self.both_success, "a_only": self.a_only,
                "b_only": self.b_only, "both_fail": self.both_fail, "p_value": self.p_value,
                "diff": self.diff, "diff_ci_low": self.diff_ci[0], "diff_ci_high": self.diff_ci[1]}


def mcnemar_exact(a: Sequence[bool], b: Sequence[bool], *, seed: int = 0, n_boot: int = 5000) -> McNemarResult:
    """Exact McNemar test on paired binary outcomes + bootstrap CI of the
    paired difference in success rate (A - B)."""
    if len(a) != len(b):
        raise ValueError("paired sequences must have equal length")
    n = len(a)
    bs = sum(1 for x, y in zip(a, b) if x and y)
    ao = sum(1 for x, y in zip(a, b) if x and not y)
    bo = sum(1 for x, y in zip(a, b) if y and not x)
    bf = n - bs - ao - bo
    p = _binom_two_sided_p(min(ao, bo), ao + bo)
    diff = (ao - bo) / n if n else float("nan")
    ci = paired_bootstrap_ci([1.0 if x else 0.0 for x in a], [1.0 if y else 0.0 for y in b],
                             seed=seed, n_boot=n_boot).ci if n else (float("nan"), float("nan"))
    return McNemarResult(n, bs, ao, bo, bf, p, diff, ci)


# -------------------------------------------------------------- continuous
@dataclass
class PairedResult:
    n: int
    mean_a: float
    mean_b: float
    mean_diff: float
    ci: tuple[float, float]
    wilcoxon_p: float | None = None
    median_diff: float | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"n": self.n, "mean_a": self.mean_a, "mean_b": self.mean_b, "mean_diff": self.mean_diff,
                "ci_low": self.ci[0], "ci_high": self.ci[1], "wilcoxon_p": self.wilcoxon_p,
                "median_diff": self.median_diff, **self.detail}


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def paired_bootstrap_ci(a: Sequence[float], b: Sequence[float], *, seed: int = 0, n_boot: int = 5000,
                        alpha: float = 0.05) -> PairedResult:
    """Percentile bootstrap CI for mean(A - B) over paired observations."""
    if len(a) != len(b):
        raise ValueError("paired sequences must have equal length")
    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    if n == 0:
        return PairedResult(0, float("nan"), float("nan"), float("nan"), (float("nan"), float("nan")))
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    sd = sorted(diffs)
    median = sd[n // 2] if n % 2 else (sd[n // 2 - 1] + sd[n // 2]) / 2
    return PairedResult(n, _mean(list(a)), _mean(list(b)), _mean(diffs), (lo, hi), median_diff=median)


def wilcoxon_signed_rank(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Two-sided Wilcoxon signed-rank p-value (scipy); None if undefined."""
    diffs = [x - y for x, y in zip(a, b) if x != y]
    if len(diffs) < 3:
        return None
    try:
        from scipy.stats import wilcoxon

        return float(wilcoxon(diffs, alternative="two-sided").pvalue)
    except Exception:
        return None


def paired_compare(a: Sequence[float], b: Sequence[float], *, seed: int = 0) -> PairedResult:
    res = paired_bootstrap_ci(a, b, seed=seed)
    res.wilcoxon_p = wilcoxon_signed_rank(a, b)
    return res


def mean_ci(xs: Sequence[float], *, seed: int = 0, n_boot: int = 5000) -> tuple[float, float, float]:
    """(mean, lo, hi) via percentile bootstrap."""
    xs = list(xs)
    if not xs:
        return (float("nan"), float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (_mean(xs), means[int(0.025 * n_boot)], means[min(n_boot - 1, int(0.975 * n_boot))])


# ------------------------------------------------------- multiplicity
def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (family-wise error control).

    Returned in the input order. Used across the secondary paired comparisons
    of a sweep: the pre-registered confirmatory pair(s) are reported unadjusted
    and everything else is adjusted as one family, so a single significant
    secondary result cannot be read as if it had been the only test run.
    """
    ps = [float(p) for p in pvalues]
    m = len(ps)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: ps[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        val = min(1.0, (m - rank) * ps[i])
        running = max(running, val)
        adj[i] = running
    return adj


# ---------------------------------------------------------------- trends
def spearman_trend(levels: Sequence[float], values: Sequence[float]) -> tuple[float, float | None]:
    """Spearman rho (and p if scipy is available) between an ordered level and a metric."""
    if len(levels) != len(values) or len(levels) < 3:
        return (float("nan"), None)
    try:
        from scipy.stats import spearmanr

        r = spearmanr(levels, values)
        return (float(r.statistic if hasattr(r, "statistic") else r[0]), float(r.pvalue))
    except Exception:
        def ranks(xs):
            order = sorted(range(len(xs)), key=lambda i: xs[i])
            rk = [0.0] * len(xs)
            i = 0
            while i < len(order):
                j = i
                while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                    j += 1
                avg = (i + j) / 2 + 1
                for k in range(i, j + 1):
                    rk[order[k]] = avg
                i = j + 1
            return rk
        ra, rb = ranks(list(levels)), ranks(list(values))
        ma, mb = _mean(ra), _mean(rb)
        num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
        den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
        return (num / den if den else float("nan"), None)


def cochran_armitage(successes: Sequence[int], totals: Sequence[int], scores: Sequence[float] | None = None
                     ) -> tuple[float, float]:
    """Cochran–Armitage trend test for binary outcomes across ordered levels.
    Returns (z, two-sided p) using the normal approximation."""
    k = len(successes)
    if k < 2 or len(totals) != k:
        return (float("nan"), float("nan"))
    t = list(scores) if scores is not None else list(range(k))
    N = sum(totals)
    R = sum(successes)
    if N == 0 or R == 0 or R == N:
        return (0.0, 1.0)
    p_bar = R / N
    t_bar = sum(ti * ni for ti, ni in zip(t, totals)) / N
    num = sum(ti * (ri - ni * p_bar) for ti, ri, ni in zip(t, successes, totals))
    var = p_bar * (1 - p_bar) * sum(ni * (ti - t_bar) ** 2 for ti, ni in zip(t, totals))
    if var <= 0:
        return (0.0, 1.0)
    z = num / math.sqrt(var)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return (z, p)


# ---------------------------------------------------------------- agreement
def cohen_kappa(a: Sequence[object], b: Sequence[object]) -> tuple[float, float]:
    """(raw agreement, Cohen's kappa) for two label sequences."""
    if len(a) != len(b) or not a:
        return (float("nan"), float("nan"))
    n = len(a)
    agree = sum(1 for x, y in zip(a, b) if x == y) / n
    labels = set(a) | set(b)
    pe = sum((sum(1 for x in a if x == l) / n) * (sum(1 for y in b if y == l) / n) for l in labels)
    kappa = (agree - pe) / (1 - pe) if pe < 1 else 1.0
    return (agree, kappa)


def confusion(truth: Sequence[bool], pred: Sequence[bool]) -> dict[str, int]:
    tp = sum(1 for t, p in zip(truth, pred) if t and p)
    tn = sum(1 for t, p in zip(truth, pred) if not t and not p)
    fp = sum(1 for t, p in zip(truth, pred) if not t and p)
    fn = sum(1 for t, p in zip(truth, pred) if t and not p)
    return {"tp": tp, "tn": tn, "fp": fp, "fn": fn}


def fmt_pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100.0 * k / n:.1f}%)" if n else "0/0 (–)"
