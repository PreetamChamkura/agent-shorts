"""Unit tests for the Twitch Helix client. No real network calls — requests
is monkeypatched throughout. Live verification (real token, real /helix/users
and /helix/clips calls) was run manually against real credentials; see the
session log, not here — these pin the pure logic (token-refresh timing,
pagination, sort order, rate-limit retry) so it doesn't regress silently.
"""

from __future__ import annotations

import json

import pytest

from clipper.twitch import (
    TwitchAuthError,
    _normalize_clip,
    _normalize_stream,
    fetch_clips,
    fetch_trending_clips,
    get_app_access_token,
    get_broadcaster_id,
    get_top_live_streams,
    is_twitch_clip_url,
    token_needs_refresh,
)

# --- token refresh timing (pure) ---------------------------------------------


def test_token_needs_refresh_when_missing():
    assert token_needs_refresh(None) is True


def test_token_needs_refresh_when_malformed():
    assert token_needs_refresh({"access_token": "x"}) is True  # no expires_at


def test_token_needs_refresh_false_well_before_expiry():
    cached = {"access_token": "x", "expires_at": 10_000.0}
    assert token_needs_refresh(cached, now=100.0) is False


def test_token_needs_refresh_true_within_margin():
    # REFRESH_MARGIN_SECONDS is 3600 — 30 minutes out should already refresh.
    cached = {"access_token": "x", "expires_at": 10_000.0}
    assert token_needs_refresh(cached, now=10_000.0 - 1800.0) is True


def test_token_needs_refresh_true_after_expiry():
    cached = {"access_token": "x", "expires_at": 10_000.0}
    assert token_needs_refresh(cached, now=20_000.0) is True


# --- is_twitch_clip_url (delegates to yt-dlp's own extractor regex) ---------


def test_is_twitch_clip_url_true_for_clips_domain():
    assert is_twitch_clip_url("https://clips.twitch.tv/SomeClipSlug") is True


def test_is_twitch_clip_url_true_for_channel_clip_path():
    assert is_twitch_clip_url("https://www.twitch.tv/somechannel/clip/SomeClipSlug") is True


def test_is_twitch_clip_url_false_for_vod():
    assert is_twitch_clip_url("https://www.twitch.tv/videos/1536751224") is False


def test_is_twitch_clip_url_false_for_youtube():
    assert is_twitch_clip_url("https://www.youtube.com/watch?v=abc123") is False


# --- get_app_access_token: caching + refresh, mocked network ---------------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_get_app_access_token_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    with pytest.raises(TwitchAuthError):
        get_app_access_token()


def test_get_app_access_token_fetches_and_caches(monkeypatch, tmp_path):
    import clipper.twitch as twitch_mod

    cache_path = tmp_path / "token.json"
    monkeypatch.setattr(twitch_mod, "TOKEN_CACHE_PATH", cache_path)
    monkeypatch.setattr(twitch_mod, "TWITCH_CACHE_DIR", tmp_path)

    calls = []

    def fake_post(url, data, timeout):
        calls.append(data)
        return _FakeResponse(200, {"access_token": "tok123", "expires_in": 5_000_000})

    monkeypatch.setattr(twitch_mod.requests, "post", fake_post)

    token = get_app_access_token(client_id="cid", client_secret="secret")
    assert token == "tok123"
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "client_credentials"

    # Second call within the cached token's life must NOT hit the network again.
    token2 = get_app_access_token(client_id="cid", client_secret="secret")
    assert token2 == "tok123"
    assert len(calls) == 1

    cached = json.loads(cache_path.read_text())
    assert cached["access_token"] == "tok123"


def test_get_app_access_token_401_raises_auth_error(monkeypatch, tmp_path):
    import clipper.twitch as twitch_mod

    monkeypatch.setattr(twitch_mod, "TOKEN_CACHE_PATH", tmp_path / "token.json")
    monkeypatch.setattr(twitch_mod, "TWITCH_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        twitch_mod.requests,
        "post",
        lambda *a, **k: _FakeResponse(401, text="invalid client"),
    )
    with pytest.raises(TwitchAuthError, match="401"):
        get_app_access_token(client_id="bad", client_secret="bad")


# --- get_broadcaster_id ------------------------------------------------------


def test_get_broadcaster_id_returns_id(monkeypatch):
    import clipper.twitch as twitch_mod

    monkeypatch.setattr(
        twitch_mod,
        "_request",
        lambda *a, **k: _FakeResponse(200, {"data": [{"id": "12345", "login": "shroud"}]}),
    )
    assert get_broadcaster_id("shroud", token="t", client_id="c") == "12345"


def test_get_broadcaster_id_raises_on_unknown_channel(monkeypatch):
    import clipper.twitch as twitch_mod

    monkeypatch.setattr(twitch_mod, "_request", lambda *a, **k: _FakeResponse(200, {"data": []}))
    with pytest.raises(ValueError, match="no channel found"):
        get_broadcaster_id("doesnotexist12345", token="t", client_id="c")


# --- fetch_clips: pagination + sort ------------------------------------------


def _clip(id_, views):
    return {
        "id": id_,
        "url": f"https://clips.twitch.tv/{id_}",
        "broadcaster_name": "x",
        "title": "t",
        "view_count": views,
        "created_at": "2026-01-01T00:00:00Z",
        "duration": 30.0,
    }


def test_normalize_clip_shape():
    n = _normalize_clip(_clip("abc", 5))
    assert n["id"] == "abc"
    assert n["view_count"] == 5
    assert isinstance(n["duration"], float)


def test_fetch_clips_sorts_by_view_count_desc(monkeypatch):
    import clipper.twitch as twitch_mod

    page = {
        "data": [_clip("low", 5), _clip("high", 500), _clip("mid", 50)],
        "pagination": {},
    }
    monkeypatch.setattr(twitch_mod, "_request", lambda *a, **k: _FakeResponse(200, page))
    clips = fetch_clips("bid", token="t", client_id="c", limit=10)
    assert [c["id"] for c in clips] == ["high", "mid", "low"]


def test_fetch_clips_respects_limit(monkeypatch):
    import clipper.twitch as twitch_mod

    page = {"data": [_clip(str(i), i) for i in range(20)], "pagination": {}}
    monkeypatch.setattr(twitch_mod, "_request", lambda *a, **k: _FakeResponse(200, page))
    clips = fetch_clips("bid", token="t", client_id="c", limit=5)
    assert len(clips) == 5


def test_fetch_clips_paginates_until_limit_or_no_cursor(monkeypatch):
    import clipper.twitch as twitch_mod

    pages = [
        {"data": [_clip(f"a{i}", i) for i in range(3)], "pagination": {"cursor": "next"}},
        {"data": [_clip(f"b{i}", i) for i in range(3)], "pagination": {}},
    ]
    calls = {"n": 0}

    def fake_request(method, url, *, headers, params=None):
        resp = _FakeResponse(200, pages[calls["n"]])
        calls["n"] += 1
        return resp

    monkeypatch.setattr(twitch_mod, "_request", fake_request)
    clips = fetch_clips("bid", token="t", client_id="c", limit=10)
    assert calls["n"] == 2  # stopped after the second page had no cursor
    assert len(clips) == 6


def test_fetch_clips_passes_date_range(monkeypatch):
    import clipper.twitch as twitch_mod

    seen_params = {}

    def fake_request(method, url, *, headers, params=None):
        seen_params.update(params or {})
        return _FakeResponse(200, {"data": [], "pagination": {}})

    monkeypatch.setattr(twitch_mod, "_request", fake_request)
    fetch_clips(
        "bid",
        token="t",
        client_id="c",
        limit=5,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-02-01T00:00:00Z",
    )
    assert seen_params["started_at"] == "2026-01-01T00:00:00Z"
    assert seen_params["ended_at"] == "2026-02-01T00:00:00Z"


# --- 429 retry ---------------------------------------------------------------


def test_request_retries_once_on_429(monkeypatch):
    import clipper.twitch as twitch_mod

    calls = []

    def fake_request(method, url, headers=None, params=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse(429, headers={"Ratelimit-Reset": "0"})
        return _FakeResponse(200, {"data": []})

    monkeypatch.setattr(twitch_mod.requests, "request", fake_request)
    monkeypatch.setattr(twitch_mod.time, "sleep", lambda s: None)

    resp = twitch_mod._request("GET", "https://x", headers={})
    assert resp.status_code == 200
    assert len(calls) == 2


# --- get_top_live_streams: zero-filter /helix/streams, sort + pagination ----


def _stream(login, viewers):
    return {
        "user_id": f"id_{login}",
        "user_login": login,
        "user_name": login,
        "viewer_count": viewers,
        "game_id": "1",
        "game_name": "Some Game",
        "title": "t",
        "language": "en",
    }


def test_normalize_stream_shape():
    n = _normalize_stream(_stream("shroud", 50000))
    assert n["broadcaster_id"] == "id_shroud"
    assert n["broadcaster_login"] == "shroud"
    assert n["viewer_count"] == 50000


def test_get_top_live_streams_sorts_by_viewer_count_desc(monkeypatch):
    import clipper.twitch as twitch_mod

    page = {
        "data": [_stream("low", 5), _stream("high", 500), _stream("mid", 50)],
        "pagination": {},
    }
    monkeypatch.setattr(twitch_mod, "_request", lambda *a, **k: _FakeResponse(200, page))
    streams = get_top_live_streams(token="t", client_id="c", count=10)
    assert [s["broadcaster_login"] for s in streams] == ["high", "mid", "low"]


def test_get_top_live_streams_no_filter_params(monkeypatch):
    # Confirms this really is the zero-filter call (no broadcaster_id/game_id
    # ever sent) — the whole point of using /helix/streams for discovery.
    import clipper.twitch as twitch_mod

    seen_params = {}

    def fake_request(method, url, *, headers, params=None):
        seen_params.update(params or {})
        assert "/streams" in url
        return _FakeResponse(200, {"data": [], "pagination": {}})

    monkeypatch.setattr(twitch_mod, "_request", fake_request)
    get_top_live_streams(token="t", client_id="c", count=5)
    assert "broadcaster_id" not in seen_params
    assert "game_id" not in seen_params


def test_get_top_live_streams_respects_count(monkeypatch):
    import clipper.twitch as twitch_mod

    page = {"data": [_stream(str(i), i) for i in range(20)], "pagination": {}}
    monkeypatch.setattr(twitch_mod, "_request", lambda *a, **k: _FakeResponse(200, page))
    streams = get_top_live_streams(token="t", client_id="c", count=5)
    assert len(streams) == 5


# --- fetch_trending_clips: streams -> per-channel fetch_clips, tagged ------


def test_fetch_trending_clips_queries_top_streams_then_each_channel(monkeypatch):
    import clipper.twitch as twitch_mod

    streams = [_normalize_stream(_stream("big", 900)), _normalize_stream(_stream("small", 100))]
    monkeypatch.setattr(twitch_mod, "get_top_live_streams", lambda **k: streams)

    calls = []

    def fake_fetch_clips(broadcaster_id, **k):
        calls.append(broadcaster_id)
        return [_normalize_clip(_clip(f"{broadcaster_id}-clip", 42))]

    monkeypatch.setattr(twitch_mod, "fetch_clips", fake_fetch_clips)

    clips = fetch_trending_clips(token="t", client_id="c", limit=10)
    assert calls == ["id_big", "id_small"]
    assert len(clips) == 2


def test_fetch_trending_clips_tags_discovery_mode(monkeypatch):
    import clipper.twitch as twitch_mod

    stream = _normalize_stream(_stream("shroud", 5000))
    monkeypatch.setattr(twitch_mod, "get_top_live_streams", lambda **k: [stream])
    monkeypatch.setattr(
        twitch_mod, "fetch_clips", lambda broadcaster_id, **k: [_normalize_clip(_clip("c1", 10))]
    )

    clips = fetch_trending_clips(token="t", client_id="c", limit=10)
    assert clips[0]["discovery"] == {
        "mode": "trending",
        "channel": "shroud",
        "viewer_count_at_discovery": 5000,
    }


def test_fetch_trending_clips_sorts_across_channels_by_view_count(monkeypatch):
    import clipper.twitch as twitch_mod

    streams = [_normalize_stream(_stream("a", 900)), _normalize_stream(_stream("b", 800))]
    monkeypatch.setattr(twitch_mod, "get_top_live_streams", lambda **k: streams)

    def fake_fetch_clips(broadcaster_id, **k):
        # channel "a" has the LOWER-viewed clip, channel "b" has the higher —
        # final order must be by clip view_count, not by channel viewer rank.
        views = 5 if broadcaster_id == "id_a" else 500
        return [_normalize_clip(_clip(f"{broadcaster_id}-clip", views))]

    monkeypatch.setattr(twitch_mod, "fetch_clips", fake_fetch_clips)

    clips = fetch_trending_clips(token="t", client_id="c", limit=10)
    assert clips[0]["view_count"] == 500


def test_fetch_trending_clips_respects_limit(monkeypatch):
    import clipper.twitch as twitch_mod

    streams = [_normalize_stream(_stream(f"ch{i}", 100 - i)) for i in range(10)]
    monkeypatch.setattr(twitch_mod, "get_top_live_streams", lambda **k: streams)
    monkeypatch.setattr(
        twitch_mod,
        "fetch_clips",
        lambda broadcaster_id, **k: [_normalize_clip(_clip(f"{broadcaster_id}-c", 1))],
    )

    clips = fetch_trending_clips(token="t", client_id="c", limit=3)
    assert len(clips) == 3


def test_fetch_trending_clips_skips_channel_on_fetch_error(monkeypatch):
    import requests

    import clipper.twitch as twitch_mod

    streams = [_normalize_stream(_stream("bad", 900)), _normalize_stream(_stream("good", 800))]
    monkeypatch.setattr(twitch_mod, "get_top_live_streams", lambda **k: streams)

    def fake_fetch_clips(broadcaster_id, **k):
        if broadcaster_id == "id_bad":
            raise requests.RequestException("boom")
        return [_normalize_clip(_clip("good-clip", 10))]

    monkeypatch.setattr(twitch_mod, "fetch_clips", fake_fetch_clips)

    clips = fetch_trending_clips(token="t", client_id="c", limit=10)
    assert len(clips) == 1
    assert clips[0]["id"] == "good-clip"


def test_fetch_trending_clips_passes_per_channel_limit_and_dates(monkeypatch):
    import clipper.twitch as twitch_mod

    stream = _normalize_stream(_stream("shroud", 5000))
    monkeypatch.setattr(twitch_mod, "get_top_live_streams", lambda **k: [stream])

    seen = {}

    def fake_fetch_clips(broadcaster_id, **k):
        seen.update(k)
        return []

    monkeypatch.setattr(twitch_mod, "fetch_clips", fake_fetch_clips)

    fetch_trending_clips(
        token="t",
        client_id="c",
        limit=10,
        per_channel_limit=3,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-02-01T00:00:00Z",
    )
    assert seen["limit"] == 3
    assert seen["started_at"] == "2026-01-01T00:00:00Z"
    assert seen["ended_at"] == "2026-02-01T00:00:00Z"
