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
    assert (ts, source) == (15.0, "fallback")


def test_select_timestamp_falls_back_when_no_visual_judge_json(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # no visual_judge.json written — long-form path
    monkeypatch.setattr(thumb_mod, "_pick_engaging_frame_cheap", lambda *a, **k: 7.0)

    ts, source = select_thumbnail_timestamp("vid1", 0.0, 20.0, "tight_crop", None)
    assert (ts, source) == (7.0, "fallback")


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
