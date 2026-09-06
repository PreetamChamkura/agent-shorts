"""Visual quality gate for Twitch clips (stage 2b) — three layers, cheapest
first, each thinning the batch before the next spends more:

  Layer 1 (this module, so far): a static-thumbnail pixel-stats pre-filter,
    run on Helix's own thumbnail_url before anything is downloaded or
    transcribed. Real testing against 8 real fetched thumbnails (variance,
    dark-pixel-fraction, edge-density — whole-frame and per-quadrant) found
    NO combination of these that reliably separates "visually empty" from
    "legitimately dark/moody content": a moody dungeon-crawler screenshot and
    an idle hand on a desk mat produce statistically similar large-flat-
    region, low-contrast signatures. A single static frame's pixel
    statistics fundamentally cannot make that call — it's a semantic
    judgment ("is anything interesting happening"), not a statistical one.
    So Layer 1 is intentionally narrow: it hard-rejects only the case pixel
    stats CAN catch safely and with no observed false-positive risk — a
    near-completely-flat single-tone frame (a stream-offline card, a solid
    loading screen). Every candidate is scored and logged regardless of
    outcome, for visibility into the distribution even though only the
    blank-frame case currently acts on it.

  Layer 2 (not yet built): face-region detection via OpenCV, feeding
    composition (not a filter).

  Layer 3 (not yet built): a vision-model judgment pass on what survives
    layers 1 and 2 — this is where "boring but not blank" content like the
    Doc clip's desk-mat thumbnail actually gets caught.

Reuses clipper.frames' PGM parsing (same dependency-free approach as
render.py's loop-stability check) rather than adding an imaging library.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import requests

from .ffmpeg import run_ffmpeg
from .frames import dark_pixel_fraction, edge_density, pixel_variance, read_pgm_gray

log = logging.getLogger("clipper.visual_gate")

THUMBNAIL_TIMEOUT = 30.0
ANALYSIS_SIZE = 64  # matches render.py's LOOP_FRAME_SIZE convention

# A near-zero-variance frame is a single flat tone throughout — a stream-
# offline card, a solid loading/color screen. Chosen with wide margin: the
# lowest variance among 8 real, legitimately-usable fetched thumbnails
# (character-menu screen, "faat_cock") was 514.7 — over 10x this floor. None
# of the 8 real samples tested (including the visually "empty" Doc clip,
# which scored 3687.7 — high, not low, due to its hand-vs-black-desk
# contrast) come remotely close to tripping this. It will not catch a
# boring-but-visible frame like Doc's; that's Layer 3's job by design.
BLANK_VARIANCE_THRESHOLD = 50.0

# Dark-pixel threshold (0-255 grayscale) used for the logged dark_fraction
# diagnostic. Not a rejection criterion — see module docstring.
DARK_PIXEL_LEVEL = 30


def fetch_thumbnail(url: str) -> bytes:
    """Download a clip's thumbnail image. The only networked call here."""
    resp = requests.get(url, timeout=THUMBNAIL_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def thumbnail_scores(image_bytes: bytes) -> dict:
    """{variance, dark_fraction, edge_density} for a thumbnail image.

    Converts via ffmpeg to a small grayscale PGM (dependency-free — no PIL/
    OpenCV/numpy) on disk, same pattern as render.py's loop-frame analysis,
    since ffmpeg's stdout can't safely carry raw PGM bytes through
    run_ffmpeg's text-mode capture.
    """
    with tempfile.TemporaryDirectory(prefix="clipper_thumb_") as td:
        tmp_dir = Path(td)
        src_path = tmp_dir / "in.jpg"
        pgm_path = tmp_dir / "out.pgm"
        src_path.write_bytes(image_bytes)
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(src_path),
                "-vf",
                f"scale={ANALYSIS_SIZE}:{ANALYSIS_SIZE},format=gray",
                str(pgm_path),
            ]
        )
        pixels = read_pgm_gray(pgm_path)

    return {
        "variance": round(pixel_variance(pixels), 1),
        "dark_fraction": round(dark_pixel_fraction(pixels, threshold=DARK_PIXEL_LEVEL), 3),
        "edge_density": round(edge_density(pixels, ANALYSIS_SIZE, ANALYSIS_SIZE), 2),
    }


def screen_thumbnail_scores(scores: dict) -> tuple[bool, str | None]:
    """Pure pass/fail decision from already-computed scores. Returns
    (passed, reason) — reason is None when passed."""
    if scores["variance"] < BLANK_VARIANCE_THRESHOLD:
        return False, (
            f"layer_1: near-uniform frame (variance={scores['variance']:.1f} < "
            f"{BLANK_VARIANCE_THRESHOLD:.0f}) — looks like a blank/placeholder frame"
        )
    return True, None


def screen_clips_layer1(clips: list[dict]) -> list[dict]:
    """Annotate every clip with clip["visual_gate"]["layer_1"] — scores plus
    pass/fail and, when rejected, why. Returns the SAME clips, all of them,
    not a filtered subset: a clip that fails layer 1 still carries its
    scores and reason for visibility (CLAUDE.md-style "log the rejection
    reason, don't just silently drop it"), and later layers or a human can
    still inspect what happened to it. Callers filter on
    clip["visual_gate"]["layer_1"]["passed"] themselves.

    A thumbnail fetch failure (dead URL, timeout) does not reject the clip —
    it's a network hiccup, not a visual judgment — it's logged and the clip
    passes through unscored.
    """
    out = []
    n_rejected = 0
    for clip in clips:
        clip = dict(clip)
        thumb_url = clip.get("thumbnail_url")
        if not thumb_url:
            log.warning("visual_gate: clip %s has no thumbnail_url — skipping layer 1", clip["id"])
            out.append(clip)
            continue
        try:
            image_bytes = fetch_thumbnail(thumb_url)
            scores = thumbnail_scores(image_bytes)
        except (requests.RequestException, OSError) as exc:
            log.warning(
                "visual_gate: thumbnail fetch/analysis failed for %s (%s) — unscored",
                clip["id"],
                exc,
            )
            out.append(clip)
            continue

        passed, reason = screen_thumbnail_scores(scores)
        clip["visual_gate"] = {"layer_1": {"passed": passed, "reason": reason, **scores}}
        if not passed:
            n_rejected += 1
            log.info("visual_gate: REJECT %s (layer 1) — %s", clip["id"], reason)
        else:
            log.info(
                "visual_gate: pass %s (layer 1) — variance=%.1f dark=%.2f edge=%.2f",
                clip["id"],
                scores["variance"],
                scores["dark_fraction"],
                scores["edge_density"],
            )
        out.append(clip)

    log.info(
        "visual_gate: layer 1 screened %d clip(s), rejected %d, %d passed through",
        len(clips),
        n_rejected,
        len(clips) - n_rejected,
    )
    return out
