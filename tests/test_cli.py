"""Unit tests for CLI arg resolution. No network, no ffmpeg."""

from __future__ import annotations

import argparse
import json

import pytest

from clipper import paths
from clipper.cli import _manual_spec, _segment_from_file, _vo_kwargs


def _args(**over):
    base = {"start": None, "end": None, "tone": None, "n": 0}
    base.update(over)
    return argparse.Namespace(**base)


def test_manual_spec_true_when_all_three_given():
    assert _manual_spec(_args(start=1.0, end=30.0, tone="burn")) is True


def test_manual_spec_false_when_none_given():
    assert _manual_spec(_args()) is False


@pytest.mark.parametrize(
    "partial",
    [
        {"start": 1.0},
        {"end": 30.0},
        {"tone": "burn"},
        {"start": 1.0, "end": 30.0},  # missing tone
    ],
)
def test_manual_spec_rejects_partial_set(partial):
    # A half-manual spec is an error, not a guess.
    with pytest.raises(SystemExit):
        _manual_spec(_args(**partial))


def _seed_segments(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "segments.json").write_text(json.dumps({"video_id": "vid1", "segments": segments}))


def test_segment_from_file_reads_the_right_index(tmp_path, monkeypatch):
    _seed_segments(
        tmp_path,
        monkeypatch,
        [
            {"n": 0, "start": 10.0, "end": 40.0, "tone": "burn"},
            {"n": 1, "start": 100.0, "end": 130.0, "tone": "wholesome"},
        ],
    )
    seg = _segment_from_file("vid1", 1)
    assert (seg["start"], seg["end"], seg["tone"]) == (100.0, 130.0, "wholesome")


def test_segment_from_file_missing_file_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    with pytest.raises(SystemExit, match="segments.json"):
        _segment_from_file("nope", 0)


def test_segment_from_file_missing_index_fails_loudly(tmp_path, monkeypatch):
    _seed_segments(tmp_path, monkeypatch, [{"n": 0, "start": 1.0, "end": 20.0, "tone": "burn"}])
    with pytest.raises(SystemExit, match="no segment n=5"):
        _segment_from_file("vid1", 5)


# --- VO kwarg derivation -----------------------------------------------------


def test_vo_kwargs_empty_when_vo_script_null():
    assert _vo_kwargs({"vo_script": None}) == {}


def test_vo_kwargs_empty_when_tts_failed():
    seg = {
        "vo_script": "line",
        "tts_failed": True,
        "punchline_at": 10.0,
        "vo_placement": "cold_open",
    }
    assert _vo_kwargs(seg) == {}


def test_vo_kwargs_present_for_good_segment():
    seg = {
        "vo_script": "a line",
        "punchline_at": 37.1,
        "vo_placement": "pre_punchline",
    }
    assert _vo_kwargs(seg) == {"punchline_at": 37.1, "vo_placement": "pre_punchline"}


# --- clip command: visual gate layer 3 wired before select_clip -----------
# Every stage function is monkeypatched at its source module — cli.py's
# `clip` branch does `from .X import Y` lazily inside the function, which
# re-binds at call time, so patching clipper.X.Y before calling cli.main()
# is the correct interception point.


TWITCH_CLIP_URL = "https://clips.twitch.tv/SomeClipSlug"


def _patch_stages(monkeypatch, *, judge_verdict, seg=None):
    import clipper.ingest as ingest_mod
    import clipper.narrate as narrate_mod
    import clipper.render as render_mod
    import clipper.select as select_mod
    import clipper.transcribe as transcribe_mod
    import clipper.visual_judge as visual_judge_mod

    monkeypatch.setattr(ingest_mod, "ingest", lambda url, **k: "clip123")
    monkeypatch.setattr(transcribe_mod, "transcribe", lambda video_id, **k: None)
    monkeypatch.setattr(visual_judge_mod, "judge_clip", lambda video_id, **k: judge_verdict)

    calls = {"select": 0, "select_clip": 0, "narrate": 0, "render": 0}

    def fake_select_clip(video_id, **k):
        calls["select_clip"] += 1

    def fake_select(video_id, **k):
        calls["select"] += 1

    monkeypatch.setattr(select_mod, "select_clip", fake_select_clip)
    monkeypatch.setattr(select_mod, "select", fake_select)
    monkeypatch.setattr(
        narrate_mod, "narrate", lambda video_id, **k: calls.__setitem__("narrate", 1)
    )
    monkeypatch.setattr(
        render_mod, "render", lambda *a, **k: calls.__setitem__("render", 1) or "out.mp4"
    )

    if seg is not None:
        import clipper.cli as cli_mod

        monkeypatch.setattr(cli_mod, "_segment_from_file", lambda video_id, n: seg)

    return calls


def test_clip_command_stops_before_select_clip_on_visual_reject(monkeypatch, capsys):
    from clipper.cli import main

    calls = _patch_stages(
        monkeypatch,
        judge_verdict={"passed": False, "reason": "static", "composition": "tight_crop"},
    )

    with pytest.raises(SystemExit, match="rejected by the visual gate"):
        main(["clip", TWITCH_CLIP_URL])

    assert calls["select_clip"] == 0
    assert calls["narrate"] == 0
    assert calls["render"] == 0


def test_clip_command_proceeds_to_select_clip_on_visual_pass(monkeypatch):
    from clipper.cli import main

    seg = {
        "n": 0,
        "start": 0.0,
        "end": 20.0,
        "tone": "confident",
        "vo_script": None,
        "loopable": False,
    }
    calls = _patch_stages(
        monkeypatch,
        judge_verdict={"passed": True, "reason": "good", "composition": "facecam_split"},
        seg=seg,
    )

    main(["clip", TWITCH_CLIP_URL])

    assert calls["select_clip"] == 1
    assert calls["narrate"] == 1
    assert calls["render"] == 1


def test_clip_command_skips_visual_gate_for_non_twitch_url(monkeypatch):
    import clipper.visual_judge as visual_judge_mod
    from clipper.cli import main

    def fail_judge(*a, **k):
        pytest.fail("judge_clip should not be called for a non-Twitch-clip URL")

    monkeypatch.setattr(visual_judge_mod, "judge_clip", fail_judge)
    seg = {
        "n": 0,
        "start": 0.0,
        "end": 20.0,
        "tone": "confident",
        "vo_script": None,
        "loopable": False,
    }
    _patch_stages(monkeypatch, judge_verdict={"passed": True, "reason": "x"}, seg=seg)

    main(["clip", "https://www.youtube.com/watch?v=abc123"])
