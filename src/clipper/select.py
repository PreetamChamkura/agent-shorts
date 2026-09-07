"""Stage 3 — select (LLM).

Reads: work/{video_id}/words.json, work/{video_id}/source.info.json
Writes: work/{video_id}/segments.json

Walks the transcript in overlapping windows, asks the model (prompts/select.md)
for up to 3 standalone moments per window, then validates hard before anything
reaches segments.json:

  - start/end snapped to word boundaries from words.json (model timestamps
    drift; a clip starting mid-word feels broken). `end` is forward-biased —
    it extends to cover a word it lands inside, never truncates it
  - `end` must also cover the payoff phrase `why` quotes verbatim, when one
    can be reliably located — extended forward if short (within MAX_DURATION),
    else the segment is dropped. Catches a truncated punchline that a bare
    numeric comparison against the model's own "payoff at N" marker would miss
  - `why` must contain two parseable timestamps, else the segment is dropped —
    a vague `why` means the model didn't actually locate the beat
  - duration inside 18-58s, punchline in the last third, confidence >= 0.7
  - tone must exist in styles.yaml, else the whole run fails loudly

Everything except `_call_model` is pure and unit-testable without network.
Caching is load-bearing: segments.json is never regenerated unless `force`, so
re-running stage 4 fifty times never re-calls the LLM.

`select_clip()` is the stage 3 variant for a single pre-selected clip (Twitch
clips — CLAUDE.md step 2b): no windowing, one call against
prompts/select_clip.md, boundaries already given rather than searched for —
but the exact same validate_segment() and output schema as above.

Also produces the static thumbnail + upload-metadata fields (CLAUDE.md
thumbnail + metadata generation step) in this SAME call, not a separate LLM
call — context is already loaded, so `thumbnail_text` (a punchier, distinct
line for the static grid image), `suggested_title`, and `suggested_hashtags`
ride along with caption_title/vo_script. Thin on validation by design: none
of the three can drop an otherwise-good segment, only warn (see
validate_segment) — a real clip shouldn't be discarded over a marketing
field, mirroring how an over-long vo_script gets nulled rather than sinking
the segment. Consumed by clipper.thumbnail (stage 4's thumbnail/metadata
generation), not by render() itself.
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
MARKER_INTERVAL = 15.0  # [h:mm:ss] markers, per prompts/select.md

MIN_DURATION = 18.0
MAX_DURATION = 58.0
CONFIDENCE_FLOOR = 0.7
VO_MAX_WORDS = 35  # CLAUDE.md narrator rules: 12-30 words, hard ceiling 35
VALID_PLACEMENTS = ("cold_open", "pre_punchline")

# thumbnail_text soft bounds (CLAUDE.md thumbnail spec: "3-6 words max").
# Enforced as a warn-and-keep, not a drop — see validate_segment: a strong
# clip shouldn't be thrown away over a marketing field the model got
# slightly wrong on length, the same reasoning VO's word ceiling uses.
THUMBNAIL_TEXT_MIN_WORDS = 3
THUMBNAIL_TEXT_MAX_WORDS = 6

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
                    "thumbnail_text": {"type": "string"},
                    "suggested_title": {"type": "string"},
                    # No minItems/maxItems — confirmed for real that
                    # output_config's json_schema rejects any array minItems
                    # other than 0 or 1 ("'minItems' values other than 0 or 1
                    # are not supported", a real 400 from a live call trying
                    # [3, 5]). Count (3-5) is enforced as a soft check in
                    # validate_segment instead, same warn-and-keep posture
                    # as thumbnail_text's word count.
                    "suggested_hashtags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
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
                    "thumbnail_text",
                    "suggested_title",
                    "suggested_hashtags",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def format_hmmss(seconds: float) -> str:
    """Seconds -> `h:mm:ss` (hours unpadded, uncapped).

    Was `mm:ss` with minutes counting past 60 — mathematically fine, but a
    12-hour livestream produced markers like [720:00], and asking the model
    to convert a 3-digit minute count to seconds (multiply by 60, add the
    remainder) is exactly the kind of arithmetic step that introduces drift
    at scale. h:mm:ss keeps every field small (the hour count is the only
    one that grows, and it grows slowly) so the conversion the model has to
    do for `start`/`end`/`punchline_at` stays low-risk regardless of source
    length.
    """
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def render_transcript(words: list[dict], marker_interval: float = MARKER_INTERVAL) -> str:
    """Transcript text with `[h:mm:ss]` markers roughly every ``marker_interval``.

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
            parts.append(f"[{format_hmmss(next_marker)}]")
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
    """Snap ``start``/``end`` to word boundaries.

    Model timestamps drift 200-600ms and a clip starting mid-word feels
    broken. ``start`` snaps to the nearest word start — jitter there just
    shifts the open by a few hundred ms either way, harmless. ``end`` is
    forward-biased (see _snap_end_forward): nearest-by-distance can pick an
    earlier word's end that's merely closer, truncating the word the raw
    value actually falls inside. This didn't cause the incident that added
    this comment (see _find_phrase_end below for what did), but it's a real,
    separate defect worth closing on its own.
    """
    if not words:
        raise ValueError("snap_to_words: no words to snap to")
    snapped_start = min((float(w["start"]) for w in words), key=lambda s: abs(s - start))
    snapped_end = _snap_end_forward(words, end)
    return round(snapped_start, 2), round(snapped_end, 2)


def _snap_end_forward(words: list[dict], raw_end: float) -> float:
    """Forward-biased end snap: if ``raw_end`` falls inside a word's own
    span, extend to THAT word's end so the word is never cut off mid-way.
    Only falls back to nearest-by-distance when ``raw_end`` sits in a
    silence gap between words (nothing to extend into) or before every word
    (degenerate input) — in the gap case the nearest word end at or before
    ``raw_end`` is used, since there's no reason to invent content forward
    into a word that hadn't started yet.
    """
    containing = [w for w in words if float(w["start"]) <= raw_end < float(w["end"])]
    if containing:
        return max(float(w["end"]) for w in containing)
    at_or_before = [float(w["end"]) for w in words if float(w["end"]) <= raw_end]
    if at_or_before:
        return max(at_or_before)
    return min((float(w["end"]) for w in words), key=lambda e: abs(e - raw_end))


def _vo_word_count(vo_script: str) -> int:
    return len(vo_script.split())


# "payoff at N" — the model's own numeric marker. Not trustworthy on its own
# for validating coverage (see _find_phrase_end): a snapped `end` can already
# exceed this number while still cutting off mid-payoff, because the model's
# own arithmetic estimate of where its quote ends can itself be short.
_PAYOFF_MARKER_RE = re.compile(r"payoff\s+at\s+(\d+(?:\.\d+)?)", re.IGNORECASE)

# The verbatim payoff quote in `why`, e.g. "...payoff at 112.0 with 'he's not
# the best, but he's very affordable'...". A quote delimiter is a `'` NOT
# flanked by a letter on the outward side; a contraction's apostrophe (he's,
# don't) has a letter on both sides and is skipped by the lookbehind/lookahead.
_PAYOFF_QUOTE_RE = re.compile(
    r"payoff\b.*?(?<![a-zA-Z])'(.+?)'(?![a-zA-Z])", re.IGNORECASE | re.DOTALL
)


def _extract_payoff_marker(why: str) -> float | None:
    m = _PAYOFF_MARKER_RE.search(why)
    return float(m.group(1)) if m else None


def _extract_payoff_quote(why: str) -> str | None:
    """The verbatim payoff phrase `why` quotes, or None if the model
    paraphrased instead of quoting — that happens, and no reliable coverage
    check is possible without a verbatim quote to search for."""
    m = _PAYOFF_QUOTE_RE.search(why)
    return m.group(1) if m else None


def _normalize_token(tok: str) -> str:
    return re.sub(r"[^a-z0-9']", "", tok.lower())


def _find_phrase_end(words: list[dict], phrase: str, search_from: float) -> float | None:
    """Where ``phrase``'s last word actually ends in ``words``.

    Searches only from shortly before ``search_from`` (the payoff marker)
    onward, so an earlier, unrelated occurrence of the same words can't be
    matched instead. Returns None if the phrase isn't a contiguous token
    run in the transcript — the model paraphrased rather than quoting
    verbatim (confirmed to happen on real output), and no reliable check is
    possible then; callers should skip enforcement rather than guess.
    """
    tokens = [t for t in (_normalize_token(tok) for tok in phrase.split()) if t]
    if not tokens:
        return None
    candidates = [w for w in words if float(w["start"]) >= search_from - 1.0]
    norm_words = [_normalize_token(w["word"]) for w in candidates]
    n = len(tokens)
    for i in range(len(norm_words) - n + 1):
        if norm_words[i : i + n] == tokens:
            return float(candidates[i + n - 1]["end"])
    return None


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

    # `end` must cover the payoff phrase `why` itself quotes verbatim, not
    # just exceed the model's own numeric "payoff at N" marker — that marker
    # can already be short of the quote's true completion (confirmed on real
    # output: end 119.22s > marker's stated "119.0s", yet still cut off
    # "very affordable" before it finished at 119.60s). Only enforced when
    # the quote can be reliably located; a paraphrased why or an unmatched
    # quote falls back to the checks below rather than risking a
    # false-positive rejection on transcription noise.
    payoff_quote = _extract_payoff_quote(why)
    if payoff_quote:
        marker = _extract_payoff_marker(why) or start
        phrase_end = _find_phrase_end(words, payoff_quote, marker)
        if phrase_end is not None and phrase_end > end:
            if phrase_end - start <= MAX_DURATION:
                log.info(
                    "select: %r end %.2f fell short of quoted payoff (ends %.2f) — extending",
                    hook,
                    end,
                    phrase_end,
                )
                end = phrase_end
            else:
                log.info(
                    "select: dropped %r — quoted payoff ends %.2f, past %.0fs even snapped forward",
                    hook,
                    phrase_end,
                    MAX_DURATION,
                )
                return None

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

    caption_title = str(raw.get("caption_title", ""))

    # Thumbnail/metadata fields (CLAUDE.md thumbnail + metadata generation
    # step): a real segment shouldn't be dropped just because one of these
    # marketing fields came back thin — warn and keep, same posture as the
    # VO word-ceiling above, never a hard reject.
    thumbnail_text = str(raw.get("thumbnail_text", "")).strip()
    word_count = len(thumbnail_text.split())
    if thumbnail_text and not (THUMBNAIL_TEXT_MIN_WORDS <= word_count <= THUMBNAIL_TEXT_MAX_WORDS):
        log.warning(
            "select: %r thumbnail_text is %d word(s) (want %d-%d): %r — keeping as-is",
            hook,
            word_count,
            THUMBNAIL_TEXT_MIN_WORDS,
            THUMBNAIL_TEXT_MAX_WORDS,
            thumbnail_text,
        )
    if thumbnail_text and thumbnail_text.strip().lower() == caption_title.strip().lower():
        log.warning(
            "select: %r thumbnail_text is identical to caption_title (%r) — should be distinct",
            hook,
            thumbnail_text,
        )
    if not thumbnail_text:
        log.warning("select: %r has no thumbnail_text", hook)

    suggested_title = str(raw.get("suggested_title", "")).strip()
    if not suggested_title:
        log.warning("select: %r has no suggested_title", hook)

    suggested_hashtags = [
        str(h).strip() for h in (raw.get("suggested_hashtags") or []) if str(h).strip()
    ]
    if not suggested_hashtags:
        log.warning("select: %r has no suggested_hashtags", hook)
    elif not (3 <= len(suggested_hashtags) <= 5):
        log.warning(
            "select: %r has %d suggested_hashtags (want 3-5): %r — keeping as-is",
            hook,
            len(suggested_hashtags),
            suggested_hashtags,
        )

    return {
        "start": start,
        "end": end,
        "punchline_at": round(punchline_at, 2),
        "tone": tone,
        "loopable": bool(raw.get("loopable", False)),
        "hook": hook,
        "caption_title": caption_title,
        "vo_script": vo_script,
        "vo_placement": placement,
        "why": why,
        "confidence": round(confidence, 2),
        "thumbnail_text": thumbnail_text,
        "suggested_title": suggested_title,
        "suggested_hashtags": suggested_hashtags,
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

    # Window count scales linearly with source length (~8 windows/hour at the
    # current 480s window / 450s step), and each window is one LLM call. A
    # multi-hour livestream therefore costs an order of magnitude more than a
    # podcast episode. Say so up front rather than only in the per-window logs.
    span = max(float(w["end"]) for w in words) - float(words[0]["start"])
    log.info(
        "select: %d windows over %.1f hours of transcript = %d LLM calls to %s",
        len(windows),
        span / 3600,
        len(windows),
        model,
    )

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
            window_start=format_hmmss(win["start"]),
            window_end=format_hmmss(win["end"]),
            transcript_window=render_transcript(win["words"]),
            **meta,
        )
        log.info(
            "select: window %d/%d (%s-%s), %d words",
            win["index"],
            win["total"],
            format_hmmss(win["start"]),
            format_hmmss(win["end"]),
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


def select_clip(video_id: str, *, force: bool = False, model: str = MODEL) -> dict:
    """Stage 3 variant for a single pre-selected clip (Twitch clips — CLAUDE.md
    step 2b), not a long-form video. No window-hunting: one LLM call against
    prompts/select_clip.md over the clip's entire transcript, whose boundaries
    are already given rather than searched for. Same output schema and the
    same validate_segment() as the long-form path — snap-to-word, the payoff-
    quote coverage check, the hard 18-58s range, and the last-third punchline
    rule all apply completely unchanged.

    A clip below MIN_DURATION is rejected before ever calling the model —
    there's nothing to trim up to, so the call would be pure waste.
    """
    wd = work_dir(video_id)
    words_path = wd / "words.json"
    segments_path = wd / "segments.json"

    if not words_path.exists():
        raise FileNotFoundError(
            f"select_clip: {words_path} missing — run stage 2 (transcribe) first."
        )

    if segments_path.exists() and not force:
        log.info("select_clip: cache hit for %s (%s), skipping the LLM", video_id, segments_path)
        return json.loads(segments_path.read_text())

    payload = json.loads(words_path.read_text())
    words = payload.get("words", [])
    if not words:
        raise ValueError(f"select_clip: {words_path} has no words")

    clip_duration = float(payload.get("duration") or words[-1]["end"])
    if clip_duration < MIN_DURATION:
        log.warning(
            "select_clip: %s is %.1fs, under the %.0fs floor — nothing to trim up to, rejecting "
            "without calling the model",
            video_id,
            clip_duration,
            MIN_DURATION,
        )
        out = {"video_id": video_id, "segments": []}
        segments_path.write_text(json.dumps(out, indent=2))
        return out

    template = (PROMPTS_DIR / "select_clip.md").read_text()
    meta = _source_metadata(wd)

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    prompt = render_prompt(
        template,
        window_index="1",
        window_total="1",
        window_start=format_hmmss(0.0),
        window_end=format_hmmss(clip_duration),
        transcript_window=render_transcript(words),
        **meta,
    )
    log.info(
        "select_clip: %s (%.1fs, %d words) -> 1 LLM call to %s",
        video_id,
        clip_duration,
        len(words),
        model,
    )
    response = _call_model(client, prompt, model)
    raw_segments = response.get("segments", [])

    collected: list[dict] = []
    for raw in raw_segments:
        seg = validate_segment(raw, words)
        if seg is not None:
            collected.append(seg)

    for n, seg in enumerate(collected):
        seg["n"] = n

    out = {"video_id": video_id, "segments": collected}
    segments_path.write_text(json.dumps(out, indent=2))
    log.info("select_clip: wrote %d segment(s) to %s", len(collected), segments_path)
    return out
