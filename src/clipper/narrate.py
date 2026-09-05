"""Stage 3.5 — narrate (TTS).

Reads: work/{video_id}/segments.json
Writes: work/{video_id}/vo_{n}.wav, work/{video_id}/vo_{n}.words.json
        (both absent when vo_script is null)

OpenAI TTS via the plain /v1/audio/speech REST endpoint. One voice,
permanently (CLAUDE.md narrator persona) — NARRATOR_VOICE/NARRATOR_MODEL are
the only place that changes.

Caching is two-tier and `force` only affects the outer one:
  - content cache: work/_tts_cache/{sha256(vo_script)}.wav (+ .words.json),
    shared across every video. Consulted before every call and NEVER
    bypassed by --force — CLAUDE.md is explicit that identical text must
    never be paid for twice.
  - per-video cache: vo_{n}.wav / vo_{n}.words.json. --force only re-copies
    from the content cache (or re-synthesizes if that's also missing); it
    does not re-pay for text already cached.

vo_{n}.words.json is a whisper transcription of the synthesized line itself
(clipper.transcribe.transcribe_audio_file — the same wrapper stage 2 uses) —
real per-word timing, not an invented even-split, so render() can caption a
cold_open VO with the existing karaoke engine on its own actual timing.
Backfilled from the content-cache audio for any line that predates this
feature — never by re-calling TTS, only ever from audio already paid for.

If TTS fails, that segment is flagged `tts_failed: true` in segments.json and
left without a vo_{n}.wav — render() then renders without VO rather than
failing the whole job.

cold_open no longer prepends a pre-roll (CLAUDE.md): the source video keeps
playing, just muted, under the VO, so it no longer lengthens the render. The
only way a cold_open VO still can't be used is if it's long enough to consume
the clip's own duration outright; that's flagged `vo_duration_exceeded` and
handled exactly like a TTS failure. Re-checked even on an already-cached
vo_{n}.wav, since the fit depends on the segment's own start/end, not on
whether synthesis happened this run. `pre_punchline` is unaffected either way.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import requests

from .ffmpeg import probe_duration
from .paths import TTS_CACHE_DIR, work_dir
from .transcribe import transcribe_audio_file

log = logging.getLogger("clipper.narrate")

TTS_URL = "https://api.openai.com/v1/audio/speech"
NARRATOR_MODEL = "tts-1-hd"
NARRATOR_VOICE = "echo"  # picked by ear from an onyx/echo/fable audition — never randomize
RESPONSE_FORMAT = "wav"
TIMEOUT = 60.0

# A cold_open VO overlays the clip's own opening seconds rather than
# prepending time now, so it must leave at least this much of the clip's own
# content after it ends — a VO that eats the whole clip isn't a cold open,
# it's the entire video.
MIN_CLIP_AFTER_VO = 3.0


def script_hash(vo_script: str) -> str:
    return hashlib.sha256(vo_script.encode("utf-8")).hexdigest()


def _ensure_words_cache(cache_audio_path: Path, cache_words_path: Path) -> None:
    """Backfill the content-cache's word-timing file for one VO line, if
    missing. Runs at most once per unique line, ever — cached like the audio,
    and only ever called when the audio is already sitting in the content
    cache, so this never triggers a fresh TTS call.
    """
    if cache_words_path.exists():
        return
    words = transcribe_audio_file(str(cache_audio_path))
    cache_words_path.write_text(json.dumps(words, indent=2))
    log.info("narrate: transcribed VO line (%d words) -> %s", len(words["words"]), cache_words_path)


def _check_duration_fit(seg: dict, vo_path: Path) -> bool:
    """For a cold_open VO, confirm it fits inside the clip's own duration.

    cold_open plays over the clip's own opening seconds now (muted, video
    keeps moving) rather than prepending a pre-roll, so the only way this
    fails is a VO long enough to consume the clip outright. If it doesn't
    fit, delete vo_path (never the content cache — the line itself is fine,
    just not usable as this segment's cold open) and flag the segment so
    render() proceeds without VO. Returns True if it changed the segment dict.

    pre_punchline never has this constraint — it's a short interjection, not
    a window at the start of the clip.
    """
    if seg.get("vo_placement") != "cold_open":
        return seg.pop("vo_duration_exceeded", None) is not None

    vo_duration = probe_duration(str(vo_path))
    clip_duration = float(seg["end"]) - float(seg["start"])

    if vo_duration <= clip_duration - MIN_CLIP_AFTER_VO:
        return seg.pop("vo_duration_exceeded", None) is not None

    log.warning(
        "narrate: n=%d cold_open VO (%.2fs) leaves under %.0fs of the %.2fs "
        "clip after it — dropping VO for this render, not the cached line",
        seg["n"],
        vo_duration,
        MIN_CLIP_AFTER_VO,
        clip_duration,
    )
    vo_path.unlink(missing_ok=True)
    seg["vo_duration_exceeded"] = True
    return True


def _synthesize(vo_script: str, *, api_key: str, voice: str, model: str) -> bytes:
    """The only networked call in this module. Returns raw wav bytes."""
    resp = requests.post(
        TTS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "voice": voice,
            "input": vo_script,
            "response_format": RESPONSE_FORMAT,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content


def narrate(
    video_id: str,
    *,
    force: bool = False,
    voice: str = NARRATOR_VOICE,
    model: str = NARRATOR_MODEL,
) -> dict:
    """Synthesize VO for every non-null vo_script in segments.json.

    Returns the (possibly updated) segments dict. A TTS failure on one segment
    never aborts the others or the run — see module docstring.
    """
    wd = work_dir(video_id)
    segments_path = wd / "segments.json"
    if not segments_path.exists():
        raise FileNotFoundError(f"narrate: {segments_path} missing — run stage 3 (select) first.")

    data = json.loads(segments_path.read_text())
    segments = data.get("segments", [])
    changed = False

    for seg in segments:
        n = seg["n"]
        vo_script = seg.get("vo_script")
        vo_path = wd / f"vo_{n}.wav"
        vo_words_path = wd / f"vo_{n}.words.json"

        if vo_script is None:
            continue  # normal path, not an error — CLAUDE.md

        if seg.get("vo_duration_exceeded"):
            # Already determined this cold_open VO can't fit the clip's own
            # duration (see _check_duration_fit) — the segment's own
            # start/end haven't changed since, so there's nothing new to
            # re-derive. Not affected by --force: that flag re-copies from
            # the content cache, it doesn't change whether the VO fits.
            log.info("narrate: n=%d previously exceeded duration cap, skipping", n)
            continue

        digest = script_hash(vo_script)
        cache_path = TTS_CACHE_DIR / f"{digest}.wav"
        cache_words_path = TTS_CACHE_DIR / f"{digest}.words.json"

        if vo_path.exists() and not force:
            log.info("narrate: cache hit for n=%d (%s), skipping", n, vo_path)
            # Backfill words.json for a line synthesized before this feature
            # existed — only from audio already in the content cache, never
            # by calling TTS again.
            if not vo_words_path.exists() and cache_path.exists():
                _ensure_words_cache(cache_path, cache_words_path)
                vo_words_path.write_text(cache_words_path.read_text())
            if _check_duration_fit(seg, vo_path):
                changed = True
            continue

        if not cache_path.exists():
            api_key = os.environ.get("TTS_API_KEY")
            if not api_key:
                raise RuntimeError("narrate: TTS_API_KEY not set (see .env.example)")
            try:
                audio = _synthesize(vo_script, api_key=api_key, voice=voice, model=model)
            except requests.RequestException as exc:
                log.warning("narrate: TTS failed for n=%d: %s — rendering without VO", n, exc)
                seg["tts_failed"] = True
                changed = True
                continue
            TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(audio)
            log.info("narrate: synthesized n=%d (%d bytes, sha256=%s)", n, len(audio), digest[:12])
        else:
            log.info("narrate: content-cache hit for n=%d (sha256=%s), no TTS call", n, digest[:12])

        _ensure_words_cache(cache_path, cache_words_path)

        vo_path.write_bytes(cache_path.read_bytes())
        vo_words_path.write_text(cache_words_path.read_text())
        seg.pop("tts_failed", None)
        changed = True
        _check_duration_fit(seg, vo_path)

    if changed:
        segments_path.write_text(json.dumps(data, indent=2))

    return data
