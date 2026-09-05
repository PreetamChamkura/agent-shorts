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
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import requests

from .paths import TTS_CACHE_DIR, work_dir

log = logging.getLogger("clipper.narrate")

TTS_URL = "https://api.openai.com/v1/audio/speech"
NARRATOR_MODEL = "tts-1-hd"
NARRATOR_VOICE = "echo"  # picked by ear from an onyx/echo/fable audition — never randomize
RESPONSE_FORMAT = "wav"
TIMEOUT = 60.0


def script_hash(vo_script: str) -> str:
    return hashlib.sha256(vo_script.encode("utf-8")).hexdigest()


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

        if vo_path.exists() and not force:
            log.info("narrate: cache hit for n=%d (%s), skipping", n, vo_path)
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

    if changed:
        segments_path.write_text(json.dumps(data, indent=2))

    return data
