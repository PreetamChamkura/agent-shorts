"""Unit tests for stage 3.5. The one networked call (_synthesize) is monkeypatched
— no real TTS call, no network, per CLAUDE.md."""

from __future__ import annotations

import json

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
