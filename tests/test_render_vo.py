"""Unit tests for render.py's pure VO-mixing helpers. No ffmpeg, no network."""

from __future__ import annotations

import pytest

from clipper.render import _audio_tail, _vo_start_seconds


def test_vo_start_cold_open_is_always_zero():
    assert _vo_start_seconds("cold_open", clip_start=100.0, punchline_at=137.0) == 0.0


def test_vo_start_pre_punchline_leads_by_1_2s():
    # punchline at absolute 137.1, clip starts at 100 -> clip-relative 37.1
    assert _vo_start_seconds(
        "pre_punchline", clip_start=100.0, punchline_at=137.1
    ) == pytest.approx(35.9)


def test_vo_start_pre_punchline_clamps_to_zero():
    # punchline 0.5s into the clip minus 1.2s lead would be negative
    assert _vo_start_seconds("pre_punchline", clip_start=100.0, punchline_at=100.5) == 0.0


def test_vo_start_rejects_unknown_placement():
    with pytest.raises(ValueError):
        _vo_start_seconds("whenever", clip_start=0.0, punchline_at=10.0)


def test_audio_tail_no_vo_is_plain_loudnorm():
    tail = _audio_tail(None)
    assert tail == "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"


def test_audio_tail_with_vo_ducks_mixes_then_loudnorms():
    tail = _audio_tail({"input_index": 1, "start": 5.0, "end": 15.0})
    assert "[1:a]adelay=5000|5000[vodelayed]" in tail
    assert "volume=0.35:enable='between(t,5.0,15.0)'" in tail
    assert "amix=inputs=2" in tail
    # loudnorm must come after amix, never before (CLAUDE.md)
    assert tail.index("amix") < tail.index("loudnorm")
    assert tail.endswith("[aout]")


def test_audio_tail_vo_input_index_matches_split_screen_offset():
    # split_screen already occupies input 1 (gameplay), so VO must be input 2.
    tail = _audio_tail({"input_index": 2, "start": 0.0, "end": 8.0})
    assert "[2:a]adelay=0|0[vodelayed]" in tail
