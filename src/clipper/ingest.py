"""Stage 1 — ingest.

Reads: nothing (takes a video URL — YouTube or Twitch, anything yt-dlp
supports; the rest of the pipeline is source-agnostic once source.mp4 exists).
Writes: work/{video_id}/source.mp4, work/{video_id}/source.info.json

Best mp4 up to 1080p. Skips the download when source.mp4 already exists unless
`force`. Never re-downloads on a rerun — caching is load-bearing.

Livestreams (YouTube and Twitch both): this pipeline is batch
(download -> transcribe -> select), which needs a finite file. A stream that
is still airing has no end, and yt-dlp follows the live edge in real time
until the broadcast stops, so ingesting one would block for however long the
streamer keeps going — an hour, or twelve. So the mode we rely on is the VOD
path (`was_live`): wait for the stream to end, then ingest the recording like
any other video. That path needs no special handling and is what
`--live-from-start` would otherwise be for.

Recording an in-progress stream is still possible via `live_from_start=True`,
but it is opt-in precisely because it blocks until the broadcast ends, and
yt-dlp documents the flag as experimental (YouTube/Twitch/TVer/mellow-fan
only). Without the opt-in, a live URL fails loudly rather than silently
hanging on what looks like an ordinary download.

Twitch VODs specifically: check_live_status needs no Twitch-specific branch.
Twitch's extractor reports `is_live=True, was_live=True` for a VOD whose
channel is still live (the recording is still growing) and
`is_live=False, was_live=True` once it's finished — yt-dlp's own derivation
collapses those to live_status 'is_live' and 'was_live' respectively, which
this module already gates on. Twitch ids also come back prefixed ('v12345'),
which only affects the work dir name, not any logic here. The same is true
of the format selector: Twitch's HLS formats are muxed (no separate
audio-only track like YouTube's DASH), so `_FORMAT`'s bestvideo+bestaudio
alternative never matches and it falls through to its own
`best[height<=1080][ext=mp4]` fallback, which does.
"""

from __future__ import annotations

import json
import logging

from .paths import work_dir

log = logging.getLogger("clipper.ingest")


class LiveStreamError(RuntimeError):
    """A stream that this batch pipeline can't ingest as-is (still airing, or
    scheduled and not yet started). Distinct from a download failure so a
    caller can tell "come back later" apart from "this is broken"."""


# bestvideo+bestaudio mp4/m4a up to 1080p, falling back to progressive.
_FORMAT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=1080][ext=mp4]/"
    "best[height<=1080]"
)


def check_live_status(info: dict, *, live_from_start: bool) -> None:
    """Gate ingest on yt-dlp's ``live_status``. Raises for states this batch
    pipeline can't process; logs and returns for the ones it can.

    ``live_status`` is None on extractors that don't report it, which is not an
    error — it's treated like an ordinary video, same as before this existed.
    """
    status = info.get("live_status")

    if status == "is_upcoming":
        raise LiveStreamError(
            f"ingest: {info.get('id')} is a scheduled stream that hasn't started — "
            "there is nothing to download yet. Ingest it once it has aired and "
            "the VOD is available."
        )

    if status == "is_live":
        if not live_from_start:
            raise LiveStreamError(
                f"ingest: {info.get('id')} is airing right now. This pipeline needs a "
                "finished file: yt-dlp follows the live edge in real time, so ingesting "
                "a live stream blocks until the broadcast ends, however long that is. "
                "Wait for the stream to end and ingest the VOD (the supported path), or "
                "pass live_from_start=True to record it from the start and block until "
                "it ends (experimental in yt-dlp; YouTube/Twitch only)."
            )
        log.warning(
            "ingest: %s is airing — recording from the start. This BLOCKS until the "
            "broadcast ends and yt-dlp documents --live-from-start as experimental.",
            info.get("id"),
        )
        return

    if status == "post_live":
        log.warning(
            "ingest: %s has just ended and its VOD is still being processed. Formats "
            "may be incomplete or lower quality than the final recording; re-ingest "
            "with force=True later if the result looks short or degraded.",
            info.get("id"),
        )
        return

    if status == "was_live":
        log.info(
            "ingest: %s is a completed livestream VOD (duration %ss) — ordinary path",
            info.get("id"),
            info.get("duration"),
        )


def ingest(url: str, *, force: bool = False, live_from_start: bool = False) -> str:
    """Download ``url`` into its work dir and return the video_id.

    Two-step so we know the id (hence the work dir) before committing to the
    download: probe metadata, then download only if needed.
    """
    from yt_dlp import YoutubeDL  # heavy + not needed for tests; import lazily

    with YoutubeDL({"quiet": True, "skip_download": True}) as probe:
        info = probe.extract_info(url, download=False)
    video_id = info["id"]

    check_live_status(info, live_from_start=live_from_start)

    duration = info.get("duration")
    if duration:
        log.info("ingest: %s runs %.0fs (%.1f hours)", video_id, duration, duration / 3600)

    wd = work_dir(video_id)
    source = wd / "source.mp4"
    info_path = wd / "source.info.json"

    if source.exists() and not force:
        log.info("ingest: cache hit for %s (%s), skipping download", video_id, source)
        if not info_path.exists():
            with YoutubeDL({"quiet": True}) as ydl:
                info_path.write_text(json.dumps(ydl.sanitize_info(info), indent=2))
        return video_id

    opts = {
        "quiet": True,
        "format": _FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(wd / "source.%(ext)s"),
        "overwrites": True,
    }
    if live_from_start:
        opts["live_from_start"] = True
    log.info("ingest: downloading %s -> %s", url, source)
    with YoutubeDL(opts) as ydl:
        full = ydl.extract_info(url, download=True)
        info_path.write_text(json.dumps(ydl.sanitize_info(full), indent=2))

    if not source.exists():
        raise FileNotFoundError(
            f"ingest: expected {source} after download but it is missing. "
            f"Merge to mp4 may have failed for {url}."
        )
    return video_id
