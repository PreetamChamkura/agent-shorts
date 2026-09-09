"""Unit tests for stage 3's pure logic. No network, no LLM, no ffmpeg."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from clipper import paths
from clipper import select as select_mod
from clipper.select import (
    CAPTION_ONLY,
    DESCRIPTION_MAX_SENTENCES,
    DESCRIPTION_MIN_SENTENCES,
    MIN_DURATION,
    PROMPTS_DIR,
    PUNCHLINE_DRIFT_TOLERANCE,
    _count_sentences,
    _distance_from_span,
    _extract_payoff_marker,
    _extract_payoff_quote,
    _find_phrase_end,
    _find_phrase_span,
    _log_usage,
    _snap_end_forward,
    build_windows,
    dedupe,
    format_hmmss,
    parse_timestamps,
    render_prompt,
    render_transcript,
    resolve_narration,
    select_clip,
    snap_to_words,
    split_cacheable_prompt,
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


# --- split_cacheable_prompt: real prompt-caching split ----------------------


def _templated(dynamic="Source stuff {video_title}", static="## Task\nDo the thing."):
    return (
        f"{dynamic}\n\n"
        "<!-- PROMPT_CACHE_BOUNDARY: everything below this line is fixed -->\n\n"
        f"{static}"
    )


def test_split_cacheable_prompt_splits_at_the_marker():
    dynamic, static = split_cacheable_prompt(_templated())
    assert "{video_title}" in dynamic
    assert "PROMPT_CACHE_BOUNDARY" not in dynamic
    assert "PROMPT_CACHE_BOUNDARY" not in static
    assert "## Task" in static


def test_split_cacheable_prompt_raises_without_a_marker():
    with pytest.raises(ValueError, match="no.*marker"):
        split_cacheable_prompt("no marker anywhere in here")


def test_split_cacheable_prompt_raises_on_unclosed_comment():
    broken = "stuff\n<!-- PROMPT_CACHE_BOUNDARY: never closes\nmore stuff"
    with pytest.raises(ValueError, match="never closes"):
        split_cacheable_prompt(broken)


def test_split_cacheable_prompt_raises_on_placeholder_after_the_boundary():
    # A placeholder below the marker would ship literal "{video_title}" text
    # on every cached call instead of the real value -- must fail loudly.
    leaky = _templated(static="## Task\nUse {video_title} here.")
    with pytest.raises(ValueError, match="video_title"):
        split_cacheable_prompt(leaky)


def test_split_cacheable_prompt_real_prompt_files_split_cleanly():
    # The actual shipped prompts -- not a synthetic fixture. Every real
    # placeholder must land above the boundary in both.
    for name in ("select.md", "select_clip.md"):
        template = (PROMPTS_DIR / name).read_text()
        dynamic, static = split_cacheable_prompt(template)
        assert "## Task" in static
        assert "{transcript_window}" in dynamic
        assert "{transcript_window}" not in static


# --- _log_usage: real per-call cost visibility ------------------------------
# Never exercised through a mocked _call_model (every test above replaces it
# wholesale), so this is the only coverage this logging line gets.


def _fake_usage(**over):
    base = dict(
        input_tokens=6581,
        output_tokens=629,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        output_tokens_details=SimpleNamespace(thinking_tokens=1200),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_log_usage_logs_the_real_split(caplog):
    caplog.set_level(logging.INFO)
    _log_usage("vid1", "claude-opus-5", _fake_usage())
    assert "in=6581" in caplog.text
    assert "out=629" in caplog.text
    assert "thinking=1200" in caplog.text
    assert "claude-opus-5" in caplog.text
    assert "vid1" in caplog.text


def test_log_usage_reports_cache_read_and_write(caplog):
    caplog.set_level(logging.INFO)
    _log_usage(
        "vid1",
        "claude-opus-5",
        _fake_usage(cache_read_input_tokens=6372, cache_creation_input_tokens=6372),
    )
    assert "cache_read=6372" in caplog.text
    assert "cache_write=6372" in caplog.text


def test_log_usage_handles_missing_output_tokens_details():
    # Defensive: some SDK responses (or a future API change) could omit
    # output_tokens_details entirely -- must not crash the whole call over
    # a logging line.
    usage = _fake_usage(output_tokens_details=None)
    _log_usage("vid1", "claude-opus-5", usage)  # must not raise


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


# --- _find_phrase_span: start+end of the SAME located occurrence -----------


def test_find_phrase_span_returns_start_and_end_of_one_match():
    words = [
        {"word": "he's", "start": 117.30, "end": 117.52},
        {"word": "not", "start": 117.52, "end": 117.64},
        {"word": "the", "start": 117.64, "end": 117.80},
        {"word": "best,", "start": 117.80, "end": 118.04},
    ]
    assert _find_phrase_span(words, "he's not the best", 117.0) == (117.30, 118.04)


def test_find_phrase_span_none_when_not_found():
    words = [{"word": "hello", "start": 0.0, "end": 0.5}]
    assert _find_phrase_span(words, "goodbye world", 0.0) is None


def test_find_phrase_end_and_span_agree_on_the_same_occurrence():
    # Real regression risk of the refactor: _find_phrase_end must still
    # return exactly the span's own end, not a separately-computed value.
    words = [
        {"word": "very", "start": 5.0, "end": 5.3},
        {"word": "good.", "start": 5.3, "end": 5.6},
        {"word": "he's", "start": 118.0, "end": 118.3},
        {"word": "very", "start": 118.3, "end": 118.5},
        {"word": "good.", "start": 118.5, "end": 118.8},
    ]
    span = _find_phrase_span(words, "very good", 118.0)
    assert span == (118.3, 118.8)
    assert _find_phrase_end(words, "very good", 118.0) == span[1]


# --- _distance_from_span ----------------------------------------------------


def test_distance_from_span_zero_when_inside():
    assert _distance_from_span(5.0, 3.0, 8.0) == 0.0


def test_distance_from_span_before_start():
    assert _distance_from_span(1.0, 3.0, 8.0) == 2.0


def test_distance_from_span_after_end():
    assert _distance_from_span(10.0, 3.0, 8.0) == 2.0


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


# --- punchline_at validated against the payoff phrase's own real span -----
# Real bug found from real output: `end` already covered the quoted payoff
# phrase, but punchline_at itself sat 5.3s before the phrase's real start —
# invisible to the end-coverage check above, since that only confirms the
# SEGMENT boundary, not this specific timestamp. Feeds real downstream
# mistakes: pre_punchline VO timing anchors 1.2s before punchline_at, and
# clipper.thumbnail's fallback frame search centers on it.


def test_validate_segment_corrects_drifted_punchline_at_to_phrase_start():
    lead = [{"word": f"w{i}", "start": float(i), "end": float(i) + 0.3} for i in range(30)]
    payoff_words = [
        {"word": "he's", "start": 35.0, "end": 35.3},
        {"word": "very", "start": 35.3, "end": 35.5},
        {"word": "affordable.", "start": 35.5, "end": 35.8},
    ]
    words = lead + payoff_words
    raw = _raw(
        start=0.0,
        end=35.8,
        punchline_at=25.0,  # 10s before the phrase's real start (35.0) — past tolerance
        why="setup at 0.0, payoff at 25.0 with 'he's very affordable'",
    )
    seg = validate_segment(raw, words)
    assert seg is not None
    assert seg["punchline_at"] == 35.0  # corrected to the phrase's own start


def test_validate_segment_leaves_punchline_at_within_tolerance_unchanged():
    lead = [{"word": f"w{i}", "start": float(i), "end": float(i) + 0.3} for i in range(30)]
    payoff_words = [
        {"word": "he's", "start": 35.0, "end": 35.3},
        {"word": "very", "start": 35.3, "end": 35.5},
        {"word": "affordable.", "start": 35.5, "end": 35.8},
    ]
    words = lead + payoff_words
    raw = _raw(
        start=0.0,
        end=35.8,
        punchline_at=34.0,  # 1.0s before the phrase start — within PUNCHLINE_DRIFT_TOLERANCE
        why="setup at 0.0, payoff at 34.0 with 'he's very affordable'",
    )
    assert PUNCHLINE_DRIFT_TOLERANCE >= 1.0
    seg = validate_segment(raw, words)
    assert seg is not None
    assert seg["punchline_at"] == 34.0  # left as-is, not snapped to 35.0


def test_validate_segment_leaves_punchline_at_inside_phrase_span_unchanged():
    lead = [{"word": f"w{i}", "start": float(i), "end": float(i) + 0.3} for i in range(30)]
    payoff_words = [
        {"word": "he's", "start": 35.0, "end": 35.3},
        {"word": "very", "start": 35.3, "end": 35.5},
        {"word": "affordable.", "start": 35.5, "end": 35.8},
    ]
    words = lead + payoff_words
    raw = _raw(
        start=0.0,
        end=35.8,
        punchline_at=35.3,  # already inside the phrase's own span
        why="setup at 0.0, payoff at 35.3 with 'he's very affordable'",
    )
    seg = validate_segment(raw, words)
    assert seg is not None
    assert seg["punchline_at"] == 35.3


def test_validate_segment_skips_punchline_check_when_quote_not_found():
    # Paraphrased why (no locatable quote) — nothing to validate punchline_at
    # against, so it must pass through completely untouched.
    seg = validate_segment(
        _raw(why="setup at 10.0, payoff at 30.0 with 'a phrase not in the transcript'"),
        _words(60),
    )
    assert seg is not None
    assert seg["punchline_at"] == 35.0  # _raw()'s default, unmodified


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


# --- thumbnail + metadata fields (CLAUDE.md thumbnail generation step) -----


def test_validate_segment_passes_through_thumbnail_fields():
    seg = validate_segment(
        _raw(
            thumbnail_text="I DIDN'T EVEN SEE HIM",
            suggested_title="He ruled this out three weeks before it happened",
            suggested_hashtags=["#valorant", "#clutch", "#shroud"],
        ),
        _words(60),
    )
    assert seg is not None
    assert seg["thumbnail_text"] == "I DIDN'T EVEN SEE HIM"
    assert seg["suggested_title"] == "He ruled this out three weeks before it happened"
    assert seg["suggested_hashtags"] == ["#valorant", "#clutch", "#shroud"]


def test_validate_segment_keeps_segment_with_missing_thumbnail_fields():
    # A blank marketing field must never sink an otherwise-good segment —
    # same posture as the VO word ceiling nulling rather than dropping.
    raw = _raw()
    raw.pop("thumbnail_text", None)
    raw.pop("suggested_title", None)
    raw.pop("suggested_hashtags", None)
    seg = validate_segment(raw, _words(60))
    assert seg is not None
    assert seg["thumbnail_text"] == ""
    assert seg["suggested_title"] == ""
    assert seg["suggested_hashtags"] == []


def test_validate_segment_out_of_range_hashtag_count_keeps_segment(caplog):
    seg = validate_segment(_raw(suggested_hashtags=["#one"]), _words(60))
    assert seg is not None
    assert seg["suggested_hashtags"] == ["#one"]


# --- suggested_description: one sentence + credit line + hashtags, not -----
# 2-4 sentences (tightened after a real generated description at that length
# read as padded — see CLAUDE.md's Thumbnail + upload metadata section).


def test_description_sentence_bounds_are_one_to_two():
    # The credit line can fuse into the context sentence (1) or stand on
    # its own (2) — either is a valid minimal description, not a fixed count.
    assert DESCRIPTION_MIN_SENTENCES == 1
    assert DESCRIPTION_MAX_SENTENCES == 2


def test_count_sentences_counts_fused_context_and_credit_as_one():
    assert _count_sentences("Hasan reads a tweet live, credit to HasanAbi on Twitch.") == 1


def test_count_sentences_counts_separate_credit_line_as_two():
    assert _count_sentences("Hasan reads a tweet live. Clip via HasanAbi on Twitch.") == 2


def test_validate_segment_accepts_one_sentence_description():
    seg = validate_segment(
        _raw(
            suggested_description=(
                "Hasan reads a tweet live, credit to HasanAbi on Twitch. #hasanabi #twitchdrama"
            ),
            suggested_hashtags=["#hasanabi", "#twitchdrama"],
        ),
        _words(60),
    )
    assert seg is not None
    assert seg["suggested_description"].startswith("Hasan reads a tweet live")


def test_validate_segment_warns_but_keeps_a_description_over_the_new_tighter_bound(caplog):
    # The OLD spec's 2-4 sentence target now overshoots the new 1-2 bound —
    # still warn-and-keep, never a dropped segment, same posture as every
    # other marketing field here.
    padded = (
        "Hasan reads a tweet live on stream. He can't believe what it says. "
        "Clip via HasanAbi on Twitch. #hasanabi #twitchdrama"
    )
    seg = validate_segment(_raw(suggested_description=padded), _words(60))
    assert seg is not None
    assert seg["suggested_description"] == padded
    assert "want 1-2" in caplog.text


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
        lambda client, video_id, system_text, user_text, model: calls.append(
            (system_text, user_text)
        )
        or {"segments": [raw_segment]},
    )

    out = select_clip("clipid")
    assert len(calls) == 1
    system_text, user_text = calls[0]
    # the split actually happened: fixed instructions in the system half,
    # real per-clip data (a placeholder value only the dynamic half gets) in
    # the user half — not the same string twice, and not swapped.
    assert "## Task" in system_text
    assert "{transcript_window}" not in system_text
    assert "## Source" in user_text
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


# --- narration mode (caption_only) ------------------------------------------


def _narr(**over):
    base = {"vo_script": None, "on_screen_caption": None}
    base.update(over)
    return base


def test_resolve_narration_keeps_the_vo_for_a_spoken_placement():
    vo, cap = resolve_narration("h", _narr(vo_script="a line"), "cold_open")
    assert (vo, cap) == ("a line", None)


def test_resolve_narration_keeps_the_caption_for_caption_only():
    vo, cap = resolve_narration("h", _narr(on_screen_caption="a held line"), CAPTION_ONLY)
    assert (vo, cap) == (None, "a held line")


def test_resolve_narration_drops_the_vo_when_the_mode_is_caption_only():
    # vo_placement is the authority: the model said caption_only, so the
    # spoken line is the one that doesn't belong.
    vo, cap = resolve_narration(
        "h", _narr(vo_script="spoken", on_screen_caption="held"), CAPTION_ONLY
    )
    assert (vo, cap) == (None, "held")


def test_resolve_narration_drops_the_caption_when_the_mode_is_spoken():
    vo, cap = resolve_narration(
        "h", _narr(vo_script="spoken", on_screen_caption="held"), "pre_punchline"
    )
    assert (vo, cap) == ("spoken", None)


def test_resolve_narration_caption_only_with_no_caption_is_silence_not_a_drop():
    assert resolve_narration("h", _narr(), CAPTION_ONLY) == (None, None)


def test_resolve_narration_treats_blank_strings_as_null():
    assert resolve_narration("h", _narr(on_screen_caption="   "), CAPTION_ONLY) == (None, None)
    assert resolve_narration("h", _narr(vo_script="  "), "cold_open") == (None, None)


def test_resolve_narration_applies_the_word_ceiling_to_a_held_caption_too():
    # Same editorial bar as vo_script — nulled, never a dropped segment.
    long_line = " ".join(["word"] * 40)
    assert resolve_narration("h", _narr(on_screen_caption=long_line), CAPTION_ONLY) == (None, None)


def test_validate_segment_accepts_caption_only():
    seg = validate_segment(
        _raw(
            vo_script=None,
            on_screen_caption="Her actual doctor is the one laughing hardest here.",
            vo_placement=CAPTION_ONLY,
            narration_mode_reason="the laugh running under the whole back half is the payoff",
        ),
        _words(60),
    )
    assert seg is not None
    assert seg["vo_script"] is None
    assert seg["on_screen_caption"] == "Her actual doctor is the one laughing hardest here."
    assert seg["vo_placement"] == CAPTION_ONLY


def test_validate_segment_still_rejects_an_unknown_placement():
    assert validate_segment(_raw(vo_placement="whenever"), _words(60)) is None


def test_validate_segment_keeps_the_segment_when_the_reason_is_missing():
    # Accountability field, warn-only — never grounds to drop a real segment.
    seg = validate_segment(_raw(narration_mode_reason=""), _words(60))
    assert seg is not None
    assert seg["narration_mode_reason"] == ""


def test_validate_segment_spoken_segment_carries_a_null_caption():
    seg = validate_segment(_raw(on_screen_caption="a stray held line"), _words(60))
    assert seg is not None
    assert seg["on_screen_caption"] is None
    assert seg["vo_script"] is not None
