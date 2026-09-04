"""Stage 2 — transcribe.

Reads: work/{video_id}/source.mp4
Writes: work/{video_id}/words.json

faster-whisper, base.en, word_timestamps=True, CPU/int8. This is the slowest
stage in the pipeline — a 40-minute video takes minutes to transcribe — so the
cache is hard: if words.json exists we never re-run it for a caption tweak.
"""

from __future__ import annotations

import json
import logging

from .paths import work_dir

log = logging.getLogger("clipper.transcribe")

MODEL = "base.en"


def transcribe(video_id: str, *, force: bool = False) -> dict:
    """Produce words.json for ``video_id`` and return the parsed dict."""
    wd = work_dir(video_id)
    source = wd / "source.mp4"
    words_path = wd / "words.json"

    if not source.exists():
        raise FileNotFoundError(
            f"transcribe: {source} missing — run stage 1 (ingest) first."
        )

    if words_path.exists() and not force:
        log.info("transcribe: cache hit for %s (%s), skipping whisper", video_id, words_path)
        return json.loads(words_path.read_text())

    from faster_whisper import WhisperModel  # heavy; import lazily

    log.info("transcribe: loading %s (cpu/int8) and transcribing %s", MODEL, source)
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(source), language="en", word_timestamps=True)

    words: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            token = w.word.strip()
            if not token:
                continue
            words.append({"word": token, "start": round(w.start, 2), "end": round(w.end, 2)})

    out = {
        "language": info.language or "en",
        "duration": round(info.duration, 1),
        "words": words,
    }
    words_path.write_text(json.dumps(out, indent=2))
    log.info("transcribe: wrote %d words to %s", len(words), words_path)
    return out
