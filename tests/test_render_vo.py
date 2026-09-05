"""Unit tests for render.py's pure VO-mixing helpers. No ffmpeg, no network.

VO and source speech are never audible at the same time (two overlapping
voices don't work at any relative volume): cold_open gets a real pre-roll
splice (tested via _freeze_preroll_video / _cold_open_audio_tail / the
caption-shifting helper), pre_punchline hard-cuts the source to silence
(tested via _pre_punchline_start_seconds / _audio_tail).
"""

from __future__ import annotations

import pytest

from clipper.render import (
    _audio_tail,
    _cold_open_audio_tail,
    _freeze_preroll_video,
    _pre_punchline_start_seconds,
    _shift_words,
)

# --- pre_punchline: hard cut to silence, no video change --------------------


def test_pre_punchline_start_leads_by_1_2s():
    # punchline at absolute 137.1, clip starts at 100 -> clip-relative 37.1
    assert _pre_punchline_start_seconds(clip_start=100.0, punchline_at=137.1) == pytest.approx(35.9)


def test_pre_punchline_start_clamps_to_zero():
    # punchline 0.5s into the clip minus 1.2s lead would be negative
    assert _pre_punchline_start_seconds(clip_start=100.0, punchline_at=100.5) == 0.0


def test_audio_tail_no_vo_is_plain_loudnorm():
    tail = _audio_tail(None)
    assert tail == "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"


def test_audio_tail_pre_punchline_is_full_silence_not_ducked():
    tail = _audio_tail({"input_index": 1, "start": 5.0, "end": 15.0})
    assert "[1:a]adelay=5000|5000[vodelayed]" in tail
    # volume=0 (hard cut), not a partial duck like 0.35 — two overlapping
    # voices don't work at any relative volume.
    assert "volume=0:enable='between(t,5.0,15.0)'" in tail
    assert "0.35" not in tail
    assert "amix=inputs=2" in tail
    # loudnorm must come after amix, never before (CLAUDE.md)
    assert tail.index("amix") < tail.index("loudnorm")
    assert tail.endswith("[aout]")


def test_audio_tail_vo_input_index_matches_split_screen_offset():
    # split_screen already occupies input 1 (gameplay), so VO must be input 2.
    tail = _audio_tail({"input_index": 2, "start": 0.0, "end": 8.0})
    assert "[2:a]adelay=0|0[vodelayed]" in tail


# --- cold_open: real pre-roll, VO plays before the clip, not under it -------


def test_freeze_preroll_video_freezes_first_frame_of_comp():
    fc = _freeze_preroll_video(4.5)
    assert "[comp]split=2[compA][compB]" in fc
    assert "[compA]loop=loop=-1:size=1:start=0" in fc
    assert "trim=duration=4.500" in fc
    assert "[frozen][compB]concat=n=2:v=1:a=0[precomp]" in fc


def test_freeze_preroll_video_dims_and_blurs():
    fc = _freeze_preroll_video(3.0)
    assert "eq=brightness=-0.2" in fc
    assert "gblur=sigma=12" in fc


def test_cold_open_audio_tail_concatenates_not_mixes():
    fc = _cold_open_audio_tail(vo_input_index=1)
    assert "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[voa]" in fc
    assert "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[srca]" in fc
    assert "[voa][srca]concat=n=2:v=0:a=1" in fc
    # never a mix/duck for cold_open — the VO and source audio never overlap
    assert "amix" not in fc
    assert "volume=" not in fc
    assert fc.index("concat") < fc.index("loudnorm")
    assert fc.endswith("[aout]")


def test_cold_open_audio_tail_input_index_matches_split_screen_offset():
    fc = _cold_open_audio_tail(vo_input_index=2)
    assert "[2:a]aformat" in fc


# --- caption shifting for the cold_open pre-roll -----------------------------


def test_shift_words_moves_every_word():
    words = [{"word": "hi", "start": 1.0, "end": 1.5}, {"word": "there", "start": 1.5, "end": 2.0}]
    shifted = _shift_words(words, 3.0)
    assert shifted == [
        {"word": "hi", "start": 4.0, "end": 4.5},
        {"word": "there", "start": 4.5, "end": 5.0},
    ]


def test_shift_words_zero_offset_returns_same_list():
    words = [{"word": "hi", "start": 1.0, "end": 1.5}]
    assert _shift_words(words, 0.0) is words


def test_shift_words_empty():
    assert _shift_words([], 5.0) == []
