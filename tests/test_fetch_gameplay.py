"""Unit tests for the filler-footage downloader's pure logic. No network."""

from __future__ import annotations

import json

import pytest

from clipper import fetch_gameplay as fg
from clipper import paths


def _files(*sizes):
    return [{"width": w, "height": h, "link": f"http://x/{w}x{h}.mp4"} for w, h in sizes]


# --- rendition picking --------------------------------------------------------


def test_best_file_picks_highest_resolution():
    best = fg._best_file(_files((1080, 1920), (2160, 3840), (1080, 1080)))
    assert best["width"] == 2160


def test_best_file_rejects_below_min_short_side():
    # 640x360 and 1280x720 both have a short side under 1080
    assert fg._best_file(_files((640, 360), (1280, 720))) is None


def test_best_file_accepts_landscape_1080_short_side():
    best = fg._best_file(_files((1920, 1080)))
    assert best is not None


def test_best_file_ignores_entries_without_link():
    assert fg._best_file([{"width": 1920, "height": 1080, "link": None}]) is None


def test_best_file_empty():
    assert fg._best_file([]) is None


# --- interleaving -------------------------------------------------------------


def _cand(platform, i):
    return {"platform": platform, "id": str(i)}


def test_interleave_alternates_platforms():
    pex = [_cand("pexels", i) for i in range(3)]
    pix = [_cand("pixabay", i) for i in range(3)]
    out = fg._interleave(pex, pix, limit=6)
    assert [c["platform"] for c in out] == [
        "pexels",
        "pixabay",
        "pexels",
        "pixabay",
        "pexels",
        "pixabay",
    ]


def test_interleave_tops_up_when_one_source_is_short():
    pex = [_cand("pexels", i) for i in range(5)]
    out = fg._interleave(pex, [], limit=4)
    assert len(out) == 4
    assert all(c["platform"] == "pexels" for c in out)


def test_interleave_respects_limit():
    pex = [_cand("pexels", i) for i in range(10)]
    pix = [_cand("pixabay", i) for i in range(10)]
    assert len(fg._interleave(pex, pix, limit=7)) == 7


def test_interleave_dedupes_same_platform_and_id():
    dupe = [_cand("pexels", 1), _cand("pexels", 1)]
    assert len(fg._interleave(dupe, [], limit=5)) == 1


def test_filename_is_platform_scoped():
    assert fg._filename({"platform": "pexels", "id": "123"}) == "pexels_123.mp4"


# --- manifest + prune ---------------------------------------------------------


def _setup_manifest(tmp_path, monkeypatch, files):
    monkeypatch.setattr(paths, "GAMEPLAY_DIR", tmp_path)
    monkeypatch.setattr(fg, "GAMEPLAY_DIR", tmp_path)
    monkeypatch.setattr(fg, "MANIFEST_PATH", tmp_path / "sources.json")
    entries = []
    for name in files:
        (tmp_path / name).write_bytes(b"VIDEO")
        entries.append({"file": name, "platform": "pexels", "source_url": f"http://x/{name}"})
    fg.write_manifest(entries)
    return tmp_path


def test_write_manifest_sorts_and_persists(tmp_path, monkeypatch):
    wd = _setup_manifest(tmp_path, monkeypatch, ["b.mp4", "a.mp4"])
    data = json.loads((wd / "sources.json").read_text())
    assert [c["file"] for c in data["clips"]] == ["a.mp4", "b.mp4"]


def test_prune_deletes_rejects_and_rewrites_manifest(tmp_path, monkeypatch):
    wd = _setup_manifest(tmp_path, monkeypatch, ["keep.mp4", "watermarked.mp4"])
    result = fg.prune(["keep.mp4"])

    assert [c["file"] for c in result["clips"]] == ["keep.mp4"]
    assert (wd / "keep.mp4").exists()
    assert not (wd / "watermarked.mp4").exists()
    on_disk = json.loads((wd / "sources.json").read_text())
    assert [c["file"] for c in on_disk["clips"]] == ["keep.mp4"]


def test_load_manifest_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(fg, "MANIFEST_PATH", tmp_path / "nope.json")
    assert fg.load_manifest() == {"clips": []}


def test_fetch_requires_at_least_one_key(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PEXELS_API_KEY"):
        fg.fetch_gameplay()
