"""Unit tests for stage 3's pure logic. No network, no LLM, no ffmpeg."""

from __future__ import annotations

import pytest

from clipper.select import (
    build_windows,
    dedupe,
    format_mmss,
    parse_timestamps,
    render_prompt,
    render_transcript,
    snap_to_words,
    validate_segment,
    why_is_located,
)
from clipper.styles import UnknownToneError


def _words(n=60, step=1.0, start=0.0):
    return [
        {
            "word": f"w{i}",
            "start": round(start + i * step, 2),
            "end": round(start + i * step + 0.4, 2),
        }
        for i in range(n)
    ]


# --- formatting / windowing -------------------------------------------------


def test_format_mmss():
    assert format_mmss(0) == "00:00"
    assert format_mmss(75.4) == "01:15"
    assert format_mmss(3661) == "61:01"  # minutes keep counting past an hour


def test_render_transcript_inserts_markers():
    out = render_transcript(_words(40), marker_interval=15.0)
    assert "[00:00]" in out and "[00:15]" in out and "[00:30]" in out
    assert "w0" in out and "w39" in out


def test_render_transcript_empty():
    assert render_transcript([]) == ""


def test_build_windows_covers_and_overlaps():
    words = _words(1200)  # 1200s of 1-per-second words
    wins = build_windows(words, window_seconds=480, overlap=30)
    assert len(wins) >= 2
    assert wins[0]["index"] == 1
    assert all(w["total"] == len(wins) for w in wins)
    # consecutive windows overlap
    assert wins[1]["start"] < wins[0]["end"]


def test_build_windows_empty():
    assert build_windows([]) == []


def test_build_windows_rejects_overlap_larger_than_window():
    with pytest.raises(ValueError):
        build_windows(_words(10), window_seconds=100, overlap=100)


# --- prompt rendering -------------------------------------------------------


def test_render_prompt_substitutes_without_touching_json_braces():
    tpl = 'Title: {video_title}\nWindow {window_index}\n{"segments": [{"start": 1}]}'
    out = render_prompt(tpl, video_title="Foo", window_index="2")
    assert "Title: Foo" in out
    assert "Window 2" in out
    # the literal JSON example survives untouched
    assert '{"segments": [{"start": 1}]}' in out


def test_render_prompt_rejects_unknown_placeholder():
    with pytest.raises(ValueError):
        render_prompt("hi", not_a_placeholder="x")


# --- why validation ---------------------------------------------------------


def test_parse_timestamps():
    assert parse_timestamps("setup 412.8 ... payoff 437.1") == [412.8, 437.1]


def test_why_is_located_requires_two_timestamps():
    assert why_is_located("setup at 412.8, payoff at 437.1")
    assert not why_is_located("it's funny")
    assert not why_is_located("something happens at 412.8")


# --- snapping ---------------------------------------------------------------


def test_snap_to_words_moves_to_boundaries():
    words = _words(20)  # word i: start=i, end=i+0.4
    start, end = snap_to_words(words, 5.3, 10.1)
    assert start == 5.0  # nearest word start
    assert end == 10.4  # nearest word end (9.4 is 0.7 away, 10.4 is 0.3)


def test_snap_to_words_empty_raises():
    with pytest.raises(ValueError):
        snap_to_words([], 1.0, 2.0)


# --- segment validation -----------------------------------------------------


def _raw(**over):
    base = {
        "start": 10.0,
        "end": 40.0,
        "punchline_at": 35.0,
        "tone": "burn",
        "loopable": False,
        "hook": "a hook",
        "caption_title": "TITLE",
        "vo_script": "a perfectly reasonable narrator line that frames the clip",
        "vo_placement": "cold_open",
        "why": "setup at 10.0 establishing X, payoff at 35.0",
        "confidence": 0.85,
    }
    base.update(over)
    return base


def test_validate_segment_accepts_good_segment():
    seg = validate_segment(_raw(), _words(60))
    assert seg is not None
    assert seg["tone"] == "burn"
    assert seg["start"] == 10.0
    assert seg["loopable"] is False


def test_validate_segment_passes_through_loopable_true():
    seg = validate_segment(_raw(loopable=True), _words(60))
    assert seg is not None
    assert seg["loopable"] is True


def test_validate_segment_defaults_loopable_false_when_missing():
    raw = _raw()
    del raw["loopable"]
    seg = validate_segment(raw, _words(60))
    assert seg is not None
    assert seg["loopable"] is False


def test_validate_segment_drops_vague_why():
    assert validate_segment(_raw(why="it's really funny"), _words(60)) is None


def test_validate_segment_drops_low_confidence():
    assert validate_segment(_raw(confidence=0.5), _words(60)) is None


def test_validate_segment_drops_out_of_range_duration():
    # 10 -> 20 is only 10s, under the 18s floor
    assert validate_segment(_raw(end=20.0, punchline_at=19.0), _words(60)) is None


def test_validate_segment_drops_early_punchline():
    # punchline at 15 is in the first third of 10-40
    assert validate_segment(_raw(punchline_at=15.0), _words(60)) is None


def test_validate_segment_drops_bad_placement():
    assert validate_segment(_raw(vo_placement="whenever"), _words(60)) is None


def test_validate_segment_unknown_tone_raises_loudly():
    # Not a silent drop: drift between prompt and styles.yaml must surface.
    with pytest.raises(UnknownToneError):
        validate_segment(_raw(tone="hilarious"), _words(60))


def test_validate_segment_nulls_overlong_vo_but_keeps_segment():
    seg = validate_segment(_raw(vo_script=" ".join(["word"] * 40)), _words(60))
    assert seg is not None
    assert seg["vo_script"] is None


def test_validate_segment_allows_null_vo():
    seg = validate_segment(_raw(vo_script=None), _words(60))
    assert seg is not None
    assert seg["vo_script"] is None


# --- dedupe -----------------------------------------------------------------


def test_dedupe_keeps_higher_confidence_of_overlapping_pair():
    a = {"start": 10.0, "end": 40.0, "confidence": 0.8}
    b = {"start": 12.0, "end": 42.0, "confidence": 0.9}
    kept = dedupe([a, b])
    assert len(kept) == 1
    assert kept[0]["confidence"] == 0.9


def test_dedupe_keeps_disjoint_segments():
    a = {"start": 10.0, "end": 40.0, "confidence": 0.8}
    b = {"start": 100.0, "end": 130.0, "confidence": 0.7}
    kept = dedupe([a, b])
    assert len(kept) == 2
    assert [s["start"] for s in kept] == [10.0, 100.0]  # sorted by start
