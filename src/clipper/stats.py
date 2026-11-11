"""Statistics for the clip A/B experiments (see experiments/*/PREREGISTRATION.md).

Pure functions over arrays — no file I/O, no pipeline imports — so every
number in a pre-registration or decision memo comes from code that has its own
tests and a Monte Carlo validation (experiments/hook_style/validate_analysis.py).
Needs the optional `experiment` dependency group (numpy, scipy).

Sign convention everywhere: effects are TREATMENT minus CONTROL.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy import stats as sst

# Mann-Whitney's asymptotic relative efficiency vs the t-test under normality.
# A rank test needs ~1/0.955 as many observations for the same power.
MWU_ARE = 0.955


# --- power / sample size ----------------------------------------------------


def t_power(n_per_arm: int, effect: float, sd: float, alpha: float = 0.05) -> float:
    """Exact power of a two-sided, equal-n, two-sample t-test (noncentral t)."""
    if n_per_arm < 2:
        return 0.0
    d = effect / sd
    df = 2 * n_per_arm - 2
    ncp = d * math.sqrt(n_per_arm / 2)
    tc = sst.t.ppf(1 - alpha / 2, df)
    return float(1 - sst.nct.cdf(tc, df, ncp) + sst.nct.cdf(-tc, df, ncp))


def n_per_arm(effect: float, sd: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Smallest per-arm n whose exact t-test power is >= ``power``."""
    if effect <= 0 or sd <= 0:
        raise ValueError("effect and sd must be positive")
    d = effect / sd
    z = sst.norm.ppf(1 - alpha / 2) + sst.norm.ppf(power)
    n = max(2, math.ceil(2 * (z / d) ** 2))
    while t_power(n, effect, sd, alpha) < power:
        n += 1
    while n > 2 and t_power(n - 1, effect, sd, alpha) >= power:
        n -= 1
    return n


def n_per_arm_mann_whitney(
    effect: float, sd: float, alpha: float = 0.05, power: float = 0.8
) -> int:
    """Per-arm n if the rank test is the one that has to carry the power."""
    return math.ceil(n_per_arm(effect, sd, alpha, power) / MWU_ARE)


def weeks_to_complete(n_total: int, clips_per_week: float, measured_fraction: float = 1.0) -> float:
    """Calendar weeks to reach ``n_total`` MEASURED clips at a posting rate.

    ``measured_fraction`` is the share of published clips that end up with a
    usable outcome row — the part of the funnel after publishing.
    """
    if clips_per_week <= 0 or not 0 < measured_fraction <= 1:
        raise ValueError("clips_per_week > 0 and 0 < measured_fraction <= 1 required")
    return n_total / (clips_per_week * measured_fraction)


# --- primary inference -------------------------------------------------------


def welch(treatment: Sequence[float], control: Sequence[float]) -> dict:
    """Welch's two-sided t-test on (treatment - control)."""
    t, c = np.asarray(treatment, float), np.asarray(control, float)
    res = sst.ttest_ind(t, c, equal_var=False)
    vt, vc = t.var(ddof=1) / len(t), c.var(ddof=1) / len(c)
    df = (vt + vc) ** 2 / (vt**2 / (len(t) - 1) + vc**2 / (len(c) - 1))
    return {
        "diff": float(t.mean() - c.mean()),
        "t": float(res.statistic),
        "df": float(df),
        "p": float(res.pvalue),
    }


def mann_whitney(treatment: Sequence[float], control: Sequence[float]) -> dict:
    """Two-sided Mann-Whitney U, plus P(treatment > control) as the effect size
    (the probability of superiority, ties counted half)."""
    t, c = np.asarray(treatment, float), np.asarray(control, float)
    res = sst.mannwhitneyu(t, c, alternative="two-sided")
    return {
        "u": float(res.statistic),
        "p": float(res.pvalue),
        "prob_superiority": float(res.statistic / (len(t) * len(c))),
    }


def bootstrap_diff_ci(
    treatment: Sequence[float],
    control: Sequence[float],
    *,
    n_boot: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
    weights_treatment: Sequence[float] | None = None,
    weights_control: Sequence[float] | None = None,
) -> dict:
    """Percentile-bootstrap CI for the (weighted) difference in means.

    Each arm is resampled independently, with replacement, at the clip level —
    the unit that was randomized. Optional weights are carried with each
    resampled clip (e.g. views, for the views-weighted sensitivity analysis).
    Fixed ``seed`` so the reported interval is reproducible from the data.
    """
    t, c = np.asarray(treatment, float), np.asarray(control, float)
    wt = np.ones_like(t) if weights_treatment is None else np.asarray(weights_treatment, float)
    wc = np.ones_like(c) if weights_control is None else np.asarray(weights_control, float)
    rng = np.random.default_rng(seed)

    def wmean(x: np.ndarray, w: np.ndarray) -> float:
        return float(np.average(x, weights=w))

    it = rng.integers(0, len(t), size=(n_boot, len(t)))
    ic = rng.integers(0, len(c), size=(n_boot, len(c)))
    bt = (t[it] * wt[it]).sum(axis=1) / wt[it].sum(axis=1)
    bc = (c[ic] * wc[ic]).sum(axis=1) / wc[ic].sum(axis=1)
    diffs = bt - bc
    lo, hi = np.quantile(diffs, [alpha / 2, 1 - alpha / 2])
    return {
        "diff": wmean(t, wt) - wmean(c, wc),
        "lo": float(lo),
        "hi": float(hi),
        "n_boot": n_boot,
        "alpha": alpha,
    }


def hedges_g(treatment: Sequence[float], control: Sequence[float]) -> float:
    """Standardized mean difference with the small-sample correction."""
    t, c = np.asarray(treatment, float), np.asarray(control, float)
    nt, nc = len(t), len(c)
    sp = math.sqrt(((nt - 1) * t.var(ddof=1) + (nc - 1) * c.var(ddof=1)) / (nt + nc - 2))
    if sp == 0:
        return 0.0
    return float((t.mean() - c.mean()) / sp * (1 - 3 / (4 * (nt + nc) - 9)))


# --- design diagnostics ------------------------------------------------------


def srm_test(counts: Sequence[int], expected_props: Sequence[float]) -> dict:
    """Sample-ratio-mismatch test: did the arms get the share that was planned?

    Two arms use the exact binomial test (valid at the small n this project
    will actually have); more arms fall back to a chi-square goodness-of-fit.
    """
    counts = [int(x) for x in counts]
    props = np.asarray(expected_props, float)
    props = props / props.sum()
    total = sum(counts)
    if total == 0:
        return {"p": 1.0, "counts": counts, "expected": [0.0] * len(counts), "test": "none"}
    if len(counts) == 2:
        p = float(sst.binomtest(counts[0], total, props[0]).pvalue)
        test = "exact binomial"
    else:
        p = float(sst.chisquare(counts, f_exp=props * total).pvalue)
        test = "chi-square"
    return {"p": p, "counts": counts, "expected": (props * total).tolist(), "test": test}


def attrition_test(kept: Sequence[int], total: Sequence[int]) -> dict:
    """Did one arm lose more clips between two funnel stages than the other?

    Fisher's exact test on the 2x2 (kept vs dropped, by arm).
    """
    (k0, k1), (t0, t1) = kept, total
    table = [[k0, t0 - k0], [k1, t1 - k1]]
    _, p = sst.fisher_exact(table)
    rates = [k0 / t0 if t0 else None, k1 / t1 if t1 else None]
    return {"p": float(p), "table": table, "rate": rates}


def blinded_pooled_sd(values: Sequence[float]) -> float:
    """SD of the outcome pooled across arms, ignoring which arm each clip is in.

    Needs no look at the treatment effect. But it is biased UPWARD whenever a
    real effect exists, because the gap between the arms adds to the spread —
    see ``blinded_adjusted_sd`` for the version the re-estimate actually uses.
    """
    v = np.asarray(values, float)
    if len(v) < 2:
        raise ValueError("need at least 2 values")
    return float(v.std(ddof=1))


def blinded_adjusted_sd(values: Sequence[float], assumed_effect: float) -> float:
    """Within-arm SD estimated blind to arm, adjusted for the assumed effect.

    With equal arms, the pooled variance is the within-arm variance plus
    (effect/2)^2 * N/(N-1). Subtracting that for the effect the experiment was
    powered to detect removes the bias the unadjusted estimate has exactly when
    the design assumptions hold (the adjusted one-sample variance estimator of
    Kieser & Friede). Without it, a true effect equal to the MDE would make the
    re-estimate ask for ~19% more clips even when nothing had gone wrong.
    """
    v = np.asarray(values, float)
    n = len(v)
    if n < 2:
        raise ValueError("need at least 2 values")
    adjusted = v.var(ddof=1) - (assumed_effect**2 / 4) * n / (n - 1)
    return float(math.sqrt(max(adjusted, 1e-9)))
