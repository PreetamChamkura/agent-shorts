"""Stage 4 — render.

Reads: work/{video_id}/source.mp4, work/{video_id}/words.json,
       work/{video_id}/vo_{n}.wav, work/{video_id}/vo_{n}.words.json
       (VO files optional — absent means no VO / no VO captions)
Writes: work/{video_id}/caps_{n}.ass, work/{video_id}/out_{n}.mp4

Four compositions: `tight_crop` (baseline — blur-fill with the subject scaled
up and center-cropped to fill more vertical space), `blur_fill` (no crop, more
dead space), `split_screen` (source panel + gameplay panel per CLAUDE.md —
available but not tone-wired yet; picking it for "low-motion segments" needs a
motion signal this pipeline doesn't compute, so it stays a manual --composition
choice until that lands), and `facecam_split` (source's own facecam panel +
main-content panel — Twitch-clip composition fix, CLAUDE.md step). Unlike
split_screen, facecam_split IS auto-selected: leaving `composition` unset runs
clipper.face_detect against the clip's own footage and uses facecam_split only
when a small corner facecam is actually found there, falling back to
tight_crop otherwise — see render()'s docstring. Caption style is a lookup on
`tone` via clipper.styles.style_for_tone — CLAUDE.md's Tone routing table,
never random, never a silent default for an unknown tone.

VO mixing (CLAUDE.md "Voiceover mixing"): two overlapping voices don't work at
any relative volume, so VO and source speech are never audible at the same
time — but the *video* never freezes for it, a static frame is dead time on
Shorts/Reels. Both placements are the same audio shape: the source cuts to
full silence (not ducked) for the VO's window while the video keeps playing
normally, VO plays at full volume, then resumes full source audio after. Only
where the window sits differs — `cold_open` at t=0 (the clip's own opening
seconds), `pre_punchline` 1.2s before the punchline. `cold_open` additionally
captions the VO's own line during its window with the same karaoke engine as
the rest of the clip (real per-word timing from a whisper transcription of
the VO audio, not an invented one — see narrate.py), since the viewer should
be able to read what the narrator is saying, not just hear it; `pre_punchline`
doesn't, it's a short interjection. Passing neither `punchline_at` nor
`vo_placement` means no VO — the normal path when vo_script was null or TTS
failed.

`caption_only` (CLAUDE.md "Narration modes") is the third mode and the only
one that touches no audio at all: `on_screen_caption` is held statically in
its own band of the caption file while the source plays untouched, for a clip
whose own sound IS the payoff and would be destroyed by silencing a window of
it. It shares the ASS file and the tone lookup with the karaoke captions but
not their band — clipper.ass.held_caption_style derives a non-colliding
placement from the tone's own karaoke alignment. Unlike a cold_open VO, it
does NOT suppress the clip's own captions: nothing is muted, so captioning
the audible dialogue stays correct, and the two blocks are on screen together
by design.

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

from .ass import HeldCaption, Word, build_ass, held_caption_duration
from .face_detect import find_face_region, is_small_corner_facecam
from .ffmpeg import probe_duration, run_ffmpeg, run_ffprobe
from .frames import mean_abs_diff, read_pgm_gray
from .paths import CAPTION_FONT, GAMEPLAY_DIR, WATERMARK_FONT, work_dir
from .styles import style_for_tone

log = logging.getLogger("clipper.render")

# Duration hard-fail bounds (CLAUDE.md: "18-58s inclusive. Hard fail outside.").
MIN_DURATION = 18.0
MAX_DURATION = 58.0

WATERMARK_HANDLE = "@FrameByFrameMoments"
WATERMARK_Y = 1400  # below the caption band, still inside the y 250-1520 safe zone

# Tight-crop scale-up factor: 1080 * 1.6 = 1728 wide, cropped back to 1080.
TIGHT_CROP_WIDTH = 1728

# Loop-hold frame stability search. A small grayscale thumbnail is plenty for
# a "did anything move" check — this deliberately doesn't need to be fancy.
LOOP_SEARCH_FRAMES = 15  # ~0.5s at 30fps
LOOP_FRAME_SIZE = 64
LOOP_MOTION_THRESHOLD = 6.0  # mean abs pixel diff (0-255); untuned heuristic
# — no real loopable:true segment has been produced yet to calibrate against.

# facecam_split's facecam panel: how much of the source frame (as a fraction
# of width/height) to crop from the detected corner, not just the tight Haar
# face box. The real overlay (background, shoulders, headset) runs well past
# the face alone — confirmed against real thumbnails, where the Doc clip's
# visible facecam box spans ~35% of frame width/height against a Haar box
# covering only ~9%. Untuned beyond those two real reference images; likely
# needs revisiting once more real facecam layouts have been seen.
FACECAM_PANEL_FRACTION = 0.35

# split_screen/facecam_split panel geometry — named so other stage-4-adjacent
# code (clipper.thumbnail, keeping text off the facecam panel) can reference
# the real overlay positions instead of duplicating these as magic numbers.
PANEL_HEIGHT = 864
MAIN_PANEL_Y = 250
FACECAM_PANEL_Y = 1056  # also the bottom (gameplay) panel's y in split_screen

COMPOSITIONS = ("blur_fill", "tight_crop", "split_screen", "facecam_split")


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


_ASS_EVENTS_FORMAT_LINE = "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"


def _merge_vo_and_clip_captions(
    vo_words: list[Word],
    clip_words: list[Word],
    vo_duration: float,
    group_size: int,
    style: dict[str, object],
) -> str:
    """Caption a cold_open VO window with the karaoke engine, then resume the
    clip's own captions once it ends — one ASS file, built from two separate
    build_ass() calls whose Dialogue lines are concatenated. Reuses the
    existing caption engine as-is rather than adding a phrase-grouping
    special case: calling it twice and stitching the [Events] sections
    guarantees a group never straddles the VO/clip boundary, which relying on
    the VO's last word happening to end in sentence punctuation would not.

    Clip words starting before ``vo_duration`` are dropped — that dialogue is
    silenced during the VO window, so captioning it would show text for audio
    the viewer can't hear.
    """
    resumed_clip_words = [w for w in clip_words if w["start"] >= vo_duration]
    vo_ass = build_ass(vo_words, group_size, style)
    clip_ass = build_ass(resumed_clip_words, group_size, style)

    header, _, vo_body = vo_ass.partition(_ASS_EVENTS_FORMAT_LINE)
    _, _, clip_body = clip_ass.partition(_ASS_EVENTS_FORMAT_LINE)

    combined_body = (vo_body.rstrip("\n") + "\n" + clip_body.rstrip("\n")).strip("\n")
    if not combined_body:
        return header + _ASS_EVENTS_FORMAT_LINE
    return f"{header}{_ASS_EVENTS_FORMAT_LINE}{combined_body}\n"


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
    are assembled separately by render()."""
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
        f"[1:v]scale=1080:{PANEL_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop=1080:{PANEL_HEIGHT},setsar=1[game];"
        f"[base][src]overlay=0:{MAIN_PANEL_Y}[t1];"
        f"[t1][game]overlay=0:{FACECAM_PANEL_Y}[comp]"
    )


def _facecam_panel_crop_box(face_region: dict) -> tuple[int, int, int, int]:
    """(crop_w, crop_h, x, y): the facecam panel's source-pixel crop box —
    the exact region _facecam_split_video() crops for its bottom panel.
    Factored out so other stage-4-adjacent code (clipper.thumbnail, picking
    a thumbnail frame that shows the facecam's own expression) can target
    the same region without duplicating this math or coupling to the full
    filter-graph string.
    """
    fw, fh = face_region["frame_width"], face_region["frame_height"]
    cx = (face_region["x"] + face_region["w"] / 2) / fw
    cy = (face_region["y"] + face_region["h"] / 2) / fh

    crop_w = round(fw * FACECAM_PANEL_FRACTION)
    crop_h = round(fh * FACECAM_PANEL_FRACTION)
    x = 0 if cx < 0.5 else fw - crop_w
    y = 0 if cy < 0.5 else fh - crop_h
    return crop_w, crop_h, x, y


def _facecam_split_video(face_region: dict) -> str:
    """Twitch-clip facecam layout (CLAUDE.md step: composition fix): the
    SAME source split into a main-content panel and a facecam panel, instead
    of split_screen's source+separate-gameplay-file. Video-only chain,
    ending in [comp] — see _blur_fill_video.

    Panel position is fixed regardless of which corner the source's own
    facecam happens to sit in — main content always on top (y=250, the
    "primary subject" slot per the existing split_screen convention — this
    clip's actual point is the gameplay moment, the facecam is reaction/
    context), facecam always on bottom (y=1056). Consistency across every
    facecam-style upload matters more here than mirroring each source's own
    layout — the whole reason split_screen (fixed geometry) was chosen over
    a dynamic pan.

    face_region is find_face_region()'s real-pixel-coordinate result;
    is_small_corner_facecam(face_region) must already be True — this
    function doesn't re-check that itself, render() does before calling it.
    """
    fw = face_region["frame_width"]
    cx = (face_region["x"] + face_region["w"] / 2) / fw
    crop_w, crop_h, facecam_x, facecam_y = _facecam_panel_crop_box(face_region)

    # Main-content panel: after scale-to-cover, crop from whichever
    # horizontal side is farthest from the facecam's corner, so the
    # overlay doesn't visibly repeat in both panels. Only horizontal
    # offset is adjustable — scale-to-cover a 16:9 source into an
    # (approximately) 1080x864 panel matches height exactly and overflows
    # only in width, so there's no vertical crop slack to shift either way.
    main_crop_x = "0" if cx >= 0.5 else "in_w-out_w"

    return (
        "color=c=black:s=1080x1920:r=30[base];"
        "[0:v]split=2[main_src][face_src];"
        f"[main_src]scale=1080:{PANEL_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop=1080:{PANEL_HEIGHT}:x={main_crop_x}:y=0,setsar=1[main];"
        f"[face_src]crop={crop_w}:{crop_h}:{facecam_x}:{facecam_y},"
        f"scale=1080:{PANEL_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop=1080:{PANEL_HEIGHT},setsar=1[face];"
        f"[base][main]overlay=0:{MAIN_PANEL_Y}[t1];"
        f"[t1][face]overlay=0:{FACECAM_PANEL_Y}[comp]"
    )


def _pre_punchline_start_seconds(clip_start: float, punchline_at: float) -> float:
    """Clip-relative VO start second for pre_punchline: 1.2s before
    punchline_at, clamped to 0 so a punchline near the very start of the clip
    can't ask for a negative start time.
    """
    return max(0.0, (punchline_at - clip_start) - 1.2)


def _audio_tail(vo: dict | None) -> str:
    """Shared audio tail for every case: no VO, cold_open, and pre_punchline
    are all the same shape now.

    No VO: plain loudnorm. Otherwise: the source cuts to full silence (not
    ducked — two overlapping voices don't work at any relative volume) for
    the VO's window, VO plays at full volume, then amix, then loudnorm on the
    *mixed* result — never before mixing (CLAUDE.md). cold_open no longer
    prepends a pre-roll (its window is just [0, vo_duration) — the clip's own
    opening seconds), so it needs nothing different from pre_punchline here;
    only where render() places the window differs.

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
    return [read_pgm_gray(f) for f in frames]


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
            diff = mean_abs_diff(frames[idx], frames[idx - 1])
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
    duration = probe_duration(str(chosen))
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
    composition: str | None = None,
    punchline_at: float | None = None,
    vo_placement: str | None = None,
    on_screen_caption: str | None = None,
    loopable: bool = False,
    force: bool = False,
) -> str:
    """Render clip ``n`` of ``video_id`` between ``start`` and ``end`` seconds.

    ``tone`` is a required lookup into styles.yaml (clipper.styles) — no
    default, since a missing tone must fail loudly rather than silently
    reusing whatever style happened to run last. ``composition`` selects the
    filter graph: tight_crop (baseline), blur_fill, split_screen, or
    facecam_split. Leaving it unset (None, the default) auto-detects: runs
    the visual gate's layer 2 (clipper.face_detect) against the source's own
    [start, end] window, and uses facecam_split only when a small corner
    facecam is actually found there — a podcast-style source with no
    facecam (or nothing consistent enough to trust) falls back to
    tight_crop, unchanged from before this existed. Passing an explicit
    composition name always skips detection and honors that choice exactly
    as it always has.

    ``punchline_at`` and ``vo_placement`` opt into VO mixing and are only
    honored when work/{video_id}/vo_{n}.wav also exists — pass neither for the
    normal no-VO path (vo_script was null, or stage 3.5 flagged tts_failed).

    ``on_screen_caption`` is the other narration mode (`caption_only`): one
    held line of framing burned into the caption file, no audio touched at
    all. Mutually exclusive with VO by construction — passing both raises,
    rather than silently rendering a viewer a spoken sentence and a different
    written one at the same time.

    ``loopable`` re-snaps `end` to a word boundary and to a stable (non-blink,
    non-motion-blurred) frame before encoding — see module docstring. False
    (the default) keeps current behavior exactly; pass segments.json's
    `loopable` flag through unchanged.

    Returns the path to out_{n}.mp4.
    """
    if composition is not None and composition not in COMPOSITIONS:
        raise ValueError(
            f"render: unknown composition {composition!r}, expected one of "
            f"{COMPOSITIONS} or None (auto-detect)"
        )

    if on_screen_caption and vo_placement is not None:
        raise ValueError(
            "render: on_screen_caption and vo_placement are mutually exclusive narration "
            f"modes (got caption {on_screen_caption!r} with placement {vo_placement!r}). "
            "Stage 3 nulls whichever one doesn't match the chosen mode — a segment "
            "carrying both means it was hand-assembled or written by something else."
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

    # Detection only runs when actually needed for this render: skipped
    # entirely above on a cache hit, and below whenever an explicit
    # composition other than facecam_split was given.
    face_region = None
    if composition is None or composition == "facecam_split":
        face_region = find_face_region(str(source), start, end)
        has_facecam = bool(face_region) and is_small_corner_facecam(face_region)

        if composition is None:
            composition = "facecam_split" if has_facecam else "tight_crop"
            if face_region and not has_facecam:
                # A face WAS found but failed the corner-proximity check —
                # the known CORNER_MARGIN_FRACTION limitation (CLAUDE.md
                # Known issues), distinct from the ordinary "no face at all"
                # case below. Logged explicitly so real frequency is
                # visible over time, not just theoretical.
                log.info(
                    "render: n=%d face detected at x=%.0f y=%.0f w=%.0f h=%.0f but "
                    "failed the corner-proximity check — falling back to tight_crop "
                    "(known limitation, see CLAUDE.md Known issues)",
                    n,
                    face_region["x"],
                    face_region["y"],
                    face_region["w"],
                    face_region["h"],
                )
            log.info("render: n=%d composition auto-detected -> %s", n, composition)
        elif not has_facecam:
            raise ValueError(
                f"render: n={n} composition='facecam_split' was requested explicitly, "
                "but no small corner facecam was detected in this clip's own footage "
                "— pass a different composition, or leave composition unset to auto-detect."
            )

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
    # which is the normal, expected no-VO path. Both placements are the same
    # shape now: a silence-and-mix window on the source's own timeline. Only
    # *where* the window sits differs — cold_open at t=0 (the clip's own
    # opening), pre_punchline 1.2s before the punchline.
    vo_path = wd / f"vo_{n}.wav"
    vo = None
    if punchline_at is not None and vo_placement is not None and vo_path.exists():
        vo_input_index = len(input_args) // 4  # each existing input is a "-ss value -i path" run
        input_args += ["-i", str(vo_path)]
        vo_duration = probe_duration(str(vo_path))
        vo_start = (
            0.0
            if vo_placement == "cold_open"
            else _pre_punchline_start_seconds(start, punchline_at)
        )
        vo = {
            "input_index": vo_input_index,
            "placement": vo_placement,
            "start": vo_start,
            "end": vo_start + vo_duration,
        }
        log.info(
            "render: %s VO n=%d, silence window %.2fs-%.2fs",
            vo_placement,
            n,
            vo_start,
            vo_start + vo_duration,
        )

    clip_words = _clip_words(words, start, end)

    # caption_only: one held line of framing, clipped to the segment so a
    # short clip can never carry a hold that outlives it. Nothing is silenced
    # — that's the whole reason this mode exists, so the clip's own captions
    # keep running underneath it in their own band (clipper.ass).
    held: HeldCaption | None = None
    if on_screen_caption:
        held_end = min(held_caption_duration(on_screen_caption), duration)
        held = {"text": on_screen_caption, "start": 0.0, "end": held_end}
        log.info(
            "render: caption_only n=%d, held 0.00s-%.2fs: %r",
            n,
            held_end,
            on_screen_caption,
        )

    # A cold_open VO plays over the clip's own opening seconds (muted, video
    # keeps moving — CLAUDE.md) rather than a prepended pre-roll, so it gets
    # captioned too: the viewer should be able to read the line, not just
    # hear it. Reuses build_ass twice (via _merge_vo_and_clip_captions) —
    # existing infrastructure, not a new mechanism. Falls back to the plain
    # clip captions if vo_{n}.words.json isn't there (e.g. a line synthesized
    # before this existed and not yet backfilled by narrate()).
    vo_words_path = wd / f"vo_{n}.words.json"
    if vo and vo["placement"] == "cold_open" and vo_words_path.exists():
        vo_words = json.loads(vo_words_path.read_text())["words"]
        ass_text = _merge_vo_and_clip_captions(
            vo_words, clip_words, vo["end"], group_size, style_overrides
        )
        log.info(
            "render: wrote %s (%d VO + %d clip caption words, tone=%s, group_size=%d)",
            ass_path,
            len(vo_words),
            len(clip_words),
            tone,
            group_size,
        )
    else:
        if vo and vo["placement"] == "cold_open":
            log.info("render: n=%d cold_open with no vo_%d.words.json — captioning clip only", n, n)
        ass_text = build_ass(clip_words, group_size, style_overrides, held)
        log.info(
            "render: wrote %s (%d caption words, tone=%s, group_size=%d)",
            ass_path,
            len(clip_words),
            tone,
            group_size,
        )
    ass_path.write_text(ass_text)

    if composition == "blur_fill":
        video_fc = _blur_fill_video()
    elif composition == "tight_crop":
        video_fc = _tight_crop_video()
    elif composition == "split_screen":
        video_fc = _split_screen_video()
    else:  # facecam_split — face_region is guaranteed set, see above
        video_fc = _facecam_split_video(face_region)

    audio_fc = _audio_tail(vo)
    tail = _caption_and_watermark_tail("comp", str(ass_path), fonts_dir, wm_font)
    fc = f"{video_fc};{tail};{audio_fc}"

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

    try:
        _validate_output(str(out_path))
    except Exception:
        # The cache check above trusts "out_path exists" to mean "valid".
        # ffmpeg already wrote the file by this point even though it fails
        # validation — leaving it in place would make the *next* run (with
        # or without --force) silently serve this same invalid file back.
        out_path.unlink(missing_ok=True)
        raise
    return str(out_path)
