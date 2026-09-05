"""Unit tests for stage 1's livestream gate. No network, no yt-dlp calls.

The pipeline is batch and needs a finite file. yt-dlp follows the live edge in
real time, so ingesting a still-airing stream blocks until the broadcast ends —
these tests pin that this fails loudly instead of looking like a slow download.
"""

from __future__ import annotations

import pytest

from clipper.ingest import LiveStreamError, check_live_status


def test_upcoming_stream_raises():
    with pytest.raises(LiveStreamError, match="hasn't started"):
        check_live_status({"id": "x", "live_status": "is_upcoming"}, live_from_start=False)


def test_upcoming_raises_even_with_live_from_start():
    # Nothing has aired yet — recording from the start isn't possible either.
    with pytest.raises(LiveStreamError):
        check_live_status({"id": "x", "live_status": "is_upcoming"}, live_from_start=True)


def test_airing_stream_raises_without_opt_in():
    with pytest.raises(LiveStreamError, match="airing right now"):
        check_live_status({"id": "x", "live_status": "is_live"}, live_from_start=False)


def test_airing_stream_error_names_the_supported_path():
    # The message has to tell the user what to do instead, not just refuse.
    with pytest.raises(LiveStreamError) as exc:
        check_live_status({"id": "x", "live_status": "is_live"}, live_from_start=False)
    assert "VOD" in str(exc.value)
    assert "live_from_start=True" in str(exc.value)


def test_airing_stream_allowed_with_explicit_opt_in(caplog):
    with caplog.at_level("WARNING"):
        check_live_status({"id": "x", "live_status": "is_live"}, live_from_start=True)
    assert "BLOCKS" in caplog.text


def test_completed_vod_is_the_ordinary_path():
    # was_live is the mode this pipeline actually relies on — no error, no warning.
    check_live_status(
        {"id": "x", "live_status": "was_live", "duration": 7200}, live_from_start=False
    )


def test_post_live_warns_but_proceeds(caplog):
    # Stream ended, VOD still processing: usable, but may be partial.
    with caplog.at_level("WARNING"):
        check_live_status({"id": "x", "live_status": "post_live"}, live_from_start=False)
    assert "still being processed" in caplog.text


def test_missing_live_status_is_not_an_error():
    # Extractors that don't report live_status must behave exactly as before.
    check_live_status({"id": "x"}, live_from_start=False)
    check_live_status({"id": "x", "live_status": None}, live_from_start=False)


def test_not_live_is_not_an_error():
    check_live_status({"id": "x", "live_status": "not_live"}, live_from_start=False)


# --- Twitch VODs: is_live/was_live -> live_status, run through yt-dlp's own -
# --- derivation, not a reimplementation of it -------------------------------
#
# check_live_status only ever sees an already-*derived* live_status (yt-dlp
# fills it in from is_live/was_live before any extractor's result reaches our
# code), so the fields below are yt-dlp's actual derivation logic
# (YoutubeDL.py's live_status inference), exercised directly — not a copy we
# maintain ourselves. This sandbox cannot reach twitch.tv to hit the real
# extractor (confirmed: connection refused to www.twitch.tv and gql.twitch.tv,
# while youtube.com/github.com/pypi.org all succeed — a network-egress
# restriction here, not a yt-dlp or Twitch issue), so this is the strongest
# real verification available: run yt-dlp's real algorithm on the exact
# is_live/was_live shapes TwitchVodIE._extract_info() and _extract_info_gql()
# return for each of Twitch's real VOD API statuses (traced from yt-dlp's own
# extractor source).


def _yt_dlp_derived_live_status(is_live, was_live):
    from yt_dlp import YoutubeDL

    ydl = YoutubeDL({"quiet": True})
    info = {
        "id": "v9999999999",
        "title": "t",
        "extractor": "twitch:vod",
        "extractor_key": "TwitchVod",
        "webpage_url": "https://x",
        "original_url": "https://x",
        "is_live": is_live,
        "was_live": was_live,
    }
    ydl._fill_common_fields(info, final=False)
    return info.get("live_status")


def test_twitch_vod_recorded_status_derives_was_live():
    # TwitchVodIE status == 'recorded': is_live=False, was_live=True.
    assert _yt_dlp_derived_live_status(is_live=False, was_live=True) == "was_live"


def test_twitch_vod_recording_status_derives_is_live():
    # TwitchVodIE status == 'recording': the VOD exists but the channel is
    # still live, so the file is still growing — must be blocked exactly like
    # a live YouTube stream, not treated as a finished archive.
    assert _yt_dlp_derived_live_status(is_live=True, was_live=True) == "is_live"


def test_twitch_vod_derived_is_live_is_blocked_by_our_gate():
    status = _yt_dlp_derived_live_status(is_live=True, was_live=True)
    with pytest.raises(LiveStreamError):
        check_live_status({"id": "v9999999999", "live_status": status}, live_from_start=False)


def test_twitch_vod_derived_was_live_passes_our_gate():
    status = _yt_dlp_derived_live_status(is_live=False, was_live=True)
    check_live_status({"id": "v1536751224", "live_status": status}, live_from_start=False)
