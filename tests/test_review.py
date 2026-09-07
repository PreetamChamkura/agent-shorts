"""Unit tests for stage 5. Pure logic tests plus one real (loopback-only)
server integration test — no network beyond 127.0.0.1, no external calls."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from clipper import paths
from clipper.review import (
    CHECKLIST_KEYS,
    _clip_assets,
    _load_review,
    _segments_with_render,
    render_review_page,
    review,
    save_review,
)


def _seed(tmp_path, monkeypatch, segments, rendered_ns=()):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "segments.json").write_text(json.dumps({"video_id": "vid1", "segments": segments}))
    for n in rendered_ns:
        (wd / f"out_{n}.mp4").write_bytes(b"FAKEMP4")
    return wd


# --- pure read/write logic ---------------------------------------------------


def test_load_review_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    assert _load_review("nope") == {"video_id": "nope", "reviews": []}


def test_save_review_rejects_bad_decision(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [{"n": 0}])
    with pytest.raises(ValueError):
        save_review("vid1", 0, "maybe", {}, "")


def test_save_review_creates_and_persists_entry(tmp_path, monkeypatch):
    wd = _seed(tmp_path, monkeypatch, [{"n": 0}])
    save_review("vid1", 0, "approved", {"vo_added_context": True}, "good clip")

    on_disk = json.loads((wd / "review.json").read_text())
    entry = on_disk["reviews"][0]
    assert entry["n"] == 0
    assert entry["decision"] == "approved"
    assert entry["notes"] == "good clip"
    assert entry["checklist"]["vo_added_context"] is True
    assert set(entry["checklist"]) == CHECKLIST_KEYS


def test_save_review_unanswered_checklist_items_are_null(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [{"n": 0}])
    data = save_review("vid1", 0, "approved", {}, "")
    checklist = data["reviews"][0]["checklist"]
    assert all(v is None for v in checklist.values())


def test_save_review_drops_unknown_checklist_keys(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [{"n": 0}])
    data = save_review("vid1", 0, "approved", {"not_a_real_field": True}, "")
    assert "not_a_real_field" not in data["reviews"][0]["checklist"]


def test_save_review_preserves_other_entries(tmp_path, monkeypatch):
    wd = _seed(tmp_path, monkeypatch, [{"n": 0}, {"n": 1}])
    save_review("vid1", 0, "approved", {}, "")
    save_review("vid1", 1, "rejected", {}, "flat")

    on_disk = json.loads((wd / "review.json").read_text())
    by_n = {r["n"]: r for r in on_disk["reviews"]}
    assert by_n[0]["decision"] == "approved"
    assert by_n[1]["decision"] == "rejected"
    assert by_n[1]["notes"] == "flat"


def test_save_review_updates_in_place_not_duplicated(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [{"n": 0}])
    save_review("vid1", 0, "approved", {}, "first")
    data = save_review("vid1", 0, "rejected", {}, "changed my mind")
    assert len(data["reviews"]) == 1
    assert data["reviews"][0]["decision"] == "rejected"
    assert data["reviews"][0]["notes"] == "changed my mind"


# --- segment filtering --------------------------------------------------------


def test_segments_with_render_filters_to_existing_files(tmp_path, monkeypatch):
    _seed(
        tmp_path,
        monkeypatch,
        [{"n": 0, "caption_title": "A"}, {"n": 1, "caption_title": "B"}],
        rendered_ns=[0],
    )
    result = _segments_with_render("vid1")
    assert [s["n"] for s in result] == [0]


def test_segments_with_render_missing_segments_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    assert _segments_with_render("nope") == []


# --- page rendering (pure) ----------------------------------------------------


def test_render_review_page_lists_clips_and_metadata():
    segments = [
        {
            "n": 0,
            "caption_title": "HE HAD NO IDEA",
            "tone": "burn",
            "hook": "a hook",
            "loopable": True,
            "vo_script": "a narrator line",
        }
    ]
    page = render_review_page("vid1", segments, {})
    assert "Clip 0" in page
    assert "HE HAD NO IDEA" in page
    assert "/media/out_0.mp4" in page
    assert "a narrator line" in page
    assert "<dt>Loopable</dt><dd>yes</dd>" in page
    assert "Did the VO add real context, rather than re-describe the clip?" in page


def test_render_review_page_shows_prior_decision_status():
    segments = [{"n": 0, "caption_title": "X"}]
    reviews_by_n = {0: {"n": 0, "decision": "approved", "checklist": {}, "notes": ""}}
    page = render_review_page("vid1", segments, reviews_by_n)
    assert "status-approved" in page
    assert ">approved<" in page


def test_render_review_page_empty_state():
    page = render_review_page("vid1", [], {})
    assert "No rendered clips" in page


def test_render_review_page_escapes_html_in_metadata():
    segments = [{"n": 0, "caption_title": "<script>alert(1)</script>"}]
    page = render_review_page("vid1", segments, {})
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- thumbnail + metadata block (CLAUDE.md thumbnail generation step) -------


def test_clip_assets_reflects_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    (tmp_path / "vid1").mkdir()
    assets = _clip_assets("vid1", 0)
    assert assets == {"has_thumbnail": False, "metadata": None}


def test_clip_assets_reads_thumbnail_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_DIR", tmp_path)
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "thumbnail_0.jpg").write_bytes(b"jpg")
    (wd / "metadata_0.json").write_text(json.dumps({"suggested_title": "T"}))

    assets = _clip_assets("vid1", 0)
    assert assets["has_thumbnail"] is True
    assert assets["metadata"] == {"suggested_title": "T"}


def test_render_review_page_shows_thumbnail_and_metadata():
    segments = [{"n": 0, "caption_title": "X"}]
    assets_by_n = {
        0: {
            "has_thumbnail": True,
            "metadata": {
                "suggested_title": "He ruled this out three weeks before it happened",
                "thumbnail_text": '"I DIDN\'T EVEN SEE HIM"',
                "suggested_hashtags": ["#valorant", "#clutch"],
            },
        }
    }
    page = render_review_page("vid1", segments, {}, assets_by_n)
    assert "/media/thumbnail_0.jpg" in page
    assert "He ruled this out three weeks before it happened" in page
    assert "#valorant" in page


def test_render_review_page_omits_thumbnail_block_when_nothing_generated():
    segments = [{"n": 0, "caption_title": "X"}]
    page = render_review_page("vid1", segments, {})
    assert '<div class="thumbnail-block">' not in page


def test_render_review_page_defaults_assets_when_not_passed():
    # Existing callers (pre-thumbnail-feature call sites, and every test
    # above this one) pass only 3 args — must not break.
    segments = [{"n": 0, "caption_title": "X"}]
    page = render_review_page("vid1", segments, {})
    assert "Clip 0" in page


# --- server integration (loopback only) ---------------------------------------


def test_server_serves_page_and_accepts_save(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [{"n": 0, "caption_title": "Test Clip"}], rendered_ns=[0])

    httpd = review("vid1", port=0, serve_forever=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]

        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
        assert resp.status == 200
        body = resp.read().decode()
        assert "Test Clip" in body

        media = urllib.request.urlopen(f"http://127.0.0.1:{port}/media/out_0.mp4", timeout=5)
        assert media.status == 200
        assert media.read() == b"FAKEMP4"

        payload = json.dumps(
            {"n": 0, "decision": "approved", "checklist": {"stands_alone": True}, "notes": "ok"}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/save",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        save_resp = urllib.request.urlopen(req, timeout=5)
        assert save_resp.status == 200
        assert json.loads(save_resp.read())["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()

    on_disk = json.loads((tmp_path / "vid1" / "review.json").read_text())
    assert on_disk["reviews"][0]["decision"] == "approved"


def test_server_rejects_path_traversal_media_request(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, [{"n": 0}], rendered_ns=[0])
    httpd = review("vid1", port=0, serve_forever=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/media/../../../etc/passwd")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
