"""Unit tests for the facecam_split composition (visual gate layer 2's
composition fix) — the filter-graph builder and render()'s auto-detect
dispatch. No real ffmpeg, no real cv2/face detection — find_face_region is
monkeypatched throughout. Real end-to-end verification (real Haar cascade,
real downloaded Twitch clips) lives in test_face_detect.py's docstrings and
the session log, not here.
"""

from __future__ import annotations

import json

import pytest

from clipper import paths
from clipper import render as render_mod
from clipper.render import FACECAM_PANEL_FRACTION, _facecam_split_video

# --- _facecam_split_video: pure filter-graph construction -------------------


def _region(x, y, w, h, fw=1920, fh=1080):
    return {"x": x, "y": y, "w": w, "h": h, "frame_width": fw, "frame_height": fh}


def test_facecam_top_right_anchors_crop_to_top_right_corner():
    # Doc clip's real detection: facecam in the top-right.
    fc = _facecam_split_video(_region(1524.0, 48.8, 165.0, 165.0))
    fw, fh = 1920, 1080
    crop_w = round(fw * FACECAM_PANEL_FRACTION)
    crop_h = round(fh * FACECAM_PANEL_FRACTION)
    # cx >= 0.5 -> facecam_x = fw - crop_w (flush right); cy < 0.5 -> facecam_y = 0 (flush top)
    assert f"crop={crop_w}:{crop_h}:{fw - crop_w}:0" in fc


def test_facecam_top_left_anchors_crop_to_top_left_corner():
    # wallbang clip's real detection: facecam in the top-left.
    fc = _facecam_split_video(_region(111.0, 123.0, 116.2, 116.2))
    crop_w = round(1920 * FACECAM_PANEL_FRACTION)
    crop_h = round(1080 * FACECAM_PANEL_FRACTION)
    assert f"crop={crop_w}:{crop_h}:0:0" in fc


def test_main_panel_crops_away_from_right_side_facecam():
    # Facecam on the right -> main content should keep the LEFT side (x=0),
    # not re-show the facecam corner.
    fc = _facecam_split_video(_region(1524.0, 48.8, 165.0, 165.0))
    assert "crop=1080:864:x=0:y=0" in fc


def test_main_panel_crops_away_from_left_side_facecam():
    # Facecam on the left -> main content should keep the RIGHT side.
    fc = _facecam_split_video(_region(111.0, 123.0, 116.2, 116.2))
    assert "crop=1080:864:x=in_w-out_w:y=0" in fc


def test_facecam_panel_always_below_main_panel():
    # Fixed positions regardless of the source's actual facecam corner —
    # per the explicit requirement that panel assignment must not flip
    # clip to clip.
    fc = _facecam_split_video(_region(111.0, 123.0, 116.2, 116.2))
    assert "[base][main]overlay=0:250[t1]" in fc
    assert "[t1][face]overlay=0:1056[comp]" in fc


def test_facecam_panel_position_is_fixed_regardless_of_source_corner():
    # Same fixed overlay positions whether the facecam is top-left or
    # top-right in the source — panel assignment must never depend on it.
    fc_right = _facecam_split_video(_region(1524.0, 48.8, 165.0, 165.0))
    fc_left = _facecam_split_video(_region(111.0, 123.0, 116.2, 116.2))
    for fc in (fc_right, fc_left):
        assert "overlay=0:250" in fc  # main content panel
        assert "overlay=0:1056" in fc  # facecam panel


def test_ends_in_comp_label():
    fc = _facecam_split_video(_region(0, 0, 100, 100))
    assert fc.endswith("[comp]")


# --- render() dispatch: auto-detect vs explicit composition -----------------


def _setup(tmp_path, monkeypatch):
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
    return wd


def test_auto_detect_uses_facecam_split_when_facecam_found(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    region = _region(1524.0, 48.8, 165.0, 165.0)
    monkeypatch.setattr(render_mod, "find_face_region", lambda *a, **k: region)
    monkeypatch.setattr(render_mod, "is_small_corner_facecam", lambda r: True)

    calls = []
    monkeypatch.setattr(
        render_mod, "_facecam_split_video", lambda r: calls.append(r) or "FACECAM_FC[comp]"
    )
    monkeypatch.setattr(
        render_mod, "_tight_crop_video", lambda: pytest.fail("should not be called")
    )

    out = render_mod.render("vid1", 0, 0.0, 20.0, "confident")
    assert out.endswith("out_0.mp4")
    assert calls == [region]


def test_auto_detect_falls_back_to_tight_crop_when_no_facecam(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(render_mod, "find_face_region", lambda *a, **k: None)
    monkeypatch.setattr(
        render_mod, "_facecam_split_video", lambda r: pytest.fail("should not be called")
    )
    calls = []
    monkeypatch.setattr(render_mod, "_tight_crop_video", lambda: calls.append(1) or "TC[comp]")

    render_mod.render("vid1", 0, 0.0, 20.0, "confident")
    assert calls == [1]


def test_auto_detect_falls_back_to_tight_crop_when_face_too_large(tmp_path, monkeypatch):
    # A region was found (e.g. a real face) but is_small_corner_facecam says
    # no — must still fall back, not force facecam_split.
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        render_mod, "find_face_region", lambda *a, **k: _region(400, 200, 1200, 700)
    )
    monkeypatch.setattr(render_mod, "is_small_corner_facecam", lambda r: False)
    calls = []
    monkeypatch.setattr(render_mod, "_tight_crop_video", lambda: calls.append(1) or "TC[comp]")
    monkeypatch.setattr(
        render_mod, "_facecam_split_video", lambda r: pytest.fail("should not be called")
    )

    render_mod.render("vid1", 0, 0.0, 20.0, "confident")
    assert calls == [1]


def test_explicit_tight_crop_skips_detection_entirely(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def fail_detect(*a, **k):
        pytest.fail("find_face_region should not be called when composition is explicit")

    monkeypatch.setattr(render_mod, "find_face_region", fail_detect)

    render_mod.render("vid1", 0, 0.0, 20.0, "confident", composition="tight_crop")


def test_explicit_facecam_split_raises_when_nothing_detected(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(render_mod, "find_face_region", lambda *a, **k: None)

    with pytest.raises(ValueError, match="no small corner facecam was detected"):
        render_mod.render("vid1", 0, 0.0, 20.0, "confident", composition="facecam_split")


def test_explicit_facecam_split_uses_detected_region(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    region = _region(111.0, 123.0, 116.2, 116.2)
    monkeypatch.setattr(render_mod, "find_face_region", lambda *a, **k: region)
    monkeypatch.setattr(render_mod, "is_small_corner_facecam", lambda r: True)
    calls = []
    monkeypatch.setattr(render_mod, "_facecam_split_video", lambda r: calls.append(r) or "FC[comp]")

    render_mod.render("vid1", 0, 0.0, 20.0, "confident", composition="facecam_split")
    assert calls == [region]


def test_unknown_composition_string_raises():
    from clipper.render import COMPOSITIONS

    assert "facecam_split" in COMPOSITIONS


def test_cache_hit_never_runs_detection(tmp_path, monkeypatch):
    # Detection is real work (ffmpeg extraction + Haar inference) — a cached
    # render must never pay for it, same as it never re-downloads/transcribes.
    wd = _setup(tmp_path, monkeypatch)
    (wd / "out_0.mp4").write_bytes(b"already rendered")

    def fail_detect(*a, **k):
        pytest.fail("find_face_region must not run on a cache hit")

    monkeypatch.setattr(render_mod, "find_face_region", fail_detect)

    out = render_mod.render("vid1", 0, 0.0, 20.0, "confident")
    assert out == str(wd / "out_0.mp4")
