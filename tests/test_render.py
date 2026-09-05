"""Unit tests for render.py's cache-integrity behavior. ffmpeg is monkeypatched
— this tests the cleanup-on-failure mechanism itself, not real encoding."""

from __future__ import annotations

import json

import pytest

from clipper import paths
from clipper import render as render_mod


def test_render_deletes_output_on_validation_failure(tmp_path, monkeypatch):
    """The cache check (`out_path.exists() and not force`) trusts that an
    existing out_{n}.mp4 is valid. ffmpeg writes the file before validation
    runs, so a validation failure must delete it — otherwise the next run,
    with or without --force, would silently serve back the invalid file.
    """
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "source.mp4").write_bytes(b"fake")
    (wd / "words.json").write_text(json.dumps({"words": []}))

    monkeypatch.setattr(render_mod, "_check_fonts", lambda: None)
    monkeypatch.setattr(render_mod, "style_for_tone", lambda tone: ({}, 3))

    def fake_run_ffmpeg(args):
        (wd / "out_0.mp4").write_bytes(b"fake video bytes")

    def fake_validate_output(path):
        raise ValueError("simulated validation failure")

    monkeypatch.setattr(render_mod, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(render_mod, "_validate_output", fake_validate_output)

    with pytest.raises(ValueError, match="simulated validation failure"):
        render_mod.render("vid1", 0, 0.0, 20.0, "confident")

    assert not (wd / "out_0.mp4").exists()


def test_render_keeps_output_on_success(tmp_path, monkeypatch):
    """Sanity check for the above: the cleanup must only fire on failure —
    a successful render's file must survive."""
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "source.mp4").write_bytes(b"fake")
    (wd / "words.json").write_text(json.dumps({"words": []}))

    monkeypatch.setattr(render_mod, "_check_fonts", lambda: None)
    monkeypatch.setattr(render_mod, "style_for_tone", lambda tone: ({}, 3))
    monkeypatch.setattr(
        render_mod, "run_ffmpeg", lambda args: (wd / "out_0.mp4").write_bytes(b"video")
    )
    monkeypatch.setattr(render_mod, "_validate_output", lambda path: None)

    out = render_mod.render("vid1", 0, 0.0, 20.0, "confident")
    assert out == str(wd / "out_0.mp4")
    assert (wd / "out_0.mp4").exists()
