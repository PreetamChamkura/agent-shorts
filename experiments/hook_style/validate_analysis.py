"""Monte Carlo validation of the hook_style analysis code, on SIMULATED data.

Nothing here is a result about hook styles. It measures the *procedure*: if the
pre-registered analysis is run on data whose truth is known, how often does it
say the wrong thing, and how often does it find a real effect? Run it before any
real data exists:

    uv run python experiments/hook_style/validate_analysis.py [--sims 4000]

The real code under test: clipper.stats (Welch, Mann-Whitney, bootstrap CI, SRM)
and clipper.experiment_analysis.decide() — the actual decision function, fed
simulated inputs. Writes validation_results.json and validation_report.md next
to this file. Fully seeded, so a rerun reproduces the same numbers.
"""

from __future__ import annotations

import argparse
import json
import platform
import time
from datetime import date
from pathlib import Path

import numpy as np
import scipy

from clipper import experiment as ex
from clipper import experiment_analysis as ea
from clipper import stats

HERE = Path(__file__).parent
SEED = 20261111
B_BOOT = 1000  # bootstrap resamples per simulated experiment (10,000 in the real analysis)


def _normal(rng, mean, sd, n):
    return np.clip(rng.normal(mean, sd, n), 0, 100)


def _skewed(rng, mean, sd, n):
    """Bounded and right-skewed: a Beta with the requested mean and SD, in percent."""
    m, v = mean / 100, (sd / 100) ** 2
    k = m * (1 - m) / v - 1
    return rng.beta(m * k, (1 - m) * k, n) * 100


SCENARIOS = {
    "normal": (_normal, 50.0),
    "skewed-bounded": (_skewed, 40.0),  # baseline 40%, Beta(~6.3, ~9.4)
}


def _res_for_decide(t, c, planned_n):
    """A minimal `res` carrying exactly what decide() reads, so the simulation
    exercises the real decision function rather than a re-implementation."""
    neutral = {"p": 1.0, "counts": [len(t), len(c)], "rate": [1.0, 1.0]}
    return {
        "treatment": "question",
        "control": "statement",
        "insufficient": False,
        "planned_n_per_arm": planned_n,
        "n": {"question": len(t), "statement": len(c)},
        "diagnostics": {
            "srm_analyzed": neutral,
            "acceptance": None,
            "attrition": None,
            "measured_without_accepted_selection": 0,
            "compliance": {"question": 1.0, "statement": 1.0},
        },
        "primary": {
            "welch": stats.welch(t, c),
            "mann_whitney": stats.mann_whitney(t, c),
            "bootstrap": stats.bootstrap_diff_ci(t, c, n_boot=B_BOOT, seed=1),
        },
    }


def simulate_inference(rng, gen, base, effect, sd, n, sims):
    welch_sig = mwu_sig = ci_sig = ship = keep = 0
    covered, widths = 0, []
    for _ in range(sims):
        c = gen(rng, base, sd, n)
        t = gen(rng, base + effect, sd, n)
        res = _res_for_decide(t, c, planned_n=n)
        p = res["primary"]
        welch_sig += p["welch"]["p"] < 0.05
        mwu_sig += p["mann_whitney"]["p"] < 0.05
        b = p["bootstrap"]
        ci_sig += b["lo"] > 0 or b["hi"] < 0
        widths.append(b["hi"] - b["lo"])
        # truth: for the clipped/beta generators the true mean difference is what
        # the sampler actually produces, so coverage is judged against the
        # empirical population difference estimated from a very large sample
        covered += b["lo"] <= TRUE_DIFF[(gen.__name__, effect)] <= b["hi"]
        d = ea.decide(res)["decision"]
        ship += d == "ship_treatment"
        keep += d == "keep_control"
    return {
        "welch_reject": welch_sig / sims,
        "mwu_reject": mwu_sig / sims,
        "ci_excludes_zero": ci_sig / sims,
        "decide_ship": ship / sims,
        "decide_keep": keep / sims,
        "ci_coverage": covered / sims,
        "mean_ci_width": float(np.mean(widths)),
    }


TRUE_DIFF: dict[tuple[str, float], float] = {}


def _calibrate_truth(effects):
    """The population mean difference each generator actually produces (clipping
    and the Beta shape move it slightly), from one very large draw."""
    rng = np.random.default_rng(SEED + 99)
    for gen, base in SCENARIOS.values():
        for eff in effects:
            big_t = gen(rng, base + eff, 12.0, 2_000_000)
            big_c = gen(rng, base, 12.0, 2_000_000)
            TRUE_DIFF[(gen.__name__, eff)] = float(big_t.mean() - big_c.mean())


def simulate_srm(rng, n_measured, sims):
    """False-alarm rate with a fair 1:1 split, and detection of rigged splits."""
    out = {}
    for label, p_t in (("fair 50/50", 0.5), ("60/40", 0.6), ("70/30", 0.7), ("80/20", 0.8)):
        alarm01 = alarm05 = alarm001 = 0
        for _ in range(sims):
            k = int(rng.binomial(n_measured, p_t))
            pval = stats.srm_test([k, n_measured - k], [1, 1])["p"]
            alarm001 += pval < 0.001
            alarm01 += pval < 0.01
            alarm05 += pval < 0.05
        out[label] = {"p<.001": alarm001 / sims, "p<.01": alarm01 / sims, "p<.05": alarm05 / sims}
    return out


def blocked_vs_simple_imbalance(n_clips, block_size):
    """P(the arm counts differ by >= k) under simple coin-flip assignment vs the
    permuted-block design (whose imbalance is bounded by half a block)."""
    from scipy import stats as sst

    simple = {}
    for k in (4, 6, 8, 10):
        # |A - B| >= k  <=>  A <= (n-k)/2 or A >= (n+k)/2
        lo = int(np.floor((n_clips - k) / 2))
        hi = int(np.ceil((n_clips + k) / 2))
        simple[k] = float(sst.binom.cdf(lo, n_clips, 0.5) + sst.binom.sf(hi - 1, n_clips, 0.5))
    return {"simple_randomization": simple, "permuted_blocks_max_imbalance": block_size // 2}


def power_vs_assumed_sd(n_per_arm, mde):
    """Exact t-test power at the planned n if the planning SD is wrong."""
    return {sd: stats.t_power(n_per_arm, mde, sd) for sd in (8, 10, 12, 15, 18, 22)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=4000)
    args = ap.parse_args()

    cfg = ex.load_config(HERE / "config.yaml")
    d = cfg.design
    n, mde, sd = int(d["planned_n_per_arm"]), float(d["mde_points"]), float(d["planning_sd"])
    sims = args.sims
    t0 = time.time()
    _calibrate_truth([0.0, mde])

    inference = {}
    for name, (gen, base) in SCENARIOS.items():
        rng = np.random.default_rng(SEED + len(inference))
        inference[name] = {
            "H0 (no effect)": simulate_inference(rng, gen, base, 0.0, sd, n, sims),
            f"H1 (+{mde:g} pts)": simulate_inference(rng, gen, base, mde, sd, n, sims),
        }

    srm = simulate_srm(np.random.default_rng(SEED + 7), n_measured=2 * n, sims=20_000)
    balance = blocked_vs_simple_imbalance(2 * n, int(d["block_size"]))
    sd_sens = power_vs_assumed_sd(n, mde)

    results = {
        "settings": {
            "sims_per_cell": sims, "n_per_arm": n, "mde_points": mde, "planning_sd": sd,
            "alpha": 0.05, "bootstrap_resamples_in_sim": B_BOOT, "seed": SEED,
            "numpy": np.__version__, "scipy": scipy.__version__, "python": platform.python_version(),
            "runtime_seconds": round(time.time() - t0, 1), "date": str(date.today()),
        },
        "inference": inference,
        "srm": srm,
        "randomization_balance": balance,
        "power_if_planning_sd_is_wrong": {str(k): v for k, v in sd_sens.items()},
    }  # fmt: skip
    (HERE / "validation_results.json").write_text(json.dumps(results, indent=2) + "\n")
    (HERE / "validation_report.md").write_text(render_report(results, cfg))
    print((HERE / "validation_report.md").read_text())


def _pc(x: float) -> str:
    return f"{100 * x:.1f}%"


def render_report(r: dict, cfg: ex.Config) -> str:
    s = r["settings"]
    se0 = 100 * np.sqrt(0.05 * 0.95 / s["sims_per_cell"])
    L = [
        "# Validation of the hook_style analysis code",
        "",
        "> **Simulated data only.** This measures the *procedure's* error rates under known truth. "
        "It says nothing about whether question hooks work.",
        "",
        f"Run {s['date']} · {s['sims_per_cell']:,} simulated experiments per cell · "
        f"n = {s['n_per_arm']} per arm · SD {s['planning_sd']:g} · "
        f"seed {s['seed']} · numpy {s['numpy']}, scipy {s['scipy']} · "
        f"{s['runtime_seconds']}s. Reproduce: `uv run python experiments/hook_style/validate_analysis.py`.",
        "",
        "The decision rule under test is the real `clipper.experiment_analysis.decide()`; "
        "\"ships\" below means all three pre-registered tests agreed and the effect was positive.",
        "",
        "## 1. False positives and power of the analysis",
        "",
        f"Monte Carlo error on a 5% rate is about ±{se0:.1f} points at this simulation size.",
        "",
        "| data | truth | Welch rejects | Mann-Whitney rejects | bootstrap CI excludes 0 | **decision rule ships** | rule keeps control | 95% CI covers truth |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for scen, cells in r["inference"].items():
        for truth, v in cells.items():
            L.append(
                f"| {scen} | {truth} | {_pc(v['welch_reject'])} | {_pc(v['mwu_reject'])} | "
                f"{_pc(v['ci_excludes_zero'])} | **{_pc(v['decide_ship'])}** | "
                f"{_pc(v['decide_keep'])} | {_pc(v['ci_coverage'])} |"
            )
    h0 = {k: v["H0 (no effect)"] for k, v in r["inference"].items()}
    h1 = {k: next(v for kk, v in c.items() if kk.startswith("H1")) for k, c in r["inference"].items()}
    cov = [v["ci_coverage"] for c in r["inference"].values() for v in c.values()]
    L += ["", "**Reading it** (every number below is from the table above, not asserted).", ""]
    for scen in r["inference"]:
        a, b = h0[scen], h1[scen]
        L.append(
            f"- *{scen}*: under no effect the decision rule ships {_pc(a['decide_ship'])} of the "
            f"time (Welch alone rejects {_pc(a['welch_reject'])}); with a true effect equal to the "
            f"MDE it ships {_pc(b['decide_ship'])} (Welch alone {_pc(b['welch_reject'])}), a cost of "
            f"{100 * (b['welch_reject'] - b['decide_ship']):.1f} points of power for requiring all "
            "three tests to agree."
        )
    L.append(
        f"- The 95% bootstrap interval covered the true difference in {_pc(min(cov))} to "
        f"{_pc(max(cov))} of simulations (nominal 95%)"
        + (
            " — below nominal, the known small-sample behaviour of the percentile bootstrap, "
            "which is why it is one of three tests and never the sole basis."
            if min(cov) < 0.94
            else "."
        )
    )
    srm = r["srm"]
    low = srm["70/30"]["p<.001"]
    L += [
        "",
        "## 2. Sample-ratio-mismatch alarm: how sensitive is it at this n?",
        "",
        f"Exact binomial test on {2 * s['n_per_arm']} measured clips, 20,000 trials per row.",
        "",
        "| true split | alarm at p<.001 (pre-registered) | p<.01 | p<.05 |",
        "|---|---|---|---|",
    ]
    for label, v in r["srm"].items():
        L.append(f"| {label} | {_pc(v['p<.001'])} | {_pc(v['p<.01'])} | {_pc(v['p<.05'])} |")
    L += [
        "",
        f"**Finding:** at p<.001 the alarm catches a 70/30 split of {2 * s['n_per_arm']} clips "
        f"{_pc(low)} of the time and an 80/20 split {_pc(srm['80/20']['p<.001'])}; a 60/40 split "
        f"{_pc(srm['60/40']['p<.001'])}. "
        + (
            "That is low sensitivity to moderate imbalance — "
            if low < 0.5
            else "That is reasonable sensitivity — "
        )
        + "the p<.001 convention comes from large online experiments. It is retained as "
        "pre-registered. The differential-attrition and acceptance diagnostics (Fisher, p<.01) "
        "also guard the design, and a passing SRM check should not be read as strong evidence "
        "of a clean split at this sample size.",
        "",
        "## 3. Why permuted blocks",
        "",
        f"Probability that the two arms differ by at least k clips out of {2 * s['n_per_arm']} under "
        "plain coin-flip assignment (permuted blocks of 4 cap the imbalance at 2):",
        "",
        "| k | simple randomization |",
        "|---|---|",
    ]
    for k, p in r["randomization_balance"]["simple_randomization"].items():
        L.append(f"| ≥ {k} | {_pc(p)} |")
    L += [
        "",
        "## 4. If the planning SD is wrong",
        "",
        f"Exact t-test power at n = {s['n_per_arm']} per arm for a {s['mde_points']:g}-point effect. "
        f"The planning SD ({s['planning_sd']:g}) rests on only two real clips.",
        "",
        "| true SD of per-clip stayed-to-watch % | power |",
        "|---|---|",
    ]
    for sd_, p in r["power_if_planning_sd_is_wrong"].items():
        L.append(f"| {sd_} | {_pc(p)} |")
    L += [
        "",
        "If the real spread is 15 points rather than 12, power at the planned n falls to about "
        f"{_pc(r['power_if_planning_sd_is_wrong']['15'])}; at 18 it is about "
        f"{_pc(r['power_if_planning_sd_is_wrong']['18'])}. This is why the pre-registration includes a "
        "blinded SD re-estimate after the first 16 measured clips.",
        "",
    ]
    return "\n".join(L)


if __name__ == "__main__":
    main()
