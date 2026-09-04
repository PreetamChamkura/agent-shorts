"""Unit tests for CLI arg resolution. No network, no ffmpeg."""

from __future__ import annotations

import argparse
import json

import pytest

from clipper import paths
from clipper.cli import _manual_spec, _segment_from_file, _vo_kwargs


def _args(**over):
    base = {"start": None, "end": None, "tone": None, "n": 0}
    base.update(over)
    return argparse.Namespace(**base)


def test_manual_spec_true_when_all_three_given():
    assert _manual_spec(_args(start=1.0, end=30.0, tone="burn")) is True


def test_manual_spec_false_when_none_given():
    assert _manual_spec(_args()) is False


@pytest.mark.parametrize(
    "partial",
    [
        {"start": 1.0},
        {"end": 30.0},
        {"tone": "burn"},
        {"start": 1.0, "end": 30.0},  # missing tone
    ],
)
def test_manual_spec_rejects_partial_set(partial):
    # A half-manual spec is an error, not a guess.
    with pytest.raises(SystemExit):
        _manual_spec(_args(**partial))


def _seed_segments(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "segments.json").write_text(json.dumps({"video_id": "vid1", "segments": segments}))


def test_segment_from_file_reads_the_right_index(tmp_path, monkeypatch):
    _seed_segments(
        tmp_path,
        monkeypatch,
        [
            {"n": 0, "start": 10.0, "end": 40.0, "tone": "burn"},
            {"n": 1, "start": 100.0, "end": 130.0, "tone": "wholesome"},
        ],
    )
    seg = _segment_from_file("vid1", 1)
    assert (seg["start"], seg["end"], seg["tone"]) == (100.0, 130.0, "wholesome")


def test_segment_from_file_missing_file_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    with pytest.raises(SystemExit, match="segments.json"):
        _segment_from_file("nope", 0)


def test_segment_from_file_missing_index_fails_loudly(tmp_path, monkeypatch):
    _seed_segments(tmp_path, monkeypatch, [{"n": 0, "start": 1.0, "end": 20.0, "tone": "burn"}])
    with pytest.raises(SystemExit, match="no segment n=5"):
        _segment_from_file("vid1", 5)


# --- VO kwarg derivation -----------------------------------------------------


def test_vo_kwargs_empty_when_vo_script_null():
    assert _vo_kwargs({"vo_script": None}) == {}


def test_vo_kwargs_empty_when_tts_failed():
    seg = {
        "vo_script": "line",
        "tts_failed": True,
        "punchline_at": 10.0,
        "vo_placement": "cold_open",
    }
    assert _vo_kwargs(seg) == {}


def test_vo_kwargs_present_for_good_segment():
    seg = {
        "vo_script": "a line",
        "punchline_at": 37.1,
        "vo_placement": "pre_punchline",
    }
    assert _vo_kwargs(seg) == {"punchline_at": 37.1, "vo_placement": "pre_punchline"}
