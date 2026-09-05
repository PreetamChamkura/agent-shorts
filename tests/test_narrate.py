"""Unit tests for stage 3.5. The one networked call (_synthesize) is monkeypatched
— no real TTS call, no network, per CLAUDE.md."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipper import narrate as narrate_mod
from clipper import paths


def _setup(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    monkeypatch.setattr(paths, "TTS_CACHE_DIR", tmp_path / "_tts_cache")
    monkeypatch.setattr(narrate_mod, "TTS_CACHE_DIR", tmp_path / "_tts_cache")
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


# --- duration-fit check (cold_open pre-roll pushing past the 58s cap) ------


def test_check_duration_fit_ignores_pre_punchline():
    seg = {"n": 0, "vo_placement": "pre_punchline", "start": 0.0, "end": 55.0}
    assert narrate_mod._check_duration_fit(seg, Path("/does/not/matter.wav")) is False
    assert "vo_duration_exceeded" not in seg


def test_check_duration_fit_passes_within_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 3.0)
    seg = {"n": 0, "vo_placement": "cold_open", "start": 0.0, "end": 40.0}
    changed = narrate_mod._check_duration_fit(seg, tmp_path / "vo_0.wav")
    assert changed is False
    assert "vo_duration_exceeded" not in seg


def test_check_duration_fit_flags_and_deletes_when_over_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 10.0)
    vo_path = tmp_path / "vo_0.wav"
    vo_path.write_bytes(b"AUDIO")
    seg = {"n": 0, "vo_placement": "cold_open", "start": 0.0, "end": 55.0}  # 55+10=65 > 58

    changed = narrate_mod._check_duration_fit(seg, vo_path)
    assert changed is True
    assert seg["vo_duration_exceeded"] is True
    assert not vo_path.exists()


def test_check_duration_fit_clears_stale_flag_once_it_fits(tmp_path, monkeypatch):
    # e.g. a fresh stage-3 re-select shortened the same n's clip window.
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 3.0)
    seg = {
        "n": 0,
        "vo_placement": "cold_open",
        "start": 0.0,
        "end": 40.0,
        "vo_duration_exceeded": True,
    }
    changed = narrate_mod._check_duration_fit(seg, tmp_path / "vo_0.wav")
    assert changed is True  # the flag was cleared, so the dict did change
    assert "vo_duration_exceeded" not in seg


def test_narrate_flags_cold_open_vo_exceeding_duration(tmp_path, monkeypatch):
    wd = _setup(
        tmp_path,
        monkeypatch,
        [{"n": 0, "vo_script": "a line", "vo_placement": "cold_open", "start": 0.0, "end": 55.0}],
    )
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"AUDIOBYTES")
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 10.0)  # 55+10=65 > 58
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    out = narrate_mod.narrate("vid1")
    seg = out["segments"][0]
    assert seg["vo_duration_exceeded"] is True
    assert not (wd / "vo_0.wav").exists()

    on_disk = json.loads((wd / "segments.json").read_text())
    assert on_disk["segments"][0]["vo_duration_exceeded"] is True


def test_narrate_keeps_content_cache_when_duration_exceeded(tmp_path, monkeypatch):
    # The line itself is fine — just not usable as this segment's pre-roll.
    # Never delete the shared content cache over a per-segment fit failure.
    _setup(
        tmp_path,
        monkeypatch,
        [{"n": 0, "vo_script": "a line", "vo_placement": "cold_open", "start": 0.0, "end": 55.0}],
    )
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: b"AUDIOBYTES")
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 10.0)
    monkeypatch.setenv("TTS_API_KEY", "fake-key")

    narrate_mod.narrate("vid1")
    cache_path = tmp_path / "_tts_cache" / f"{narrate_mod.script_hash('a line')}.wav"
    assert cache_path.exists()
    assert cache_path.read_bytes() == b"AUDIOBYTES"


def test_narrate_rechecks_duration_fit_on_cache_hit(tmp_path, monkeypatch):
    # vo_0.wav already exists from a prior run that predates this check (or
    # predates a segments.json re-select that shortened/lengthened the clip)
    # — the fit must still be enforced, not skipped just because it's cached.
    wd = _setup(
        tmp_path,
        monkeypatch,
        [{"n": 0, "vo_script": "a line", "vo_placement": "cold_open", "start": 0.0, "end": 55.0}],
    )
    (wd / "vo_0.wav").write_bytes(b"OLD_AUDIO")
    calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: calls.append(1) or b"x")
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: 10.0)

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
                "end": 55.0,
                "vo_duration_exceeded": True,
            }
        ],
    )
    probe_calls = []
    monkeypatch.setattr(narrate_mod, "probe_duration", lambda p: probe_calls.append(1) or 10.0)
    synth_calls = []
    monkeypatch.setattr(narrate_mod, "_synthesize", lambda *a, **k: synth_calls.append(1) or b"x")

    narrate_mod.narrate("vid1")
    assert probe_calls == []
    assert synth_calls == []
