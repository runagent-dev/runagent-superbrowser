import math

from eval.core.stats import (cochran_armitage, cohen_kappa, confusion, mcnemar_exact, paired_bootstrap_ci,
                             paired_compare, spearman_trend, wilson_ci)


def test_wilson_known_values():
    lo, hi = wilson_ci(59, 66)   # 89.4%: Wilson 95% = [0.797, 0.948]
    assert abs(lo - 0.7969) < 0.002 and abs(hi - 0.9477) < 0.002
    assert wilson_ci(0, 10)[0] == 0.0 and abs(wilson_ci(10, 10)[1] - 1.0) < 1e-9
    assert all(math.isnan(v) for v in wilson_ci(0, 0))


def test_mcnemar_exact_discordant_pairs():
    a = [True] * 12 + [False] * 3 + [True] * 5 + [False] * 4     # 24 pairs
    b = [True] * 12 + [True] * 3 + [False] * 5 + [False] * 4
    r = mcnemar_exact(a, b)
    assert (r.both_success, r.a_only, r.b_only, r.both_fail) == (12, 5, 3, 4)
    # exact binomial two-sided p for 3 of 8 discordant at p=.5 = 0.7266
    assert abs(r.p_value - 0.7265625) < 1e-6
    assert abs(r.diff - (5 - 3) / 24) < 1e-9
    assert r.diff_ci[0] <= r.diff <= r.diff_ci[1]
    r2 = mcnemar_exact([True] * 10, [False] * 10)
    assert r2.a_only == 10 and abs(r2.p_value - 2 / 1024) < 1e-9


def test_paired_bootstrap_and_wilcoxon():
    a = [10, 12, 9, 14, 11, 13, 10, 12, 15, 11]
    b = [8, 9, 9, 10, 10, 11, 9, 10, 12, 9]
    r = paired_compare(a, b, seed=1)
    assert r.n == 10 and abs(r.mean_diff - 2.0) < 1e-9
    assert r.ci[0] > 0 and r.ci[0] <= r.mean_diff <= r.ci[1]
    assert r.wilcoxon_p is not None and r.wilcoxon_p < 0.05
    assert r.median_diff == 2.0
    z = paired_bootstrap_ci([], [])
    assert z.n == 0


def test_trend_tests():
    rho, p = spearman_trend([0, 1, 2, 3, 4, 5], [0.9, 0.85, 0.7, 0.6, 0.4, 0.3])
    assert rho < -0.99
    z, p = cochran_armitage([18, 14, 9], [20, 20, 20])
    assert z < -2.5 and p < 0.02
    z0, p0 = cochran_armitage([10, 10, 10], [20, 20, 20])
    assert abs(z0) < 1e-9 and p0 == 1.0


def test_kappa_and_confusion():
    h1 = [True, True, False, False, True, False, True, True]
    h2 = [True, True, False, True, True, False, False, True]
    agree, kappa = cohen_kappa(h1, h2)
    assert abs(agree - 0.75) < 1e-9
    assert abs(kappa - 0.4667) < 0.01
    assert cohen_kappa([True] * 5, [True] * 5)[1] == 1.0
    assert confusion(h1, h2) == {"tp": 4, "tn": 2, "fp": 1, "fn": 1}
