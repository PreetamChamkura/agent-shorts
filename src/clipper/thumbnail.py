"""Stage 4 (alongside the video render) — static thumbnail + upload metadata.

Reads: work/{video_id}/source.mp4, work/{video_id}/visual_judge.json
       (optional — Twitch clips only, see select_thumbnail_timestamp)
Writes: work/{video_id}/thumbnail_{n}.jpg, work/{video_id}/metadata_{n}.json

Why this exists at all (2026 platform research, see CLAUDE.md): YouTube
Shorts' custom-thumbnail feature (July 2026) is YPP + Studio-desktop only —
the Data API's thumbnails.set is undocumented/unreliable for Shorts. So this
is NOT an automated-upload feature; it generates the same kind of asset the
video file itself is — something the human copies in at manual upload time.
No publishing code, per CLAUDE.md's Out of scope.

## Frame selection

Prefers Layer 3's own already-computed judgment (clipper.visual_judge's
best_frame_timestamp) over re-sampling and re-judging: that layer already
looked at several frames of this exact clip to decide pass/reject, so it
also names which one is most worth using as a thumbnail — see
select_thumbnail_timestamp. That data only exists for Twitch clips (Layer 3
never runs on the long-form YouTube path) and can fall outside the FINAL
segment's own [start, end] (select_clip can trim tighter than the full clip
Layer 3 sampled across) — both cases fall back to a cheap pixel-stats scan
within [start, end] (_pick_engaging_frame_cheap), reusing clipper.frames'
edge_density the same way visual_gate's layer 1 does. That fallback is a
proxy, not a judgment — it cannot tell "engaging" from "busy" any more than
layer 1 can tell "blank" from "dark and moody" (CLAUDE.md), so it's
explicitly the second choice, not the primary mechanism.

For facecam_split, the fallback scores ONLY the facecam panel's own source
region (clipper.render._facecam_panel_crop_box) rather than the whole
frame — the point is catching a visible reaction, and the main-content
panel's own motion would otherwise dominate the score for a reason that has
nothing to do with the facecam.

## Composition + text treatment

Resolves composition (tight_crop vs facecam_split) independently via
clipper.face_detect, exactly like render() and visual_judge() each already
do — CLAUDE.md's stage-independence contract: sharing one detection call
across stages would couple them, so this small, cheap OpenCV pass runs a
third time rather than being threaded through as a shared argument.

Text overlay uses the tone's own highlight color (clipper.styles,
clipper.ass.DEFAULT_STYLE) for consistency with the video's own captions,
Montserrat ExtraBold (the same vendored caption font), and a black
translucent scrim placed behind the text band for legibility regardless of
the source frame's own brightness — no existing "pre-roll dim" treatment
was found elsewhere in this codebase to reuse (checked before building:
narrate.py's cold_open change removed a prepended pre-roll rather than
adding a dim filter), so this scrim is a new, small treatment built for
this specific need. Text and scrim both stay inside a centered 1080x1080
safe square (CLAUDE.md safe zones: Reels/TikTok grid-crop the cover image),
vertically clear of the facecam_split panel seam at y≈960.

Text is passed to ffmpeg via a temp textfile (drawtext's `textfile=`), not
inlined into the filter-graph string — thumbnail_text can contain quotes,
colons, and emoji, exactly the characters that make ffmpeg filter-string
escaping "fail silently and weirdly" per CLAUDE.md; writing it to a file and
referencing the path sidesteps that entirely, the same reason captions are
written to caps_{n}.ass and referenced by path rather than inlined.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path

from .ass import DEFAULT_STYLE
from .face_detect import find_face_region, is_small_corner_facecam
from .ffmpeg import run_ffmpeg, run_ffprobe
from .frames import edge_density, read_pgm_gray
from .paths import CAPTION_FONT, work_dir
from .render import _facecam_panel_crop_box, _facecam_split_video, _tight_crop_video
from .styles import style_for_tone

log = logging.getLogger("clipper.thumbnail")

THUMB_WIDTH = 1080
THUMB_HEIGHT = 1920

# Centered safe square (CLAUDE.md: Reels crops the cover to 4:5 in-feed and
# 3:4 on the profile grid, TikTok's grid does its own crop — content outside
# a centered square gets cut on at least one platform).
SAFE_SQUARE_TOP = (THUMB_HEIGHT - THUMB_WIDTH) // 2  # 420
SAFE_SQUARE_BOTTOM = SAFE_SQUARE_TOP + THUMB_WIDTH  # 1500

# Text band vertical center: inside the safe square, in its lower half (the
# conventional thumbnail-text position) and clear of the facecam_split panel
# seam at y≈960 (CLAUDE.md safe zones) so the band never visually collides
# with it.
TEXT_BAND_CENTER_Y = 1300

TEXT_FONTSIZE = 88
TEXT_MARGIN_X = 60  # each side, inside the 1080-wide frame
# Rough average glyph width for Montserrat ExtraBold as a fraction of
# fontsize — untuned, a wrapping estimate rather than a measured value (no
# text-measurement library in this project's dependency set). Errs toward
# wrapping a little early rather than overflowing the safe square.
_AVG_CHAR_WIDTH_FACTOR = 0.62
LINE_GAP = 14
SCRIM_ALPHA = 0.55
SCRIM_PAD_Y = 36

# Cheap-fallback frame scoring (only used when Layer 3's data is unavailable
# or falls outside the final segment window — see module docstring).
_N_FALLBACK_CANDIDATES = 6
_FRAME_SCORE_SIZE = 96

# Confirmed for real (not assumed): ffmpeg drawtext + this vendored Montserrat
# font renders emoji as broken tofu boxes, not the glyph — Montserrat is a
# text font with no emoji coverage, and drawtext has no font-fallback
# mechanism (CLAUDE.md is explicit that this project never relies on
# fontconfig substitution for exactly this kind of silent breakage). The
# select.md/select_clip.md prompt still allows thumbnail_text up to 2 emoji
# as an accent — those stay in metadata.json for a human assembling a
# thumbnail by hand in other software, which can render them fine — but the
# auto-generated overlay strips them first, since a real glyph would beat a
# tofu box and a missing character is far less noticeable than a broken one.
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # emoticons, transport, symbols & pictographs (+ extended-A)
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator letters (flag emoji)
    "\U0000fe0f"  # variation selector-16 (emoji presentation)
    "]+"
)


def _strip_emoji(text: str) -> str:
    """Remove emoji before the thumbnail overlay renders it — see _EMOJI_RE."""
    return _EMOJI_RE.sub("", text)


# See _extract_full_frame: render()'s multi-branch filter graphs (a real
# seeked input plus a synthetic `color=` source feeding an overlay chain) can
# return a solid-black first output frame before the two sync up — the exact
# issue clipper.visual_judge's _extract_preview_frame already found and
# fixed for its own (downscaled) still-frame extraction. Duplicated here
# rather than shared, per this project's stage-independence convention
# (render.py duplicates rather than imports select.py's word-snap logic for
# the same reason) — kept at the same value since it fixed the real failure
# there and this is the same underlying ffmpeg behavior.
_WARMUP_FRAMES = 8


def _wrap_lines(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap. A single word longer than ``max_chars`` still gets
    its own line rather than being split mid-word."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = f"{current} {w}".strip()
        if not current or len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _ass_to_ffmpeg_hex(ass_color: str) -> str:
    """'&HAABBGGRR' (ASS/SSA byte order) -> '0xRRGGBB' (ffmpeg drawtext).

    Alpha is dropped — the scrim behind the text is what actually handles
    legibility (see module docstring); the text itself renders fully opaque.
    """
    hexpart = ass_color.upper().removeprefix("&H")
    if len(hexpart) != 8:
        raise ValueError(f"not a valid &HAABBGGRR color: {ass_color!r}")
    _aa, bb, gg, rr = hexpart[0:2], hexpart[2:4], hexpart[4:6], hexpart[6:8]
    return f"0x{rr}{gg}{bb}"


def _tone_accent_hex(tone: str) -> str:
    """The tone's own highlight color (clipper.styles), as an ffmpeg hex —
    same color the video's own active-word captions use, for the thumbnail
    text to look like it belongs to the same clip."""
    overrides, _group_size = style_for_tone(tone)
    style = {**DEFAULT_STYLE, **overrides}
    return _ass_to_ffmpeg_hex(str(style["highlight"]))


def _extract_score_frame(
    source: str, ts: float, crop_box: tuple[int, int, int, int] | None
) -> bytes | None:
    """Small grayscale pixels for one fallback candidate frame at ``ts``,
    optionally cropped to ``crop_box`` (w, h, x, y) first — used to score
    just the facecam panel's own source region for facecam_split, not the
    whole frame (see module docstring). Returns None on extraction failure
    (e.g. ``ts`` past the source's actual end) rather than raising — a
    missing candidate simply doesn't compete for the pick.
    """
    vf = ""
    if crop_box is not None:
        w, h, x, y = crop_box
        vf += f"crop={w}:{h}:{x}:{y},"
    vf += f"scale={_FRAME_SCORE_SIZE}:{_FRAME_SCORE_SIZE},format=gray"

    with tempfile.TemporaryDirectory(prefix="clipper_thumbpick_") as td:
        out = Path(td) / "f.pgm"
        run_ffmpeg(["-y", "-ss", f"{ts:.3f}", "-i", source, "-frames:v", "1", "-vf", vf, str(out)])
        if not out.exists():
            return None
        return read_pgm_gray(out)


def _evenly_spaced(start: float, end: float, n: int) -> list[float]:
    """n absolute timestamps evenly spread across [start, end], inset from
    the very edges. Mirrors the sampling pattern clipper.visual_judge and
    clipper.face_detect each already use — duplicated rather than imported,
    same stage-independence reasoning as _WARMUP_FRAMES above."""
    span = end - start
    if n < 1 or span <= 0:
        return []
    inset = span * 0.05
    lo, hi = start + inset, end - inset
    if n == 1:
        return [(lo + hi) / 2]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def _pick_engaging_frame_cheap(
    source: str,
    start: float,
    end: float,
    composition: str,
    face_region: dict | None,
) -> float:
    """Fallback frame pick when Layer 3's data isn't usable (see module
    docstring): highest edge_density among a handful of candidates sampled
    across [start, end] — the same cheap texture/detail proxy visual_gate's
    layer 1 uses, scoped to the facecam panel's own region for facecam_split.
    An untuned pixel-stats heuristic, not a vision judgment, same caveat as
    layer 1 (CLAUDE.md) — the best available signal without paying for
    another vision-model call on every long-form render.
    """
    crop_box = (
        _facecam_panel_crop_box(face_region)
        if composition == "facecam_split" and face_region
        else None
    )
    best_ts: float | None = None
    best_score = -1.0
    for ts in _evenly_spaced(start, end, _N_FALLBACK_CANDIDATES):
        pixels = _extract_score_frame(source, ts, crop_box)
        if pixels is None:
            continue
        score = edge_density(pixels, _FRAME_SCORE_SIZE, _FRAME_SCORE_SIZE)
        if score > best_score:
            best_score, best_ts = score, ts
    if best_ts is None:
        log.warning(
            "thumbnail: every fallback candidate frame failed to extract for %.2f-%.2fs — "
            "using the segment midpoint",
            start,
            end,
        )
        best_ts = (start + end) / 2
    return best_ts


def select_thumbnail_timestamp(
    video_id: str,
    start: float,
    end: float,
    composition: str,
    face_region: dict | None,
) -> tuple[float, str]:
    """Absolute source.mp4 timestamp for this clip's thumbnail, and a short
    tag naming how it was picked ("layer3" or "fallback" — for logging and
    tests, not shown to any end user). See module docstring for the
    preference order.
    """
    wd = work_dir(video_id)
    judge_path = wd / "visual_judge.json"
    if judge_path.exists():
        cached = json.loads(judge_path.read_text())
        ts = cached.get("best_frame_timestamp")
        if ts is not None:
            if start <= float(ts) <= end:
                return float(ts), "layer3"
            log.info(
                "thumbnail: %s Layer 3 best frame %.2fs falls outside the final segment "
                "%.2f-%.2fs (select_clip trimmed past it) — falling back to a cheap scan",
                video_id,
                ts,
                start,
                end,
            )

    source = str(wd / "source.mp4")
    ts = _pick_engaging_frame_cheap(source, start, end, composition, face_region)
    return ts, "fallback"


def _extract_full_frame(source: str, ts: float, video_fc: str, out_path: Path) -> None:
    """One full-resolution (1080x1920) frame at ``ts``, composited through
    ``video_fc`` (render.py's real video-only filter chain) — see
    _WARMUP_FRAMES for why this bursts and keeps only the last frame rather
    than requesting one directly."""
    with tempfile.TemporaryDirectory(prefix="clipper_thumbframe_") as td:
        pattern = Path(td) / "w_%02d.png"
        run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{ts:.3f}",
                "-i",
                source,
                "-frames:v",
                str(_WARMUP_FRAMES),
                "-filter_complex",
                f"{video_fc};[comp]scale={THUMB_WIDTH}:{THUMB_HEIGHT}[out]",
                "-map",
                "[out]",
                str(pattern),
            ]
        )
        produced = sorted(Path(td).glob("w_*.png"))
        if produced:
            produced[-1].replace(out_path)


def _resolve_composition(source: str, start: float, end: float) -> tuple[str, dict | None]:
    """Same detection render() runs when composition is left unset — see
    module docstring on why this isn't shared/threaded through instead."""
    face_region = find_face_region(source, start, end)
    if face_region and is_small_corner_facecam(face_region):
        return "facecam_split", face_region
    return "tight_crop", None


def _validate_thumbnail(out_path: str) -> None:
    proc = run_ffprobe(
        ["-v", "error", "-show_entries", "stream=width,height", "-of", "json", out_path]
    )
    streams = json.loads(proc.stdout).get("streams", [])
    if not streams or (streams[0].get("width"), streams[0].get("height")) != (
        THUMB_WIDTH,
        THUMB_HEIGHT,
    ):
        raise ValueError(f"thumbnail: {out_path} is not {THUMB_WIDTH}x{THUMB_HEIGHT}")


def generate_thumbnail(
    video_id: str,
    n: int,
    start: float,
    end: float,
    tone: str,
    thumbnail_text: str,
    *,
    composition: str | None = None,
    force: bool = False,
) -> str:
    """Generate work/{video_id}/thumbnail_{n}.jpg: a static 1080x1920 image,
    the real composited frame plus a tone-colored, scrim-backed
    ``thumbnail_text`` overlay kept inside the centered safe square.

    ``composition`` mirrors render()'s own parameter: leave unset to
    auto-detect (tight_crop vs facecam_split) via the same clipper.face_detect
    logic render() uses. Cached like every other stage output — skipped when
    thumbnail_{n}.jpg already exists, unless ``force``.
    """
    wd = work_dir(video_id)
    source = wd / "source.mp4"
    out_path = wd / f"thumbnail_{n}.jpg"

    if not source.exists():
        raise FileNotFoundError(f"thumbnail: {source} missing — run stage 1 (ingest) first.")
    if out_path.exists() and not force:
        log.info("thumbnail: cache hit for n=%d (%s), skipping", n, out_path)
        return str(out_path)
    if not thumbnail_text.strip():
        raise ValueError(f"thumbnail: n={n} has no thumbnail_text to render")

    face_region = None
    if composition is None:
        composition, face_region = _resolve_composition(str(source), start, end)
    elif composition == "facecam_split":
        face_region = find_face_region(str(source), start, end)
        if not face_region or not is_small_corner_facecam(face_region):
            raise ValueError(
                f"thumbnail: n={n} composition='facecam_split' was requested explicitly, "
                "but no small corner facecam was detected — pass tight_crop, or leave "
                "composition unset to auto-detect."
            )

    ts, pick_source = select_thumbnail_timestamp(video_id, start, end, composition, face_region)
    log.info(
        "thumbnail: n=%d composition=%s frame=%.2fs (source=%s)", n, composition, ts, pick_source
    )

    video_fc = (
        _facecam_split_video(face_region) if composition == "facecam_split" else _tight_crop_video()
    )

    accent_hex = _tone_accent_hex(tone)
    render_text = _strip_emoji(thumbnail_text.strip().upper()).strip()
    if render_text != thumbnail_text.strip().upper():
        log.info(
            "thumbnail: n=%d stripped emoji from thumbnail_text for the rendered overlay "
            "(kept in metadata_%d.json): %r -> %r",
            n,
            n,
            thumbnail_text,
            render_text,
        )
    usable_width = THUMB_WIDTH - 2 * TEXT_MARGIN_X
    max_chars = max(4, int(usable_width / (TEXT_FONTSIZE * _AVG_CHAR_WIDTH_FACTOR)))
    lines = _wrap_lines(render_text, max_chars)
    line_height = TEXT_FONTSIZE * 1.25
    text_block_h = len(lines) * line_height
    box_h = round(text_block_h + 2 * SCRIM_PAD_Y)
    box_y = round(TEXT_BAND_CENTER_Y - box_h / 2)
    text_y = round(box_y + SCRIM_PAD_Y)

    if box_y < SAFE_SQUARE_TOP or box_y + box_h > SAFE_SQUARE_BOTTOM:
        log.warning(
            "thumbnail: n=%d text band (%d-%d) doesn't fully fit the %d-%d safe square — "
            "%d line(s) of thumbnail_text may be long for this layout",
            n,
            box_y,
            box_y + box_h,
            SAFE_SQUARE_TOP,
            SAFE_SQUARE_BOTTOM,
            len(lines),
        )

    with tempfile.TemporaryDirectory(prefix="clipper_thumb_") as td:
        tmp_dir = Path(td)
        frame_path = tmp_dir / "frame.png"
        _extract_full_frame(str(source), ts, video_fc, frame_path)
        if not frame_path.exists():
            raise RuntimeError(f"thumbnail: n={n} failed to extract a frame at {ts:.2f}s")

        text_path = tmp_dir / "text.txt"
        text_path.write_text("\n".join(lines), encoding="utf-8")

        vf = (
            "eq=contrast=1.05:brightness=-0.02,"
            f"drawbox=x=0:y={box_y}:w={THUMB_WIDTH}:h={box_h}:color=black@{SCRIM_ALPHA}:t=fill,"
            f"drawtext=fontfile={CAPTION_FONT}:textfile={text_path}:fontcolor={accent_hex}:"
            f"fontsize={TEXT_FONTSIZE}:bordercolor=black:borderw=10:line_spacing={LINE_GAP}:"
            f"x=(w-text_w)/2:y={text_y}"
        )
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(frame_path),
                "-vf",
                vf,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_path),
            ]
        )

    try:
        _validate_thumbnail(str(out_path))
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    log.info(
        "thumbnail: wrote %s (%d word(s), %d line(s))",
        out_path,
        len(thumbnail_text.split()),
        len(lines),
    )
    return str(out_path)


def write_metadata(video_id: str, n: int, seg: dict, *, force: bool = False) -> str:
    """Write work/{video_id}/metadata_{n}.json — suggested_title,
    thumbnail_text, and suggested_hashtags from segments.json, for a human
    to copy in at manual upload time. Not embedded anywhere in the video or
    thumbnail; upload stays manual per CLAUDE.md's Out of scope.
    """
    wd = work_dir(video_id)
    out_path = wd / f"metadata_{n}.json"
    if out_path.exists() and not force:
        log.info("thumbnail: metadata cache hit for n=%d (%s), skipping", n, out_path)
        return str(out_path)

    payload = {
        "video_id": video_id,
        "n": n,
        "suggested_title": seg.get("suggested_title", ""),
        "thumbnail_text": seg.get("thumbnail_text", ""),
        "suggested_hashtags": seg.get("suggested_hashtags", []),
        "caption_title": seg.get("caption_title", ""),
        "tone": seg.get("tone", ""),
    }
    out_path.write_text(json.dumps(payload, indent=2))
    log.info("thumbnail: wrote %s", out_path)
    return str(out_path)
