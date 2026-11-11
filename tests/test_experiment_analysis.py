"""Tests for clipper.experiment_analysis. Experiments are built through the real
assignment + selection-logging path (clipper.experiment) from simulated data, so
the decision rule is exercised end to end. Everything here is synthetic — none
of it is, or may be mistaken for, a real result."""

from __future__ import annotations

import csv

import pytest

pytest.importorskip("scipy")

import numpy as np
import yaml

from clipper import experiment as ex
from clipper import experiment_analysis as ea
from clipper import paths


def _design(**over):
    d = {
        "seed": 7,
        "block_size": 4,
        "control_arm": "statement",
        "arms": {
            "statement": {"weight": 1, "hook_style": "STATEMENT"},
            "question": {"weight": 1, "hook_style": "QUESTION"},
        },
        "eligible_tier": "STANDARD",
        "primary_metric": "stayed_to_watch_pct",
        "measurement_age_days": 7,
        "measurement_window_days": [6, 10],
        "mde_points": 10,
        "planning_sd": 12,
        "alpha": 0.05,
        "power": 0.8,
        "planned_n_per_arm": 20,
        "blinded_reestimate_at_n": 10_000,  # off unless a test turns it on
    }
    d.update(over)
    return d


@pytest.fixture
def make(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    monkeypatch.setattr(paths, "EXPERIMENTS_DIR", root)

    def build(**design_over) -> ex.Config:
        d = root / "hook_style"
        d.mkdir(parents=True, exist_ok=True)
        cfg = {
            "name": "hook_style",
            "status": "active",
            "launch_date": "2026-12-01",
            "preregistration": "PREREGISTRATION.md",
            "design": _design(**design_over),
        }
        (d / "config.yaml").write_text(yaml.safe_dump(cfg))
        (d / "PREREGISTRATION.md").write_text("pre-reg")
        return ex.load_config(d / "config.yaml")

    return build


def _enroll(cfg, n_clips, *, comply=True, accept=True):
    """Assign n_clips and log a selection for each; returns the records."""
    recs = []
    for i in range(n_clips):
        rec = ex.assign(cfg, f"clip{i:03d}")
        recs.append(rec)
        style = cfg.arms[rec["arm"]]["hook_style"]
        if not accept:
            ex.record_selection(
                ex.Enrollment(cfg.name, rec["arm"], style, rec["index"]), rec["video_id"], None
            )
            continue
        good = "Who told him?" if style == "QUESTION" else "He told them."
        bad = "He told them." if style == "QUESTION" else "Who told him?"
        seg = {
            "vo_placement": "cold_open",
            "vo_script": good if comply else bad,
            "suggested_title": "A title",
        }
        ex.record_selection(
            ex.Enrollment(cfg.name, rec["arm"], style, rec["index"]), rec["video_id"], seg
        )
    return recs


def _write_outcomes(cfg, recs, *, effect, sd, seed=0, age=7, keep=None):
    """outcomes.csv with stayed_to_watch ~ N(50 + effect*[question], sd)."""
    rng = np.random.default_rng(seed)
    path = cfg.dir / "outcomes.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["video_id", "youtube_id", "published_at", "snapshot_date", "views",
             "stayed_to_watch_pct", "avg_view_duration_s", "avg_view_pct", "notes"]
        )  # fmt: skip
        for rec in recs:
            if keep is not None and not keep(rec):
                continue
            mu = 50 + (effect if rec["arm"] == "question" else 0)
            pct = float(np.clip(rng.normal(mu, sd), 0, 100))
            views = int(rng.integers(50, 5000))
            w.writerow(
                [rec["video_id"], "yt", "2026-12-01", f"2026-12-{1 + age:02d}", views,
                 f"{pct:.2f}", f"{rng.normal(20, 4):.1f}", f"{rng.normal(60, 8):.1f}", ""]
            )  # fmt: skip
    return path


def _run(cfg, **kw):
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    return rows, ea.analyze(cfg, rows, n_boot=1500, **kw)


# --- loading ----------------------------------------------------------------


def test_load_outcomes_parses_numbers_and_blanks(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text(
        "video_id,published_at,snapshot_date,views,stayed_to_watch_pct,notes\n"
        "a,2026-12-01,2026-12-08,120,54.5,\n"
    )
    row = ea.load_outcomes(p)[0]
    assert row["views"] == 120 and row["stayed_to_watch_pct"] == 54.5 and row["notes"] is None


def test_load_outcomes_missing_file_is_empty(tmp_path):
    assert ea.load_outcomes(tmp_path / "nope.csv") == []


def test_load_outcomes_rejects_out_of_range_and_garbage(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("video_id,stayed_to_watch_pct\na,154\n")
    with pytest.raises(ValueError, match="outside 0-100"):
        ea.load_outcomes(p)
    p.write_text("video_id,stayed_to_watch_pct\na,lots\n")
    with pytest.raises(ValueError, match="bad stayed_to_watch_pct"):
        ea.load_outcomes(p)


# --- merging ----------------------------------------------------------------


def test_measurement_window_decides_what_counts_as_measured(make):
    cfg = make()
    recs = _enroll(cfg, 4)
    for age, expect in ((5, False), (6, True), (7, True), (10, True), (11, False)):
        _write_outcomes(cfg, recs[:1], effect=0, sd=1, age=age)
        rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
        assert rows[0]["measured"] is expect, age


def test_a_clip_with_no_outcome_row_is_unpublished_not_measured(make):
    cfg = make()
    recs = _enroll(cfg, 4)
    _write_outcomes(cfg, recs, effect=0, sd=5, keep=lambda r: r["index"] < 2)
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    assert [r["published"] for r in rows] == [True, True, False, False]
    assert [r["measured"] for r in rows] == [True, True, False, False]


def test_outcome_rows_for_clips_outside_the_experiment_are_ignored(make):
    cfg = make()
    recs = _enroll(cfg, 2)
    p = _write_outcomes(cfg, recs, effect=0, sd=5)
    with p.open("a") as f:
        f.write("stranger,yt,2026-12-01,2026-12-08,10,40,10,50,\n")
    rows = ea.build_rows(cfg, ea.load_outcomes(p))
    assert [r["video_id"] for r in rows] == ["clip000", "clip001"]


def test_unaccepted_clips_are_assigned_but_not_accepted(make):
    cfg = make()
    _enroll(cfg, 4, accept=False)
    rows = ea.build_rows(cfg, [])
    assert len(rows) == 4 and not any(r["accepted"] for r in rows)


def test_funnel_counts_each_stage_by_arm(make):
    cfg = make()
    recs = _enroll(cfg, 8)
    _write_outcomes(cfg, recs, effect=0, sd=5, keep=lambda r: r["index"] < 6)
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    f = ea.funnel(rows, ["question", "statement"])
    assert f["question"]["assigned"] == f["statement"]["assigned"] == 4
    assert sum(v["measured"] for v in f.values()) == 6
    assert sum(v["published"] for v in f.values()) == 6


def test_holm_matches_the_hand_calculation_and_preserves_order():
    assert ea.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert ea.holm([0.5]) == [0.5]


# --- the decision rule ------------------------------------------------------


def test_a_clear_positive_effect_ships_the_treatment(make):
    cfg = make()
    recs = _enroll(cfg, 40)
    _write_outcomes(cfg, recs, effect=+25, sd=6)
    _, res = _run(cfg)
    d = res["decision"]
    assert d["decision"] == "ship_treatment" and d["label"] == "SHIP QUESTION"
    assert not d["underpowered"] and not d["flags"]
    assert res["primary"]["welch"]["diff"] > 15
    assert res["primary"]["bootstrap"]["lo"] > 0


def test_a_clear_negative_effect_keeps_the_control(make):
    cfg = make()
    recs = _enroll(cfg, 40)
    _write_outcomes(cfg, recs, effect=-25, sd=6)
    _, res = _run(cfg)
    assert res["decision"]["decision"] == "keep_control"
    assert res["decision"]["label"] == "KEEP STATEMENT"


def test_no_effect_is_inconclusive_not_a_ship(make):
    cfg = make()
    recs = _enroll(cfg, 40)
    _write_outcomes(cfg, recs, effect=0, sd=8, seed=3)
    _, res = _run(cfg)
    assert res["decision"]["decision"] == "inconclusive"


def test_below_planned_n_is_labelled_underpowered(make):
    cfg = make(planned_n_per_arm=20)
    recs = _enroll(cfg, 12)  # 6 per arm
    _write_outcomes(cfg, recs, effect=+30, sd=4)
    _, res = _run(cfg)
    d = res["decision"]
    assert d["underpowered"] and "underpowered" in d["label"]


def test_too_few_measured_clips_is_no_decision(make):
    cfg = make()
    recs = _enroll(cfg, 4)
    _write_outcomes(cfg, recs, effect=0, sd=5, keep=lambda r: r["index"] == 0)
    _, res = _run(cfg)
    assert res["insufficient"] and res["decision"]["decision"] == "no_decision"
    assert "primary" not in res


def test_a_sample_ratio_mismatch_withholds_the_decision_even_with_a_huge_effect(make):
    cfg = make()
    recs = _enroll(cfg, 60)

    seen = {"question": 0, "statement": 0}

    def keep_unbalanced(rec):  # measure all 30 control clips but only 8 treatment clips
        seen[rec["arm"]] += 1
        return rec["arm"] == "statement" or seen["question"] <= 8

    _write_outcomes(cfg, recs, effect=+30, sd=4, keep=keep_unbalanced)
    _, res = _run(cfg)
    assert res["diagnostics"]["srm_analyzed"]["p"] < 0.001
    assert res["decision"]["decision"] == "no_decision"
    assert "sample-ratio" in res["decision"]["label"]


def test_low_compliance_downgrades_a_significant_result_to_inconclusive(make):
    cfg = make()
    recs = _enroll(cfg, 40, comply=False)  # the model ignored the arm every time
    _write_outcomes(cfg, recs, effect=+25, sd=6)
    _, res = _run(cfg)
    assert res["primary"]["welch"]["p"] < 0.05  # would otherwise ship
    d = res["decision"]
    assert d["decision"] == "inconclusive" and any("compliance" in f for f in d["flags"])


def test_differential_acceptance_by_arm_is_flagged(make):
    cfg = make()
    recs, accepted_ids = [], set()
    for i in range(60):
        rec = ex.assign(cfg, f"clip{i:03d}")
        recs.append(rec)
        style = cfg.arms[rec["arm"]]["hook_style"]
        # the question prompt gets accepted far less often than the statement one
        accepted = rec["arm"] == "statement" or i % 6 == 0
        seg = None
        if accepted:
            accepted_ids.add(rec["video_id"])
            line = "Who?" if style == "QUESTION" else "He."
            seg = {"vo_placement": "cold_open", "vo_script": line, "suggested_title": "t"}
        enrol = ex.Enrollment(cfg.name, rec["arm"], style, rec["index"])
        ex.record_selection(enrol, rec["video_id"], seg)
    _write_outcomes(cfg, recs, effect=+30, sd=4, keep=lambda r: r["video_id"] in accepted_ids)
    _, res = _run(cfg)
    assert res["diagnostics"]["acceptance"]["p"] < 0.01


def test_outcomes_for_clips_selection_rejected_raise_an_integrity_flag_not_a_crash(make):
    cfg = make()
    recs = _enroll(cfg, 40)
    # throw away the selection log: every measured clip now looks unaccepted
    cfg.selections_path.write_text("")
    _write_outcomes(cfg, recs, effect=+25, sd=6)
    _, res = _run(cfg)
    assert res["diagnostics"]["measured_without_accepted_selection"] == 40
    d = res["decision"]
    assert d["decision"] == "inconclusive" and any("inconsistent" in f for f in d["flags"])


# --- blinded re-estimate ----------------------------------------------------


def test_blinded_reestimate_waits_for_enough_measured_clips(make):
    cfg = make(blinded_reestimate_at_n=16)
    recs = _enroll(cfg, 12)
    _write_outcomes(cfg, recs, effect=0, sd=10)
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    assert ea.blinded_reestimate(cfg, rows) is None


def test_blinded_reestimate_can_raise_the_planned_n_when_the_sd_is_larger(make):
    cfg = make(planned_n_per_arm=26, blinded_reestimate_at_n=16)
    recs = _enroll(cfg, 20)
    _write_outcomes(cfg, recs, effect=0, sd=25, seed=4)  # much noisier than planned (12)
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    re_ = ea.blinded_reestimate(cfg, rows)
    assert re_["pooled_sd"] > 15
    assert re_["planned_after"] > re_["planned_before"] == 26


def test_blinded_reestimate_never_lowers_the_planned_n(make):
    cfg = make(planned_n_per_arm=26, blinded_reestimate_at_n=16)
    recs = _enroll(cfg, 20)
    _write_outcomes(cfg, recs, effect=0, sd=3, seed=5)  # far quieter than planned
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    re_ = ea.blinded_reestimate(cfg, rows)
    assert re_["n_per_arm_rank"] < 26
    assert re_["planned_after"] == 26


def test_blinded_reestimate_does_not_inflate_when_the_design_assumptions_hold(make):
    # true effect == the MDE and true SD == the planning SD: the re-estimate
    # should see ~12, not the ~13 an unadjusted pooled SD would report
    cfg = make(planned_n_per_arm=26, blinded_reestimate_at_n=16)
    recs = _enroll(cfg, 400)
    _write_outcomes(cfg, recs, effect=10, sd=12, seed=6)
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    re_ = ea.blinded_reestimate(cfg, rows)
    assert re_["pooled_sd"] > re_["adjusted_sd"]
    assert re_["adjusted_sd"] == pytest.approx(12, rel=0.08)
    assert re_["planned_after"] <= 30  # not the ~31+ the unadjusted SD would demand


# --- memo and chart ---------------------------------------------------------


def test_memo_states_the_decision_the_interval_and_the_diagnostics(make):
    cfg = make()
    recs = _enroll(cfg, 40)
    _write_outcomes(cfg, recs, effect=+25, sd=6)
    _, res = _run(cfg)
    memo = ea.render_memo(res)
    assert "## Decision: SHIP QUESTION" in memo
    assert "95% bootstrap CI" in memo and "Sample ratio (analysed)" in memo
    assert "## Funnel" in memo and "SYNTHETIC" not in memo
    assert "To be written by the owner" in memo


def test_memo_banner_marks_synthetic_data(make):
    cfg = make()
    recs = _enroll(cfg, 8)
    _write_outcomes(cfg, recs, effect=0, sd=5)
    _, res = _run(cfg)
    assert ea.render_memo(res, synthetic=True).startswith("> **SYNTHETIC DATA")


def test_memo_with_no_data_is_an_honest_no_decision(make):
    cfg = make()
    _, res = _run(cfg)
    memo = ea.render_memo(res)
    assert "NO DECISION" in memo and "## Primary result" not in memo


def test_plot_primary_draws_with_data_and_is_none_without(make):
    import matplotlib

    matplotlib.use("Agg")
    cfg = make()
    recs = _enroll(cfg, 20)
    _write_outcomes(cfg, recs, effect=+10, sd=8)
    rows, res = _run(cfg)
    ax = ea.plot_primary(res, rows)
    assert ax is not None and "95% CI" in ax.get_title()

    empty = make()
    (empty.dir / "assignments.jsonl").write_text("")
    _, res0 = _run(empty)
    assert ea.plot_primary(res0, []) is None
