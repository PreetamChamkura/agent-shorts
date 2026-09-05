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
