"""`clipper experiment ...` end to end, against a temp experiments dir."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("scipy")

import yaml

from clipper import cli, paths
from clipper import experiment as ex


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    d = root / "hook_style"
    d.mkdir(parents=True)
    design = {
        "seed": 7, "block_size": 4, "control_arm": "statement",
        "arms": {
            "statement": {"weight": 1, "hook_style": "STATEMENT"},
            "question": {"weight": 1, "hook_style": "QUESTION"},
        },
        "eligible_tier": "STANDARD", "primary_metric": "stayed_to_watch_pct",
        "measurement_window_days": [6, 10], "mde_points": 10, "planning_sd": 12,
        "alpha": 0.05, "power": 0.8, "planned_n_per_arm": 26,
        "max_duration_weeks": 26, "blinded_reestimate_at_n": 16,
    }  # fmt: skip
    (d / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "hook_style",
                "status": "active",
                "launch_date": "2026-12-01",
                "preregistration": "PREREGISTRATION.md",
                "design": design,
            }
        )
    )
    (d / "PREREGISTRATION.md").write_text("pre-reg")
    (d / "outcomes.csv").write_text(
        "video_id,youtube_id,published_at,snapshot_date,views,stayed_to_watch_pct,"
        "avg_view_duration_s,avg_view_pct,notes\n"
    )
    monkeypatch.setattr(paths, "EXPERIMENTS_DIR", root)
    return ex.load_config(d / "config.yaml")


def test_verify_ok_on_a_clean_log(cfg, capsys):
    ex.assign(cfg, "a")
    ex.assign(cfg, "b")
    cli.main(["experiment", "verify"])
    assert "OK — 2 assignment(s)" in capsys.readouterr().out


def test_verify_exits_nonzero_and_names_the_problem_when_the_log_is_tampered(cfg, capsys):
    for v in "abcd":
        ex.assign(cfg, v)
    recs = ex.read_assignments(cfg)
    recs[1]["arm"] = "statement" if recs[1]["arm"] == "question" else "question"
    cfg.assignments_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    with pytest.raises(SystemExit) as e:
        cli.main(["experiment", "verify"])
    assert e.value.code == 1
    assert "prescribes" in capsys.readouterr().out


def test_power_prints_the_planned_n_and_weeks(capsys):
    cli.main(["experiment", "power", "--mde", "10", "--sd", "12", "--rates", "2", "3"])
    out = capsys.readouterr().out
    assert "rank test:     26 per arm (52 total)" in out
    assert "2 clip(s)/week -> 29 weeks" in out and "3 clip(s)/week -> 19 weeks" in out


def test_status_reports_the_funnel_and_integrity(cfg, capsys):
    for v in "abcd":
        ex.assign(cfg, v)
    cli.main(["experiment", "status"])
    out = capsys.readouterr().out
    assert "status: active" in out and "assigned" in out
    assert "log integrity: OK" in out and "planned: 26 measured clips per arm" in out


def test_analyze_with_no_data_writes_an_honest_no_decision(cfg, capsys):
    cli.main(["experiment", "analyze"])
    assert "NO DECISION" in capsys.readouterr().out
    memo = (cfg.dir / "decision_memo.md").read_text()
    assert "NO DECISION" in memo and "SYNTHETIC" not in memo
    saved = json.loads((cfg.dir / "results.json").read_text())
    assert saved["decision"]["decision"] == "no_decision"


def test_analyze_refuses_to_run_on_a_log_that_fails_verification(cfg, capsys):
    for v in "abcd":
        ex.assign(cfg, v)
    (cfg.dir / "PREREGISTRATION.md").write_text("edited after launch")
    with pytest.raises(SystemExit) as e:
        cli.main(["experiment", "analyze"])
    assert e.value.code == 1
    assert "refusing to analyze" in capsys.readouterr().out
    assert not (cfg.dir / "decision_memo.md").exists()


def test_unknown_experiment_name_is_a_clear_error(cfg):
    with pytest.raises(SystemExit, match="no experiment named"):
        cli.main(["experiment", "--name", "nope", "verify"])
