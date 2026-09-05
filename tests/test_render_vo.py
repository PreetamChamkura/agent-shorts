"""Unit tests for render.py's pure VO-mixing helpers. No ffmpeg, no network.

VO and source speech are never audible at the same time (two overlapping
voices don't work at any relative volume), but the video never freezes for
it — both placements now share the same audio mechanism (silence-and-mix
window, tested via _audio_tail), differing only in where the window sits
(_pre_punchline_start_seconds) and whether the VO itself gets captioned
(_merge_vo_and_clip_captions, cold_open only).
"""

from __future__ import annotations

import pytest

from clipper.render import _audio_tail, _merge_vo_and_clip_captions, _pre_punchline_start_seconds

# --- shared silence-and-mix window (both placements) -------------------------


def test_pre_punchline_start_leads_by_1_2s():
    # punchline at absolute 137.1, clip starts at 100 -> clip-relative 37.1
    assert _pre_punchline_start_seconds(clip_start=100.0, punchline_at=137.1) == pytest.approx(35.9)


def test_pre_punchline_start_clamps_to_zero():
    # punchline 0.5s into the clip minus 1.2s lead would be negative
    assert _pre_punchline_start_seconds(clip_start=100.0, punchline_at=100.5) == 0.0


def test_audio_tail_no_vo_is_plain_loudnorm():
    tail = _audio_tail(None)
    assert tail == "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"


def test_audio_tail_is_full_silence_not_ducked():
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


def test_audio_tail_cold_open_window_starts_at_zero():
    # cold_open's window is the clip's own opening seconds: start=0.
    tail = _audio_tail({"input_index": 1, "start": 0.0, "end": 4.5})
    assert "[1:a]adelay=0|0[vodelayed]" in tail
    assert "volume=0:enable='between(t,0.0,4.5)'" in tail


def test_audio_tail_vo_input_index_matches_split_screen_offset():
    # split_screen already occupies input 1 (gameplay), so VO must be input 2.
    tail = _audio_tail({"input_index": 2, "start": 0.0, "end": 8.0})
    assert "[2:a]adelay=0|0[vodelayed]" in tail


# --- VO captioning during the cold_open window -------------------------------


def test_merge_vo_and_clip_captions_includes_vo_words():
    vo_words = [{"word": "Hey.", "start": 0.0, "end": 0.5}]
    clip_words = [{"word": "later", "start": 10.0, "end": 10.4}]
    ass = _merge_vo_and_clip_captions(vo_words, clip_words, vo_duration=4.5, group_size=3, style={})
    assert "HEY." in ass
    assert "LATER" in ass


def test_merge_vo_and_clip_captions_drops_clip_words_during_vo_window():
    # A clip word starting inside the VO window is silenced audio — it must
    # not be captioned, that would show text for audio the viewer can't hear.
    vo_words = [{"word": "Hey.", "start": 0.0, "end": 0.5}]
    clip_words = [
        {"word": "silenced", "start": 1.0, "end": 1.5},  # inside [0, 4.5)
        {"word": "audible", "start": 5.0, "end": 5.4},  # after the window
    ]
    ass = _merge_vo_and_clip_captions(vo_words, clip_words, vo_duration=4.5, group_size=3, style={})
    assert "SILENCED" not in ass
    assert "AUDIBLE" in ass


def test_merge_vo_and_clip_captions_keeps_clip_word_exactly_at_boundary():
    vo_words = [{"word": "Hey.", "start": 0.0, "end": 0.5}]
    clip_words = [{"word": "right_at_boundary", "start": 4.5, "end": 4.9}]
    ass = _merge_vo_and_clip_captions(vo_words, clip_words, vo_duration=4.5, group_size=3, style={})
    assert "RIGHT_AT_BOUNDARY" in ass


def test_merge_vo_and_clip_captions_never_straddles_boundary_in_one_group():
    # Even with group_size=3 and no sentence-ending punctuation on the VO's
    # last word, the VO's words and the clip's resumed words must land in
    # separate phrase groups, not merged into one Dialogue line's text.
    vo_words = [
        {"word": "no", "start": 0.0, "end": 0.3},
        {"word": "period", "start": 0.3, "end": 0.6},
    ]
    clip_words = [
        {"word": "clip", "start": 4.5, "end": 4.8},
        {"word": "word", "start": 4.8, "end": 5.0},
    ]
    ass = _merge_vo_and_clip_captions(vo_words, clip_words, vo_duration=4.5, group_size=3, style={})
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    # No single line's text contains both a VO word and a clip word.
    for ln in dialogues:
        text = ln.split(",,", 1)[1]
        has_vo = "NO" in text or "PERIOD" in text
        has_clip = "CLIP" in text or "WORD" in text
        assert not (has_vo and has_clip), f"VO and clip words mixed in one group: {ln!r}"


def test_merge_vo_and_clip_captions_one_header_not_two():
    vo_words = [{"word": "Hey.", "start": 0.0, "end": 0.5}]
    clip_words = [{"word": "later", "start": 10.0, "end": 10.4}]
    ass = _merge_vo_and_clip_captions(vo_words, clip_words, vo_duration=4.5, group_size=3, style={})
    assert ass.count("[Script Info]") == 1
    assert ass.count("[Events]") == 1


def test_merge_vo_and_clip_captions_empty_clip_words():
    vo_words = [{"word": "Hey.", "start": 0.0, "end": 0.5}]
    ass = _merge_vo_and_clip_captions(vo_words, [], vo_duration=4.5, group_size=3, style={})
    assert "HEY." in ass
