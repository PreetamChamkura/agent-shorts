"""Unit tests for render.py's loop-hold mechanism (CLAUDE.md: a `loopable`
segment must end cleanly on the last frame). Frame extraction is
monkeypatched — no real ffmpeg in this suite; the real integration was
verified separately against actual ffmpeg output.
"""

from __future__ import annotations

from clipper import render as render_mod
from clipper.frames import mean_abs_diff
from clipper.render import (
    LOOP_MOTION_THRESHOLD,
    _snap_loop_end_forward,
    pick_loop_freeze_end,
)

# --- word-boundary re-snap (mirrors select.py's forward bias) ---------------


def test_snap_loop_end_forward_extends_into_containing_word():
    words = [
        {"word": "a", "start": 10.0, "end": 10.1},
        {"word": "verylongword", "start": 10.15, "end": 15.0},
    ]
    assert _snap_loop_end_forward(words, 10.2) == 15.0


def test_snap_loop_end_forward_gap_uses_nearest_at_or_before():
    words = [{"word": "a", "start": 1.0, "end": 2.0}, {"word": "b", "start": 5.0, "end": 6.0}]
    assert _snap_loop_end_forward(words, 3.5) == 2.0


def test_snap_loop_end_forward_before_all_words_returns_raw():
    words = [{"word": "a", "start": 10.0, "end": 11.0}]
    assert _snap_loop_end_forward(words, 2.0) == 2.0


# --- frame diffing ------------------------------------------------------------


def testmean_abs_diff_identical_frames_is_zero():
    frame = bytes([100] * 16)
    assert mean_abs_diff(frame, frame) == 0.0


def testmean_abs_diff_measures_magnitude():
    a = bytes([0] * 16)
    b = bytes([10] * 16)
    assert mean_abs_diff(a, b) == 10.0


# --- pick_loop_freeze_end: search logic, ffmpeg extraction mocked -----------


def _stable_frame(seed: int) -> bytes:
    # Tiny near-identical frames (diff ~0-1) — "nothing moved".
    return bytes([seed % 3, 100, 101, 102])


def _noisy_frame(seed: int) -> bytes:
    # Large swings between consecutive frames — "something moved".
    return bytes([seed * 60 % 256, 0, 255, 0])


def test_pick_loop_freeze_end_accepts_stable_frame_at_exact_timestamp(monkeypatch):
    frames = [_stable_frame(i) for i in range(5)]
    monkeypatch.setattr(render_mod, "_extract_loop_candidate_frames", lambda *a, **k: frames)
    assert pick_loop_freeze_end("fake.mp4", 100.0) == 100.0


def test_pick_loop_freeze_end_steps_back_from_unstable_frame(monkeypatch):
    # Last frame (index 4) is unstable relative to index 3; index 3 is
    # stable relative to index 2. Expect one frame (1/30s) back.
    frames = [_stable_frame(i) for i in range(4)] + [_noisy_frame(99)]
    monkeypatch.setattr(render_mod, "_extract_loop_candidate_frames", lambda *a, **k: frames)
    result = pick_loop_freeze_end("fake.mp4", 100.0)
    assert result < 100.0
    # result is rounded to 3 decimals, so allow for that rounding
    assert abs((100.0 - result) - 1 / 30) < 1e-3


def test_pick_loop_freeze_end_falls_back_when_nothing_stable(monkeypatch):
    frames = [_noisy_frame(i) for i in range(6)]
    monkeypatch.setattr(render_mod, "_extract_loop_candidate_frames", lambda *a, **k: frames)
    # Nothing passes the threshold anywhere in the search window — must not
    # raise, must return the original timestamp as a safe fallback.
    assert pick_loop_freeze_end("fake.mp4", 100.0) == 100.0


def test_pick_loop_freeze_end_too_few_frames_returns_as_is(monkeypatch):
    monkeypatch.setattr(render_mod, "_extract_loop_candidate_frames", lambda *a, **k: [b"\x00"])
    assert pick_loop_freeze_end("fake.mp4", 50.0) == 50.0


def test_loop_motion_threshold_is_positive():
    # Sanity: the constant exists and is a plausible 0-255-scale value.
    assert 0 < LOOP_MOTION_THRESHOLD < 255
