"""Stage 4 — render.

Reads: work/{video_id}/source.mp4, work/{video_id}/words.json
Writes: work/{video_id}/caps_{n}.ass, work/{video_id}/out_{n}.mp4

Three compositions: `tight_crop` (baseline — blur-fill with the subject scaled
up and center-cropped to fill more vertical space), `blur_fill` (no crop, more
dead space), and `split_screen` (source panel + gameplay panel per CLAUDE.md —
available but not tone-wired yet; picking it for "low-motion segments" needs a
motion signal this pipeline doesn't compute, so it stays a manual --composition
choice until that lands). No VO. Caption style is a lookup on `tone` via
clipper.styles.style_for_tone — CLAUDE.md's Tone routing table, never random,
never a silent default for an unknown tone. Every ffmpeg/ffprobe call goes
through run_ffmpeg/run_ffprobe. Re-running never touches stage 1 or 2.
"""

from __future__ import annotations

import hashlib
import json
import logging

from .ass import Word, build_ass
from .ffmpeg import run_ffmpeg, run_ffprobe
from .paths import CAPTION_FONT, GAMEPLAY_DIR, WATERMARK_FONT, work_dir
from .styles import style_for_tone

log = logging.getLogger("clipper.render")

# Duration hard-fail bounds (CLAUDE.md: "18-58s inclusive. Hard fail outside.").
MIN_DURATION = 18.0
MAX_DURATION = 58.0

WATERMARK_HANDLE = "@HANDLE"
WATERMARK_Y = 1400  # below the caption band, still inside the y 250-1520 safe zone

# Tight-crop scale-up factor: 1080 * 1.6 = 1728 wide, cropped back to 1080.
TIGHT_CROP_WIDTH = 1728

COMPOSITIONS = ("blur_fill", "tight_crop", "split_screen")


def _check_fonts() -> None:
    missing = [str(p) for p in (CAPTION_FONT, WATERMARK_FONT) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Vendored fonts missing (no fontconfig fallback by design):\n  "
            + "\n  ".join(missing)
            + "\nDownload the Montserrat family (OFL) and place these exact files "
            "in ./fonts/ before rendering."
        )


def _clip_words(words: list[dict], start: float, end: float) -> list[Word]:
    """Words whose start falls in [start, end), shifted to clip-relative time."""
    out: list[Word] = []
    for w in words:
        ws = float(w["start"])
        if start <= ws < end:
            out.append(
                {
                    "word": str(w["word"]),
                    "start": max(0.0, ws - start),
                    "end": max(0.0, float(w["end"]) - start),
                }
            )
    return out


def _caption_and_watermark_tail(
    comp_label: str, ass_path: str, fonts_dir: str, wm_font: str
) -> str:
    """Shared tail for every composition: burn captions, then the watermark."""
    return (
        f"[{comp_label}]ass={ass_path}:fontsdir={fonts_dir}[capped];"
        f"[capped]drawtext=fontfile={wm_font}:text='{WATERMARK_HANDLE}':"
        f"fontcolor=white@0.45:fontsize=40:x=(w-tw)/2:y={WATERMARK_Y},fps=30,setsar=1[vout]"
    )


def _blur_fill_filtergraph(ass_path: str, fonts_dir: str, wm_font: str) -> str:
    tail = _caption_and_watermark_tail("comp", ass_path, fonts_dir, wm_font)
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=28[bgb];"
        "[fg]scale=1080:-2[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp];"
        f"{tail};"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )


def _tight_crop_filtergraph(ass_path: str, fonts_dir: str, wm_font: str) -> str:
    """Blur-fill, but scale the foreground ~1.6x and center-crop the sides so
    the subject fills more vertical space before the blurred remainder shows.
    """
    tail = _caption_and_watermark_tail("comp", ass_path, fonts_dir, wm_font)
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=28[bgb];"
        f"[fg]scale={TIGHT_CROP_WIDTH}:-2,crop=1080:ih[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp];"
        f"{tail};"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )


def _split_screen_filtergraph(ass_path: str, fonts_dir: str, wm_font: str) -> str:
    """Source panel + gameplay panel per CLAUDE.md. Gameplay audio discarded.

    The gameplay chain scales to *cover* the 1080x864 panel and then
    center-crops, so footage of any aspect fills the panel exactly: a plain
    `scale=-2:864` only reaches 1080 wide when the source is at least 5:4, and
    anything narrower (16:9, or a vertical 9:16 meme) would come up short and
    leave the panel unfilled. `setsar=1` keeps non-square-pixel sources from
    rendering stretched.
    """
    tail = _caption_and_watermark_tail("comp", ass_path, fonts_dir, wm_font)
    return (
        "color=c=black:s=1080x1920:r=30[base];"
        "[0:v]scale=1080:-2[src];"
        "[1:v]scale=1080:864:force_original_aspect_ratio=increase,"
        "crop=1080:864,setsar=1[game];"
        "[base][src]overlay=0:250[t1];"
        "[t1][game]overlay=0:1056[comp];"
        f"{tail};"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )


def _probe_duration(path: str) -> float:
    proc = run_ffprobe(["-v", "error", "-show_entries", "format=duration", "-of", "json", path])
    return float(json.loads(proc.stdout)["format"]["duration"])


def _pick_gameplay(video_id: str, n: int, needed_duration: float) -> tuple[str, float]:
    """Pick a gameplay file and a start offset, both seeded by {video_id}:{n}.

    Reproducible per clip, and different across clips/videos, per CLAUDE.md.
    Returns (path, offset_seconds).
    """
    files = sorted(GAMEPLAY_DIR.glob("*.mp4"))
    if not files:
        raise FileNotFoundError(
            f"split_screen: no gameplay footage in {GAMEPLAY_DIR}. "
            "Add assets/gameplay/*.mp4 (a placeholder is fine for testing geometry)."
        )

    digest = hashlib.sha256(f"{video_id}:{n}".encode()).digest()
    file_idx = int.from_bytes(digest[:8], "big") % len(files)
    offset_frac = int.from_bytes(digest[8:16], "big") / float(1 << 64)

    chosen = files[file_idx]
    duration = _probe_duration(str(chosen))
    span = max(0.0, duration - needed_duration)
    offset = round(offset_frac * span, 2)
    return str(chosen), offset


def _validate_output(out_path: str) -> None:
    """Probe the finished mp4 and assert the non-negotiable output spec.

    Silent bad output is worse than a crash — every mismatch raises.
    """
    proc = run_ffprobe(
        [
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate:format=duration",
            "-of",
            "json",
            out_path,
        ]
    )
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"validate: {out_path} has no video stream")
    if (video.get("width"), video.get("height")) != (1080, 1920):
        raise ValueError(
            f"validate: {out_path} is {video.get('width')}x{video.get('height')}, "
            "expected 1080x1920"
        )
    num, _, den = str(video.get("r_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den or 1)
    if abs(fps - 30.0) > 0.01:
        raise ValueError(f"validate: {out_path} is {fps}fps, expected 30")

    if not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError(f"validate: {out_path} has no audio stream")

    duration = float(data.get("format", {}).get("duration", 0.0))
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        raise ValueError(
            f"validate: {out_path} duration {duration:.1f}s outside {MIN_DURATION}-{MAX_DURATION}s"
        )
    log.info("validate: %s OK (1080x1920, %.2ffps, %.1fs, audio present)", out_path, fps, duration)


def render(
    video_id: str,
    n: int,
    start: float,
    end: float,
    tone: str,
    *,
    composition: str = "tight_crop",
    force: bool = False,
) -> str:
    """Render clip ``n`` of ``video_id`` between ``start`` and ``end`` seconds.

    ``tone`` is a required lookup into styles.yaml (clipper.styles) — no
    default, since a missing tone must fail loudly rather than silently
    reusing whatever style happened to run last. ``composition`` selects the
    filter graph: tight_crop (default baseline), blur_fill, or split_screen.
    Returns the path to out_{n}.mp4.
    """
    if composition not in COMPOSITIONS:
        raise ValueError(
            f"render: unknown composition {composition!r}, expected one of {COMPOSITIONS}"
        )

    duration = end - start
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        raise ValueError(
            f"render: clip duration {duration:.1f}s outside {MIN_DURATION}-{MAX_DURATION}s "
            f"(start={start}, end={end})"
        )

    # Validated before the cache check so a bad tone fails loudly every time,
    # never silently masked by reusing a stale render from a prior tone.
    style_overrides, group_size = style_for_tone(tone)

    _check_fonts()

    wd = work_dir(video_id)
    source = wd / "source.mp4"
    words_path = wd / "words.json"
    ass_path = wd / f"caps_{n}.ass"
    out_path = wd / f"out_{n}.mp4"

    if not source.exists():
        raise FileNotFoundError(f"render: {source} missing — run stage 1 (ingest) first.")
    if not words_path.exists():
        raise FileNotFoundError(f"render: {words_path} missing — run stage 2 (transcribe) first.")

    if out_path.exists() and not force:
        log.info("render: cache hit for %s (%s), skipping. Pass force to re-render.", out_path, n)
        return str(out_path)

    words = json.loads(words_path.read_text())["words"]
    clip_words = _clip_words(words, start, end)
    ass_text = build_ass(clip_words, group_size, style_overrides)
    ass_path.write_text(ass_text)
    log.info(
        "render: wrote %s (%d caption words, tone=%s, group_size=%d)",
        ass_path,
        len(clip_words),
        tone,
        group_size,
    )

    fonts_dir = str(CAPTION_FONT.parent)
    wm_font = str(WATERMARK_FONT)

    input_args = ["-ss", f"{start}", "-i", str(source)]

    if composition == "blur_fill":
        fc = _blur_fill_filtergraph(str(ass_path), fonts_dir, wm_font)
    elif composition == "tight_crop":
        fc = _tight_crop_filtergraph(str(ass_path), fonts_dir, wm_font)
    else:  # split_screen
        gameplay_path, gameplay_offset = _pick_gameplay(video_id, n, duration)
        log.info("render: split_screen gameplay=%s offset=%.2fs", gameplay_path, gameplay_offset)
        input_args += ["-ss", f"{gameplay_offset}", "-i", gameplay_path]
        fc = _split_screen_filtergraph(str(ass_path), fonts_dir, wm_font)

    run_ffmpeg(
        [
            "-y",
            *input_args,
            "-t",
            f"{duration}",
            "-filter_complex",
            fc,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )

    _validate_output(str(out_path))
    return str(out_path)
