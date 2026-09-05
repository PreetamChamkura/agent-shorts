"""Stage 2 — transcribe.

Reads: work/{video_id}/source.mp4
Writes: work/{video_id}/words.json

faster-whisper, base.en, word_timestamps=True, CPU/int8. This is the slowest
stage in the pipeline — a 40-minute video takes minutes to transcribe — so the
cache is hard: if words.json exists we never re-run it for a caption tweak.

`transcribe_audio_file` is the underlying whisper wrapper, pulled out as a
reusable, stage-agnostic utility (mirrors ffmpeg.py's probe_duration): stage
3.5 also uses it to transcribe a synthesized VO line so cold_open can caption
it with the same karaoke engine as the rest of the clip, on real per-word
timing rather than an invented one.
"""

from __future__ import annotations

import json
import logging

from .paths import work_dir

log = logging.getLogger("clipper.transcribe")

MODEL = "base.en"


def transcribe_audio_file(audio_path: str, *, model: str = MODEL) -> dict:
    """Whisper-transcribe any audio/video file. Returns {language, duration, words}.

    No caching, no stage-specific file paths — callers own that. This is the
    generic wrapper both stage 2 and stage 3.5 build their own caching on top of.
    """
    from faster_whisper import WhisperModel  # heavy; import lazily

    log.info("transcribe: loading %s (cpu/int8) and transcribing %s", model, audio_path)
    whisper = WhisperModel(model, device="cpu", compute_type="int8")
    segments, info = whisper.transcribe(audio_path, language="en", word_timestamps=True)

    words: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            token = w.word.strip()
            if not token:
                continue
            words.append({"word": token, "start": round(w.start, 2), "end": round(w.end, 2)})

    return {
        "language": info.language or "en",
        "duration": round(info.duration, 1),
        "words": words,
    }


def transcribe(video_id: str, *, force: bool = False) -> dict:
    """Produce words.json for ``video_id`` and return the parsed dict."""
    wd = work_dir(video_id)
    source = wd / "source.mp4"
    words_path = wd / "words.json"

    if not source.exists():
        raise FileNotFoundError(f"transcribe: {source} missing — run stage 1 (ingest) first.")

    if words_path.exists() and not force:
        log.info("transcribe: cache hit for %s (%s), skipping whisper", video_id, words_path)
        return json.loads(words_path.read_text())

    out = transcribe_audio_file(str(source))
    words_path.write_text(json.dumps(out, indent=2))
    log.info("transcribe: wrote %d words to %s", len(out["words"]), words_path)
    return out
