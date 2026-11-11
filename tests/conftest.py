"""Shared fixtures."""

from __future__ import annotations

import pytest

from clipper import paths


@pytest.fixture(autouse=True)
def _isolate_experiments_dir(tmp_path_factory, monkeypatch):
    """Point every test at an empty experiments dir.

    The real experiments/ holds the pre-registered config and append-only
    assignment logs. Selection code writes to them whenever an experiment is
    active, so a test that reaches select_clip must never be able to see the
    real directory. Tests of the experiment machinery itself build their own.
    """
    monkeypatch.setattr(paths, "EXPERIMENTS_DIR", tmp_path_factory.mktemp("no_experiments"))
