"""Unit tests for face_detect.py (visual gate layer 2). No real cv2 model
loading, no real ffmpeg — _detect_boxes_in_saved_frames is monkeypatched
throughout (same "mock the wrapper, not the library" convention as
narrate.py's _synthesize). Real end-to-end verification (real Haar cascade,
real downloaded Twitch clips, a real podcast source) was done in the
session, not here — see face_detect.py's module/function docstrings for
the real numbers that shaped scaleFactor, CLUSTER_DISTANCE_PX, and the
corner-position check in is_small_corner_facecam.
"""

from __future__ import annotations

from clipper.face_detect import (
    CORNER_MARGIN_FRACTION,
    FACECAM_MAX_AREA_FRACTION,
    _cluster_boxes,
    _sample_timestamps,
    find_face_region,
    is_small_corner_facecam,
)

# --- _sample_timestamps -------------------------------------------------


def test_sample_timestamps_spans_the_range_with_edge_inset():
    ts = _sample_timestamps(0.0, 100.0, 5)
    assert len(ts) == 5
    assert ts[0] > 0.0  # inset from the very start
    assert ts[-1] < 100.0  # inset from the very end
    assert ts == sorted(ts)


def test_sample_timestamps_single_sample_is_midpoint():
    ts = _sample_timestamps(0.0, 100.0, 1)
    assert len(ts) == 1
    assert 40.0 < ts[0] < 60.0


def test_sample_timestamps_zero_returns_empty():
    assert _sample_timestamps(0.0, 10.0, 0) == []


# --- _cluster_boxes -----------------------------------------------------


def test_cluster_boxes_empty_returns_none():
    assert _cluster_boxes([]) is None


def test_cluster_boxes_groups_nearby_and_ignores_outlier():
    # Three boxes clustered near (100,100); one outlier far away (a one-off
    # false positive, like the hand-texture noise seen on the real Doc clip).
    boxes = [
        (100, 100, 50, 50),
        (105, 98, 50, 50),
        (98, 103, 50, 50),
        (900, 900, 40, 40),  # outlier, never repeats
    ]
    result = _cluster_boxes(boxes)
    assert result is not None
    (x, y, w, h), hits = result
    assert hits == 3
    assert 95 <= x <= 105
    assert 95 <= y <= 105


def test_cluster_boxes_single_box_is_its_own_cluster_of_one():
    result = _cluster_boxes([(10, 10, 20, 20)])
    assert result == ((10.0, 10.0, 20.0, 20.0), 1)


def test_cluster_boxes_picks_the_larger_cluster():
    boxes = [
        (0, 0, 10, 10),
        (900, 900, 10, 10),
        (905, 902, 10, 10),
        (898, 899, 10, 10),
    ]
    (_, _, _, _), hits = _cluster_boxes(boxes)
    assert hits == 3  # the (900,900)-ish cluster, not the lone (0,0)


# --- is_small_corner_facecam: area AND position, not area alone ------------


def _region(x, y, w, h, fw=1920, fh=1080):
    return {"x": x, "y": y, "w": w, "h": h, "frame_width": fw, "frame_height": fh}


def test_real_doc_clip_facecam_is_recognized():
    # Real detection from the session: top-right corner, small.
    assert is_small_corner_facecam(_region(1524.0, 48.8, 165.0, 165.0)) is True


def test_real_wallbang_clip_facecam_is_recognized():
    # Real detection from the session: top-left corner, smaller box.
    assert is_small_corner_facecam(_region(111.0, 123.0, 116.2, 116.2)) is True


def test_real_podcast_face_is_not_a_corner_facecam():
    # Real detection from the session (Hot Ones, FALlhXl6CmA): small in area
    # (medium shot) but NOT positioned in a corner — must not be classified
    # as an overlay. This is the exact bug found and fixed in this session:
    # area alone said "small, must be a facecam," which was wrong.
    assert is_small_corner_facecam(_region(444.0, 797.0, 130.0, 130.0)) is False


def test_large_centered_face_is_not_a_facecam():
    # Subject fills most of the frame — clearly podcast-style regardless of
    # position.
    assert is_small_corner_facecam(_region(400, 200, 1200, 700)) is False


def test_small_but_dead_center_is_not_a_facecam():
    # Small area, but centered — not tucked into any corner.
    assert is_small_corner_facecam(_region(900, 490, 120, 100)) is False


def test_area_threshold_boundary():
    fw, fh = 1000, 1000
    # Exactly at the area ceiling, tucked in the top-left corner.
    side = (FACECAM_MAX_AREA_FRACTION * fw * fh) ** 0.5
    assert is_small_corner_facecam(_region(0, 0, side, side, fw, fh)) is True


def test_corner_margin_boundary_matches_real_hot_ones_case():
    # Confirms the real Hot Ones failure is a margin call, not a fluke: its
    # x-center (0.256) sits just outside CORNER_MARGIN_FRACTION (0.25).
    fw = 1920
    cx_fraction = (444.0 + 130.0 / 2) / fw
    assert cx_fraction > CORNER_MARGIN_FRACTION


# --- find_face_region: integration, cv2 loop mocked -------------------------


def test_find_face_region_returns_none_when_nothing_recurs(monkeypatch):
    import clipper.face_detect as fd

    monkeypatch.setattr(fd, "_load_cascade", lambda: object())
    monkeypatch.setattr(fd, "_extract_sample_frames", lambda *a, **k: [object()] * 3)
    # Every "detection" is a one-off, never in the same place twice.
    monkeypatch.setattr(
        fd,
        "_detect_boxes_in_saved_frames",
        lambda *a, **k: ([(0, 0, 10, 10), (500, 500, 10, 10), (900, 10, 10, 10)], 640, 360),
    )
    assert find_face_region("fake.mp4", 0.0, 30.0) is None


def test_find_face_region_returns_scaled_result_when_consistent(monkeypatch):
    import clipper.face_detect as fd

    monkeypatch.setattr(fd, "_load_cascade", lambda: object())
    monkeypatch.setattr(fd, "_extract_sample_frames", lambda *a, **k: [object()] * 4)
    # Same box (100,50,60,60) recurs 3 times at detect resolution 640x360;
    # real source is 1920x1080 -> scale factor 3.
    monkeypatch.setattr(
        fd,
        "_detect_boxes_in_saved_frames",
        lambda *a, **k: ([(100, 50, 60, 60)] * 3 + [(900, 900, 10, 10)], 640, 360),
    )
    monkeypatch.setattr(fd, "probe_frame_size", lambda source: (1920, 1080))

    region = find_face_region("fake.mp4", 0.0, 30.0)
    assert region is not None
    assert region["x"] == 300.0  # 100 * 3
    assert region["y"] == 150.0  # 50 * 3
    assert region["w"] == 180.0  # 60 * 3
    assert region["hits"] == 3
    assert region["samples"] == 4
    assert region["frame_width"] == 1920


def test_find_face_region_no_frames_extracted_returns_none(monkeypatch):
    import clipper.face_detect as fd

    monkeypatch.setattr(fd, "_load_cascade", lambda: object())
    monkeypatch.setattr(fd, "_extract_sample_frames", lambda *a, **k: [])
    assert find_face_region("fake.mp4", 0.0, 30.0) is None
