"""Stage 3 — select (LLM).

Reads: work/{video_id}/words.json, work/{video_id}/source.info.json
Writes: work/{video_id}/segments.json

Walks the transcript in overlapping windows, asks the model (prompts/select.md)
for up to 3 standalone moments per window, then validates hard before anything
reaches segments.json:

  - start/end snapped to word boundaries from words.json (model timestamps
    drift; a clip starting mid-word feels broken)
  - `why` must contain two parseable timestamps, else the segment is dropped —
    a vague `why` means the model didn't actually locate the beat
  - duration inside 18-58s, punchline in the last third, confidence >= 0.7
  - tone must exist in styles.yaml, else the whole run fails loudly

Everything except `_call_model` is pure and unit-testable without network.
Caching is load-bearing: segments.json is never regenerated unless `force`, so
re-running stage 4 fifty times never re-calls the LLM.
"""

from __future__ import annotations

import json
import logging
import re

from .paths import PROMPTS_DIR, work_dir
from .styles import style_for_tone

log = logging.getLogger("clipper.select")

MODEL = "claude-opus-5"
MAX_TOKENS = 8000

# Transcript windowing. Big enough to hold a moment plus surrounding context,
# small enough that the model can actually attend to all of it. The overlap
# keeps a moment that straddles a boundary from being cut in half; duplicates
# it produces are removed by _dedupe.
WINDOW_SECONDS = 480.0
WINDOW_OVERLAP = 30.0
MARKER_INTERVAL = 15.0  # [mm:ss] markers, per prompts/select.md

MIN_DURATION = 18.0
MAX_DURATION = 58.0
CONFIDENCE_FLOOR = 0.7
VO_MAX_WORDS = 35  # CLAUDE.md narrator rules: 12-30 words, hard ceiling 35
VALID_PLACEMENTS = ("cold_open", "pre_punchline")

# Placeholders in prompts/select.md. Substituted by explicit replace, never
# str.format — the template contains a literal JSON example whose braces would
# blow up any format-string parsing.
_PLACEHOLDERS = (
    "video_title",
    "channel_name",
    "upload_date",
    "video_description_first_500_chars",
    "window_index",
    "window_total",
    "window_start",
    "window_end",
    "transcript_window",
)

# output_config schema — guarantees the response is valid JSON in this shape,
# so a fenced or chatty reply can't become a parse failure.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "punchline_at": {"type": "number"},
                    "tone": {"type": "string"},
                    "loopable": {"type": "boolean"},
                    "hook": {"type": "string"},
                    "caption_title": {"type": "string"},
                    "vo_script": {"type": ["string", "null"]},
                    "vo_placement": {"type": "string"},
                    "why": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "start",
                    "end",
                    "punchline_at",
                    "tone",
                    "loopable",
                    "hook",
                    "caption_title",
                    "vo_script",
                    "vo_placement",
                    "why",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def format_mmss(seconds: float) -> str:
    """Seconds -> `mm:ss` (minutes keep counting past 60)."""
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def render_transcript(words: list[dict], marker_interval: float = MARKER_INTERVAL) -> str:
    """Transcript text with `[mm:ss]` markers roughly every ``marker_interval``.

    Markers are absolute within the full video — segments must come back in
    absolute seconds, not window-relative.
    """
    if not words:
        return ""

    parts: list[str] = []
    next_marker = float(words[0]["start"]) // marker_interval * marker_interval
    for w in words:
        ws = float(w["start"])
        while ws >= next_marker:
            parts.append(f"[{format_mmss(next_marker)}]")
            next_marker += marker_interval
        parts.append(str(w["word"]))
    return " ".join(parts)


def build_windows(
    words: list[dict],
    window_seconds: float = WINDOW_SECONDS,
    overlap: float = WINDOW_OVERLAP,
) -> list[dict]:
    """Split ``words`` into overlapping windows.

    Each window is {index, total, start, end, words}. ``index`` is 1-based to
    match the prompt's "Window {window_index} of {window_total}".
    """
    if not words:
        return []
    if overlap >= window_seconds:
        raise ValueError(f"overlap {overlap} must be smaller than window {window_seconds}")

    last_end = max(float(w["end"]) for w in words)
    step = window_seconds - overlap

    windows: list[dict] = []
    start = float(words[0]["start"])
    while start < last_end:
        end = start + window_seconds
        chunk = [w for w in words if start <= float(w["start"]) < end]
        if chunk:
            windows.append({"start": start, "end": min(end, last_end), "words": chunk})
        start += step

    for i, win in enumerate(windows, start=1):
        win["index"] = i
        win["total"] = len(windows)
    return windows


def render_prompt(template: str, **values: str) -> str:
    """Substitute the prompt's `{placeholders}` without touching its JSON braces."""
    unknown = set(values) - set(_PLACEHOLDERS)
    if unknown:
        raise ValueError(f"unknown prompt placeholders: {sorted(unknown)}")
    out = template
    for key in _PLACEHOLDERS:
        if key in values:
            out = out.replace("{" + key + "}", str(values[key]))
    return out


def parse_timestamps(text: str) -> list[float]:
    """Every number in ``text`` that could be a timestamp, in order."""
    return [float(m) for m in re.findall(r"\d+(?:\.\d+)?", text or "")]


def why_is_located(why: str) -> bool:
    """CLAUDE.md: `why` must contain two parseable timestamps, else the model
    didn't actually locate the beat and the segment is dropped."""
    return len(parse_timestamps(why)) >= 2


def snap_to_words(words: list[dict], start: float, end: float) -> tuple[float, float]:
    """Snap ``start``/``end`` to the nearest word boundaries.

    Model timestamps drift 200-600ms and a clip starting mid-word feels broken,
    so start lands on a word's start and end on a word's end.
    """
    if not words:
        raise ValueError("snap_to_words: no words to snap to")
    snapped_start = min((float(w["start"]) for w in words), key=lambda s: abs(s - start))
    snapped_end = min((float(w["end"]) for w in words), key=lambda e: abs(e - end))
    return round(snapped_start, 2), round(snapped_end, 2)


def _vo_word_count(vo_script: str) -> int:
    return len(vo_script.split())


def validate_segment(raw: dict, words: list[dict]) -> dict | None:
    """Normalize and validate one model segment. Returns None if it's dropped.

    Raises UnknownToneError (via style_for_tone) if the tone has no block in
    styles.yaml — CLAUDE.md wants that drift to surface immediately, not be
    quietly skipped like the other rejections.
    """
    hook = raw.get("hook", "?")

    why = str(raw.get("why", ""))
    if not why_is_located(why):
        log.info("select: dropped %r — `why` lacks two timestamps: %r", hook, why)
        return None

    # Fails loudly on drift between the selection prompt and styles.yaml.
    tone = str(raw.get("tone", ""))
    style_for_tone(tone)

    try:
        start = float(raw["start"])
        end = float(raw["end"])
        punchline_at = float(raw["punchline_at"])
        confidence = float(raw["confidence"])
    except (KeyError, TypeError, ValueError) as exc:
        log.info("select: dropped %r — unusable numeric field: %s", hook, exc)
        return None

    if confidence < CONFIDENCE_FLOOR:
        log.info(
            "select: dropped %r — confidence %.2f below %.2f", hook, confidence, CONFIDENCE_FLOOR
        )
        return None

    start, end = snap_to_words(words, start, end)
    duration = end - start
    if not (MIN_DURATION <= duration <= MAX_DURATION):
        log.info(
            "select: dropped %r — %.1fs outside %.0f-%.0fs after snapping",
            hook,
            duration,
            MIN_DURATION,
            MAX_DURATION,
        )
        return None

    # Payoff must land in the last third, else it's a clip that peaks early.
    if not (start + duration * 2 / 3 <= punchline_at <= end):
        log.info(
            "select: dropped %r — punchline %.1f not in last third of %.1f-%.1f",
            hook,
            punchline_at,
            start,
            end,
        )
        return None

    placement = str(raw.get("vo_placement", ""))
    if placement not in VALID_PLACEMENTS:
        log.info("select: dropped %r — bad vo_placement %r", hook, placement)
        return None

    # A null vo_script is a normal path. An over-long one gets dropped to null
    # and flagged rather than sinking an otherwise good segment.
    vo_script = raw.get("vo_script")
    if vo_script is not None:
        vo_script = str(vo_script).strip() or None
    if vo_script is not None and _vo_word_count(vo_script) > VO_MAX_WORDS:
        log.warning(
            "select: %r vo_script is %d words (ceiling %d) — nulling the line, keeping segment",
            hook,
            _vo_word_count(vo_script),
            VO_MAX_WORDS,
        )
        vo_script = None

    return {
        "start": start,
        "end": end,
        "punchline_at": round(punchline_at, 2),
        "tone": tone,
        "loopable": bool(raw.get("loopable", False)),
        "hook": hook,
        "caption_title": str(raw.get("caption_title", "")),
        "vo_script": vo_script,
        "vo_placement": placement,
        "why": why,
        "confidence": round(confidence, 2),
    }


def dedupe(segments: list[dict], min_overlap: float = 2.0) -> list[dict]:
    """Drop near-duplicate segments produced by the window overlap.

    Two segments overlapping by more than ``min_overlap`` seconds are the same
    moment seen twice; the higher-confidence one wins.
    """
    ordered = sorted(segments, key=lambda s: (-s["confidence"], s["start"]))
    kept: list[dict] = []
    for seg in ordered:
        clash = any(
            min(seg["end"], k["end"]) - max(seg["start"], k["start"]) > min_overlap for k in kept
        )
        if not clash:
            kept.append(seg)
    return sorted(kept, key=lambda s: s["start"])


def _source_metadata(wd) -> dict:
    """Title/channel/date/description from yt-dlp's info json, if present."""
    info_path = wd / "source.info.json"
    if not info_path.exists():
        log.warning("select: %s missing — prompting without source metadata", info_path)
        return {}
    info = json.loads(info_path.read_text())
    return {
        "video_title": info.get("title", ""),
        "channel_name": info.get("channel") or info.get("uploader", ""),
        "upload_date": info.get("upload_date", ""),
        "video_description_first_500_chars": (info.get("description") or "")[:500],
    }


def _call_model(client, prompt: str, model: str) -> dict:
    """The only networked call in this module. Returns the parsed JSON object."""
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"select: model refused the request ({response.stop_details})")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def select(video_id: str, *, force: bool = False, model: str = MODEL) -> dict:
    """Produce segments.json for ``video_id`` and return the parsed dict."""
    wd = work_dir(video_id)
    words_path = wd / "words.json"
    segments_path = wd / "segments.json"

    if not words_path.exists():
        raise FileNotFoundError(f"select: {words_path} missing — run stage 2 (transcribe) first.")

    if segments_path.exists() and not force:
        log.info("select: cache hit for %s (%s), skipping the LLM", video_id, segments_path)
        return json.loads(segments_path.read_text())

    words = json.loads(words_path.read_text())["words"]
    windows = build_windows(words)
    if not windows:
        raise ValueError(f"select: {words_path} has no words to window")

    template = (PROMPTS_DIR / "select.md").read_text()
    meta = _source_metadata(wd)

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    collected: list[dict] = []
    for win in windows:
        prompt = render_prompt(
            template,
            window_index=str(win["index"]),
            window_total=str(win["total"]),
            window_start=format_mmss(win["start"]),
            window_end=format_mmss(win["end"]),
            transcript_window=render_transcript(win["words"]),
            **meta,
        )
        log.info(
            "select: window %d/%d (%s-%s), %d words",
            win["index"],
            win["total"],
            format_mmss(win["start"]),
            format_mmss(win["end"]),
            len(win["words"]),
        )
        payload = _call_model(client, prompt, model)
        raw_segments = payload.get("segments", [])
        for raw in raw_segments:
            seg = validate_segment(raw, words)
            if seg is not None:
                collected.append(seg)
        log.info(
            "select: window %d returned %d, kept %d so far",
            win["index"],
            len(raw_segments),
            len(collected),
        )

    final = dedupe(collected)
    for n, seg in enumerate(final):
        seg["n"] = n

    out = {"video_id": video_id, "segments": final}
    segments_path.write_text(json.dumps(out, indent=2))
    log.info("select: wrote %d segments to %s", len(final), segments_path)
    return out
