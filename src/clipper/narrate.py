"""Stage 3.5 — narrate (TTS).

Reads: work/{video_id}/segments.json
Writes: work/{video_id}/vo_{n}.wav (absent when vo_script is null)

OpenAI TTS via the plain /v1/audio/speech REST endpoint. One voice,
permanently (CLAUDE.md narrator persona) — NARRATOR_VOICE/NARRATOR_MODEL are
the only place that changes.

Caching is two-tier and `force` only affects the outer one:
  - content cache: work/_tts_cache/{sha256(vo_script)}.wav, shared across every
    video. Consulted before every call and NEVER bypassed by --force — CLAUDE.md
    is explicit that identical text must never be paid for twice.
  - per-video cache: vo_{n}.wav. --force only re-copies from the content cache
    (or re-synthesizes if that's also missing); it does not re-pay for text
    already cached.

If TTS fails, that segment is flagged `tts_failed: true` in segments.json and
left without a vo_{n}.wav — render() then renders without VO rather than
failing the whole job.

A `cold_open` VO also gets a duration-fit check: its pre-roll lengthens the
final render by the VO's own length (CLAUDE.md), and CLAUDE.md's 18-58s
output bound is non-negotiable — "hard fail outside", not something a render
should quietly exceed. If clip_duration + vo_duration would blow that ceiling,
the segment is flagged `vo_duration_exceeded: true` and left without a
vo_{n}.wav, exactly like a TTS failure — render() then proceeds without VO
rather than producing (and only then discovering) an out-of-spec file. This
is re-checked even on an already-cached vo_{n}.wav, since the fit depends on
the segment's own start/end, not on whether synthesis happened this run.
`pre_punchline` never lengthens the render, so it has no such constraint.
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

log = logging.getLogger("clipper.narrate")

TTS_URL = "https://api.openai.com/v1/audio/speech"
NARRATOR_MODEL = "tts-1-hd"
NARRATOR_VOICE = "echo"  # picked by ear from an onyx/echo/fable audition — never randomize
RESPONSE_FORMAT = "wav"
TIMEOUT = 60.0

# Mirrors render.py's (and select.py's) MAX_DURATION — duplicated rather than
# imported, per CLAUDE.md's stage-independence contract; see render.py's
# _snap_loop_end_forward for the same tradeoff made the same way.
MAX_DURATION = 58.0


def script_hash(vo_script: str) -> str:
    return hashlib.sha256(vo_script.encode("utf-8")).hexdigest()


def _check_duration_fit(seg: dict, vo_path: Path) -> bool:
    """For a cold_open VO, confirm clip_duration + vo_duration still fits the
    18-58s bound; if not, delete vo_path (never the content cache — the line
    itself is still fine, just not usable as a pre-roll on this segment) and
    flag the segment so render() proceeds without VO. Returns True if it
    changed the segment dict (caller should persist segments.json).

    pre_punchline never changes total duration, so it's never affected here.
    """
    if seg.get("vo_placement") != "cold_open":
        return seg.pop("vo_duration_exceeded", None) is not None

    vo_duration = probe_duration(str(vo_path))
    clip_duration = float(seg["end"]) - float(seg["start"])
    total = clip_duration + vo_duration

    if total <= MAX_DURATION:
        return seg.pop("vo_duration_exceeded", None) is not None

    log.warning(
        "narrate: n=%d cold_open VO (%.2fs) would push the %.2fs clip to %.2fs, "
        "over the %.0fs cap — dropping VO for this render, not the cached line",
        seg["n"],
        vo_duration,
        clip_duration,
        total,
        MAX_DURATION,
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

        if vo_script is None:
            continue  # normal path, not an error — CLAUDE.md

        if seg.get("vo_duration_exceeded"):
            # Already determined this cold_open VO can't fit the 18-58s
            # bound (see _check_duration_fit) — the segment's own start/end
            # haven't changed since, so there's nothing new to re-derive.
            # Not affected by --force: that flag re-copies from the content
            # cache, it doesn't change whether the result would fit.
            log.info("narrate: n=%d previously exceeded duration cap, skipping", n)
            continue

        if vo_path.exists() and not force:
            log.info("narrate: cache hit for n=%d (%s), skipping", n, vo_path)
            if _check_duration_fit(seg, vo_path):
                changed = True
            continue

        digest = script_hash(vo_script)
        cache_path = TTS_CACHE_DIR / f"{digest}.wav"

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

        vo_path.write_bytes(cache_path.read_bytes())
        seg.pop("tts_failed", None)
        changed = True
        _check_duration_fit(seg, vo_path)

    if changed:
        segments_path.write_text(json.dumps(data, indent=2))

    return data
