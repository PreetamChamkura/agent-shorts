"""Unit tests for CLI arg resolution. No network, no ffmpeg."""

from __future__ import annotations

import argparse
import json

import pytest

from clipper import paths
from clipper.cli import (
    _manual_spec,
    _print_clip_list,
    _segment_from_file,
    _vo_kwargs,
    _write_clips_out,
)


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


def _patch_stages(monkeypatch, *, judge_verdict, seg=None, thumbnail_error=None):
    import clipper.ingest as ingest_mod
    import clipper.narrate as narrate_mod
    import clipper.render as render_mod
    import clipper.select as select_mod
    import clipper.thumbnail as thumbnail_mod
    import clipper.transcribe as transcribe_mod
    import clipper.visual_judge as visual_judge_mod

    monkeypatch.setattr(ingest_mod, "ingest", lambda url, **k: "clip123")
    monkeypatch.setattr(transcribe_mod, "transcribe", lambda video_id, **k: None)
    monkeypatch.setattr(visual_judge_mod, "judge_clip", lambda video_id, **k: judge_verdict)

    calls = {"select": 0, "select_clip": 0, "narrate": 0, "render": 0, "thumbnail": 0}

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

    def fake_generate_thumbnail(*a, **k):
        calls["thumbnail"] += 1
        if thumbnail_error is not None:
            raise thumbnail_error
        return "thumbnail_0.jpg"

    monkeypatch.setattr(thumbnail_mod, "generate_thumbnail", fake_generate_thumbnail)
    monkeypatch.setattr(thumbnail_mod, "write_metadata", lambda *a, **k: "metadata_0.json")

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
        "thumbnail_text": "SOME QUOTE",
        "punchline_at": 15.0,
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
    assert calls["thumbnail"] == 1


# --- clip command: thumbnail as the automatic last step --------------------


def test_clip_command_generates_thumbnail_after_render(monkeypatch):
    from clipper.cli import main

    seg = {
        "n": 0,
        "start": 0.0,
        "end": 20.0,
        "tone": "confident",
        "vo_script": None,
        "loopable": False,
        "thumbnail_text": "SOME QUOTE",
        "punchline_at": 15.0,
    }
    calls = _patch_stages(
        monkeypatch,
        judge_verdict={"passed": True, "reason": "good", "composition": "tight_crop"},
        seg=seg,
    )

    main(["clip", TWITCH_CLIP_URL])
    assert calls["thumbnail"] == 1


def test_clip_command_skips_thumbnail_for_manual_mode(monkeypatch):
    from clipper.cli import main

    calls = _patch_stages(
        monkeypatch, judge_verdict={"passed": True, "reason": "good", "composition": "tight_crop"}
    )

    main(
        [
            "clip",
            "https://www.youtube.com/watch?v=abc123",
            "--start",
            "0.0",
            "--end",
            "20.0",
            "--tone",
            "confident",
        ]
    )
    assert calls["thumbnail"] == 0
    assert calls["render"] == 1  # the render itself still ran and succeeded


def test_clip_command_thumbnail_failure_does_not_fail_the_run(monkeypatch, capsys, caplog):
    from clipper.cli import main

    seg = {
        "n": 0,
        "start": 0.0,
        "end": 20.0,
        "tone": "confident",
        "vo_script": None,
        "loopable": False,
        "thumbnail_text": "SOME QUOTE",
        "punchline_at": 15.0,
    }
    calls = _patch_stages(
        monkeypatch,
        judge_verdict={"passed": True, "reason": "good", "composition": "tight_crop"},
        seg=seg,
        thumbnail_error=RuntimeError("no face found anywhere"),
    )

    with caplog.at_level("WARNING"):
        main(["clip", TWITCH_CLIP_URL])  # must not raise despite the thumbnail error

    assert calls["render"] == 1
    assert calls["thumbnail"] == 1
    assert "out.mp4" in capsys.readouterr().out  # render's result was still printed
    assert any("thumbnail generation failed" in r.message for r in caplog.records)


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


# --- _print_clip_list / _write_clips_out: shared by fetch-clips and --------
# --- fetch-trending ----------------------------------------------------


def _clip(id_, views=10, discovery=None, gate=None):
    c = {
        "id": id_,
        "view_count": views,
        "created_at": "2026-01-01T00:00:00Z",
        "duration": 30.0,
        "title": "a title",
    }
    if discovery is not None:
        c["discovery"] = discovery
    if gate is not None:
        c["visual_gate"] = {"layer_1": gate}
    return c


def test_print_clip_list_shows_discovery_mode(capsys):
    clips = [_clip("a", discovery={"mode": "trending", "channel": "shroud"})]
    _print_clip_list(clips)
    out = capsys.readouterr().out
    assert "[trending:shroud]" in out


def test_print_clip_list_shows_channel_discovery_mode(capsys):
    clips = [_clip("a", discovery={"mode": "channel", "channel": "shroud"})]
    _print_clip_list(clips)
    out = capsys.readouterr().out
    assert "[channel:shroud]" in out


def test_print_clip_list_no_discovery_tag_when_absent(capsys):
    _print_clip_list([_clip("a")])
    out = capsys.readouterr().out
    assert "[" not in out.split("a")[0]  # nothing bracketed before the id


def test_print_clip_list_shows_viewers_followers_language_for_trending(capsys):
    clips = [
        _clip(
            "a",
            discovery={
                "mode": "trending",
                "channel": "shroud",
                "viewer_count_at_discovery": 5000,
                "follower_count_at_discovery": 500_000,
                "language": "en",
            },
        )
    ]
    _print_clip_list(clips)
    out = capsys.readouterr().out
    assert "lang=en" in out
    assert "viewers=5,000" in out
    assert "followers=500,000" in out


def test_print_clip_list_counts_layer1_rejections(capsys):
    clips = [
        _clip(
            "a",
            gate={
                "passed": False,
                "reason": "blank",
                "variance": 1,
                "dark_fraction": 1,
                "edge_density": 1,
            },
        ),
        _clip("b", gate={"passed": True, "variance": 500, "dark_fraction": 0.1, "edge_density": 5}),
    ]
    n_rejected = _print_clip_list(clips)
    assert n_rejected == 1


def test_write_clips_out_noop_when_no_path(tmp_path):
    # Must not raise or create anything when --out wasn't given.
    _write_clips_out(None, [_clip("a")], channel="shroud")


def test_write_clips_out_includes_channel_when_given(tmp_path):
    out_path = tmp_path / "out.json"
    _write_clips_out(str(out_path), [_clip("a")], channel="shroud")
    data = json.loads(out_path.read_text())
    assert data["channel"] == "shroud"
    assert len(data["clips"]) == 1


def test_write_clips_out_omits_channel_key_for_trending(tmp_path):
    out_path = tmp_path / "out.json"
    _write_clips_out(str(out_path), [_clip("a")], channel=None)
    data = json.loads(out_path.read_text())
    assert "channel" not in data


# --- thumbnail command dispatch: standalone, not wired into clip/render ----


def test_thumbnail_command_calls_generate_thumbnail_and_write_metadata(monkeypatch):
    import clipper.cli as cli_mod
    import clipper.thumbnail as thumbnail_mod
    from clipper.cli import main

    seg = {
        "n": 0,
        "start": 0.0,
        "end": 20.0,
        "tone": "confident",
        "thumbnail_text": "HE HAD NO IDEA",
    }
    monkeypatch.setattr(cli_mod, "_segment_from_file", lambda video_id, n: seg)

    seen_thumb = {}
    seen_meta = {}
    monkeypatch.setattr(
        thumbnail_mod,
        "generate_thumbnail",
        lambda video_id, n, start, end, tone, text, **k: (
            seen_thumb.update(
                video_id=video_id, n=n, start=start, end=end, tone=tone, text=text, **k
            )
            or "work/vid1/thumbnail_0.jpg"
        ),
    )
    monkeypatch.setattr(
        thumbnail_mod,
        "write_metadata",
        lambda video_id, n, s, **k: (
            seen_meta.update(video_id=video_id, n=n, **k) or "work/vid1/metadata_0.json"
        ),
    )

    main(["thumbnail", "vid1", "--n", "0", "--force"])

    assert seen_thumb["video_id"] == "vid1"
    assert seen_thumb["text"] == "HE HAD NO IDEA"
    assert seen_thumb["force"] is True
    assert seen_meta["force"] is True


# --- fetch-trending command dispatch ----------------------------------------


def test_fetch_trending_command_calls_fetch_trending_clips(monkeypatch, capsys):
    import clipper.twitch as twitch_mod
    from clipper.cli import main

    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setattr(twitch_mod, "get_app_access_token", lambda **k: "tok")

    seen = {}

    def fake_fetch_trending(**k):
        seen.update(k)
        return [_clip("x", discovery={"mode": "trending", "channel": "shroud"})]

    monkeypatch.setattr(twitch_mod, "fetch_trending_clips", fake_fetch_trending)

    main(["fetch-trending", "--limit", "7", "--top-channels", "12", "--no-visual-gate"])

    assert seen["limit"] == 7
    assert seen["top_channels"] == 12
    assert seen["language"] == "en"  # default
    assert seen["min_followers"] == 250_000  # default
    out = capsys.readouterr().out
    assert "trending clip(s)" in out


def test_fetch_trending_command_forwards_language_and_min_followers(monkeypatch):
    import clipper.twitch as twitch_mod
    from clipper.cli import main

    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setattr(twitch_mod, "get_app_access_token", lambda **k: "tok")

    seen = {}

    def fake_fetch_trending(**k):
        seen.update(k)
        return []

    monkeypatch.setattr(twitch_mod, "fetch_trending_clips", fake_fetch_trending)

    main(
        [
            "fetch-trending",
            "--language",
            "",
            "--min-followers",
            "0",
            "--no-visual-gate",
        ]
    )

    # "" / 0 mean "disable this filter" — must reach fetch_trending_clips as
    # None, not as a falsy string/zero that a naive `or None` might mishandle
    # differently, and definitely not silently left at the "en"/250000 default.
    assert seen["language"] is None
    assert seen["min_followers"] is None
