"""Unit tests for the Twitch-clip visual gate's layer 1 (static-thumbnail
pixel-stats pre-filter). No real network, no real ffmpeg — thumbnail_scores
is monkeypatched throughout; the real threshold calibration (variance,
dark_fraction, edge_density scored against 8 real fetched thumbnails,
including the Doc clip) was done in the session, not here — see the
BLANK_VARIANCE_THRESHOLD comment in visual_gate.py for the numbers.
"""

from __future__ import annotations

from clipper.visual_gate import (
    BLANK_VARIANCE_THRESHOLD,
    screen_thumbnail_scores,
)

# --- pure pass/fail decision --------------------------------------------


def test_screen_scores_rejects_near_zero_variance():
    passed, reason = screen_thumbnail_scores(
        {"variance": 5.0, "dark_fraction": 0.99, "edge_density": 0.1}
    )
    assert passed is False
    assert "layer_1" in reason
    assert "variance" in reason


def test_screen_scores_passes_real_content_regardless_of_darkness():
    # The Doc clip's real scores: high variance (hand-vs-desk contrast), high
    # dark_fraction, moderate edge_density — must NOT be rejected by layer 1,
    # that's the whole point of the narrowed scope (see module docstring).
    passed, reason = screen_thumbnail_scores(
        {"variance": 3687.7, "dark_fraction": 0.64, "edge_density": 10.72}
    )
    assert passed is True
    assert reason is None


def test_screen_scores_passes_dark_moody_gameplay():
    # A real dark-dungeon-crawler sample from the session's calibration —
    # low variance, high dark_fraction, but legitimate content.
    passed, reason = screen_thumbnail_scores(
        {"variance": 676.4, "dark_fraction": 0.41, "edge_density": 7.43}
    )
    assert passed is True


def test_blank_variance_threshold_has_wide_margin_below_real_content():
    # The lowest variance among 8 real, legitimately-usable samples tested
    # was 514.7 (a character-menu screen) — the threshold must stay well
    # under that, not creep up toward it.
    assert BLANK_VARIANCE_THRESHOLD < 514.7 / 5


# --- screen_clips_layer1: annotates every clip, drops none ------------------


def _clip(id_, thumbnail_url="https://x/thumb.jpg"):
    return {"id": id_, "thumbnail_url": thumbnail_url, "view_count": 1}


def test_screen_clips_layer1_annotates_pass_and_reject(monkeypatch):
    import clipper.visual_gate as vg

    def fake_fetch(url):
        return b"fake-bytes"

    def fake_scores(image_bytes):
        return {"variance": 5.0, "dark_fraction": 0.9, "edge_density": 0.1}

    monkeypatch.setattr(vg, "fetch_thumbnail", fake_fetch)
    monkeypatch.setattr(vg, "thumbnail_scores", fake_scores)

    out = vg.screen_clips_layer1([_clip("a")])
    assert len(out) == 1  # rejected clips are NOT dropped from the list
    gate = out[0]["visual_gate"]["layer_1"]
    assert gate["passed"] is False
    assert gate["variance"] == 5.0


def test_screen_clips_layer1_logs_scores_for_passing_clips_too(monkeypatch):
    import clipper.visual_gate as vg

    monkeypatch.setattr(vg, "fetch_thumbnail", lambda url: b"x")
    monkeypatch.setattr(
        vg,
        "thumbnail_scores",
        lambda b: {"variance": 2000.0, "dark_fraction": 0.3, "edge_density": 12.0},
    )
    out = vg.screen_clips_layer1([_clip("a")])
    gate = out[0]["visual_gate"]["layer_1"]
    assert gate["passed"] is True
    # Scores are present even though nothing rejected this clip — visibility
    # into the distribution, per the explicit ask to log everything.
    assert gate["variance"] == 2000.0
    assert gate["dark_fraction"] == 0.3
    assert gate["edge_density"] == 12.0


def test_screen_clips_layer1_missing_thumbnail_passes_through_unscored(monkeypatch, caplog):
    import clipper.visual_gate as vg

    out = vg.screen_clips_layer1([_clip("a", thumbnail_url=None)])
    assert len(out) == 1
    assert "visual_gate" not in out[0]


def test_screen_clips_layer1_fetch_failure_passes_through_unscored(monkeypatch):
    import requests

    import clipper.visual_gate as vg

    def fake_fetch(url):
        raise requests.RequestException("timeout")

    monkeypatch.setattr(vg, "fetch_thumbnail", fake_fetch)
    out = vg.screen_clips_layer1([_clip("a")])
    assert len(out) == 1
    assert "visual_gate" not in out[0]  # not rejected — a network hiccup, not a visual judgment


def test_screen_clips_layer1_preserves_all_original_fields(monkeypatch):
    import clipper.visual_gate as vg

    monkeypatch.setattr(vg, "fetch_thumbnail", lambda url: b"x")
    monkeypatch.setattr(
        vg,
        "thumbnail_scores",
        lambda b: {"variance": 999.0, "dark_fraction": 0.1, "edge_density": 5.0},
    )
    clip = {"id": "a", "thumbnail_url": "https://x", "view_count": 500, "title": "T"}
    out = vg.screen_clips_layer1([clip])
    assert out[0]["view_count"] == 500
    assert out[0]["title"] == "T"


def test_screen_clips_layer1_does_not_mutate_input_list(monkeypatch):
    import clipper.visual_gate as vg

    monkeypatch.setattr(vg, "fetch_thumbnail", lambda url: b"x")
    monkeypatch.setattr(
        vg,
        "thumbnail_scores",
        lambda b: {"variance": 999.0, "dark_fraction": 0.1, "edge_density": 5.0},
    )
    original = _clip("a")
    vg.screen_clips_layer1([original])
    assert "visual_gate" not in original  # the input dict itself is untouched
