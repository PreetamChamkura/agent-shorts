"""Tests for clipper.experiment (the feature flag + randomization) and its
integration with select_clip. Every test runs against a temp experiments dir
(see conftest.py) — none can touch the real, pre-registered logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from clipper import experiment as ex
from clipper import paths
from clipper import select as select_mod
from clipper.select import select_clip, select_clip_batch

REAL_CONFIG = paths.REPO_ROOT / "experiments" / "hook_style" / "config.yaml"

# The first 16 assignments the committed seed prescribes. Pinned because a
# change to arm_at/_block_arms mid-experiment would silently re-define what
# "the k-th clip's arm" means and invalidate every later assignment.
GOLDEN_SEQUENCE = [
    "question", "question", "statement", "statement",
    "question", "statement", "statement", "question",
    "statement", "question", "question", "statement",
    "question", "question", "statement", "statement",
]  # fmt: skip


def _config_dict(status="active", **design_over):
    design = {
        "seed": 12345,
        "block_size": 4,
        "control_arm": "statement",
        "arms": {
            "statement": {"weight": 1, "hook_style": "STATEMENT"},
            "question": {"weight": 1, "hook_style": "QUESTION"},
        },
        "eligible_tier": "STANDARD",
        "primary_metric": "stayed_to_watch_pct",
    }
    design.update(design_over)
    return {
        "name": "hook_style",
        "status": status,
        "launch_date": "2026-11-12",
        "preregistration": "PREREGISTRATION.md",
        "design": design,
    }


@pytest.fixture
def exp(tmp_path, monkeypatch):
    """An active experiment in a temp experiments/ dir."""

    def make(status="active", **design_over) -> ex.Config:
        root = tmp_path / "experiments"
        d = root / "hook_style"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(yaml.safe_dump(_config_dict(status, **design_over)))
        (d / "PREREGISTRATION.md").write_text("# pre-registration v1\n")
        monkeypatch.setattr(paths, "EXPERIMENTS_DIR", root)
        return ex.load_config(d / "config.yaml")

    return make


# --- the sequence -----------------------------------------------------------


def test_committed_config_produces_the_pinned_sequence():
    cfg = ex.load_config(REAL_CONFIG)
    assert ex.arm_sequence(cfg, 16) == GOLDEN_SEQUENCE


def test_sequence_is_deterministic_and_every_block_is_balanced(exp):
    cfg = exp()
    seq = ex.arm_sequence(cfg, 40)
    assert seq == ex.arm_sequence(cfg, 40)
    for b in range(10):
        block = seq[b * 4 : (b + 1) * 4]
        assert block.count("statement") == 2 and block.count("question") == 2
    assert seq.count("statement") == seq.count("question") == 20


def test_a_different_seed_gives_a_different_sequence(exp):
    a = ex.arm_sequence(exp(seed=1), 40)
    b = ex.arm_sequence(exp(seed=2), 40)
    assert a != b


def test_arm_at_matches_the_sequence_and_reports_block_and_position(exp):
    cfg = exp()
    seq = ex.arm_sequence(cfg, 12)
    for i in range(12):
        arm, block, pos = ex.arm_at(cfg, i)
        assert (arm, block, pos) == (seq[i], i // 4, i % 4)


def test_unequal_weights_are_respected_within_a_block(exp):
    cfg = exp(
        block_size=6,
        arms={
            "statement": {"weight": 2, "hook_style": "STATEMENT"},
            "question": {"weight": 1, "hook_style": "QUESTION"},
        },
    )
    block = ex.arm_sequence(cfg, 6)
    assert block.count("statement") == 4 and block.count("question") == 2


def test_block_size_that_cannot_balance_the_arms_is_a_config_error(exp):
    with pytest.raises(ex.ExperimentError, match="multiple"):
        exp(block_size=5)


def test_control_arm_must_be_one_of_the_arms(exp):
    with pytest.raises(ex.ExperimentError, match="control_arm"):
        exp(control_arm="nope")


# --- assignment -------------------------------------------------------------


def test_assign_logs_a_complete_record_and_advances_the_index(exp):
    cfg = exp()
    a = ex.assign(cfg, "clipA")
    b = ex.assign(cfg, "clipB")
    assert (a["index"], b["index"]) == (0, 1)
    assert a["arm"] == ex.arm_at(cfg, 0)[0]
    for key in ("experiment", "video_id", "block", "pos", "seed", "ts"):
        assert key in a
    assert a["prereg_sha256"] == ex.prereg_sha256(cfg)
    assert a["design_sha256"] == ex.design_sha256(cfg.design)
    assert len(ex.read_assignments(cfg)) == 2


def test_assign_is_idempotent_so_a_rerun_can_never_reroll(exp):
    cfg = exp()
    first = ex.assign(cfg, "clipA")
    for _ in range(5):
        assert ex.assign(cfg, "clipA") == first
    assert len(ex.read_assignments(cfg)) == 1
    # and a later clip still gets the next slot, not a skipped one
    assert ex.assign(cfg, "clipB")["index"] == 1


# --- enroll (what the pipeline calls) --------------------------------------


def test_enroll_does_nothing_while_the_flag_is_off(exp):
    cfg = exp(status="preregistered")
    assert ex.enroll("clipA", tier="STANDARD") is None
    assert ex.read_assignments(cfg) == []


def test_enroll_does_nothing_when_no_experiment_exists():
    assert ex.enroll("clipA", tier="STANDARD") is None  # conftest: empty dir


def test_enroll_assigns_an_eligible_clip_when_active(exp):
    cfg = exp()
    e = ex.enroll("clipA", tier="STANDARD")
    assert e is not None
    assert e.experiment == "hook_style" and e.index == 0
    assert e.hook_style == cfg.arms[e.arm]["hook_style"]
    assert ex.hook_style_label(e) == e.hook_style


def test_enroll_skips_clips_outside_the_eligible_tier_without_logging(exp):
    cfg = exp()
    assert ex.enroll("bigclip", tier="HIGH") is None
    assert ex.read_assignments(cfg) == []
    assert ex.hook_style_label(None) == "NONE"


def test_frozen_experiment_keeps_existing_arms_but_stops_enrolling(exp):
    cfg = exp()
    before = ex.enroll("clipA", tier="STANDARD")
    # flip to frozen
    d = cfg.dir
    (d / "config.yaml").write_text(yaml.safe_dump(_config_dict("frozen")))
    again = ex.enroll("clipA", tier="STANDARD")
    assert again is not None and again.arm == before.arm
    assert ex.enroll("clipNEW", tier="STANDARD") is None
    assert len(ex.read_assignments(ex.load_config(d / "config.yaml"))) == 1


def test_two_running_experiments_is_an_error(exp, tmp_path):
    exp()
    other = tmp_path / "experiments" / "other"
    other.mkdir()
    cfg = _config_dict()
    cfg["name"] = "other"
    (other / "config.yaml").write_text(yaml.safe_dump(cfg))
    (other / "PREREGISTRATION.md").write_text("x")
    with pytest.raises(ex.ExperimentError, match="more than one"):
        ex.running_config()


# --- verify -----------------------------------------------------------------


def _fill(cfg, n=8):
    for i in range(n):
        ex.assign(cfg, f"clip{i}")


def test_verify_passes_on_a_clean_log(exp):
    cfg = exp()
    _fill(cfg)
    assert ex.verify(cfg) == []


def test_verify_passes_on_an_empty_log(exp):
    assert ex.verify(exp()) == []


def test_verify_catches_a_flipped_arm(exp):
    cfg = exp()
    _fill(cfg)
    recs = ex.read_assignments(cfg)
    recs[3]["arm"] = "statement" if recs[3]["arm"] == "question" else "question"
    cfg.assignments_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    problems = ex.verify(cfg)
    assert any("line 3" in p and "prescribes" in p for p in problems)


def test_verify_catches_an_edited_preregistration(exp):
    cfg = exp()
    _fill(cfg, 4)
    (cfg.dir / "PREREGISTRATION.md").write_text("# pre-registration v2 (edited after launch)\n")
    assert any("pre-registration changed" in p for p in ex.verify(cfg))


def test_verify_catches_an_edited_design_block(exp):
    cfg = exp()
    _fill(cfg, 4)
    edited = ex.load_config(cfg.dir / "config.yaml")
    d = dict(edited.design, planned_n_per_arm=5)
    object.__setattr__(edited, "design", d)
    assert any("design block changed" in p for p in ex.verify(edited))


def test_verify_catches_duplicates_and_index_gaps(exp):
    cfg = exp()
    _fill(cfg, 4)
    recs = ex.read_assignments(cfg)
    recs[2]["video_id"] = recs[0]["video_id"]  # duplicate
    del recs[1]  # leaves a gap at index 1
    cfg.assignments_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    problems = " | ".join(ex.verify(cfg))
    assert "more than once" in problems
    assert "index is" in problems


def test_the_committed_assignment_log_verifies():
    # holds at pre-registration (empty log) and after launch (real log)
    assert ex.verify(ex.load_config(REAL_CONFIG)) == []


# --- the manipulation and its compliance check ------------------------------


def test_hook_line_is_the_spoken_line_for_spoken_modes_and_the_caption_otherwise():
    assert ex.hook_line({"vo_placement": "cold_open", "vo_script": " Hi? "}) == "Hi?"
    assert ex.hook_line({"vo_placement": "pre_punchline", "vo_script": "Hi"}) == "Hi"
    assert ex.hook_line({"vo_placement": "caption_only", "on_screen_caption": "Cap."}) == "Cap."
    assert ex.hook_line({"vo_placement": "cold_open", "vo_script": None}) is None
    assert ex.hook_line({"vo_placement": "caption_only", "on_screen_caption": "  "}) is None


@pytest.mark.parametrize(
    "style, line, expected",
    [
        ("QUESTION", "Who told him?", True),
        ("QUESTION", "Who told him? ", True),
        ("QUESTION", "He already told them.", False),
        ("QUESTION", "Is this real? Wow.", False),  # '?' must END the line
        ("STATEMENT", "He already told them.", True),
        ("STATEMENT", "Who told him?", False),
        ("STATEMENT", "Wait, what? He left.", False),
        ("NONE", "anything", None),
        ("QUESTION", None, None),
    ],
)
def test_check_compliance(style, line, expected):
    assert ex.check_compliance(style, line) is expected


def test_record_selection_logs_compliance_and_title_form(exp):
    cfg = exp()
    q = ex.Enrollment("hook_style", "question", "QUESTION", 0)
    seg = {
        "vo_placement": "cold_open",
        "vo_script": "Who told him?",
        "suggested_title": "Did he really say that?",
    }
    ex.record_selection(q, "clipA", seg)
    ex.record_selection(q, "clipB", None)  # rejected by selection
    ex.record_selection(None, "clipC", seg)  # not enrolled -> nothing logged
    recs = ex.read_selections(cfg)
    assert len(recs) == 2
    a, b = recs
    assert a["accepted"] and a["compliant"] is True and a["title_is_question"] is True
    assert a["hook_line"] == "Who told him?" and a["arm"] == "question"
    assert b["accepted"] is False and b["compliant"] is None and b["hook_line"] is None


# --- integration with selection --------------------------------------------


def _words(n=60):
    return [{"word": f"w{i}", "start": float(i), "end": float(i) + 0.4} for i in range(n)]


def _seed_clip(tmp_path, monkeypatch, video_id, *, views=3_000, duration=60.0):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path / "work")
    wd = tmp_path / "work" / video_id
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "words.json").write_text(
        json.dumps({"language": "en", "duration": duration, "words": _words(60)})
    )
    (wd / "source.info.json").write_text(json.dumps({"view_count": views, "title": "t"}))
    return wd


_RAW = {
    "start": 5.0, "end": 40.0, "punchline_at": 35.0, "tone": "burn", "loopable": False,
    "hook": "a hook", "caption_title": "TITLE",
    "vo_script": "Who told him?", "on_screen_caption": None, "vo_placement": "cold_open",
    "why": "setup at 5.0 establishing X, payoff at 35.0", "confidence": 0.85,
}  # fmt: skip


def _capture(monkeypatch):
    seen = []

    def fake(client, video_id, system_text, user_text, model, **kw):
        seen.append(user_text)
        return {"segments": [dict(_RAW)]}

    monkeypatch.setattr(select_mod, "_call_model", fake)
    return seen


def test_select_clip_puts_the_assigned_arm_in_the_prompt_and_stamps_the_segment(
    exp, tmp_path, monkeypatch
):
    cfg = exp()
    _seed_clip(tmp_path, monkeypatch, "clipA")
    seen = _capture(monkeypatch)
    out = select_clip("clipA")

    logged = ex.lookup(cfg, "clipA")
    style = cfg.arms[logged["arm"]]["hook_style"]
    assert f"Hook style: {style}" in seen[0]
    seg = out["segments"][0]
    assert seg["experiment"] == {"name": "hook_style", "arm": logged["arm"], "index": 0}
    sel = ex.read_selections(cfg)
    assert len(sel) == 1 and sel[0]["accepted"] and sel[0]["arm"] == logged["arm"]


def test_select_clip_without_an_experiment_says_none_and_logs_nothing(tmp_path, monkeypatch):
    _seed_clip(tmp_path, monkeypatch, "clipA")
    seen = _capture(monkeypatch)
    out = select_clip("clipA")
    assert "Hook style: NONE" in seen[0]
    assert "experiment" not in out["segments"][0]


def test_forced_reselection_keeps_the_same_arm(exp, tmp_path, monkeypatch):
    cfg = exp()
    _seed_clip(tmp_path, monkeypatch, "clipA")
    seen = _capture(monkeypatch)
    select_clip("clipA")
    first = ex.lookup(cfg, "clipA")["arm"]
    for _ in range(3):
        select_clip("clipA", force=True)
    assert ex.lookup(cfg, "clipA")["arm"] == first
    assert len(ex.read_assignments(cfg)) == 1
    assert len(set(s.split("Hook style: ")[1].split("\n")[0] for s in seen)) == 1


def test_high_tier_clips_are_not_enrolled(exp, tmp_path, monkeypatch):
    cfg = exp()
    _seed_clip(tmp_path, monkeypatch, "bigclip", views=900_000)
    seen = _capture(monkeypatch)
    select_clip("bigclip")
    assert "Hook style: NONE" in seen[0]
    assert ex.read_assignments(cfg) == []


def test_batch_gives_each_candidate_its_own_arm_in_the_balanced_order(exp, tmp_path, monkeypatch):
    cfg = exp(block_size=2)
    for v in ("c0", "c1", "c2", "c3"):
        _seed_clip(tmp_path, monkeypatch, v)
    captured = {}

    def fake_batch(client, video_ids, system_text, user_text, model):
        captured["user"] = user_text
        captured["system"] = system_text
        return {"results": [{"clip_index": i, "segments": [dict(_RAW)]} for i in range(1, 5)]}

    monkeypatch.setattr(select_mod, "_call_batch_model", fake_batch)
    select_clip_batch(["c0", "c1", "c2", "c3"])

    arms = [ex.lookup(cfg, f"c{i}")["arm"] for i in range(4)]
    assert arms == ex.arm_sequence(cfg, 4)
    assert arms[:2].count("question") == 1 and arms[2:].count("question") == 1  # block of 2
    labels = [s.split("\n")[0] for s in captured["user"].split("Hook style: ")[1:]]
    assert labels == [cfg.arms[a]["hook_style"] for a in arms]
    assert "Hook style (controlled experiment)" in captured["system"]


def test_batch_does_not_enrol_a_clip_that_is_under_the_duration_floor(exp, tmp_path, monkeypatch):
    cfg = exp()
    _seed_clip(tmp_path, monkeypatch, "short", duration=10.0)
    _seed_clip(tmp_path, monkeypatch, "long")
    monkeypatch.setattr(
        select_mod,
        "_call_batch_model",
        lambda *a, **k: {"results": [{"clip_index": 1, "segments": [dict(_RAW)]}]},
    )
    select_clip_batch(["short", "long"])
    enrolled = [r["video_id"] for r in ex.read_assignments(cfg)]
    assert enrolled == ["long"]  # the short clip never took a slot


def test_the_prompt_declares_the_exception_to_the_no_questions_rule():
    p = (paths.PROMPTS_DIR / "select_clip.md").read_text()
    assert "Hook style: {hook_style}" in p
    assert "## Hook style (controlled experiment)" in p
    assert "single deliberate exception" in p
    # the older rule must point at the exception rather than contradict it
    assert "the one exception is\na QUESTION-arm hook line" in p
    assert Path(paths.PROMPTS_DIR / "select_clip.md").exists()
