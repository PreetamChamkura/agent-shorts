"""Unit tests for stage 4's thumbnail + metadata generation (CLAUDE.md
thumbnail generation step). No real ffmpeg, no real cv2 — run_ffmpeg,
_extract_full_frame, and find_face_region are monkeypatched throughout, same
"mock the wrapper, not the library" convention as the rest of this codebase.
Real end-to-end verification (a real clip, a real generated JPEG) lives in
the session log, not here.
"""

from __future__ import annotations

import json

import pytest

from clipper import paths
from clipper import thumbnail as thumb_mod
from clipper.thumbnail import (
    SAFE_SQUARE_BOTTOM,
    SAFE_SQUARE_TOP,
    _ass_to_ffmpeg_hex,
    _evenly_spaced,
    _strip_emoji,
    _wrap_lines,
    generate_thumbnail,
    select_thumbnail_timestamp,
    write_metadata,
)

# --- pure helpers -------------------------------------------------------


def test_wrap_lines_fits_on_one_line_when_short():
    assert _wrap_lines("HE HAD NO IDEA", 30) == ["HE HAD NO IDEA"]


def test_wrap_lines_breaks_at_word_boundary():
    lines = _wrap_lines("HE HAD ABSOLUTELY NO IDEA WHATSOEVER", 15)
    assert all(len(line) <= 15 or " " not in line for line in lines)
    assert " ".join(lines).split() == ["HE", "HAD", "ABSOLUTELY", "NO", "IDEA", "WHATSOEVER"]


def test_wrap_lines_never_splits_a_single_long_word():
    lines = _wrap_lines("SUPERCALIFRAGILISTICEXPIALIDOCIOUS", 5)
    assert lines == ["SUPERCALIFRAGILISTICEXPIALIDOCIOUS"]


def test_wrap_lines_empty_text_is_empty():
    assert _wrap_lines("", 20) == []


def test_ass_to_ffmpeg_hex_house_yellow():
    # &H0000E5FF: AA=00 BB=00 GG=E5 RR=FF -> RGB FF E5 00
    assert _ass_to_ffmpeg_hex("&H0000E5FF") == "0xFFE500"


def test_ass_to_ffmpeg_hex_burn_red():
    # &H003B3BE2: AA=00 BB=3B GG=3B RR=E2 -> RGB E2 3B 3B
    assert _ass_to_ffmpeg_hex("&H003B3BE2") == "0xE23B3B"


def test_ass_to_ffmpeg_hex_rejects_malformed_input():
    with pytest.raises(ValueError):
        _ass_to_ffmpeg_hex("not-a-color")


def test_evenly_spaced_insets_from_edges():
    ts = _evenly_spaced(100.0, 200.0, 5)
    assert len(ts) == 5
    assert ts[0] > 100.0
    assert ts[-1] < 200.0
    assert ts == sorted(ts)


def test_evenly_spaced_zero_span_is_empty():
    assert _evenly_spaced(50.0, 50.0, 5) == []


def test_safe_square_is_centered_1080_tall():
    assert SAFE_SQUARE_BOTTOM - SAFE_SQUARE_TOP == 1080
    assert SAFE_SQUARE_TOP == 420  # (1920-1080)/2


# --- text placement: facecam_split must never overlap the facecam panel ----
# Real bug found from real output: a single fixed text band (y=1300) sat
# inside the facecam panel itself, directly over the visible face's mouth.


def test_text_band_facecam_split_stays_above_facecam_panel():
    from clipper.render import FACECAM_PANEL_Y
    from clipper.thumbnail import _text_band_center, _text_safe_bounds

    center = _text_band_center("facecam_split")
    top, bottom = _text_safe_bounds("facecam_split")
    assert bottom == FACECAM_PANEL_Y
    assert top <= center < bottom < FACECAM_PANEL_Y + 1


def test_text_band_default_composition_unaffected():
    from clipper.thumbnail import TEXT_BAND_CENTER_Y_DEFAULT, _text_band_center, _text_safe_bounds

    assert _text_band_center("tight_crop") == TEXT_BAND_CENTER_Y_DEFAULT
    assert _text_safe_bounds("tight_crop") == (SAFE_SQUARE_TOP, SAFE_SQUARE_BOTTOM)


def test_text_band_facecam_split_differs_from_default():
    from clipper.thumbnail import _text_band_center

    assert _text_band_center("facecam_split") != _text_band_center("tight_crop")


def test_strip_emoji_removes_emoji_confirmed_broken_in_drawtext():
    # Confirmed for real against a live ffmpeg + Montserrat render: emoji
    # come out as tofu boxes, not the glyph — see _EMOJI_RE's comment.
    assert _strip_emoji('"NO WAY 😱🔥"') == '"NO WAY "'


def test_strip_emoji_leaves_plain_text_untouched():
    assert _strip_emoji("YOU DON'T JUST GET BANNED") == "YOU DON'T JUST GET BANNED"


# --- select_thumbnail_timestamp: Layer 3 reuse vs cheap fallback -----------


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "source.mp4").write_bytes(b"fake")
    return wd


def test_select_timestamp_reuses_layer3_when_in_range(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    (wd / "visual_judge.json").write_text(
        json.dumps({"passed": True, "best_frame_timestamp": 12.5})
    )

    def fail_fallback(*a, **k):
        pytest.fail("cheap fallback should not run when Layer 3 data is usable")

    monkeypatch.setattr(thumb_mod, "_pick_engaging_frame_cheap", fail_fallback)

    ts, source = select_thumbnail_timestamp("vid1", 0.0, 20.0, "tight_crop", None)
    assert (ts, source) == (12.5, "layer3")


def test_select_timestamp_falls_back_when_layer3_frame_outside_segment(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    # select_clip trimmed the final segment to [10, 20] — Layer 3's pick at
    # 2.0s (from the full original clip) no longer falls inside it.
    (wd / "visual_judge.json").write_text(json.dumps({"passed": True, "best_frame_timestamp": 2.0}))
    monkeypatch.setattr(thumb_mod, "_pick_engaging_frame_cheap", lambda *a, **k: 15.0)

    ts, source = select_thumbnail_timestamp("vid1", 10.0, 20.0, "tight_crop", None)
    assert (ts, source) == (15.0, "fallback:punchline")


def test_select_timestamp_falls_back_when_no_visual_judge_json(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # no visual_judge.json written — long-form path
    monkeypatch.setattr(thumb_mod, "_pick_engaging_frame_cheap", lambda *a, **k: 7.0)

    ts, source = select_thumbnail_timestamp("vid1", 0.0, 20.0, "tight_crop", None)
    assert (ts, source) == (7.0, "fallback:punchline")


def test_select_timestamp_forwards_punchline_at_to_fallback(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # no visual_judge.json — forces the fallback path
    seen = []
    monkeypatch.setattr(
        thumb_mod, "_pick_engaging_frame_cheap", lambda *a, **k: seen.append(a) or 9.0
    )

    select_thumbnail_timestamp("vid1", 0.0, 20.0, "tight_crop", None, 15.0)
    assert seen[0][-1] == 15.0  # punchline_at forwarded positionally


def test_select_timestamp_facecam_split_fallback_ignores_anchor_entirely(tmp_path, monkeypatch):
    # facecam_split's fallback must never even attempt quote-location — no
    # words.json read, no anchor resolution, just the plain "fallback" tag.
    _setup(tmp_path, monkeypatch)  # no visual_judge.json — forces the fallback path

    def fail_resolve(*a, **k):
        pytest.fail("facecam_split's fallback must not resolve an anchor at all")

    monkeypatch.setattr(thumb_mod, "_resolve_anchor", fail_resolve)
    monkeypatch.setattr(thumb_mod, "_pick_engaging_frame_cheap", lambda *a, **k: 3.0)

    ts, source = select_thumbnail_timestamp(
        "vid1", 0.0, 20.0, "facecam_split", None, 12.0, '"A QUOTE"'
    )
    assert (ts, source) == (3.0, "fallback")


# --- _locate_quoted_phrase / _resolve_anchor: anchor on the quote's own ----
# spoken timestamp, not blindly on punchline_at. Real bug found from real
# output: a segment's thumbnail_text was spoken 15+ seconds away from its
# own punchline_at in the same segment — centering on punchline_at picked a
# real person, but not the one saying the quoted line.


def test_locate_quoted_phrase_finds_contiguous_run():
    words = [
        {"word": "Where", "start": 101.46, "end": 101.58},
        {"word": "did", "start": 101.58, "end": 101.68},
        {"word": "you", "start": 101.68, "end": 101.76},
        {"word": "go", "start": 101.76, "end": 101.88},
        {"word": "to", "start": 101.88, "end": 101.98},
        {"word": "medical", "start": 101.98, "end": 102.12},
        {"word": "school?", "start": 102.12, "end": 102.40},
    ]
    assert thumb_mod._locate_quoted_phrase(words, "Where did you go to medical school") == (
        101.46,
        102.40,
    )


def test_locate_quoted_phrase_strips_wrapping_quote_marks():
    words = [
        {"word": "Where", "start": 101.46, "end": 101.58},
        {"word": "did", "start": 101.58, "end": 101.68},
    ]
    # thumbnail_text is styled with literal quote marks — not part of the
    # spoken words, must be stripped by normalization like any punctuation.
    assert thumb_mod._locate_quoted_phrase(words, '"Where did"') == (101.46, 101.68)


def test_locate_quoted_phrase_none_when_paraphrased():
    # Real regression case: thumbnail_text said "MED SCHOOL", the transcript
    # actually says "medical school" — not a verbatim quote, must not match.
    words = [
        {"word": "Where", "start": 101.46, "end": 101.58},
        {"word": "did", "start": 101.58, "end": 101.68},
        {"word": "you", "start": 101.68, "end": 101.76},
        {"word": "go", "start": 101.76, "end": 101.88},
        {"word": "to", "start": 101.88, "end": 101.98},
        {"word": "medical", "start": 101.98, "end": 102.12},
        {"word": "school?", "start": 102.12, "end": 102.40},
    ]
    assert thumb_mod._locate_quoted_phrase(words, "Where did you go to med school") is None


def test_resolve_anchor_uses_quote_location_when_found(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    words = [
        {"word": "he's", "start": 30.0, "end": 30.3},
        {"word": "affordable.", "start": 30.3, "end": 30.8},
    ]
    (wd / "words.json").write_text(json.dumps({"words": words}))

    anchor, source = thumb_mod._resolve_anchor("vid1", 0.0, 40.0, '"he\'s affordable"', 15.0)
    assert source == "quote"
    assert anchor == (30.0 + 30.8) / 2


def test_resolve_anchor_falls_back_to_punchline_when_not_a_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    words = [{"word": "unrelated", "start": 5.0, "end": 5.5}]
    (wd / "words.json").write_text(json.dumps({"words": words}))

    anchor, source = thumb_mod._resolve_anchor("vid1", 0.0, 40.0, "A REACTION PHRASE", 15.0)
    assert (anchor, source) == (15.0, "punchline")


def test_resolve_anchor_falls_back_to_midpoint_when_no_punchline_or_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    # No words.json at all — the long-form path before Layer 3 or a segment
    # predating this feature.
    anchor, source = thumb_mod._resolve_anchor("vid1", 10.0, 30.0, "SOMETHING", None)
    assert (anchor, source) == (20.0, "punchline")


def test_resolve_anchor_restricts_search_to_the_segments_own_words(tmp_path, monkeypatch):
    # An identical phrase earlier in the video (outside [start, end]) must
    # not be matched instead of nothing found within the segment itself.
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    words = [
        {"word": "hello", "start": 5.0, "end": 5.5},  # outside [start, end]
        {"word": "unrelated", "start": 25.0, "end": 25.5},  # inside, but not the phrase
    ]
    (wd / "words.json").write_text(json.dumps({"words": words}))

    anchor, source = thumb_mod._resolve_anchor("vid1", 20.0, 30.0, '"hello"', 27.0)
    assert (anchor, source) == (27.0, "punchline")


# --- tight_crop's fallback face-gate: two real bugs found from real output -
# (1) an unconditional highest-edge_density pick chose a visually busy but
# completely person-free B-roll shot near the payoff. (2) fixing that with a
# bare single-frame Haar check let the SAME shot through anyway — Haar threw
# 4 false-positive hits on bottle labels/caps in that exact frame. The real
# fix requires a hit to match an ALREADY temporally-confirmed position
# (find_face_region's own clustering over the search window), not just any
# single-frame hit. facecam_split is untouched by any of this (see
# test_pick_engaging_frame_cheap_facecam_split_unaffected_by_face_gate).


def test_best_by_edge_density_picks_highest_score(monkeypatch):
    scores = {1.0: 5.0, 2.0: 9.0, 3.0: 7.0}
    monkeypatch.setattr(
        thumb_mod, "_extract_score_frame", lambda source, ts, crop_box: bytes([int(scores[ts])])
    )
    monkeypatch.setattr(thumb_mod, "edge_density", lambda pixels, w, h: pixels[0])

    result = thumb_mod._best_by_edge_density("src.mp4", [1.0, 2.0, 3.0], None)
    assert result == 2.0


def test_best_by_edge_density_none_when_every_extraction_fails(monkeypatch):
    monkeypatch.setattr(thumb_mod, "_extract_score_frame", lambda source, ts, crop_box: None)
    assert thumb_mod._best_by_edge_density("src.mp4", [1.0, 2.0], None) is None


_CONFIRMED = {
    "x": 100.0,
    "y": 100.0,
    "w": 200.0,
    "h": 200.0,
    "frame_width": 1920,
    "frame_height": 1080,
}


def test_frame_matches_confirmed_face_true_when_hit_lands_near_it(monkeypatch):
    from clipper.face_detect import DETECT_WIDTH

    scale = DETECT_WIDTH / _CONFIRMED["frame_width"]
    cx = (_CONFIRMED["x"] + _CONFIRMED["w"] / 2) * scale
    cy = (_CONFIRMED["y"] + _CONFIRMED["h"] / 2) * scale
    # A hit a few pixels off the confirmed center — well within CLUSTER_DISTANCE_PX.
    monkeypatch.setattr(
        thumb_mod, "_detect_faces_at", lambda source, ts: [(int(cx) - 5, int(cy) - 5, 40, 40)]
    )
    assert thumb_mod._frame_matches_confirmed_face("src.mp4", 5.0, _CONFIRMED) is True


def test_frame_matches_confirmed_face_false_for_an_unrelated_hit(monkeypatch):
    # Real scenario: a false-positive hit on bottle-label texture, nowhere
    # near where the confirmed face actually is.
    monkeypatch.setattr(thumb_mod, "_detect_faces_at", lambda source, ts: [(500, 500, 40, 40)])
    assert thumb_mod._frame_matches_confirmed_face("src.mp4", 5.0, _CONFIRMED) is False


def test_frame_matches_confirmed_face_false_when_no_hits_at_all(monkeypatch):
    monkeypatch.setattr(thumb_mod, "_detect_faces_at", lambda source, ts: [])
    assert thumb_mod._frame_matches_confirmed_face("src.mp4", 5.0, _CONFIRMED) is False


def test_pick_engaging_frame_tight_crop_requires_a_confirmed_match(monkeypatch):
    # find_face_region confirms SOMETHING recurs in the window; only one of
    # three candidates actually matches that confirmed position — must pick
    # among ONLY the matching candidate(s), not the highest-scoring overall.
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda source, start, end: _CONFIRMED)
    matches = {10.0: False, 11.0: True, 12.0: False}
    monkeypatch.setattr(
        thumb_mod,
        "_frame_matches_confirmed_face",
        lambda source, ts, confirmed: matches.get(ts, False),
    )
    monkeypatch.setattr(thumb_mod, "_evenly_spaced", lambda start, end, n: [10.0, 11.0, 12.0])
    seen_scored = []
    monkeypatch.setattr(
        thumb_mod,
        "_best_by_edge_density",
        lambda source, timestamps, crop_box: seen_scored.append(timestamps) or timestamps[0],
    )

    result = thumb_mod._pick_engaging_frame_tight_crop("src.mp4", 0.0, 20.0, 11.0)
    assert result == 11.0
    assert seen_scored[0] == [11.0]  # only the confirmed-matching candidate was scored


def test_pick_engaging_frame_tight_crop_treats_unconfirmed_window_as_no_face(monkeypatch):
    # find_face_region finds nothing recurring in the narrow window (returns
    # None) — must move on to widening, never fall through to checking
    # individual frames without a confirmed position to check them against.
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda source, start, end: None)

    def fail_match(*a, **k):
        pytest.fail("must not check individual frames when nothing was confirmed in the window")

    monkeypatch.setattr(thumb_mod, "_frame_matches_confirmed_face", fail_match)
    monkeypatch.setattr(thumb_mod, "_evenly_spaced", lambda start, end, n: [5.0, 6.0, 7.0])
    monkeypatch.setattr(
        thumb_mod, "_best_by_edge_density", lambda source, timestamps, crop_box: 6.0
    )

    result = thumb_mod._pick_engaging_frame_tight_crop("src.mp4", 0.0, 30.0, 15.0)
    assert result == 6.0  # fell all the way through to the faceless fallback


def test_pick_engaging_frame_tight_crop_widens_search_when_nothing_confirmed_nearby(monkeypatch):
    # Nothing confirmed in the initial (narrow) window; the widened window
    # confirms something. Must retry with the wider radius before giving up.
    calls = []

    def fake_find_face_region(source, start, end):
        calls.append((start, end))
        return None if len(calls) == 1 else _CONFIRMED

    monkeypatch.setattr(thumb_mod, "find_face_region", fake_find_face_region)
    monkeypatch.setattr(thumb_mod, "_frame_matches_confirmed_face", lambda source, ts, c: True)
    monkeypatch.setattr(thumb_mod, "_evenly_spaced", lambda start, end, n: [2.0])
    monkeypatch.setattr(
        thumb_mod, "_best_by_edge_density", lambda source, timestamps, crop_box: timestamps[0]
    )

    result = thumb_mod._pick_engaging_frame_tight_crop("src.mp4", 0.0, 30.0, 15.0)
    assert result == 2.0
    assert len(calls) == 2  # widened once


def test_pick_engaging_frame_tight_crop_falls_back_to_faceless_when_none_confirmed(
    monkeypatch, caplog
):
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda source, start, end: None)
    monkeypatch.setattr(thumb_mod, "_evenly_spaced", lambda start, end, n: [5.0, 6.0, 7.0])
    monkeypatch.setattr(
        thumb_mod, "_best_by_edge_density", lambda source, timestamps, crop_box: 6.0
    )

    with caplog.at_level("WARNING"):
        result = thumb_mod._pick_engaging_frame_tight_crop("src.mp4", 0.0, 30.0, 15.0)

    assert result == 6.0
    assert any("no confirmed-recurring face found" in r.message for r in caplog.records)


def test_pick_engaging_frame_tight_crop_defaults_anchor_to_midpoint_when_no_punchline(
    monkeypatch,
):
    seen_anchors = []

    def fake_find_face_region(source, start, end):
        seen_anchors.append((start + end) / 2)
        return None

    monkeypatch.setattr(thumb_mod, "find_face_region", fake_find_face_region)
    monkeypatch.setattr(thumb_mod, "_evenly_spaced", lambda start, end, n: [1.0])
    monkeypatch.setattr(
        thumb_mod, "_best_by_edge_density", lambda source, timestamps, crop_box: 1.0
    )

    thumb_mod._pick_engaging_frame_tight_crop("src.mp4", 10.0, 30.0, None)
    assert seen_anchors[0] == 20.0  # (10+30)/2, no punchline_at given


def test_pick_engaging_frame_cheap_dispatches_to_tight_crop_search(monkeypatch):
    seen = []
    monkeypatch.setattr(
        thumb_mod,
        "_pick_engaging_frame_tight_crop",
        lambda source, start, end, punchline_at: seen.append(punchline_at) or 42.0,
    )

    result = thumb_mod._pick_engaging_frame_cheap("src.mp4", 0.0, 20.0, "tight_crop", None, 12.0)
    assert result == 42.0
    assert seen == [12.0]


def test_pick_engaging_frame_cheap_facecam_split_unaffected_by_face_gate(monkeypatch):
    # facecam_split must go through the ORIGINAL full-segment edge_density
    # scan, never the face-gated search — this fix must not touch it.
    def fail_tight_crop_search(*a, **k):
        pytest.fail("facecam_split must not use the tight_crop face-gated search")

    monkeypatch.setattr(thumb_mod, "_pick_engaging_frame_tight_crop", fail_tight_crop_search)
    monkeypatch.setattr(thumb_mod, "find_face_region", fail_tight_crop_search)
    monkeypatch.setattr(
        thumb_mod, "_best_by_edge_density", lambda source, timestamps, crop_box: 5.0
    )

    region = {"x": 0, "y": 0, "w": 100, "h": 100, "frame_width": 1920, "frame_height": 1080}
    result = thumb_mod._pick_engaging_frame_cheap(
        "src.mp4", 0.0, 20.0, "facecam_split", region, 12.0
    )
    assert result == 5.0


# --- _extract_full_frame: seeks BEFORE ts, lands ON it, not past it --------
# Real bug found from real output: a naive seek-to-ts-then-keep-last-of-burst
# lands ~_WARMUP_FRAMES/30s PAST ts, not on it — confirmed by extracting both
# and comparing against a real clip (a duller facecam expression than the
# frame Layer 3 actually named). Fixed like render.py's
# pick_loop_freeze_end: seek backward by the lookahead window instead.


def test_extract_full_frame_seeks_before_ts_not_at_it(tmp_path, monkeypatch):
    seen_args = []
    monkeypatch.setattr(thumb_mod, "run_ffmpeg", lambda args: seen_args.append(args))

    thumb_mod._extract_full_frame("fake.mp4", 31.35, "TC[comp]", tmp_path / "out.png")

    args = seen_args[0]
    seek_value = float(args[args.index("-ss") + 1])
    expected = 31.35 - thumb_mod._WARMUP_FRAMES / 30.0
    assert seek_value == pytest.approx(expected, abs=0.001)
    assert seek_value < 31.35


def test_extract_full_frame_seek_never_goes_negative(tmp_path, monkeypatch):
    seen_args = []
    monkeypatch.setattr(thumb_mod, "run_ffmpeg", lambda args: seen_args.append(args))

    thumb_mod._extract_full_frame("fake.mp4", 0.05, "TC[comp]", tmp_path / "out.png")

    args = seen_args[0]
    seek_value = float(args[args.index("-ss") + 1])
    assert seek_value == 0.0


# --- generate_thumbnail: composition resolution + wiring -------------------


def _setup_render(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(thumb_mod, "style_for_tone", lambda tone: ({}, 3))
    monkeypatch.setattr(
        thumb_mod,
        "_extract_full_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"png"),
    )
    monkeypatch.setattr(
        thumb_mod, "run_ffmpeg", lambda args: (wd / "thumbnail_0.jpg").write_bytes(b"jpg")
    )
    monkeypatch.setattr(thumb_mod, "_validate_thumbnail", lambda path: None)
    return wd


def test_generate_thumbnail_auto_detects_tight_crop_when_no_facecam(tmp_path, monkeypatch):
    _setup_render(tmp_path, monkeypatch)
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda *a, **k: None)
    monkeypatch.setattr(thumb_mod, "_tight_crop_video", lambda: "TC[comp]")
    monkeypatch.setattr(
        thumb_mod, "_facecam_split_video", lambda r: pytest.fail("should not be called")
    )
    monkeypatch.setattr(thumb_mod, "select_thumbnail_timestamp", lambda *a, **k: (5.0, "fallback"))

    out = generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA")
    assert out.endswith("thumbnail_0.jpg")


def test_generate_thumbnail_auto_detects_facecam_split(tmp_path, monkeypatch):
    _setup_render(tmp_path, monkeypatch)
    region = {
        "x": 1524.0,
        "y": 48.8,
        "w": 165.0,
        "h": 165.0,
        "frame_width": 1920,
        "frame_height": 1080,
    }
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda *a, **k: region)
    monkeypatch.setattr(thumb_mod, "is_small_corner_facecam", lambda r: True)
    calls = []
    monkeypatch.setattr(thumb_mod, "_facecam_split_video", lambda r: calls.append(r) or "FC[comp]")
    monkeypatch.setattr(thumb_mod, "select_thumbnail_timestamp", lambda *a, **k: (5.0, "fallback"))

    generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA")
    assert calls == [region]


def test_generate_thumbnail_cache_hit_skips_everything(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    (wd / "thumbnail_0.jpg").write_bytes(b"already made")

    def fail(*a, **k):
        pytest.fail("should not run detection or extraction on a cache hit")

    monkeypatch.setattr(thumb_mod, "find_face_region", fail)
    monkeypatch.setattr(thumb_mod, "_extract_full_frame", fail)

    out = generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA")
    assert out == str(wd / "thumbnail_0.jpg")


def test_generate_thumbnail_missing_source_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        generate_thumbnail("nope", 0, 0.0, 20.0, "confident", "TEXT HERE")


def test_generate_thumbnail_rejects_empty_thumbnail_text(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no thumbnail_text"):
        generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "   ")


def test_generate_thumbnail_explicit_facecam_split_raises_when_nothing_detected(
    tmp_path, monkeypatch
):
    _setup_render(tmp_path, monkeypatch)
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda *a, **k: None)

    with pytest.raises(ValueError, match="no small corner facecam was detected"):
        generate_thumbnail(
            "vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA", composition="facecam_split"
        )


def test_generate_thumbnail_explicit_tight_crop_skips_detection(tmp_path, monkeypatch):
    _setup_render(tmp_path, monkeypatch)

    def fail_detect(*a, **k):
        pytest.fail("find_face_region should not run when composition is explicit")

    monkeypatch.setattr(thumb_mod, "find_face_region", fail_detect)
    monkeypatch.setattr(thumb_mod, "_tight_crop_video", lambda: "TC[comp]")
    monkeypatch.setattr(thumb_mod, "select_thumbnail_timestamp", lambda *a, **k: (5.0, "fallback"))

    generate_thumbnail(
        "vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA", composition="tight_crop"
    )


def test_generate_thumbnail_facecam_split_drawbox_never_reaches_facecam_panel(
    tmp_path, monkeypatch
):
    from clipper.render import FACECAM_PANEL_Y

    wd = _setup_render(tmp_path, monkeypatch)
    region = {
        "x": 1524.0,
        "y": 48.8,
        "w": 165.0,
        "h": 165.0,
        "frame_width": 1920,
        "frame_height": 1080,
    }
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda *a, **k: region)
    monkeypatch.setattr(thumb_mod, "is_small_corner_facecam", lambda r: True)
    monkeypatch.setattr(thumb_mod, "_facecam_split_video", lambda r: "FC[comp]")
    monkeypatch.setattr(thumb_mod, "select_thumbnail_timestamp", lambda *a, **k: (5.0, "layer3"))

    seen_args = []

    def fake_run_ffmpeg(args):
        seen_args.append(args)
        (wd / "thumbnail_0.jpg").write_bytes(b"jpg")

    monkeypatch.setattr(thumb_mod, "run_ffmpeg", fake_run_ffmpeg)

    generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA — SEE FOR YOURSELF")

    vf = seen_args[0][seen_args[0].index("-vf") + 1]
    drawbox = next(part for part in vf.split(",") if part.startswith("drawbox"))
    box_y = int(drawbox.split("y=")[1].split(":")[0])
    box_h = int(drawbox.split(":h=")[1].split(":")[0])
    assert box_y + box_h <= FACECAM_PANEL_Y


def test_generate_thumbnail_force_regenerates(tmp_path, monkeypatch):
    wd = _setup_render(tmp_path, monkeypatch)
    (wd / "thumbnail_0.jpg").write_bytes(b"stale")
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda *a, **k: None)
    monkeypatch.setattr(thumb_mod, "_tight_crop_video", lambda: "TC[comp]")
    monkeypatch.setattr(thumb_mod, "select_thumbnail_timestamp", lambda *a, **k: (5.0, "fallback"))

    calls = []
    monkeypatch.setattr(
        thumb_mod,
        "_extract_full_frame",
        lambda source, ts, video_fc, out_path: calls.append(1) or out_path.write_bytes(b"png"),
    )

    generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA", force=True)
    assert calls == [1]


def test_generate_thumbnail_invalid_output_is_removed(tmp_path, monkeypatch):
    wd = _setup_render(tmp_path, monkeypatch)
    monkeypatch.setattr(thumb_mod, "find_face_region", lambda *a, **k: None)
    monkeypatch.setattr(thumb_mod, "_tight_crop_video", lambda: "TC[comp]")
    monkeypatch.setattr(thumb_mod, "select_thumbnail_timestamp", lambda *a, **k: (5.0, "fallback"))

    def fail_validate(path):
        raise ValueError("bad dimensions")

    monkeypatch.setattr(thumb_mod, "_validate_thumbnail", fail_validate)

    with pytest.raises(ValueError, match="bad dimensions"):
        generate_thumbnail("vid1", 0, 0.0, 20.0, "confident", "HE HAD NO IDEA")
    assert not (wd / "thumbnail_0.jpg").exists()


# --- write_metadata -----------------------------------------------------


def test_write_metadata_writes_expected_fields(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    seg = {
        "n": 0,
        "suggested_title": "He ruled this out three weeks before it happened",
        "thumbnail_text": '"I DIDN\'T EVEN SEE HIM"',
        "suggested_hashtags": ["#valorant", "#clutch", "#shroud"],
        "caption_title": "HE HAD NO IDEA",
        "tone": "revealing",
    }
    out = write_metadata("vid1", 0, seg)
    data = json.loads((wd / "metadata_0.json").read_text())
    assert data["video_id"] == "vid1"
    assert data["suggested_hashtags"] == ["#valorant", "#clutch", "#shroud"]
    assert out == str(wd / "metadata_0.json")


def test_write_metadata_cache_hit_skips_rewrite(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    (wd / "metadata_0.json").write_text(json.dumps({"stale": True}))

    write_metadata("vid1", 0, {"suggested_title": "new"})
    data = json.loads((wd / "metadata_0.json").read_text())
    assert data == {"stale": True}


def test_write_metadata_force_overwrites(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch)
    (wd / "metadata_0.json").write_text(json.dumps({"stale": True}))

    write_metadata("vid1", 0, {"suggested_title": "new"}, force=True)
    data = json.loads((wd / "metadata_0.json").read_text())
    assert data["suggested_title"] == "new"
