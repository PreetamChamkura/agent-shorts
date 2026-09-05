"""Stage 4 — render.

Reads: work/{video_id}/source.mp4, work/{video_id}/words.json,
       work/{video_id}/vo_{n}.wav (optional — absent means no VO)
Writes: work/{video_id}/caps_{n}.ass, work/{video_id}/out_{n}.mp4

Three compositions: `tight_crop` (baseline — blur-fill with the subject scaled
up and center-cropped to fill more vertical space), `blur_fill` (no crop, more
dead space), and `split_screen` (source panel + gameplay panel per CLAUDE.md —
available but not tone-wired yet; picking it for "low-motion segments" needs a
motion signal this pipeline doesn't compute, so it stays a manual --composition
choice until that lands). Caption style is a lookup on `tone` via
clipper.styles.style_for_tone — CLAUDE.md's Tone routing table, never random,
never a silent default for an unknown tone.

VO mixing (CLAUDE.md "Voiceover mixing"): two overlapping voices don't work at
any relative volume, so VO and source speech are never audible at the same
time — behavior splits by `vo_placement`. `cold_open` gets an actual pre-roll:
a freeze on the clip's first frame (dimmed + blurred) for the VO's duration,
source silent, then a cut to live clip + full audio — the VO plays *before*
the clip, not under it, which also lengthens the final render by the VO's
duration. `pre_punchline` hard-cuts the source to silence for the VO's window
(not ducked) and resumes full volume immediately after. Passing neither
`punchline_at` nor `vo_placement` means no VO — the normal path when
vo_script was null or TTS failed.

Loop hold (CLAUDE.md: a `loopable` segment must end cleanly on the last frame
so a platform's auto-repeat reads as seamless): when the caller passes
``loopable=True``, `end` is re-snapped to a word boundary (mirrors
select.py's forward bias — duplicated rather than imported, to keep stage
modules independent per CLAUDE.md's stage contract) and the exact frame at
that timestamp is checked for stability against its neighbor; an unstable
(mid-blink/motion-blurred) frame steps backward a few frames at a time until
a stable one is found. Non-loopable segments are untouched by any of this.

Every ffmpeg/ffprobe call goes through run_ffmpeg/run_ffprobe. Re-running
never touches stage 1, 2, or 3.5.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path

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

# Loop-hold frame stability search. A small grayscale thumbnail is plenty for
# a "did anything move" check — this deliberately doesn't need to be fancy.
LOOP_SEARCH_FRAMES = 15  # ~0.5s at 30fps
LOOP_FRAME_SIZE = 64
LOOP_MOTION_THRESHOLD = 6.0  # mean abs pixel diff (0-255); untuned heuristic
# — no real loopable:true segment has been produced yet to calibrate against.

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


# None of the three compositions below apply a video fade in/out. That's
# deliberate, not an oversight: a segment flagged `loopable` in segments.json
# depends on ending cleanly on the last frame for a platform's auto-repeat to
# read as a seamless loop (CLAUDE.md). If a fade-out is ever added here, gate
# it on `not loopable`.


def _blur_fill_video() -> str:
    """Video-only chain, ending in [comp]. Caption/watermark tail and audio
    are assembled separately by render() so a cold_open pre-roll can splice
    in between this and the tail."""
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=28[bgb];"
        "[fg]scale=1080:-2[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp]"
    )


def _tight_crop_video() -> str:
    """Blur-fill, but scale the foreground ~1.6x and center-crop the sides so
    the subject fills more vertical space before the blurred remainder shows.
    Video-only chain, ending in [comp] — see _blur_fill_video.
    """
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=28[bgb];"
        f"[fg]scale={TIGHT_CROP_WIDTH}:-2,crop=1080:ih[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp]"
    )


def _split_screen_video() -> str:
    """Source panel + gameplay panel per CLAUDE.md. Gameplay audio discarded.
    Video-only chain, ending in [comp] — see _blur_fill_video.

    The gameplay chain scales to *cover* the 1080x864 panel and then
    center-crops, so footage of any aspect fills the panel exactly: a plain
    `scale=-2:864` only reaches 1080 wide when the source is at least 5:4, and
    anything narrower (16:9, or a vertical 9:16 meme) would come up short and
    leave the panel unfilled. `setsar=1` keeps non-square-pixel sources from
    rendering stretched.
    """
    return (
        "color=c=black:s=1080x1920:r=30[base];"
        "[0:v]scale=1080:-2[src];"
        "[1:v]scale=1080:864:force_original_aspect_ratio=increase,"
        "crop=1080:864,setsar=1[game];"
        "[base][src]overlay=0:250[t1];"
        "[t1][game]overlay=0:1056[comp]"
    )


def _freeze_preroll_video(vo_duration: float) -> str:
    """Freeze [comp]'s first frame (dimmed + blurred) for vo_duration seconds,
    then cut to live [comp] footage. Produces [precomp].

    [comp] is consumed twice below (once frozen, once live) — that needs an
    explicit `split`; reusing a filter label as input to two different
    filters isn't valid fan-out on its own and silently produces a bogus
    default-parameter stream for one consumer instead of an error.

    cold_open VO plays *before* the clip starts, not under it — two
    overlapping voices don't work at any relative volume (CLAUDE.md).
    """
    return (
        "[comp]split=2[compA][compB];"
        f"[compA]loop=loop=-1:size=1:start=0,trim=duration={vo_duration:.3f},"
        "setpts=PTS-STARTPTS,eq=brightness=-0.2,gblur=sigma=12[frozen];"
        "[frozen][compB]concat=n=2:v=1:a=0[precomp]"
    )


def _pre_punchline_start_seconds(clip_start: float, punchline_at: float) -> float:
    """Clip-relative VO start second for pre_punchline: 1.2s before
    punchline_at, clamped to 0 so a punchline near the very start of the clip
    can't ask for a negative start time. cold_open has no equivalent — it
    prepends a pre-roll instead of overlaying a point in the clip's timeline.
    """
    return max(0.0, (punchline_at - clip_start) - 1.2)


def _audio_tail(vo: dict | None) -> str:
    """Shared audio tail for the no-VO and pre_punchline cases.

    No VO: plain loudnorm. pre_punchline: the source cuts to full silence
    (not ducked — two overlapping voices don't work at any relative volume)
    for the VO's window, VO plays at full volume, then amix, then loudnorm on
    the *mixed* result — never before mixing (CLAUDE.md). cold_open doesn't
    use this function; see _cold_open_audio_tail + _freeze_preroll_video,
    since there the VO precedes the clip rather than overlapping it.

    ``vo``, when present, is {"input_index": int, "start": float, "end": float}
    — the ffmpeg input index of vo_{n}.wav and its clip-relative time window.
    """
    if vo is None:
        return "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    idx = vo["input_index"]
    vs, ve = vo["start"], vo["end"]
    delay_ms = int(round(vs * 1000))
    return (
        f"[{idx}:a]adelay={delay_ms}|{delay_ms}[vodelayed];"
        f"[0:a]volume=0:enable='between(t,{vs},{ve})'[silenced];"
        "[silenced][vodelayed]amix=inputs=2:duration=first:dropout_transition=0,"
        "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )


def _cold_open_audio_tail(vo_input_index: int) -> str:
    """VO plays to completion in silence, then the clip's own audio at full
    volume — a sequential concat, not a mix, since cold_open VO precedes the
    clip rather than overlapping it. aformat normalizes both streams to the
    same rate/layout first since concat (unlike amix) requires matching
    formats.
    """
    return (
        f"[{vo_input_index}:a]aformat=sample_rates=48000:channel_layouts=stereo[voa];"
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[srca];"
        "[voa][srca]concat=n=2:v=0:a=1,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    )


def _shift_words(words: list[Word], offset: float) -> list[Word]:
    """Shift every word's start/end by ``offset`` seconds.

    Used when a cold_open pre-roll prepends ``offset`` seconds of frozen
    frame ahead of the clip — captions must land on the real clip content
    once it starts, not during the pre-roll.
    """
    if offset == 0.0:
        return words
    return [
        {"word": w["word"], "start": w["start"] + offset, "end": w["end"] + offset} for w in words
    ]


def _snap_loop_end_forward(words: list[dict], raw_end: float) -> float:
    """Forward-biased word-boundary snap, for `loopable` segments only.

    Deliberately mirrors clipper.select._snap_end_forward rather than
    importing it — CLAUDE.md's stage contract keeps stage modules
    independent, and this is small enough that duplicating it costs less
    than coupling render() to select()'s internals. select.py should
    already have snapped `end` correctly by the time it reaches here; this
    is defense-in-depth specifically for the loop case, where landing
    mid-word matters even more than usual.
    """
    containing = [w for w in words if float(w["start"]) <= raw_end < float(w["end"])]
    if containing:
        return max(float(w["end"]) for w in containing)
    at_or_before = [float(w["end"]) for w in words if float(w["end"]) <= raw_end]
    if at_or_before:
        return max(at_or_before)
    return raw_end


def _read_pgm_gray(path: Path) -> bytes:
    """Parse a binary grayscale PGM (P5) into raw pixel bytes. No imaging
    library needed — PGM is a trivial fixed format, unlike PNG/JPEG."""
    data = path.read_bytes()
    if not data.startswith(b"P5"):
        raise ValueError(f"not a binary PGM: {path}")
    tokens: list[bytes] = []
    i = 2
    while len(tokens) < 3:
        while data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            while data[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not data[j : j + 1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    width, height, _maxval = (int(t) for t in tokens)
    i += 1  # the single whitespace byte separating the header from pixel data
    return data[i : i + width * height]


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-pixel difference between two equal-length grayscale
    frames — a coarse but adequate "how much moved" signal."""
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _extract_loop_candidate_frames(source: str, clip_end_abs: float, tmp_dir: Path) -> list[bytes]:
    """LOOP_SEARCH_FRAMES+1 small grayscale frames ending at ``clip_end_abs``
    (absolute seconds into ``source``), oldest first. The extra leading frame
    gives every real candidate a prior neighbor to diff against.
    """
    lead = (LOOP_SEARCH_FRAMES + 1) / 30.0
    window_start = max(0.0, clip_end_abs - lead)
    pattern = tmp_dir / "f_%03d.pgm"
    run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{window_start:.3f}",
            "-i",
            source,
            "-t",
            f"{lead:.3f}",
            "-vf",
            f"scale={LOOP_FRAME_SIZE}:{LOOP_FRAME_SIZE},format=gray",
            "-vsync",
            "0",
            str(pattern),
        ]
    )
    frames = sorted(tmp_dir.glob("f_*.pgm"))
    return [_read_pgm_gray(f) for f in frames]


def pick_loop_freeze_end(source: str, clip_end_abs: float) -> float:
    """For a loopable segment, confirm the frame at ``clip_end_abs`` isn't
    mid-blink/motion-blurred (a stable, low frame-to-frame difference); if it
    is, step backward frame-by-frame — up to LOOP_SEARCH_FRAMES — until one
    passes, rather than forcing the exact snapped timestamp. Returns the
    (possibly earlier) absolute timestamp to actually end on.
    """
    with tempfile.TemporaryDirectory(prefix="clipper_loop_") as td:
        frames = _extract_loop_candidate_frames(source, clip_end_abs, Path(td))
        if len(frames) < 2:
            log.warning(
                "render: only %d frame(s) available near loop end %.3fs — using it as-is",
                len(frames),
                clip_end_abs,
            )
            return clip_end_abs

        for offset in range(len(frames) - 1):
            idx = len(frames) - 1 - offset
            diff = _mean_abs_diff(frames[idx], frames[idx - 1])
            if diff <= LOOP_MOTION_THRESHOLD:
                adjusted = round(clip_end_abs - offset / 30.0, 3)
                if offset:
                    log.info(
                        "render: loop end frame at %.3fs was unstable (diff=%.1f), "
                        "using %.3fs instead (%d frame(s) earlier)",
                        clip_end_abs,
                        diff,
                        adjusted,
                        offset,
                    )
                return adjusted

        log.warning(
            "render: no frame within %d frames of loop end %.3fs was stable — using it anyway",
            LOOP_SEARCH_FRAMES,
            clip_end_abs,
        )
        return clip_end_abs


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
    punchline_at: float | None = None,
    vo_placement: str | None = None,
    loopable: bool = False,
    force: bool = False,
) -> str:
    """Render clip ``n`` of ``video_id`` between ``start`` and ``end`` seconds.

    ``tone`` is a required lookup into styles.yaml (clipper.styles) — no
    default, since a missing tone must fail loudly rather than silently
    reusing whatever style happened to run last. ``composition`` selects the
    filter graph: tight_crop (default baseline), blur_fill, or split_screen.

    ``punchline_at`` and ``vo_placement`` opt into VO mixing and are only
    honored when work/{video_id}/vo_{n}.wav also exists — pass neither for the
    normal no-VO path (vo_script was null, or stage 3.5 flagged tts_failed).

    ``loopable`` re-snaps `end` to a word boundary and to a stable (non-blink,
    non-motion-blurred) frame before encoding — see module docstring. False
    (the default) keeps current behavior exactly; pass segments.json's
    `loopable` flag through unchanged.

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

    if loopable:
        # Defense-in-depth even though select.py's snap_to_words should
        # already land on a word boundary: re-snap here too, then confirm
        # the exact frame at that timestamp is stable enough to freeze/loop
        # on, stepping back a few frames if not. Non-loopable segments never
        # enter this branch — the "no fade" comment above stays correct for
        # them unchanged.
        snapped_end = _snap_loop_end_forward(words, end)
        if snapped_end != end:
            log.info("render: loopable end %.2f -> %.2f (word-boundary re-snap)", end, snapped_end)
            end = snapped_end
        adjusted_end = pick_loop_freeze_end(str(source), end)
        if adjusted_end != end:
            end = adjusted_end
        duration = end - start
        if not (MIN_DURATION <= duration <= MAX_DURATION):
            raise ValueError(
                f"render: loop-adjusted duration {duration:.1f}s outside "
                f"{MIN_DURATION}-{MAX_DURATION}s (start={start}, end={end})"
            )

    fonts_dir = str(CAPTION_FONT.parent)
    wm_font = str(WATERMARK_FONT)

    input_args = ["-ss", f"{start}", "-i", str(source)]

    if composition == "split_screen":
        gameplay_path, gameplay_offset = _pick_gameplay(video_id, n, duration)
        log.info("render: split_screen gameplay=%s offset=%.2fs", gameplay_path, gameplay_offset)
        input_args += ["-ss", f"{gameplay_offset}", "-i", gameplay_path]

    # VO opts in only when both fields are given AND stage 3.5 actually left a
    # file — vo_script null or a flagged tts_failed means neither is passed,
    # which is the normal, expected no-VO path. Shape differs by placement:
    # cold_open only needs its own duration (it prepends a pre-roll rather
    # than overlaying a point in the clip), pre_punchline needs a window.
    vo_path = wd / f"vo_{n}.wav"
    vo = None
    if punchline_at is not None and vo_placement is not None and vo_path.exists():
        vo_input_index = len(input_args) // 4  # each existing input is a "-ss value -i path" run
        input_args += ["-i", str(vo_path)]
        vo_duration = _probe_duration(str(vo_path))
        if vo_placement == "cold_open":
            vo = {"input_index": vo_input_index, "placement": "cold_open", "duration": vo_duration}
            log.info("render: cold_open VO n=%d, %.2fs pre-roll", n, vo_duration)
        else:
            vo_start = _pre_punchline_start_seconds(start, punchline_at)
            vo = {
                "input_index": vo_input_index,
                "placement": "pre_punchline",
                "start": vo_start,
                "end": vo_start + vo_duration,
            }
            log.info(
                "render: pre_punchline VO n=%d, silence window %.2fs-%.2fs",
                n,
                vo_start,
                vo_start + vo_duration,
            )

    # cold_open prepends vo["duration"] seconds of frozen pre-roll ahead of
    # the clip, so captions (timed relative to the clip) must shift by the
    # same amount to still land on the real footage once it starts.
    caption_offset = vo["duration"] if vo and vo["placement"] == "cold_open" else 0.0
    clip_words = _shift_words(_clip_words(words, start, end), caption_offset)
    ass_text = build_ass(clip_words, group_size, style_overrides)
    ass_path.write_text(ass_text)
    log.info(
        "render: wrote %s (%d caption words, tone=%s, group_size=%d)",
        ass_path,
        len(clip_words),
        tone,
        group_size,
    )

    if composition == "blur_fill":
        video_fc = _blur_fill_video()
    elif composition == "tight_crop":
        video_fc = _tight_crop_video()
    else:  # split_screen
        video_fc = _split_screen_video()

    if vo and vo["placement"] == "cold_open":
        video_fc = f"{video_fc};{_freeze_preroll_video(vo['duration'])}"
        comp_label = "precomp"
        audio_fc = _cold_open_audio_tail(vo["input_index"])
        total_duration = duration + vo["duration"]
    else:
        comp_label = "comp"
        audio_fc = _audio_tail(vo)
        total_duration = duration

    tail = _caption_and_watermark_tail(comp_label, str(ass_path), fonts_dir, wm_font)
    fc = f"{video_fc};{tail};{audio_fc}"

    run_ffmpeg(
        [
            "-y",
            *input_args,
            "-t",
            f"{total_duration}",
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
