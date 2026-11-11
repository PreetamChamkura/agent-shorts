"""The analysis notebook must execute cleanly in both states it will meet:
no data yet (today) and a finished experiment (later). Executed for real with
nbclient against synthetic experiment directories — nothing here is a result."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import numpy as np
import pytest
import yaml

from clipper import experiment as ex
from clipper import paths

nbformat = pytest.importorskip("nbformat")
nbclient = pytest.importorskip("nbclient")
pytest.importorskip("scipy")

NOTEBOOK = paths.REPO_ROOT / "experiments" / "hook_style" / "analysis.ipynb"


def _experiment_dir(tmp_path: Path, monkeypatch) -> ex.Config:
    root = tmp_path / "experiments"
    d = root / "hook_style"
    d.mkdir(parents=True)
    real = yaml.safe_load((paths.REPO_ROOT / "experiments/hook_style/config.yaml").read_text())
    real["status"] = "active"
    real["launch_date"] = "2026-12-01"
    real["design"]["blinded_reestimate_at_n"] = 10_000
    (d / "config.yaml").write_text(yaml.safe_dump(real))
    shutil.copy(paths.REPO_ROOT / "experiments/hook_style/PREREGISTRATION.md", d)
    (d / "outcomes.csv").write_text(
        "video_id,youtube_id,published_at,snapshot_date,views,stayed_to_watch_pct,"
        "avg_view_duration_s,avg_view_pct,notes\n"
    )
    monkeypatch.setattr(paths, "EXPERIMENTS_DIR", root)
    return ex.load_config(d / "config.yaml")


def _execute(exp_dir: Path, monkeypatch):
    monkeypatch.setenv("EXPERIMENT_DIR", str(exp_dir))
    nb = nbformat.read(NOTEBOOK, as_version=4)
    nbclient.NotebookClient(nb, timeout=120, kernel_name="python3").execute()
    return "\n".join(
        out.get("text", "")
        for cell in nb.cells
        if cell.cell_type == "code"
        for out in cell.outputs
        if out.output_type == "stream"
    )


def test_committed_notebook_has_no_outputs_or_execution_counts():
    nb = nbformat.read(NOTEBOOK, as_version=4)
    for cell in nb.cells:
        if cell.cell_type == "code":
            assert cell.outputs == [] and cell.execution_count is None


def test_notebook_runs_in_the_empty_state(tmp_path, monkeypatch):
    cfg = _experiment_dir(tmp_path, monkeypatch)
    out = _execute(cfg.dir, monkeypatch)
    assert "NO DECISION" in out
    assert "No primary result yet" in out
    assert "SHIP" not in out


def test_notebook_runs_on_a_finished_synthetic_experiment(tmp_path, monkeypatch):
    cfg = _experiment_dir(tmp_path, monkeypatch)
    rng = np.random.default_rng(0)
    with (cfg.dir / "outcomes.csv").open("a", newline="") as f:
        w = csv.writer(f)
        for i in range(60):
            rec = ex.assign(cfg, f"clip{i:03d}")
            style = cfg.arms[rec["arm"]]["hook_style"]
            seg = {
                "vo_placement": "cold_open",
                "vo_script": "Who?" if style == "QUESTION" else "Him.",
                "suggested_title": "t",
            }
            ex.record_selection(
                ex.Enrollment(cfg.name, rec["arm"], style, rec["index"]), rec["video_id"], seg
            )
            mu = 50 + (20 if rec["arm"] == "question" else 0)
            w.writerow(
                [rec["video_id"], "yt", "2026-12-02", "2026-12-09", int(rng.integers(50, 4000)),
                 f"{np.clip(rng.normal(mu, 6), 0, 100):.1f}", "20", "60", ""]
            )  # fmt: skip
    out = _execute(cfg.dir, monkeypatch)
    assert "OK — 60 assignment(s) match the seed" in out
    assert "SHIP QUESTION" in out
