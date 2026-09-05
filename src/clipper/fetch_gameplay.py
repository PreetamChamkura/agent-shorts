"""Stock filler-footage acquisition for assets/gameplay/.

Not a pipeline stage — a one-off setup tool. Pulls "satisfying" b-roll from
the official Pexels and Pixabay video APIs (never page scraping), filters on
duration and resolution, downloads into assets/gameplay/, and records where
every file came from in assets/gameplay/sources.json.

Watermark screening is deliberately NOT automated here: detecting a burned-in
logo needs eyes on a frame, not metadata. `fetch_gameplay` therefore also
writes one thumbnail per clip into a review directory so a human (or an agent
with vision) can look before the batch is trusted, and `prune` removes the
rejects and rewrites the manifest to match.

Requires PEXELS_API_KEY / PIXABAY_API_KEY in .env. Either may be absent — the
corresponding source is skipped with a warning rather than failing the run.
"""

from __future__ import annotations

import json
import logging
import os

import requests

from .ffmpeg import run_ffmpeg
from .paths import GAMEPLAY_DIR

log = logging.getLogger("clipper.fetch_gameplay")

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/videos/"

QUERIES = ("satisfying", "oddly satisfying")

# Long enough to cover a 58s clip's panel without looping, and high enough
# resolution that the 1080x864 cover-crop never upscales.
MIN_DURATION = 20.0
MIN_SHORT_SIDE = 1080

MANIFEST_PATH = GAMEPLAY_DIR / "sources.json"
TIMEOUT = 60.0


def _best_file(files: list[dict]) -> dict | None:
    """Highest-resolution usable mp4 from a list of {width,height,link} dicts.

    Returns None when nothing meets MIN_SHORT_SIDE — a clip we'd have to
    upscale into the panel is worse than no clip.
    """
    usable = [
        f
        for f in files
        if f.get("link")
        and min(int(f.get("width") or 0), int(f.get("height") or 0)) >= MIN_SHORT_SIDE
    ]
    if not usable:
        return None
    return max(usable, key=lambda f: int(f["width"]) * int(f["height"]))


def _pexels_candidates(api_key: str, query: str, per_page: int = 30) -> list[dict]:
    resp = requests.get(
        PEXELS_SEARCH_URL,
        headers={"Authorization": api_key},
        params={"query": query, "per_page": per_page, "min_duration": int(MIN_DURATION)},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for video in resp.json().get("videos", []):
        files = [
            {"width": f.get("width"), "height": f.get("height"), "link": f.get("link")}
            for f in video.get("video_files", [])
            if (f.get("file_type") or "").endswith("mp4")
        ]
        best = _best_file(files)
        if best is None or float(video.get("duration") or 0) < MIN_DURATION:
            continue
        out.append(
            {
                "platform": "pexels",
                "id": str(video.get("id")),
                "source_url": video.get("url"),
                "download_url": best["link"],
                "duration": float(video.get("duration")),
                "width": int(best["width"]),
                "height": int(best["height"]),
                "credit": (video.get("user") or {}).get("name"),
                "query": query,
            }
        )
    return out


def _pixabay_candidates(api_key: str, query: str, per_page: int = 30) -> list[dict]:
    resp = requests.get(
        PIXABAY_SEARCH_URL,
        params={"key": api_key, "q": query, "per_page": per_page, "video_type": "film"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for hit in resp.json().get("hits", []):
        # Pixabay nests renditions under "videos": {large|medium|small|tiny}
        files = [
            {"width": v.get("width"), "height": v.get("height"), "link": v.get("url")}
            for v in (hit.get("videos") or {}).values()
            if isinstance(v, dict)
        ]
        best = _best_file(files)
        if best is None or float(hit.get("duration") or 0) < MIN_DURATION:
            continue
        out.append(
            {
                "platform": "pixabay",
                "id": str(hit.get("id")),
                "source_url": hit.get("pageURL"),
                "download_url": best["link"],
                "duration": float(hit.get("duration")),
                "width": int(best["width"]),
                "height": int(best["height"]),
                "credit": hit.get("user"),
                "query": query,
            }
        )
    return out


def _interleave(pexels: list[dict], pixabay: list[dict], limit: int) -> list[dict]:
    """Alternate sources so a batch never comes wholly from one platform."""
    picked: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for a, b in zip(pexels, pixabay, strict=False):
        for cand in (a, b):
            key = (cand["platform"], cand["id"])
            if key not in seen:
                seen.add(key)
                picked.append(cand)
    for cand in pexels + pixabay:  # top up if one source ran short
        key = (cand["platform"], cand["id"])
        if key not in seen:
            seen.add(key)
            picked.append(cand)
    return picked[:limit]


def _filename(cand: dict) -> str:
    return f"{cand['platform']}_{cand['id']}.mp4"


def _download(url: str, dest) -> int:
    with requests.get(url, stream=True, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        size = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
                size += len(chunk)
    return size


def _write_thumbnail(video_path, thumb_path) -> None:
    """One mid-clip frame, for human watermark/logo screening."""
    run_ffmpeg(
        ["-y", "-ss", "3", "-i", str(video_path), "-frames:v", "1", "-update", "1", str(thumb_path)]
    )


def write_manifest(entries: list[dict]) -> dict:
    """Write sources.json recording where each kept file came from."""
    data = {"clips": sorted(entries, key=lambda e: e["file"])}
    GAMEPLAY_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(data, indent=2))
    return data


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"clips": []}
    return json.loads(MANIFEST_PATH.read_text())


def prune(keep_files: list[str]) -> dict:
    """Delete downloaded clips not in ``keep_files`` and rewrite the manifest.

    The screening step: run after eyeballing the thumbnails, passing only the
    files without visible watermarks/logos.
    """
    keep = set(keep_files)
    manifest = load_manifest()
    kept, dropped = [], []
    for entry in manifest.get("clips", []):
        path = GAMEPLAY_DIR / entry["file"]
        if entry["file"] in keep:
            kept.append(entry)
        else:
            path.unlink(missing_ok=True)
            dropped.append(entry["file"])
    if dropped:
        log.info("prune: removed %d clip(s): %s", len(dropped), ", ".join(dropped))
    return write_manifest(kept)


def fetch_gameplay(limit: int = 12, thumbs_dir=None) -> dict:
    """Download up to ``limit`` clips across both platforms and write the manifest.

    Returns the manifest. Also writes one thumbnail per clip into ``thumbs_dir``
    (default: assets/gameplay/_thumbs/) for watermark screening — nothing here
    inspects pixels, that step needs eyes.
    """
    pexels_key = os.environ.get("PEXELS_API_KEY")
    pixabay_key = os.environ.get("PIXABAY_API_KEY")
    if not pexels_key and not pixabay_key:
        raise RuntimeError(
            "fetch_gameplay: neither PEXELS_API_KEY nor PIXABAY_API_KEY is set "
            "(see .env.example). Get free keys at pexels.com/api and pixabay.com/api/docs."
        )

    pexels: list[dict] = []
    pixabay: list[dict] = []
    for query in QUERIES:
        if pexels_key:
            pexels += _pexels_candidates(pexels_key, query)
        else:
            log.warning("fetch_gameplay: PEXELS_API_KEY unset — skipping Pexels")
        if pixabay_key:
            pixabay += _pixabay_candidates(pixabay_key, query)
        else:
            log.warning("fetch_gameplay: PIXABAY_API_KEY unset — skipping Pixabay")

    picked = _interleave(pexels, pixabay, limit)
    log.info(
        "fetch_gameplay: %d pexels + %d pixabay candidates -> %d picked",
        len(pexels),
        len(pixabay),
        len(picked),
    )

    GAMEPLAY_DIR.mkdir(parents=True, exist_ok=True)
    thumbs = thumbs_dir or (GAMEPLAY_DIR / "_thumbs")
    thumbs.mkdir(parents=True, exist_ok=True)

    entries = []
    for cand in picked:
        name = _filename(cand)
        dest = GAMEPLAY_DIR / name
        try:
            size = _download(cand["download_url"], dest)
        except requests.RequestException as exc:
            log.warning("fetch_gameplay: download failed for %s: %s", cand["source_url"], exc)
            dest.unlink(missing_ok=True)
            continue
        _write_thumbnail(dest, thumbs / f"{name}.png")
        entries.append(
            {
                "file": name,
                "platform": cand["platform"],
                "source_url": cand["source_url"],
                "download_url": cand["download_url"],
                "duration": cand["duration"],
                "resolution": f"{cand['width']}x{cand['height']}",
                "credit": cand["credit"],
                "query": cand["query"],
                "bytes": size,
            }
        )
        log.info(
            "fetch_gameplay: %s (%.0fs, %s)", name, cand["duration"], entries[-1]["resolution"]
        )

    manifest = write_manifest(entries)
    log.info(
        "fetch_gameplay: %d clip(s) in %s; thumbnails in %s — screen these for "
        "watermarks/logos, then call prune() with the keepers",
        len(entries),
        GAMEPLAY_DIR,
        thumbs,
    )
    return manifest
