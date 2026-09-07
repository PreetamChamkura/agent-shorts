"""Unit tests for the Twitch-clip visual gate's layer 3 (vision-model
judgment). No real network, no real ffmpeg/cv2 — _call_model and the frame
extraction are monkeypatched throughout, same "mock the wrapper, not the
library" convention used across this codebase. Real end-to-end verification
(real Doc-clip rejection, real good-clip pass, actual reasons) was run
manually against real credentials in the session, not here.
"""

from __future__ import annotations

import pytest

from clipper.visual_judge import (
    N_SAMPLE_FRAMES,
    _build_prompt,
    _sample_timestamps,
    judge_clip,
    resolve_composition,
)

# --- _sample_timestamps ------------------------------------------------


def test_sample_timestamps_spans_range_with_edge_inset():
    ts = _sample_timestamps(100.0, 5)
    assert len(ts) == 5
    assert ts[0] > 0.0
    assert ts[-1] < 100.0
    assert ts == sorted(ts)


def test_sample_timestamps_default_count_is_five():
    assert N_SAMPLE_FRAMES == 5


def test_sample_timestamps_single_is_midpoint():
    ts = _sample_timestamps(100.0, 1)
    assert 40.0 < ts[0] < 60.0


def test_sample_timestamps_zero_is_empty():
    assert _sample_timestamps(30.0, 0) == []


# --- resolve_composition: reuses face_detect exactly like render() ------


def test_resolve_composition_facecam_split_when_small_corner_facecam(monkeypatch):
    import clipper.visual_judge as vj

    region = {
        "x": 1524.0,
        "y": 48.8,
        "w": 165.0,
        "h": 165.0,
        "frame_width": 1920,
        "frame_height": 1080,
    }
    monkeypatch.setattr(vj, "find_face_region", lambda *a, **k: region)
    monkeypatch.setattr(vj, "is_small_corner_facecam", lambda r: True)

    composition, face_region = resolve_composition("fake.mp4", 30.0)
    assert composition == "facecam_split"
    assert face_region == region


def test_resolve_composition_tight_crop_when_no_facecam(monkeypatch):
    import clipper.visual_judge as vj

    monkeypatch.setattr(vj, "find_face_region", lambda *a, **k: None)

    composition, face_region = resolve_composition("fake.mp4", 30.0)
    assert composition == "tight_crop"
    assert face_region is None


def test_resolve_composition_tight_crop_when_face_not_corner(monkeypatch):
    import clipper.visual_judge as vj

    region = {
        "x": 444.0,
        "y": 797.0,
        "w": 130.0,
        "h": 130.0,
        "frame_width": 1920,
        "frame_height": 1080,
    }
    monkeypatch.setattr(vj, "find_face_region", lambda *a, **k: region)
    monkeypatch.setattr(vj, "is_small_corner_facecam", lambda r: False)

    composition, face_region = resolve_composition("fake.mp4", 30.0)
    assert composition == "tight_crop"
    assert face_region is None


# --- _build_prompt: facecam question included only for facecam_split -------


def test_build_prompt_includes_facecam_question_for_facecam_split():
    prompt = _build_prompt("facecam_split", 5)
    assert "facecam" in prompt.lower()
    assert "5" in prompt


def test_build_prompt_omits_facecam_question_for_tight_crop():
    prompt_tc = _build_prompt("tight_crop", 5)
    prompt_fc = _build_prompt("facecam_split", 5)
    # tight_crop's prompt is strictly shorter — the facecam bullet is absent.
    assert len(prompt_tc) < len(prompt_fc)


def test_build_prompt_reflects_frame_count():
    prompt = _build_prompt("tight_crop", 3)
    assert "3" in prompt


def test_build_prompt_facecam_split_notes_reaction_for_best_frame():
    prompt_fc = _build_prompt("facecam_split", 5)
    prompt_tc = _build_prompt("tight_crop", 5)
    assert "reaction or expression" in prompt_fc
    assert "reaction or expression" not in prompt_tc


# --- judge_clip: integration, network + ffmpeg mocked -----------------------


def _setup(tmp_path, monkeypatch):
    import clipper.visual_judge as vj
    from clipper import paths

    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "source.mp4").write_bytes(b"fake")
    monkeypatch.setattr(vj, "probe_duration", lambda src: 30.0)
    monkeypatch.setattr(vj, "resolve_composition", lambda src, dur: ("tight_crop", None))
    return wd


def test_judge_clip_missing_source_raises(tmp_path, monkeypatch):
    from clipper import paths

    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        judge_clip("nope")


def test_judge_clip_returns_structured_verdict(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)

    def fake_extract(source, ts, video_fc, out_path):
        out_path.write_bytes(b"fake-png-bytes")

    monkeypatch.setattr(vj, "_extract_preview_frame", fake_extract)
    monkeypatch.setattr(
        vj,
        "_call_model",
        lambda client, prompt, paths_, model: {"passed": False, "reason": "static"},
    )

    result = judge_clip("vid1")
    assert result["passed"] is False
    assert result["reason"] == "static"
    assert result["composition"] == "tight_crop"
    assert result["frames_sampled"] == N_SAMPLE_FRAMES


def test_judge_clip_pass_verdict(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        vj,
        "_extract_preview_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"x"),
    )
    monkeypatch.setattr(
        vj,
        "_call_model",
        lambda client, prompt, paths_, model: {"passed": True, "reason": "real motion throughout"},
    )

    result = judge_clip("vid1")
    assert result["passed"] is True
    assert "motion" in result["reason"]


def test_judge_clip_no_frames_extracted_raises(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)
    # _extract_preview_frame never writes the file -> no frames survive.
    monkeypatch.setattr(vj, "_extract_preview_frame", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="no preview frames"):
        judge_clip("vid1")


def test_judge_clip_uses_facecam_split_video_chain_when_detected(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    wd = _setup(tmp_path, monkeypatch)
    region = {"x": 100, "y": 100, "w": 50, "h": 50, "frame_width": 1920, "frame_height": 1080}
    monkeypatch.setattr(vj, "resolve_composition", lambda src, dur: ("facecam_split", region))

    seen_fc = []

    def fake_facecam_split(r):
        seen_fc.append(r)
        return "FACECAM_CHAIN[comp]"

    monkeypatch.setattr(vj, "_facecam_split_video", fake_facecam_split)
    monkeypatch.setattr(
        vj,
        "_extract_preview_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"x"),
    )
    monkeypatch.setattr(
        vj, "_call_model", lambda client, prompt, paths_, model: {"passed": True, "reason": "fine"}
    )

    result = judge_clip("vid1")
    assert result["composition"] == "facecam_split"
    assert seen_fc == [region]
    del wd  # unused beyond setup


# --- _extract_preview_frame: seeks BEFORE ts, lands ON it, not past it -----
# Real bug found from real output: a naive seek-to-ts-then-keep-last-of-burst
# lands ~_WARMUP_FRAMES/30s PAST ts, not on it — confirmed by extracting both
# and comparing against a real clip. Fixed like render.py's
# pick_loop_freeze_end: seek backward by the lookahead window instead.


def test_extract_preview_frame_seeks_before_ts_not_at_it(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    seen_args = []

    def fake_run_ffmpeg(args):
        seen_args.append(args)
        # No real ffmpeg — nothing gets written to the pattern path, which is
        # fine, this test only checks the seek argument that was requested.
        return None

    monkeypatch.setattr(vj, "run_ffmpeg", fake_run_ffmpeg)

    vj._extract_preview_frame("fake.mp4", 31.35, "TC[comp]", tmp_path / "out.png")

    args = seen_args[0]
    seek_value = float(args[args.index("-ss") + 1])
    expected = 31.35 - vj._WARMUP_FRAMES / 30.0
    assert seek_value == pytest.approx(expected, abs=0.001)
    assert seek_value < 31.35


def test_extract_preview_frame_seek_never_goes_negative(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    seen_args = []
    monkeypatch.setattr(vj, "run_ffmpeg", lambda args: seen_args.append(args))

    vj._extract_preview_frame("fake.mp4", 0.05, "TC[comp]", tmp_path / "out.png")

    args = seen_args[0]
    seek_value = float(args[args.index("-ss") + 1])
    assert seek_value == 0.0


# --- best_frame_index -> best_frame_timestamp mapping ----------------------
# The model's index refers to the frames it was actually shown (frame_paths'
# order), not the original timestamps list — a failed extraction can leave
# gaps, so these two lists can diverge. See judge_clip()'s comment.


def test_judge_clip_maps_best_frame_index_to_its_timestamp(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(vj, "_sample_timestamps", lambda duration, n: [1.0, 2.0, 3.0])
    monkeypatch.setattr(
        vj,
        "_extract_preview_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"x"),
    )
    monkeypatch.setattr(
        vj,
        "_call_model",
        lambda client, prompt, paths_, model: {
            "passed": True,
            "reason": "fine",
            "best_frame_index": 2,
        },
    )

    result = judge_clip("vid1")
    assert result["best_frame_timestamp"] == 3.0


def test_judge_clip_best_frame_index_accounts_for_extraction_gaps(tmp_path, monkeypatch):
    # Frame 1 (ts=2.0) fails to extract — frame_paths only has [1.0, 3.0]'s
    # frames, so the model's index 1 (second image it was actually shown)
    # must map to 3.0, not to the original list's index-1 timestamp (2.0).
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(vj, "_sample_timestamps", lambda duration, n: [1.0, 2.0, 3.0])

    def fake_extract(source, ts, video_fc, out_path):
        if ts != 2.0:
            out_path.write_bytes(b"x")

    monkeypatch.setattr(vj, "_extract_preview_frame", fake_extract)
    monkeypatch.setattr(
        vj,
        "_call_model",
        lambda client, prompt, paths_, model: {
            "passed": True,
            "reason": "fine",
            "best_frame_index": 1,
        },
    )

    result = judge_clip("vid1")
    assert result["frames_sampled"] == 2
    assert result["best_frame_timestamp"] == 3.0


def test_judge_clip_out_of_range_best_frame_index_falls_back_to_zero(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(vj, "_sample_timestamps", lambda duration, n: [5.0, 6.0])
    monkeypatch.setattr(
        vj,
        "_extract_preview_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"x"),
    )
    monkeypatch.setattr(
        vj,
        "_call_model",
        lambda client, prompt, paths_, model: {
            "passed": True,
            "reason": "fine",
            "best_frame_index": 99,
        },
    )

    result = judge_clip("vid1")
    assert result["best_frame_timestamp"] == 5.0


def test_judge_clip_default_model_is_sonnet():
    from clipper.visual_judge import JUDGE_MODEL

    assert JUDGE_MODEL == "claude-sonnet-5"


# --- caching: a real per-clip API cost must never be paid twice -------------


def test_judge_clip_cache_hit_skips_the_model_call(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    wd = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        vj, "_call_model", lambda *a, **k: calls.append(1) or {"passed": True, "reason": "x"}
    )
    monkeypatch.setattr(
        vj,
        "_extract_preview_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"x"),
    )

    first = judge_clip("vid1")
    assert len(calls) == 1
    assert (wd / "visual_judge.json").exists()

    second = judge_clip("vid1")
    assert len(calls) == 1  # not called again
    assert second == first


def test_judge_clip_force_recalls_the_model(tmp_path, monkeypatch):
    import clipper.visual_judge as vj

    _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        vj, "_call_model", lambda *a, **k: calls.append(1) or {"passed": True, "reason": "x"}
    )
    monkeypatch.setattr(
        vj,
        "_extract_preview_frame",
        lambda source, ts, video_fc, out_path: out_path.write_bytes(b"x"),
    )

    judge_clip("vid1")
    judge_clip("vid1", force=True)
    assert len(calls) == 2


def test_judge_clip_cache_hit_does_not_require_source_to_exist(tmp_path, monkeypatch):
    # Once cached, the verdict must be servable even if source.mp4 were
    # since removed — mirrors every other stage's cache-hit-skips-the-
    # existence-check behavior.
    import json

    import clipper.visual_judge as vj
    from clipper import paths

    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "visual_judge.json").write_text(
        json.dumps(
            {"passed": True, "reason": "cached", "composition": "tight_crop", "frames_sampled": 5}
        )
    )

    result = vj.judge_clip("vid1")
    assert result["reason"] == "cached"
