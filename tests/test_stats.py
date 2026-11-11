"""Tests for clipper.stats. Reference values for the power calculation are the
standard published ones (Cohen / G*Power: n per arm for 80% power, two-sided
alpha .05, two-sample t-test — d=0.5 -> 64, d=0.8 -> 26, d=1.0 -> 17), so the
sample sizes in a pre-registration can be traced to something external."""

from __future__ import annotations

import pytest

pytest.importorskip("scipy")

import numpy as np

from clipper import stats


@pytest.mark.parametrize("d, expected", [(0.5, 64), (0.8, 26), (1.0, 17), (1.25, 12)])
def test_n_per_arm_matches_published_reference_values(d, expected):
    # effect = d * sd; sd fixed arbitrarily, the answer depends only on d
    assert stats.n_per_arm(effect=d * 12, sd=12) == expected


def test_n_per_arm_is_the_smallest_n_that_reaches_the_target_power():
    n = stats.n_per_arm(effect=10, sd=12)
    assert stats.t_power(n, 10, 12) >= 0.8
    assert stats.t_power(n - 1, 10, 12) < 0.8


def test_power_rises_with_n_and_is_alpha_at_zero_effect_limit():
    assert stats.t_power(10, 8, 12) < stats.t_power(40, 8, 12) < stats.t_power(100, 8, 12)
    # a vanishing effect -> power approaches the false-positive rate
    assert stats.t_power(40, 1e-9, 12) == pytest.approx(0.05, abs=1e-3)


def test_n_per_arm_rejects_nonsense():
    with pytest.raises(ValueError):
        stats.n_per_arm(effect=0, sd=12)
    with pytest.raises(ValueError):
        stats.n_per_arm(effect=5, sd=-1)


def test_mann_whitney_sample_size_is_larger_by_the_are_factor():
    n = stats.n_per_arm(10, 12)
    assert stats.n_per_arm_mann_whitney(10, 12) == int(np.ceil(n / stats.MWU_ARE))


def test_weeks_to_complete():
    assert stats.weeks_to_complete(48, clips_per_week=3) == 16
    # only 80% of published clips end up measured -> longer
    assert stats.weeks_to_complete(48, 3, measured_fraction=0.8) == pytest.approx(20)
    with pytest.raises(ValueError):
        stats.weeks_to_complete(48, 0)


def test_welch_matches_scipy_and_sign_is_treatment_minus_control():
    rng = np.random.default_rng(1)
    t, c = rng.normal(60, 10, 30), rng.normal(50, 14, 25)
    res = stats.welch(t, c)
    ref = stats.sst.ttest_ind(t, c, equal_var=False)
    assert res["p"] == pytest.approx(ref.pvalue)
    assert res["t"] == pytest.approx(ref.statistic)
    assert res["diff"] == pytest.approx(t.mean() - c.mean())
    assert res["diff"] > 0


def test_mann_whitney_probability_of_superiority():
    # every treatment value beats every control value
    res = stats.mann_whitney([5, 6, 7], [1, 2, 3])
    assert res["prob_superiority"] == 1.0
    # identical samples -> 0.5
    assert stats.mann_whitney([1, 2, 3, 4], [1, 2, 3, 4])["prob_superiority"] == 0.5


def test_bootstrap_ci_is_reproducible_for_a_fixed_seed_and_brackets_the_estimate():
    rng = np.random.default_rng(2)
    t, c = rng.normal(60, 10, 30), rng.normal(50, 10, 30)
    a = stats.bootstrap_diff_ci(t, c, n_boot=2000, seed=7)
    b = stats.bootstrap_diff_ci(t, c, n_boot=2000, seed=7)
    assert a == b
    assert a["lo"] < a["diff"] < a["hi"]
    assert a["diff"] == pytest.approx(t.mean() - c.mean())
    # a different seed moves the interval slightly but not wildly
    c2 = stats.bootstrap_diff_ci(t, c, n_boot=2000, seed=8)
    assert abs(c2["lo"] - a["lo"]) < 1.5


def test_bootstrap_ci_excludes_zero_for_a_clear_effect_and_includes_it_for_none():
    rng = np.random.default_rng(3)
    big = stats.bootstrap_diff_ci(rng.normal(70, 5, 40), rng.normal(50, 5, 40), n_boot=2000)
    assert big["lo"] > 0
    null = stats.bootstrap_diff_ci(rng.normal(50, 10, 40), rng.normal(50, 10, 40), n_boot=2000)
    assert null["lo"] < 0 < null["hi"]


def test_weighted_bootstrap_uses_the_weights():
    t, c = [60.0, 90.0], [50.0, 50.0]
    # all the weight on the 90 -> weighted mean(t) = ~90, not 75
    heavy = stats.bootstrap_diff_ci(
        t, c, n_boot=500, weights_treatment=[1e-9, 1.0], weights_control=[1, 1]
    )
    assert heavy["diff"] == pytest.approx(40.0, abs=1e-3)
    assert stats.bootstrap_diff_ci(t, c, n_boot=500)["diff"] == pytest.approx(25.0)


def test_hedges_g_is_zero_for_identical_samples_and_signed():
    assert stats.hedges_g([1, 2, 3, 4], [1, 2, 3, 4]) == 0.0
    assert stats.hedges_g([5, 6, 7, 8], [1, 2, 3, 4]) > 0
    assert stats.hedges_g([1, 2, 3, 4], [5, 6, 7, 8]) < 0


def test_srm_flags_a_rigged_split_and_passes_a_fair_one():
    assert stats.srm_test([30, 30], [1, 1])["p"] == pytest.approx(1.0)
    assert stats.srm_test([48, 24], [1, 1])["p"] < 0.01
    assert stats.srm_test([48, 24], [1, 1])["test"] == "exact binomial"


def test_srm_respects_unequal_planned_allocation():
    # planned 2:1 and observed 2:1 is perfectly fine
    assert stats.srm_test([40, 20], [2, 1])["p"] > 0.5


def test_srm_with_three_arms_uses_chi_square():
    res = stats.srm_test([20, 20, 20], [1, 1, 1])
    assert res["test"] == "chi-square" and res["p"] == pytest.approx(1.0)


def test_srm_with_no_data_is_not_an_alarm():
    assert stats.srm_test([0, 0], [1, 1])["p"] == 1.0


def test_attrition_test_detects_one_arm_losing_far_more_clips():
    even = stats.attrition_test(kept=[18, 17], total=[20, 20])
    assert even["p"] > 0.5
    lopsided = stats.attrition_test(kept=[19, 8], total=[20, 20])
    assert lopsided["p"] < 0.01
    assert lopsided["rate"] == [0.95, 0.4]


def test_blinded_pooled_sd_ignores_arm_and_needs_two_values():
    assert stats.blinded_pooled_sd([10, 20, 30]) == pytest.approx(10.0)
    with pytest.raises(ValueError):
        stats.blinded_pooled_sd([5])
