"""Unit tests for stage 3's pure logic. No network, no LLM, no ffmpeg."""

from __future__ import annotations

import json

import pytest

from clipper import paths
from clipper import select as select_mod
from clipper.select import (
    MIN_DURATION,
    _extract_payoff_marker,
    _extract_payoff_quote,
    _find_phrase_end,
    _snap_end_forward,
    build_windows,
    dedupe,
    format_hmmss,
    parse_timestamps,
    render_prompt,
    render_transcript,
    select_clip,
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


def test_format_hmmss():
    assert format_hmmss(0) == "0:00:00"
    assert format_hmmss(75.4) == "0:01:15"
    assert format_hmmss(3661) == "1:01:01"
    # A 12-hour livestream marker stays small-field h:mm:ss, never a
    # 3-digit minute count the model would have to multiply out.
    assert format_hmmss(43200) == "12:00:00"


def test_render_transcript_inserts_markers():
    out = render_transcript(_words(40), marker_interval=15.0)
    assert "[0:00:00]" in out and "[0:00:15]" in out and "[0:00:30]" in out
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


def test_snap_end_forward_extends_into_containing_word_not_nearest_end():
    # raw_end (10.2) falls inside a long word (10.15-15.0), but a much
    # closer-by-distance word ends right before it (10.0-10.1). Nearest-
    # neighbor would incorrectly backward-snap to 10.1, cutting the long
    # word off entirely before it even started. Forward-bias must not.
    words = [
        {"word": "a", "start": 10.0, "end": 10.1},
        {"word": "verylongword", "start": 10.15, "end": 15.0},
    ]
    assert _snap_end_forward(words, 10.2) == 15.0


def test_snap_end_forward_gap_uses_nearest_at_or_before():
    # raw_end sits in a silence gap between words — nothing to extend into.
    words = [{"word": "a", "start": 1.0, "end": 2.0}, {"word": "b", "start": 5.0, "end": 6.0}]
    assert _snap_end_forward(words, 3.5) == 2.0


def test_snap_end_forward_before_all_words_falls_back_to_nearest():
    words = [{"word": "a", "start": 10.0, "end": 11.0}]
    assert _snap_end_forward(words, 2.0) == 11.0


# --- payoff quote extraction and coverage ------------------------------------


def test_extract_payoff_marker():
    assert _extract_payoff_marker("setup at 1, payoff at 112.0, reaction to 119") == 112.0
    assert _extract_payoff_marker("no payoff mentioned here") is None


def test_extract_payoff_quote_handles_contractions():
    why = "payoff at 112.0 with 'he's not the best, but he's very affordable'; reaction to 119.0"
    assert _extract_payoff_quote(why) == "he's not the best, but he's very affordable"


def test_extract_payoff_quote_none_when_paraphrased():
    assert _extract_payoff_quote("payoff at 50 shows him laughing, no quote given") is None


def test_find_phrase_end_locates_real_completion():
    words = [
        {"word": "he's", "start": 118.34, "end": 119.22},
        {"word": "very", "start": 119.22, "end": 119.30},
        {"word": "affordable.", "start": 119.30, "end": 119.60},
    ]
    assert _find_phrase_end(words, "very affordable", 118.0) == 119.60


def test_find_phrase_end_none_when_not_found():
    words = [{"word": "hello", "start": 0.0, "end": 0.5}]
    assert _find_phrase_end(words, "goodbye world", 0.0) is None


def test_find_phrase_end_ignores_earlier_unrelated_occurrence():
    words = [
        {"word": "very", "start": 5.0, "end": 5.3},
        {"word": "good.", "start": 5.3, "end": 5.6},
        {"word": "he's", "start": 118.0, "end": 118.3},
        {"word": "very", "start": 118.3, "end": 118.5},
        {"word": "good.", "start": 118.5, "end": 118.8},
    ]
    assert _find_phrase_end(words, "very good", 118.0) == 118.8


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


def test_validate_segment_extends_end_to_cover_quoted_payoff():
    # Replicates the real incident: raw end lands short of the payoff quote
    # the model itself names in `why`. Lead words pad past MIN_DURATION.
    lead = [{"word": f"w{i}", "start": float(i), "end": float(i) + 0.3} for i in range(18)]
    payoff_words = [
        {"word": "he's", "start": 18.0, "end": 18.3},
        {"word": "very", "start": 18.3, "end": 18.5},
        {"word": "affordable.", "start": 18.5, "end": 18.8},
    ]
    words = lead + payoff_words
    raw = _raw(
        start=0.0,
        end=18.3,  # would snap to "he's" end — short of the quoted payoff
        punchline_at=17.0,
        why="setup at 0.0, payoff at 18.0 with 'he's very affordable'",
    )
    seg = validate_segment(raw, words)
    assert seg is not None
    assert seg["end"] == 18.8  # extended to cover "affordable."


def test_validate_segment_drops_when_payoff_extension_exceeds_max_duration():
    lead = [{"word": f"w{i}", "start": float(i), "end": float(i) + 0.3} for i in range(60)]
    tail = [
        {"word": "he's", "start": 60.0, "end": 60.3},
        {"word": "very", "start": 60.3, "end": 60.5},
        {"word": "affordable.", "start": 60.5, "end": 60.8},
    ]
    words = lead + tail
    raw = _raw(
        start=0.0,
        end=30.0,
        punchline_at=25.0,
        why="setup at 0.0, payoff at 60.0 with 'he's very affordable'",
    )
    assert validate_segment(raw, words) is None


def test_validate_segment_skips_coverage_check_when_quote_not_found():
    # Model paraphrased rather than quoted verbatim (confirmed to happen on
    # real output) — no reliable check possible, so it must not reject.
    words = _words(60)
    raw = _raw(why="setup at 10.0, payoff at 30.0 with 'a phrase not in the transcript'")
    seg = validate_segment(raw, words)
    assert seg is not None


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


# --- select_clip: no windowing, one call, reuses validate_segment ----------
# _call_model is the only networked seam (like select() itself, which has no
# direct test either — CLAUDE.md: no network in tests) so it's monkeypatched
# throughout, same pattern as narrate.py's tests mock _synthesize.


def _setup_clip(tmp_path, monkeypatch, words, duration=None):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "clipid"
    wd.mkdir(parents=True)
    payload = {
        "language": "en",
        "duration": duration if duration is not None else 40.0,
        "words": words,
    }
    (wd / "words.json").write_text(json.dumps(payload))
    return wd


def test_select_clip_rejects_below_floor_without_calling_model(tmp_path, monkeypatch):
    wd = _setup_clip(tmp_path, monkeypatch, _words(10, step=1.0), duration=10.0)
    calls = []
    monkeypatch.setattr(
        select_mod, "_call_model", lambda *a, **k: calls.append(1) or {"segments": []}
    )

    out = select_clip("clipid")
    assert out["segments"] == []
    assert calls == []  # never called — nothing to trim up to below MIN_DURATION
    assert json.loads((wd / "segments.json").read_text())["segments"] == []


def test_select_clip_calls_model_once_and_validates(tmp_path, monkeypatch):
    words = _words(60, step=1.0)  # 0..59.4s, comfortably above MIN_DURATION
    wd = _setup_clip(tmp_path, monkeypatch, words, duration=60.0)

    raw_segment = {
        "start": 5.0,
        "end": 40.0,
        "punchline_at": 35.0,
        "tone": "burn",
        "loopable": False,
        "hook": "a hook",
        "caption_title": "TITLE",
        "vo_script": "a perfectly reasonable narrator line that frames the clip",
        "vo_placement": "cold_open",
        "why": "setup at 5.0 establishing X, payoff at 35.0",
        "confidence": 0.85,
    }
    calls = []
    monkeypatch.setattr(
        select_mod,
        "_call_model",
        lambda client, prompt, model: calls.append(prompt) or {"segments": [raw_segment]},
    )

    out = select_clip("clipid")
    assert len(calls) == 1
    assert len(out["segments"]) == 1
    seg = out["segments"][0]
    assert seg["n"] == 0
    assert seg["tone"] == "burn"
    # went through the real validate_segment (snapped to word boundaries)
    assert seg["start"] == 5.0
    assert wd.joinpath("segments.json").exists()


def test_select_clip_drops_invalid_segments_from_model(tmp_path, monkeypatch):
    words = _words(60, step=1.0)
    _setup_clip(tmp_path, monkeypatch, words, duration=60.0)
    # missing two timestamps in `why` -> validate_segment drops it
    bad_segment = {
        "start": 5.0,
        "end": 40.0,
        "punchline_at": 35.0,
        "tone": "burn",
        "loopable": False,
        "hook": "a hook",
        "caption_title": "TITLE",
        "vo_script": None,
        "vo_placement": "cold_open",
        "why": "it's just funny",
        "confidence": 0.85,
    }
    monkeypatch.setattr(select_mod, "_call_model", lambda *a, **k: {"segments": [bad_segment]})
    out = select_clip("clipid")
    assert out["segments"] == []


def test_select_clip_cache_hit_skips_model(tmp_path, monkeypatch):
    wd = _setup_clip(tmp_path, monkeypatch, _words(60, step=1.0), duration=60.0)
    (wd / "segments.json").write_text(json.dumps({"video_id": "clipid", "segments": [{"n": 0}]}))
    calls = []
    monkeypatch.setattr(
        select_mod, "_call_model", lambda *a, **k: calls.append(1) or {"segments": []}
    )
    out = select_clip("clipid")
    assert calls == []
    assert out["segments"] == [{"n": 0}]


def test_select_clip_missing_words_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        select_clip("nope")


def test_min_duration_floor_matches_the_hard_range():
    # Sanity: the pre-model reject threshold is the same 18s floor everything
    # else in this module uses, not a separately-tuned number.
    assert MIN_DURATION == 18.0
