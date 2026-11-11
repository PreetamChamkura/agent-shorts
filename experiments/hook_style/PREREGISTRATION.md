# Pre-registration: question vs. statement hook line

**Experiment:** `hook_style` · **Registered:** 2026-11-11 · **Status at registration:** `preregistered` (not launched; no clip has been assigned, and `assignments.jsonl` is empty in the commit that adds this file)

This document is committed before launch and is hashed into every assignment record (`prereg_sha256`). If it is edited after launch, `clipper experiment verify` fails. Deviations go in `AMENDMENTS.md`, never in this file.

## 1. Question

Does phrasing the narrator's hook line as a **question** change how many viewers stay to watch, compared with a **statement**, on otherwise-identical clips?

One change is tested: the grammatical form of the hook line. Nothing else is varied.

## 2. Hypotheses

- **H0:** mean stayed-to-watch % is the same under both hook styles.
- **H1:** it differs. The *expected* direction is question > statement, but the test is **two-sided** because a question hook that hurts is just as decision-relevant.

## 3. Design

| | |
|---|---|
| Unit of randomization and analysis | one published clip |
| Control | `statement` — the pipeline's existing register |
| Treatment | `question` — hook line phrased as one direct question |
| Allocation | 1:1, **permuted blocks of 4** (2 of each), seeded |
| Seed | `20261111` |
| Assignment moment | the first `select_clip` call for a clip while status is `active` |
| Eligible clips | Twitch clips that pass Layer 3, are **STANDARD tier** (< 100,000 source views), are ≥ 18 s, and are not already selected |

**Randomization.** The k-th enrolled clip receives the k-th element of a deterministic sequence generated from the seed (`clipper.experiment.arm_sequence`). Assignment is **idempotent**: a clip already in the log keeps its arm, so re-running or `--force`-ing selection can never re-roll it. Every assignment is appended to `assignments.jsonl` with its sequence index, block, seed, and the hashes of this document and of the `design` block in `config.yaml`. `verify` recomputes the whole sequence from the seed and checks the log against it.

**The manipulation.** A per-clip line `Hook style: STATEMENT | QUESTION` is added to the selection prompt, with a fixed rule section. The *hook line* is `vo_script` for the two spoken placements and `on_screen_caption` for `caption_only`. It is **required in both arms** (otherwise a clip with no line would receive no treatment).

- `QUESTION`: the line is one direct question ending in `?`.
- `STATEMENT`: the line contains no `?`.

**Deliberate deviation from a standing rule.** The project's narrator persona says "no questions to the audience." The `question` arm intentionally breaks that rule for the hook line only; every other persona rule (one beat, 2–6 word target, present tense, no hype, no describing visible action, don't give away the payoff) still applies. This is authorized by launching the experiment, and it applies only to clips enrolled in it.

**Compliance** is checked mechanically at selection time (line ends in `?` iff question arm) and logged. The title is *not* manipulated; whether it happens to be a question is logged per arm as a covariate threat (§8).

## 4. Outcomes

**Primary — `stayed_to_watch_pct`:** the "Stayed to watch" share in YouTube Studio's *Viewed vs. swiped away* report for the Short, read **7 days after publication** (accepted window: 6–10 days). *Note:* the originally proposed "3-second retention" is not a number YouTube Studio exposes, so this is the closest published equivalent of the same idea — did the viewer stay past the opening or swipe away.

**Secondary (descriptive, no decision weight):** views at the snapshot (analysed as `log1p`), average view duration (s), average % viewed. Holm-adjusted across the three.

Outcomes are entered by hand from Studio into `outcomes.csv` (see the header there). No API client is used.

## 5. Sample size and stopping

Computed with `clipper.stats.n_per_arm` (exact noncentral-t power; validated against the published reference values 64 / 26 / 17 per arm for d = 0.5 / 0.8 / 1.0).

- **Smallest effect of interest (MDE): 10 percentage points.** A shift this size would move a typical clip from about 50% to about 60% — big enough to justify overriding a persona rule. Anything smaller cannot be resolved at this project's posting rate and would not justify the change either.
- **Planning SD of per-clip stayed-to-watch %: 12 points.** *Caveat:* this rests on the only two real clips measured so far (54% and 36.3%, SD ≈ 12.5, n = 2) and is a planning value, not an estimate. See the blinded re-estimate below.
- α = 0.05 (two-sided), power = 0.80.
- **Result: 24 per arm for the t-test; 26 per arm if the rank test must carry the power. Planned n = 26 per arm, 52 measured clips.**

| MDE (pts) | n/arm: t-test (rank test) | total: t-test (rank test) | weeks to finish at 1 / 2 / 3 / 5 clips per week¹ |
|---|---|---|---|
| 8  | 37 (39) | 74 (78) | 87 / 43 / 29 / 17 |
| **10** | **24 (26)** | **48 (52)** | **58 / 29 / 19 / 12** |
| 12 | 17 (18) | 34 (36) | 40 / 20 / 13 / 8 |
| 15 | 12 (13) | 24 (26) | 29 / 14 / 10 / 6 |

¹ Assumes 90% of published clips yield a usable measurement. Weeks are for the rank-test-sized total (the planned n).

**What this means in practice.** The planned 52 measured clips take **about 19 weeks at 3 published clips per week, 29 at 2, and 58 at 1.** Inside the 26-week cap, the experiment reaches planned n only at roughly 3 or more clips per week; at 2 per week it ends with about 46 measured clips (88% of plan) and at 1 per week about 23 (44%). It cannot finish in a month. If the realistic posting rate is below 2 per week, the right move is to test something with more data per clip rather than to loosen this design after launch.

**Stop rule:** stop at **26 measured clips per arm, or 26 weeks after `launch_date`, whichever comes first.** The analysis is run **once**, at the stop. There are **no interim looks at the treatment effect.**

**Blinded sample-size re-estimate (the only interim).** When the first 16 clips have a measured outcome, estimate the within-arm SD of `stayed_to_watch_pct` *without looking at arm*, and recompute n once with `clipper.stats.n_per_arm`. The estimator is the pooled SD adjusted for the variance the assumed 10-point effect contributes (`clipper.stats.blinded_adjusted_sd`; without that adjustment a true effect equal to the MDE would inflate the estimate and ask for ~19% more clips even when nothing had gone wrong). The planned n may be raised, never lowered, and the 26-week cap does not move. If n is raised past what the cap allows, the experiment is reported as underpowered.

## 6. Analysis plan

Intention-to-treat: every enrolled clip that was published and has a valid measurement is analysed **in the arm it was assigned**, whether or not the model complied.

1. **Primary test:** Welch's t-test on the difference in mean `stayed_to_watch_pct` (question − statement), two-sided, α = 0.05.
2. **Robustness:** Mann-Whitney U (retention data is bounded and skewed) and a percentile bootstrap 95% CI on the difference in means (10,000 clip-level resamples, seed = 20261111). Effect size: Hedges' g and probability of superiority.
3. **Sensitivity (reported, not decisive):** views-weighted bootstrap; per-protocol (compliant clips only).

**Decision rule, fixed now:**

| Outcome | Decision |
|---|---|
| Welch p < .05 **and** Mann-Whitney p < .05 **and** bootstrap CI excludes 0, all positive, no diagnostic flags | **Ship `question`** |
| Same, all negative | **Keep `statement`** |
| Anything else | **Inconclusive** |

A non-significant result at planned n means *no effect of 10 points or more was detected* — it does **not** mean "no effect." If the stop is reached before planned n, the memo says **underpowered**, and a non-significant result is "inconclusive," not "no difference."

**Diagnostics (checked before any decision is allowed):**

- **Sample-ratio mismatch** — exact binomial test of the analysed arm counts against 1:1. p < 0.001 ⇒ **design integrity failure, no decision.**
- **Differential attrition** — among clips that selection *accepted*, Fisher's exact test of (published & measured) vs. not, by arm. This catches, for example, the poster quietly declining to publish one arm's clips. p < 0.01 ⇒ flag.
- **Differential selection acceptance** — enrolment is at the selection call, so if the question prompt changes how often clips are accepted, the surviving clips are a selected subset. Fisher's exact test of accepted vs. rejected, by arm. p < 0.01 ⇒ flag.
- **Compliance** — below 80% in either arm ⇒ flag.
- **Title covariate** — share of titles ending in `?`, by arm, reported.

Any flag downgrades a ship/keep decision to **Inconclusive (diagnostic flag)**, except SRM, which withholds the decision entirely.

**Missing data.** A clip with no outcome row, or a snapshot outside the 6–10 day window, is excluded and counted by arm in the attrition diagnostic. No imputation.

## 7. Integrity

`clipper experiment verify` checks that (a) every assignment equals the element the seed prescribes for its index, (b) indices are contiguous and no clip is assigned twice, and (c) every record's `prereg_sha256` and `design_sha256` match the current files. `validate_analysis.py` Monte Carlo-validates the analysis code (false-positive rate, power, CI coverage, SRM sensitivity) on simulated data before any real data exists.

## 8. Known threats and limitations (stated in advance)

- **Low volume.** One channel, a small posting rate; only large effects are detectable (§5).
- **Not blinded.** The person posting knows each clip's arm.
- **Post-assignment selection.** Acceptance happens after assignment (diagnostic in §6).
- **Interference and drift.** Clips posted close together compete for the same feed, and the platform's distribution is non-stationary. Randomization balances this on average but adds noise; fixed-age measurement limits age confounding.
- **Metric proxy.** Stayed-to-watch stands in for 3-second retention.
- **Per-clip aggregates.** Studio reports one percentage per clip, so precision varies with views; the views-weighted sensitivity analysis addresses this, and tiny-view clips are noisy but not excluded (excluding on views would condition on an outcome).
- **Model compliance** with the arm is probabilistic, hence the ITT primary analysis.
- **Single hook-line change.** A null says nothing about stronger or different question formats.

## 9. How to launch

Edit `config.yaml`: set `status: active` and `launch_date`, commit. Nothing is randomized until then, and with `status: preregistered` or `frozen` the pipeline behaves exactly as it did before this experiment existed.
