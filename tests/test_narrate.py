"""Unit tests for stage 3.5. The two networked/heavy calls (_synthesize,
transcribe_audio_file) are monkeypatched — no real TTS call, no real whisper,
no network, per CLAUDE.md."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipper import narrate as narrate_mod
from clipper import paths

_FAKE_WORDS = {
    "language": "en",
    "duration": 1.0,
    "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
}


def _setup(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    monkeypatch.setattr(paths, "TTS_CACHE_DIR", tmp_path / "_tts_cache")
    monkeypatch.setattr(narrate_mod, "TTS_CACHE_DIR", tmp_path / "_tts_cache")
    # transcribe_audio_file is only reached once real audio bytes are cached
    # (see _ensure_words_cache) — real whisper can't decode fake test bytes,
    # so every test that gets that far needs this mocked regardless of
    # whether it's the thing under test.
    monkeypatch.setattr(narrate_mod, "transcribe_audio_file", lambda path: dict(_FAKE_WORDS))
    wd = tmp_path / "vid1"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "segments.json").write_text(json.dumps({"video_id": "vid1", "segments": segments}))
    return wd


def test_script_hash_is_stable_sha256():
    h1 = narrate_mod.script_hash("hello world")
    h2 = narrate_mod.script_hash("hello world")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest


def test_null_vo_script_is_skipped_no_call(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": None}])
    calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"")

    out = narrate_mod.narrate("vid1")
    assert calls == []
    assert not (wd / "vo_0.wav").exists()
    assert out["segments"][0].get("tts_failed") is None


def test_missing_segments_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        narrate_mod.narrate("nope")


def test_synthesizes_and_writes_vo_file(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a narrator line"}])
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"FAKEWAVDATA")
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    assert (wd / "vo_0.wav").read_bytes() == b"FAKEWAVDATA"


def test_synthesizes_and_writes_words_json(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a narrator line"}])
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"FAKEWAVDATA")
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    words = json.loads((wd / "vo_0.words.json").read_text())
    assert words == _FAKE_WORDS


def test_second_call_never_repays_content_cache(tmp_path, monkeypatch):
    """CLAUDE.md: 'never pay twice for the same line' — even with --force."""
    wd = _setup(
        tmp_path,
        monkeypatch,
        [
            {"n": 0, "vo_script": "the exact same line"},
            {"n": 1, "vo_script": "the exact same line"},
        ],
    )
    calls = []
    monkeypatch.setattr(
        narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"AUDIOBYTES"
    )
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    assert len(calls) == 1  # only one real call for two identical scripts
    assert (wd / "vo_0.wav").read_bytes() == b"AUDIOBYTES"
    assert (wd / "vo_1.wav").read_bytes() == b"AUDIOBYTES"
    assert (wd / "vo_0.words.json").exists()
    assert (wd / "vo_1.words.json").exists()


def test_transcribe_audio_file_called_once_for_two_identical_lines(tmp_path, monkeypatch):
    wd = _setup(
        tmp_path,
        monkeypatch,
        [
            {"n": 0, "vo_script": "the exact same line"},
            {"n": 1, "vo_script": "the exact same line"},
        ],
    )
    transcribe_calls = []
    monkeypatch.setattr(
        narrate_mod,
        "transcribe_audio_file",
        lambda path: transcribe_calls.append(1) or dict(_FAKE_WORDS),
    )
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"AUDIOBYTES")
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    assert len(transcribe_calls) == 1
    assert (wd / "vo_0.words.json").exists()
    assert (wd / "vo_1.words.json").exists()


def test_force_recopies_but_does_not_resynthesize(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a line"}])
    calls = []
    monkeypatch.setattr(
        narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"AUDIOBYTES"
    )
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    assert len(calls) == 1

    narrate_mod.narrate("vid1", force=True)
    assert len(calls) == 1  # force re-copies from content cache, never re-calls TTS
    assert (wd / "vo_0.wav").read_bytes() == b"AUDIOBYTES"


def test_without_force_cached_vo_file_skips_entirely(tmp_path, monkeypatch):
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a line"}])
    (wd / "vo_0.wav").write_bytes(b"ALREADY_THERE")
    calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"NEW")

    narrate_mod.narrate("vid1")
    assert calls == []
    assert (wd / "vo_0.wav").read_bytes() == b"ALREADY_THERE"


def test_cache_hit_backfills_words_json_from_content_cache_only(tmp_path, monkeypatch):
    # vo_0.wav exists (a line synthesized before vo_*.words.json existed) and
    # its audio is ALSO already in the content cache (as it always is in real
    # usage) — backfilling the words file must use that, never call TTS.
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a line"}])
    digest = narrate_mod.script_hash("a line")
    cache_dir = tmp_path / "_tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{digest}.wav").write_bytes(b"CACHED_AUDIO")
    (wd / "vo_0.wav").write_bytes(b"CACHED_AUDIO")

    synth_calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: synth_calls.append(1) or b"x")

    narrate_mod.narrate("vid1")
    assert synth_calls == []
    assert (wd / "vo_0.words.json").exists()
    assert (cache_dir / f"{digest}.words.json").exists()


def test_cache_hit_without_content_cache_leaves_words_json_absent(tmp_path, monkeypatch):
    # vo_0.wav exists with no matching content-cache entry (synthetic/
    # orphaned state) — must not call TTS just to backfill words.json; it's
    # fine for the caption merge to simply not have data to work with.
    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a line"}])
    (wd / "vo_0.wav").write_bytes(b"ORPHANED_AUDIO")
    synth_calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: synth_calls.append(1) or b"x")

    narrate_mod.narrate("vid1")
    assert synth_calls == []
    assert not (wd / "vo_0.words.json").exists()


def test_tts_failure_flags_segment_and_does_not_raise(tmp_path, monkeypatch):
    import requests

    wd = _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a line"}])

    def _boom(*a, **k):
        raise requests.RequestException("network exploded")

    monkeypatch.setattr(narrate_mod, "_synthesize", _boom)
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    out = narrate_mod.narrate("vid1")
    assert out["segments"][0]["tts_failed"] is True
    assert not (wd / "vo_0.wav").exists()

    on_disk = json.loads((wd / "segments.json").read_text())
    assert on_disk["segments"][0]["tts_failed"] is True


def test_missing_api_key_raises_before_any_call(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [{"n": 0, "vo_script": "a line"}])
    monkeypatch.delenv("TTS_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"x")

    with pytest.raises(RuntimeError, match="TTS_API_KEY"):
        narrate_mod.narrate("vid1")
    assert calls == []


# --- duration-fit check (cold_open VO consuming the whole clip) -------------
#
# cold_open no longer prepends a pre-roll — it plays over the clip's own
# opening seconds, muted — so it no longer lengthens the render. The only
# way it still can't be used is a VO long enough to consume the clip itself
# (MIN_CLIP_AFTER_VO margin), not the old "combined length over 58s" check.


def test_check_duration_fit_ignores_pre_punchline():
    seg = {"n": 0, "vo_placement": "pre_punchline", "start": 0.0, "end": 15.0}
    assert narrate_mod._check_duration_fit(seg, Path("/does/not/matter.wav")) is False
    assert "vo_duration_exceeded" not in seg


def test_check_duration_fit_passes_within_bound(tmp_path, monkeypatch):
    # 55s clip, 10s VO — comfortably leaves 45s of clip after it. This is
    # exactly the shape the OLD additive check would have wrongly rejected.
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 10.0)
    seg = {"n": 0, "vo_placement": "cold_open", "start": 0.0, "end": 55.0}
    changed = narrate_mod._check_duration_fit(seg, tmp_path / "vo_0.wav")
    assert changed is False
    assert "vo_duration_exceeded" not in seg


def test_check_duration_fit_flags_and_deletes_when_vo_consumes_clip(tmp_path, monkeypatch):
    # 15s clip, 13s VO — only 2s of clip left after it, under the 3s margin.
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 13.0)
    vo_path = tmp_path / "vo_0.wav"
    vo_path.write_bytes(b"AUDIO")
    seg = {"n": 0, "vo_placement": "cold_open", "start": 0.0, "end": 15.0}

    changed = narrate_mod._check_duration_fit(seg, vo_path)
    assert changed is True
    assert seg["vo_duration_exceeded"] is True
    assert not vo_path.exists()


def test_check_duration_fit_clears_stale_flag_once_it_fits(tmp_path, monkeypatch):
    # e.g. a fresh stage-3 re-select lengthened the same n's clip window.
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 10.0)
    seg = {
        "n": 0,
        "vo_placement": "cold_open",
        "start": 0.0,
        "end": 55.0,
        "vo_duration_exceeded": True,
    }
    changed = narrate_mod._check_duration_fit(seg, tmp_path / "vo_0.wav")
    assert changed is True  # the flag was cleared, so the dict did change
    assert "vo_duration_exceeded" not in seg


def test_narrate_flags_cold_open_vo_consuming_clip(tmp_path, monkeypatch):
    wd = _setup(
        tmp_path,
        monkeypatch,
        [{"n": 0, "vo_script": "a line", "vo_placement": "cold_open", "start": 0.0, "end": 15.0}],
    )
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"AUDIOBYTES")
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 13.0)  # 15-13=2 < 3s margin
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    out = narrate_mod.narrate("vid1")
    seg = out["segments"][0]
    assert seg["vo_duration_exceeded"] is True
    assert not (wd / "vo_0.wav").exists()

    on_disk = json.loads((wd / "segments.json").read_text())
    assert on_disk["segments"][0]["vo_duration_exceeded"] is True


def test_narrate_keeps_content_cache_when_duration_exceeded(tmp_path, monkeypatch):
    # The line itself is fine — just not usable as this segment's cold open.
    # Never delete the shared content cache over a per-segment fit failure.
    _setup(
        tmp_path,
        monkeypatch,
        [{"n": 0, "vo_script": "a line", "vo_placement": "cold_open", "start": 0.0, "end": 15.0}],
    )
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"AUDIOBYTES")
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 13.0)
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    cache_path = tmp_path / "_tts_cache" / f"{narrate_mod.script_hash('a line')}.wav"
    assert cache_path.exists()
    assert cache_path.read_bytes() == b"AUDIOBYTES"


def test_narrate_rechecks_duration_fit_on_cache_hit(tmp_path, monkeypatch):
    # vo_0.wav already exists from a prior run that predates this check (or
    # predates a segments.json re-select that shortened the clip) — the fit
    # must still be enforced, not skipped just because it's cached.
    wd = _setup(
        tmp_path,
        monkeypatch,
        [{"n": 0, "vo_script": "a line", "vo_placement": "cold_open", "start": 0.0, "end": 15.0}],
    )
    (wd / "vo_0.wav").write_bytes(b"OLD_AUDIO")
    calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"x")
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 13.0)

    out = narrate_mod.narrate("vid1")
    assert calls == []  # never re-synthesizes just to run this check
    assert out["segments"][0]["vo_duration_exceeded"] is True
    assert not (wd / "vo_0.wav").exists()


def test_narrate_short_circuits_when_already_flagged(tmp_path, monkeypatch):
    _setup(
        tmp_path,
        monkeypatch,
        [
            {
                "n": 0,
                "vo_script": "a line",
                "vo_placement": "cold_open",
                "start": 0.0,
                "end": 15.0,
                "vo_duration_exceeded": True,
            }
        ],
    )
    probe_calls = []
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: probe_calls.append(1) or 13.0)
    synth_calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: synth_calls.append(1) or b"x")

    narrate_mod.narrate("vid1")
    assert probe_calls == []
    assert synth_calls == []
