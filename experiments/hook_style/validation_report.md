# Validation of the hook_style analysis code

> **Simulated data only.** This measures the *procedure's* error rates under known truth. It says nothing about whether question hooks work.

Run 2026-11-11 · 10,000 simulated experiments per cell · n = 26 per arm · SD 12 · seed 20261111 · numpy 2.5.2, scipy 1.18.1 · 62.6s. Reproduce: `uv run python experiments/hook_style/validate_analysis.py`.

The decision rule under test is the real `clipper.experiment_analysis.decide()`; "ships" below means all three pre-registered tests agreed and the effect was positive.

## 1. False positives and power of the analysis

Monte Carlo error on a 5% rate is about ±0.2 points at this simulation size.

| data | truth | Welch rejects | Mann-Whitney rejects | bootstrap CI excludes 0 | **decision rule ships** | rule keeps control | 95% CI covers truth |
|---|---|---|---|---|---|---|---|
| normal | H0 (no effect) | 4.7% | 4.7% | 6.3% | **1.8%** | 1.9% | 93.7% |
| normal | H1 (+10 pts) | 83.6% | 81.4% | 85.8% | **80.1%** | 0.0% | 93.9% |
| skewed-bounded | H0 (no effect) | 5.2% | 4.9% | 6.3% | **2.1%** | 2.0% | 93.7% |
| skewed-bounded | H1 (+10 pts) | 83.9% | 81.4% | 85.9% | **80.3%** | 0.0% | 94.2% |

**Reading it** (every number below is from the table above, not asserted).

- *normal*: under no effect the decision rule ships 1.8% of the time (Welch alone rejects 4.7%); with a true effect equal to the MDE it ships 80.1% (Welch alone 83.6%), a cost of 3.6 points of power for requiring all three tests to agree.
- *skewed-bounded*: under no effect the decision rule ships 2.1% of the time (Welch alone rejects 5.2%); with a true effect equal to the MDE it ships 80.3% (Welch alone 83.9%), a cost of 3.6 points of power for requiring all three tests to agree.
- The 95% bootstrap interval covered the true difference in 93.7% to 94.2% of simulations (nominal 95%) — below nominal, the known small-sample behaviour of the percentile bootstrap, which is why it is one of three tests and never the sole basis.

## 2. Sample-ratio-mismatch alarm: how sensitive is it at this n?

Exact binomial test on 52 measured clips, 20,000 trials per row.

| true split | alarm at p<.001 (pre-registered) | p<.01 | p<.05 |
|---|---|---|---|
| fair 50/50 | 0.1% | 0.9% | 3.7% |
| 60/40 | 1.7% | 11.4% | 26.5% |
| 70/30 | 26.8% | 61.9% | 81.1% |
| 80/20 | 86.0% | 97.8% | 99.6% |

**Finding:** at p<.001 the alarm catches a 70/30 split of 52 clips 26.8% of the time and an 80/20 split 86.0%; a 60/40 split 1.7%. That is low sensitivity to moderate imbalance — the p<.001 convention comes from large online experiments. It is retained as pre-registered. The differential-attrition and acceptance diagnostics (Fisher, p<.01) also guard the design, and a passing SRM check should not be read as strong evidence of a clean split at this sample size.

## 3. Why permuted blocks

Probability that the two arms differ by at least k clips out of 52 under plain coin-flip assignment (permuted blocks of 4 cap the imbalance at 2):

| k | simple randomization |
|---|---|
| ≥ 4 | 67.8% |
| ≥ 6 | 48.8% |
| ≥ 8 | 33.2% |
| ≥ 10 | 21.2% |

## 4. If the planning SD is wrong

Exact t-test power at n = 26 per arm for a 10-point effect. The planning SD (12) rests on only two real clips.

| true SD of per-clip stayed-to-watch % | power |
|---|---|
| 8 | 99.3% |
| 10 | 94.2% |
| 12 | 83.8% |
| 15 | 65.4% |
| 18 | 50.2% |
| 22 | 36.2% |

If the real spread is 15 points rather than 12, power at the planned n falls to about 65.4%; at 18 it is about 50.2%. This is why the pre-registration includes a blinded SD re-estimate after the first 16 measured clips.
