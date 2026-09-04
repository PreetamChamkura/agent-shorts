"""Stage 4 — render.

Reads: work/{video_id}/source.mp4, work/{video_id}/words.json
Writes: work/{video_id}/caps_{n}.ass, work/{video_id}/out_{n}.mp4

Milestone 1: blur-fill only, no gameplay, no VO, one hardcoded style
(white heavy, 3-word groups, yellow highlight). Every ffmpeg/ffprobe call goes
through run_ffmpeg/run_ffprobe. Re-running never touches stage 1 or 2.
"""

from __future__ import annotations

import json
import logging

from .ass import DEFAULT_STYLE, Word, build_ass
from .ffmpeg import run_ffmpeg, run_ffprobe
from .paths import CAPTION_FONT, WATERMARK_FONT, work_dir

log = logging.getLogger("clipper.render")

# Duration hard-fail bounds (CLAUDE.md: "18-58s inclusive. Hard fail outside.").
MIN_DURATION = 18.0
MAX_DURATION = 58.0

# Milestone-1 hardcoded style.
GROUP_SIZE = 3
WATERMARK_HANDLE = "@HANDLE"


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


def _blur_fill_filtergraph(ass_path: str, fonts_dir: str, wm_font: str) -> str:
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=28[bgb];"
        "[fg]scale=1080:-2[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp];"
        f"[comp]ass={ass_path}:fontsdir={fonts_dir}[capped];"
        f"[capped]drawtext=fontfile={wm_font}:text='{WATERMARK_HANDLE}':"
        "fontcolor=white@0.45:fontsize=40:x=(w-tw)/2:y=1000,fps=30,setsar=1[vout];"
        "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )


def _validate_output(out_path: str) -> None:
    """Probe the finished mp4 and assert the non-negotiable output spec.

    Silent bad output is worse than a crash — every mismatch raises.
    """
    proc = run_ffprobe(
        [
            "-v", "error",
            "-show_entries", "stream=codec_type,width,height,r_frame_rate:format=duration",
            "-of", "json",
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
            f"validate: {out_path} duration {duration:.1f}s outside "
            f"{MIN_DURATION}-{MAX_DURATION}s"
        )
    log.info("validate: %s OK (1080x1920, %.2ffps, %.1fs, audio present)", out_path, fps, duration)


def render(video_id: str, n: int, start: float, end: float, *, force: bool = False) -> str:
    """Render clip ``n`` of ``video_id`` between ``start`` and ``end`` seconds.

    Returns the path to out_{n}.mp4.
    """
    duration = end - start
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        raise ValueError(
            f"render: clip duration {duration:.1f}s outside {MIN_DURATION}-{MAX_DURATION}s "
            f"(start={start}, end={end})"
        )

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
    ass_text = build_ass(clip_words, GROUP_SIZE, DEFAULT_STYLE)
    ass_path.write_text(ass_text)
    log.info("render: wrote %s (%d caption words)", ass_path, len(clip_words))

    fc = _blur_fill_filtergraph(str(ass_path), str(CAPTION_FONT.parent), str(WATERMARK_FONT))
    run_ffmpeg(
        [
            "-y",
            "-ss", f"{start}",
            "-i", str(source),
            "-t", f"{duration}",
            "-filter_complex", fc,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
    )

    _validate_output(str(out_path))
    return str(out_path)
