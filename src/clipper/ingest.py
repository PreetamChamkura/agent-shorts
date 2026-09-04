"""Stage 1 — ingest.

Reads: nothing (takes a YouTube URL).
Writes: work/{video_id}/source.mp4, work/{video_id}/source.info.json

Best mp4 up to 1080p. Skips the download when source.mp4 already exists unless
`force`. Never re-downloads on a rerun — caching is load-bearing.
"""

from __future__ import annotations

import json
import logging

from .paths import work_dir

log = logging.getLogger("clipper.ingest")

# bestvideo+bestaudio mp4/m4a up to 1080p, falling back to progressive.
_FORMAT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
    "best[height<=1080][ext=mp4]/"
    "best[height<=1080]"
)


def ingest(url: str, *, force: bool = False) -> str:
    """Download ``url`` into its work dir and return the video_id.

    Two-step so we know the id (hence the work dir) before committing to the
    download: probe metadata, then download only if needed.
    """
    from yt_dlp import YoutubeDL  # heavy + not needed for tests; import lazily

    with YoutubeDL({"quiet": True, "skip_download": True}) as probe:
        info = probe.extract_info(url, download=False)
    video_id = info["id"]

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
