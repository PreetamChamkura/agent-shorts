"""Visual gate layer 2 — facecam region detection (CLAUDE.md step: Twitch
clip composition fix).

OpenCV Haar-cascade face detection, no API call. Not a pass/fail filter —
this feeds render.py's composition choice (where to crop, or whether to
split-screen), never rejects a clip.

Real testing against a real downloaded Twitch clip (the "Doc" clip,
1932625054) found the obvious approach — run the cascade once on one
frame — unreliable on its own: a single frame produced 3 "face" hits, only
one of which (the actual facecam, top-right) was real; the other two were
skin/hand-texture false positives from Haar's well-known weakness on
textured non-face content. Sampling several frames across the clip and
keeping only a box that recurs at essentially the same position solves
this cleanly: the real facecam is a FIXED overlay, so it lands in nearly
the same spot every sample, while spurious hand/texture detections land in
a different, inconsistent spot each time and never repeat. Across 7 samples
of the Doc clip, the real facecam box recurred in 4; each false positive
appeared exactly once and never again.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from .ffmpeg import run_ffmpeg

log = logging.getLogger("clipper.face_detect")

CASCADE_NAME = "haarcascade_frontalface_default.xml"

N_SAMPLE_FRAMES = 7
# Detection runs on a downscaled frame for speed; the result is scaled back
# up to the source's real resolution before being returned.
DETECT_WIDTH = 640

# Two detections are "the same" box if their centers land within this many
# pixels of each other, in DETECT_WIDTH's coordinate space.
CLUSTER_DISTANCE_PX = 40.0

# A box must recur at least this many times across the samples to be trusted
# as the real facecam rather than a one-off false positive.
MIN_CLUSTER_HITS = 2

# A detected region covering more of the frame than this is treated as "the
# subject fills the frame" (podcast-style), not a small corner facecam — see
# is_small_corner_facecam().
FACECAM_MAX_AREA_FRACTION = 0.15

# A box's center must fall within this fraction of frame width/height from
# BOTH a horizontal and vertical edge to count as "in a corner" rather than
# just small — see is_small_corner_facecam().
CORNER_MARGIN_FRACTION = 0.25


def _load_cascade():
    import cv2

    path = cv2.data.haarcascades + CASCADE_NAME
    cascade = cv2.CascadeClassifier(path)
    if cascade.empty():
        raise RuntimeError(f"face_detect: failed to load cascade at {path}")
    return cascade


def detect_faces_in_frame(gray_frame, cascade) -> list[tuple[int, int, int, int]]:
    """Raw Haar-cascade hits for one grayscale frame (numpy array), as
    (x, y, w, h) tuples. No clustering/filtering — see find_face_region for
    the temporally-consistent version actually meant for use.

    scaleFactor=1.05 (finer than the common 1.1 default): a real facecam
    (wallbang clip, 3868091153) was missed on every one of 7 samples at
    1.1 — its face was ~47px in the 640-wide detection frame, and the
    Doc clip's facecam sits at ~170px. Facecam size varies a lot between
    streamers/setups, and 1.1's coarser geometric search steps skip over
    some legitimate sizes entirely; 1.05 caught it immediately at the same
    minNeighbors. Costs more time per frame, but this only ever runs on a
    handful of downscaled samples per clip.
    """
    boxes = cascade.detectMultiScale(gray_frame, scaleFactor=1.05, minNeighbors=5, minSize=(20, 20))
    return [tuple(int(v) for v in box) for box in boxes]


def _sample_timestamps(start: float, end: float, n: int) -> list[float]:
    """n timestamps evenly spread across [start, end], inset from the very
    edges (a clip's first/last instants are more likely to be transitional —
    a cut, a fade, motion blur)."""
    if n < 1:
        return []
    span = end - start
    inset = span * 0.05
    lo, hi = start + inset, end - inset
    if n == 1:
        return [(lo + hi) / 2]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def _extract_sample_frames(source: str, timestamps: list[float], tmp_dir: Path) -> list[Path]:
    paths = []
    for i, ts in enumerate(timestamps):
        out = tmp_dir / f"sample_{i:02d}.png"
        run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{ts:.3f}",
                "-i",
                source,
                "-frames:v",
                "1",
                "-vf",
                f"scale={DETECT_WIDTH}:-2",
                "-update",
                "1",
                str(out),
            ]
        )
        if out.exists():
            paths.append(out)
    return paths


def _cluster_boxes(
    all_boxes: list[tuple[int, int, int, int]],
) -> tuple[tuple[float, float, float, float], int] | None:
    """Group boxes by proximity of their centers; return the (averaged box,
    hit count) of the largest cluster, or None if ``all_boxes`` is empty.

    Simple greedy clustering — fine at this scale (a handful of samples,
    each with at most a few detections): for every box, all other boxes
    within CLUSTER_DISTANCE_PX of its center join its cluster.
    """
    if not all_boxes:
        return None

    def center(b):
        x, y, w, h = b
        return (x + w / 2, y + h / 2)

    best_cluster: list[tuple[int, int, int, int]] = []
    for anchor in all_boxes:
        ax, ay = center(anchor)
        cluster = [
            b
            for b in all_boxes
            if ((center(b)[0] - ax) ** 2 + (center(b)[1] - ay) ** 2) ** 0.5 <= CLUSTER_DISTANCE_PX
        ]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster

    n = len(best_cluster)
    avg = tuple(sum(vals) / n for vals in zip(*best_cluster, strict=True))
    return avg, n


def _detect_boxes_in_saved_frames(
    frame_paths: list[Path], cascade
) -> tuple[list[tuple[int, int, int, int]], int, int]:
    """cv2-heavy inner loop, pulled out of find_face_region so tests can
    monkeypatch this one function instead of reaching into cv2 internals —
    same "mock the wrapper, not the library" convention as narrate.py's
    _synthesize / transcribe.py's transcribe_audio_file.

    Returns (all_boxes, detect_width, detect_height) — width/height are the
    actual pixel size of the (already-downscaled) frames actually loaded,
    0/0 if none of ``frame_paths`` could be loaded.
    """
    import cv2

    detect_w = detect_h = 0
    all_boxes: list[tuple[int, int, int, int]] = []
    for path in frame_paths:
        img = cv2.imread(str(path))
        if img is None:
            continue
        if not detect_w:
            detect_h, detect_w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        all_boxes.extend(detect_faces_in_frame(gray, cascade))
    return all_boxes, detect_w, detect_h


def find_face_region(
    source: str, start: float, end: float, *, n_samples: int = N_SAMPLE_FRAMES
) -> dict | None:
    """The facecam's screen position within [start, end] of ``source``, in
    the SOURCE's real pixel coordinates — or None if nothing recurred
    consistently enough to trust (MIN_CLUSTER_HITS), including the ordinary
    case of a podcast-style source with no small corner facecam to find.

    Returns {"x", "y", "w", "h", "frame_width", "frame_height", "hits",
    "samples"} — x/y/w/h describe the detected box, frame_width/height the
    source's own resolution at that point (so a caller never has to
    re-probe it), hits/samples the recurrence count out of how many frames
    were actually sampled (fewer than n_samples if some extractions failed).
    """
    cascade = _load_cascade()
    timestamps = _sample_timestamps(start, end, n_samples)

    with tempfile.TemporaryDirectory(prefix="clipper_face_") as td:
        frame_paths = _extract_sample_frames(source, timestamps, Path(td))
        if not frame_paths:
            log.warning("face_detect: no frames extracted for %s [%.1f-%.1f]", source, start, end)
            return None

        all_boxes, detect_w, detect_h = _detect_boxes_in_saved_frames(frame_paths, cascade)

    clustered = _cluster_boxes(all_boxes)
    if clustered is None:
        log.info(
            "face_detect: no face detected at all in %d sample(s) of %s", len(frame_paths), source
        )
        return None

    (x, y, w, h), hits = clustered
    if hits < MIN_CLUSTER_HITS:
        log.info(
            "face_detect: best candidate recurred %d/%d sample(s), below %d needed — noise",
            hits,
            len(frame_paths),
            MIN_CLUSTER_HITS,
        )
        return None

    real_w, real_h = probe_frame_size(source)
    scale = real_w / detect_w
    result = {
        "x": round(x * scale, 1),
        "y": round(y * scale, 1),
        "w": round(w * scale, 1),
        "h": round(h * scale, 1),
        "frame_width": real_w,
        "frame_height": real_h,
        "hits": hits,
        "samples": len(frame_paths),
    }
    log.info(
        "face_detect: facecam at x=%.0f y=%.0f w=%.0f h=%.0f (%d/%d samples, source %dx%d)",
        result["x"],
        result["y"],
        result["w"],
        result["h"],
        hits,
        len(frame_paths),
        real_w,
        real_h,
    )
    return result


def probe_frame_size(source: str) -> tuple[int, int]:
    """(width, height) of ``source``'s video stream, via ffprobe."""
    import json as _json

    from .ffmpeg import run_ffprobe

    proc = run_ffprobe(
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            source,
        ]
    )
    stream = _json.loads(proc.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def is_small_corner_facecam(region: dict) -> bool:
    """True if ``region`` (from find_face_region) looks like a small
    corner-overlay facecam rather than a subject filling the frame
    (podcast-style — CLAUDE.md: that case keeps using tight_crop as-is).

    Area alone isn't enough: a real face in a wide/medium shot (tested
    against the Hot Ones podcast source, FALlhXl6CmA) can be just as small
    in area as a genuine corner overlay — a guest sitting further from
    camera in a 1920x1080 frame, not a picture-in-picture box. What actually
    distinguishes an overlay is POSITION: it sits tucked into a corner, not
    generally placed within the frame. Real check: small area AND the box's
    center falls within CORNER_MARGIN_FRACTION of both a horizontal and a
    vertical edge simultaneously (i.e. actually in a corner region, not just
    off to one side or high/low in an otherwise-centered composition).
    Confirmed against real data: the Doc clip's facecam center (0.840, 0.116)
    passes; the Hot Ones face center (0.256, 0.777) fails on the x-axis
    margin, correctly rejected as a real face, not an overlay.
    """
    area_fraction = (region["w"] * region["h"]) / (region["frame_width"] * region["frame_height"])
    if area_fraction > FACECAM_MAX_AREA_FRACTION:
        return False

    cx = (region["x"] + region["w"] / 2) / region["frame_width"]
    cy = (region["y"] + region["h"] / 2) / region["frame_height"]
    near_left_or_right_edge = cx <= CORNER_MARGIN_FRACTION or cx >= (1 - CORNER_MARGIN_FRACTION)
    near_top_or_bottom_edge = cy <= CORNER_MARGIN_FRACTION or cy >= (1 - CORNER_MARGIN_FRACTION)
    return near_left_or_right_edge and near_top_or_bottom_edge
