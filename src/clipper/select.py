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

Real cost finding (a full per-call token reconstruction, not a guess): the
fixed instructions in select.md/select_clip.md ran ~6,372 tokens versus
~100-300 tokens of real per-clip data — over 95% of every call was
byte-identical text, paid at full price every time, because nothing was ever
split into a cached `system` block. `split_cacheable_prompt()` cuts each
prompt file at its `PROMPT_CACHE_BOUNDARY` marker into the fixed remainder
(sent as a cached `system` block) and the real per-call Source/transcript
data (sent as the user turn, since it can never cache anyway) — measured
~63% cheaper on a real same-session batch. `_call_model` also logs the real
`response.usage` for every call (input/output/thinking/cache split) — before
this, the pipeline logged nothing, so a cost audit had to replay prompts
through `count_tokens` to reconstruct a lower-bound estimate after the fact.

`select_clip()` is the stage 3 variant for a single pre-selected clip (Twitch
clips — CLAUDE.md step 2b): no windowing, one call against
prompts/select_clip.md, boundaries already given rather than searched for —
but the exact same validate_segment() and output schema as above.

Narration mode (CLAUDE.md "Narration modes") is decided in this same call:
`vo_placement` picks between spoken framing (`cold_open`/`pre_punchline`,
carried by `vo_script`) and held visual framing (`caption_only`, carried by
`on_screen_caption`), with both null when the clip needs no framing at all.
Exactly one delivery field is ever non-null — `resolve_narration` enforces
that, treating `vo_placement` as the authority and nulling the field that
doesn't belong to the chosen mode rather than dropping the segment.

Also produces the static thumbnail + upload-metadata fields (CLAUDE.md
thumbnail + metadata generation step) in this SAME call, not a separate LLM
call — context is already loaded, so `thumbnail_text` (a punchier, distinct
line for the static grid image), `suggested_title`, `suggested_hashtags`, and
`suggested_description` (a short credit-and-hashtags blurb for the upload
form's own description field, repeating the first few hashtags since that's
what surfaces above the title on-platform) ride along with
caption_title/vo_script. Thin on validation by design: none of the four can
drop an otherwise-good segment, only warn (see validate_segment) — a real
clip shouldn't be discarded over a marketing field, mirroring how an
over-long vo_script gets nulled rather than sinking the segment. Consumed by
clipper.thumbnail (stage 4's thumbnail/metadata
generation), not by render() itself.
"""

from __future__ import annotations

import json
import logging
import re

from .audience import describe_tier, resolve_audience_tier
from .paths import PROMPTS_DIR, work_dir
from .styles import style_for_tone

log = logging.getLogger("clipper.select")

MODEL = "claude-opus-5"  # select()'s long-form path — unchanged, not in scope
MAX_TOKENS = 8000

# Cost is the explicit priority over selection quality for select_clip
# specifically (project is self-funded, prior spend wasn't sustainable —
# real numbers in the batching/effort-cap history above). Switched from
# claude-opus-5 to claude-sonnet-5 with no comparison eval first, by
# explicit instruction: whatever quality difference this brings is accepted,
# not measured before shipping. Scoped to select_clip/select_clip_batch
# only — MODEL above (select()'s long-form path) is untouched, since that
# wasn't part of what was asked.
SELECT_CLIP_MODEL = "claude-sonnet-5"

# Thinking is DISABLED outright for select_clip's call, not just capped —
# unlike claude-opus-5 (where budget_tokens 400s and effort is only ever a
# depth preference, confirmed for real to still blow the full token budget
# even at "medium"), claude-sonnet-5 accepts thinking={"type": "disabled"},
# an actual hard zero rather than a lower preference. Thinking tokens were
# the single biggest driver of every real blowout logged so far, so this is
# the strongest available lever, not a compromise one.
SELECT_EFFORT = "medium"  # select()'s long-form path only; select_clip disables thinking entirely

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

# Real finding from a real investigation (CLAUDE.md Known issues): a clip
# ("Nightmare dungeons") that flipped accept/reject across effort levels in
# an eval sweep turned out to have been accepted at confidence=0.71 in the
# first place — one hundredth above this hard floor. Not a model or effort
# defect; CONFIDENCE_FLOOR is a binary cutoff, so a case that close to it is
# a near-coin-flip by construction, regardless of what decided it. This
# doesn't fix that — it makes it visible: logged whenever confidence lands
# within this margin of the floor, on either side, so how often the
# selector is making a genuinely marginal call stops being invisible. If
# that turns out to be common, that's grounds to revisit the floor itself
# later — not a per-instance fix.
CONFIDENCE_BORDERLINE_MARGIN = 0.03
VO_MAX_WORDS = 35  # CLAUDE.md narrator rules: 2-6 words is the target, 8-20 an outer
# allowance for a beat that can't compress further, 35 the hard ceiling — unchanged
# here since this constant has only ever been the hard-reject cutoff, not the target

# Narration modes (CLAUDE.md "Narration modes"). `vo_placement` is the mode
# selector, and it is the AUTHORITY when the model contradicts itself: the two
# delivery fields are mutually exclusive, so whichever one doesn't belong to
# the chosen mode gets nulled rather than the segment being dropped (see
# validate_segment). The field name is a legacy wart — it predates there being
# a non-VO mode — kept because renaming it would invalidate every segments.json
# already on disk for no behavioural gain.
CAPTION_ONLY = "caption_only"
VALID_PLACEMENTS = ("cold_open", "pre_punchline", CAPTION_ONLY)

# A held caption is the same editorial line as a vo_script, just delivered to
# the eye instead of the ear, so it answers to the same word ceiling — see
# CLAUDE.md's narrator rules, which apply to both modes without exception.
CAPTION_MAX_WORDS = VO_MAX_WORDS

# thumbnail_text soft bounds (CLAUDE.md thumbnail spec: "3-6 words max").
# Enforced as a warn-and-keep, not a drop — see validate_segment: a strong
# clip shouldn't be thrown away over a marketing field the model got
# slightly wrong on length, the same reasoning VO's word ceiling uses.
THUMBNAIL_TEXT_MIN_WORDS = 3
THUMBNAIL_TEXT_MAX_WORDS = 6

# suggested_description soft bounds. The spec walked down over time: 2-4
# sentences -> 1-2 -> now a credit line + the first 3 hashtags, with an
# opening context sentence optional (only when it adds something the title
# doesn't). So the real floor is one sentence (the credit line — "Clip via
# X on Twitch.") and the ceiling is two (credit line + the optional
# context sentence). Same warn-and-keep posture as thumbnail_text's word
# count above — a sentence count outside this is a reason to look at the
# field, not to drop a real segment over a marketing field.
DESCRIPTION_MIN_SENTENCES = 1
DESCRIPTION_MAX_SENTENCES = 2

# Log-severity split only, not a correction gate — see validate_segment's
# punchline_at derivation below. `punchline_at` used to be trusted as the
# model's own independent guess (checked against the quoted payoff's real
# span only loosely, correcting just when the two disagreed by more than
# this many seconds). Real bug found from real output: a segment's
# punchline_at (112.0) sat 5.3s before its own quoted payoff phrase actually
# started (117.30) — `end` already covered the phrase (the existing check
# this one is next to), but nothing had ever confirmed punchline_at itself
# wasn't off in the same way. That drift fed real downstream mistakes:
# pre_punchline VO timing anchors 1.2s before punchline_at, and
# clipper.thumbnail's fallback frame search centers on it.
#
# Tolerance-gated correction wasn't enough: real repeated trials on one clip
# (the ironmouse "Spanish ballad" clip) produced punchline_at values of
# 26.0, 28.5, 29.0, 38.0, and 40.0 across identical input — a 14-second
# spread, well past this tolerance, meaning the model's own guess was never
# something to trust as a baseline in the first place. Same fix `end`
# already has: whenever the payoff quote is locatable in the real
# transcript, punchline_at is DERIVED from that phrase's own real start,
# unconditionally — not compared against the model's guess and corrected
# only past a threshold. This constant now only decides whether that
# derivation gets logged at INFO (small, expected jitter) or WARNING (the
# model's guess was substantially off, worth noticing if it happens often).
PUNCHLINE_DRIFT_TOLERANCE = 2.0

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
    # select_clip.md only — the long-form select.md path has no equivalent
    # signal (a YouTube upload's view count says nothing about whether one
    # moment inside it is recognizable), so it neither supplies nor
    # references this one.
    "audience_tier",
)

# output_config schema — guarantees the response is valid JSON in this shape,
# so a fenced or chatty reply can't become a parse failure. Shared between the
# single-clip and batch schemas below so the two can never drift apart on
# what a segment object actually contains.
_SEGMENT_SCHEMA = {
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
        "on_screen_caption": {"type": ["string", "null"]},
        "vo_placement": {"type": "string"},
        "narration_mode_reason": {"type": "string"},
        "why": {"type": "string"},
        "confidence": {"type": "number"},
        "thumbnail_text": {"type": "string"},
        "suggested_title": {"type": "string"},
        "title_style": {"type": "string"},
        # No minItems/maxItems — confirmed for real that output_config's
        # json_schema rejects any array minItems other than 0 or 1 ("'minItems'
        # values other than 0 or 1 are not supported", a real 400 from a live
        # call trying [3, 5]). Count (3-5) is enforced as a soft check in
        # validate_segment instead, same warn-and-keep posture as
        # thumbnail_text's word count.
        "suggested_hashtags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "suggested_description": {"type": "string"},
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
        "on_screen_caption",
        "vo_placement",
        "narration_mode_reason",
        "why",
        "confidence",
        "thumbnail_text",
        "suggested_title",
        "title_style",
        "suggested_hashtags",
        "suggested_description",
    ],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"segments": {"type": "array", "items": _SEGMENT_SCHEMA}},
    "required": ["segments"],
    "additionalProperties": False,
}

# select_clip_batch's response: one result per candidate, in the same order
# submitted. clip_index is 1-based and echoed back by the model so a
# reordered or dropped result is detectable rather than silently mismatched
# to the wrong video_id.
_BATCH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clip_index": {"type": "integer"},
                    "segments": {"type": "array", "items": _SEGMENT_SCHEMA},
                },
                "required": ["clip_index", "segments"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
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


# Real cost finding (measured, not estimated — a full per-call reconstruction
# via count_tokens): the fixed instructions in select.md/select_clip.md run
# ~6,372 tokens, versus ~100-300 tokens of real per-call Source/transcript
# data — over 95% of every call was fixed text, paid at full price every
# time, because nothing was ever split into a cacheable `system` block.
# Placeholders only ever appear above this marker in both prompt files (the
# Source + transcript sections); split_cacheable_prompt() cuts there so the
# fixed remainder can be sent as a cached system prompt while the genuinely
# per-call data — which can never cache anyway — stays in the user turn.
_CACHE_BOUNDARY_MARKER = "<!-- PROMPT_CACHE_BOUNDARY:"


def split_cacheable_prompt(template: str) -> tuple[str, str]:
    """Split a prompt template into (dynamic_user_text, static_system_text)
    at its `_CACHE_BOUNDARY_MARKER` HTML comment.

    The static half must be byte-identical across every call to the same
    prompt file to actually cache — checked here by construction: no
    placeholder may appear on its side of the marker. Raises if the marker
    is missing (a prompt edit deleted it — the split silently stops
    happening) or if a placeholder leaked past it (a prompt edit would
    silently ship literal `{video_title}` text on every cached call instead
    of the real value). Both are real-drift failures, not edge cases to
    tolerate quietly.
    """
    start = template.find(_CACHE_BOUNDARY_MARKER)
    if start == -1:
        raise ValueError(
            f"split_cacheable_prompt: no {_CACHE_BOUNDARY_MARKER!r} marker in this prompt — "
            "it must mark where the fixed instructions begin, or nothing can be cached"
        )
    end = template.find("-->", start)
    if end == -1:
        raise ValueError("split_cacheable_prompt: the PROMPT_CACHE_BOUNDARY comment never closes")

    dynamic = template[:start].rstrip("\n") + "\n"
    static = template[end + len("-->") :].lstrip("\n")
    leaked = [p for p in _PLACEHOLDERS if "{" + p + "}" in static]
    if leaked:
        raise ValueError(
            f"split_cacheable_prompt: placeholder(s) {leaked} appear AFTER the cache boundary — "
            "they would never be substituted and would ship literally on every cached call"
        )
    return dynamic, static


def _log_usage(video_id: str, model: str, usage) -> None:
    """Real per-call token usage, logged unconditionally — the prerequisite
    for ever auditing cost from logs instead of reconstructing it after the
    fact (see CLAUDE.md's cost investigation: before this, the pipeline
    logged nothing, so every real number had to be rebuilt from scratch by
    replaying prompts through count_tokens). `thinking_tokens` is billed as
    output but invisible in the visible response text — output_tokens_details
    is the only place it shows up, and it's exactly the number that made the
    earlier reconstruction a floor rather than the real total.
    """
    thinking = usage.output_tokens_details.thinking_tokens if usage.output_tokens_details else 0
    log.info(
        "select: usage %s model=%s in=%d cache_read=%d cache_write=%d out=%d(thinking=%d)",
        video_id,
        model,
        usage.input_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
        usage.output_tokens,
        thinking,
    )


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


def _line_word_count(line: str) -> int:
    """Words in a narrator line — a spoken vo_script or a held on_screen_caption.
    One helper because they answer to one word ceiling (CLAUDE.md)."""
    return len(line.split())


def _clean_line(value: object) -> str | None:
    """A narrator line stripped to None when it's absent or blank."""
    if value is None:
        return None
    return str(value).strip() or None


def _count_sentences(text: str) -> int:
    """Rough sentence count for the soft `suggested_description` length
    check — split on sentence-ending punctuation, drop empty fragments from
    trailing punctuation or double spacing. Not meant to be exact (an
    abbreviation or a decimal number would over-count), only enough to warn
    when a description reads like one line or like a wall of text."""
    return len([s for s in re.split(r"[.!?]+", text) if s.strip()])


def resolve_narration(hook: str, raw: dict, placement: str) -> tuple[str | None, str | None]:
    """Return the ``(vo_script, on_screen_caption)`` this segment actually ships.

    Exactly one can be non-null: a spoken line and a held line are two ways to
    deliver the same framing, and running both means the viewer reads one
    sentence while hearing a different one. ``placement`` is the authority when
    the model contradicts itself — it is the field where the model states the
    mode it chose, so the delivery field that doesn't belong to that mode is the
    one that gets dropped.

    Nothing here can sink a segment. An over-long line, a contradiction, or a
    caption_only with no caption all degrade to a quieter but still-valid
    segment, the same warn-and-keep posture the VO ceiling has always had — a
    real moment shouldn't be thrown away over how its framing is delivered.
    """
    vo_script = _clean_line(raw.get("vo_script"))
    caption = _clean_line(raw.get("on_screen_caption"))

    if vo_script is not None and _line_word_count(vo_script) > VO_MAX_WORDS:
        log.warning(
            "select: %r vo_script is %d words (ceiling %d) — nulling the line, keeping segment",
            hook,
            _line_word_count(vo_script),
            VO_MAX_WORDS,
        )
        vo_script = None
    if caption is not None and _line_word_count(caption) > CAPTION_MAX_WORDS:
        log.warning(
            "select: %r on_screen_caption is %d words (ceiling %d) — nulling it, keeping segment",
            hook,
            _line_word_count(caption),
            CAPTION_MAX_WORDS,
        )
        caption = None

    if placement == CAPTION_ONLY:
        if vo_script is not None:
            log.warning(
                "select: %r is caption_only but also returned a vo_script (%r) — "
                "dropping the VO, the two modes are exclusive",
                hook,
                vo_script,
            )
            vo_script = None
        if caption is None:
            log.warning(
                "select: %r chose caption_only but left on_screen_caption empty — "
                "rendering silent, keeping segment",
                hook,
            )
        return None, caption

    if caption is not None:
        log.warning(
            "select: %r is %s but also returned an on_screen_caption (%r) — "
            "dropping the caption, the two modes are exclusive",
            hook,
            placement,
            caption,
        )
    return vo_script, None


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


def _find_phrase_span(
    words: list[dict], phrase: str, search_from: float
) -> tuple[float, float] | None:
    """(start, end) of ``phrase`` as a contiguous token run in ``words``, or
    None if it isn't found verbatim.

    Searches only from shortly before ``search_from`` (the payoff marker)
    onward, so an earlier, unrelated occurrence of the same words can't be
    matched instead. Returns None if the phrase isn't a contiguous token
    run in the transcript — the model paraphrased rather than quoting
    verbatim (confirmed to happen on real output), and no reliable check is
    possible then; callers should skip enforcement rather than guess.

    The single search implementation both _find_phrase_end and
    validate_segment's punchline_at check build on, so a start and an end
    derived from the same call always refer to the SAME located occurrence
    rather than two independent searches risking different matches.
    """
    tokens = [t for t in (_normalize_token(tok) for tok in phrase.split()) if t]
    if not tokens:
        return None
    candidates = [w for w in words if float(w["start"]) >= search_from - 1.0]
    norm_words = [_normalize_token(w["word"]) for w in candidates]
    n = len(tokens)
    for i in range(len(norm_words) - n + 1):
        if norm_words[i : i + n] == tokens:
            return float(candidates[i]["start"]), float(candidates[i + n - 1]["end"])
    return None


def _find_phrase_end(words: list[dict], phrase: str, search_from: float) -> float | None:
    """Where ``phrase``'s last word actually ends — see _find_phrase_span."""
    span = _find_phrase_span(words, phrase, search_from)
    return span[1] if span else None


def _distance_from_span(value: float, span_start: float, span_end: float) -> float:
    """0 if ``value`` falls inside [span_start, span_end], else how far
    outside it sits (from the nearer edge)."""
    if value < span_start:
        return span_start - value
    if value > span_end:
        return value - span_end
    return 0.0


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

    if abs(confidence - CONFIDENCE_FLOOR) <= CONFIDENCE_BORDERLINE_MARGIN:
        log.info(
            "select: %r confidence %.2f is within %.2f of the %.2f floor — a near-coin-flip "
            "call, accepted or not",
            hook,
            confidence,
            CONFIDENCE_BORDERLINE_MARGIN,
            CONFIDENCE_FLOOR,
        )

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
    phrase_span = None
    if payoff_quote:
        marker = _extract_payoff_marker(why) or start
        phrase_span = _find_phrase_span(words, payoff_quote, marker)
        if phrase_span is not None and phrase_span[1] > end:
            phrase_end = phrase_span[1]
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

    # punchline_at is DERIVED from the quoted payoff's own real transcript
    # span whenever that quote is locatable — not trusted as a separate,
    # independently-guessed timestamp from the model and merely sanity-
    # checked against the quote. Same posture as `end`'s extension above,
    # and the same reason: real repeated trials on one clip showed the
    # model's own punchline_at guess swinging 26.0-40.0 across identical
    # input (see PUNCHLINE_DRIFT_TOLERANCE's own comment) — a value that
    # unstable was never something to treat as correct-until-proven-wrong.
    # Set to the phrase's own start (where the payoff actually begins
    # landing), unconditionally, whenever `_find_phrase_span` locates it —
    # never gated on how close the model's guess already was, since ANY
    # amount of independent-guess noise is exactly what this removes. Only
    # falls back to the model's own value when the quote isn't locatable at
    # all (a paraphrased `why`, or genuinely unquotable content like a
    # musical payoff with no dialogue to quote — confirmed real case: the
    # ironmouse clip's `why` never once produced a matchable quote across
    # repeated trials, so this derivation cannot apply there; its punchline
    # instability is a structurally different problem, not something this
    # fix reaches).
    if phrase_span is not None:
        phrase_start, phrase_end = phrase_span
        if punchline_at != phrase_start:
            drift = _distance_from_span(punchline_at, phrase_start, phrase_end)
            log_fn = log.warning if drift > PUNCHLINE_DRIFT_TOLERANCE else log.info
            log_fn(
                "select: %r punchline_at=%.2f (model's own guess) replaced with %.2f — the "
                "quoted payoff's real start (%.1fs away)",
                hook,
                punchline_at,
                phrase_start,
                drift,
            )
            punchline_at = phrase_start

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

    # Null on both is a normal path (nothing worth adding), not an error.
    vo_script, on_screen_caption = resolve_narration(hook, raw, placement)

    # The mode is a judgment call, so it gets the same accountability every
    # other judgment field here has (`why`'s located timestamps,
    # `best_frame_reason` in visual_judge): the model has to say what it
    # traded. Warn-only — a thin reason is a signal to look at the prompt,
    # never a reason to throw away a real segment.
    narration_mode_reason = str(raw.get("narration_mode_reason", "")).strip()
    if not narration_mode_reason:
        log.warning("select: %r chose %s with no narration_mode_reason", hook, placement)

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

    title_style = str(raw.get("title_style", "")).strip().lower()
    if title_style not in ("concrete", "generic"):
        log.warning(
            "select: %r title_style is %r, want 'concrete' or 'generic' — keeping as-is",
            hook,
            title_style,
        )

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

    suggested_description = str(raw.get("suggested_description", "")).strip()
    if not suggested_description:
        log.warning("select: %r has no suggested_description", hook)
    else:
        sentence_count = _count_sentences(suggested_description)
        if not (DESCRIPTION_MIN_SENTENCES <= sentence_count <= DESCRIPTION_MAX_SENTENCES):
            log.warning(
                "select: %r suggested_description is %d sentence(s) (want %d-%d) — keeping as-is",
                hook,
                sentence_count,
                DESCRIPTION_MIN_SENTENCES,
                DESCRIPTION_MAX_SENTENCES,
            )
        if suggested_hashtags and not any(h in suggested_description for h in suggested_hashtags):
            log.warning(
                "select: %r suggested_description doesn't repeat any of suggested_hashtags %r "
                "— keeping as-is",
                hook,
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
        "on_screen_caption": on_screen_caption,
        "vo_placement": placement,
        "narration_mode_reason": narration_mode_reason,
        "why": why,
        "confidence": round(confidence, 2),
        "thumbnail_text": thumbnail_text,
        "suggested_title": suggested_title,
        "title_style": title_style,
        "suggested_hashtags": suggested_hashtags,
        "suggested_description": suggested_description,
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


def _call_model(
    client,
    video_id: str,
    system_text: str,
    user_text: str,
    model: str,
    *,
    disable_thinking: bool = False,
) -> dict:
    """The only networked call in this module. Returns the parsed JSON object.

    ``system_text`` is the prompt's fixed instructions (from
    split_cacheable_prompt) — byte-identical across every call to the same
    prompt file, sent as a cached system block so a batch's 2nd call onward
    reads it at ~0.1x instead of paying full price again. ``user_text`` is
    the real per-call Source/transcript data, which can never cache since
    it's unique every time.

    ``disable_thinking`` is select_clip's cost-priority path (SELECT_EFFORT's
    comment): claude-sonnet-5 accepts thinking={"type": "disabled"}, a real
    hard zero. select()'s long-form path leaves this False and keeps
    adaptive thinking at SELECT_EFFORT, unchanged — not in scope for the
    cost-priority switch. ``effort`` is only meaningful when thinking is on,
    so it's omitted entirely when disabled rather than sent alongside a
    setting it can't affect.
    """
    output_config: dict = {"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}}
    if disable_thinking:
        thinking: dict = {"type": "disabled"}
    else:
        thinking = {"type": "adaptive"}
        output_config["effort"] = SELECT_EFFORT
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking=thinking,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        output_config=output_config,
        messages=[{"role": "user", "content": user_text}],
    )
    _log_usage(video_id, model, response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError(f"select: model refused the request ({response.stop_details})")
    return json.loads(_extract_text_or_raise(response, f"select({video_id})"))


def _extract_text_or_raise(response, label: str) -> str:
    """The response's text block, or a clear error naming what actually
    happened — never the bare StopIteration a naive next(...) raises when
    there isn't one.

    Confirmed for real (not a hypothetical): a call can hit stop_reason
    "max_tokens" having spent its ENTIRE budget on thinking (a real observed
    out=8000/thinking=8000, zero text) even at capped effort — effort makes
    this rarer, it does not make it impossible, because it's a depth
    preference, not a hard ceiling (see SELECT_EFFORT's own comment: no
    numeric cap exists on this model tier). That specific case gets its own
    message because "the model thought itself out of budget" is a
    structurally different, actionable problem (MAX_TOKENS is too tight for
    this call) from every other reason a text block might be missing.
    """
    for block in response.content:
        if block.type == "text":
            return block.text
    if response.stop_reason == "max_tokens":
        thinking = (
            response.usage.output_tokens_details.thinking_tokens
            if response.usage.output_tokens_details
            else 0
        )
        raise RuntimeError(
            f"select: {label} hit max_tokens ({response.usage.output_tokens} output tokens, "
            f"{thinking} of them thinking) before producing any text — MAX_TOKENS={MAX_TOKENS} "
            "left no room for the actual response"
        )
    raise RuntimeError(
        f"select: {label} returned no text block (stop_reason={response.stop_reason!r})"
    )


def _call_batch_model(
    client, video_ids: list[str], system_text: str, user_text: str, model: str
) -> dict:
    """select_clip_batch's only networked call. Same shape as _call_model,
    logged distinctly (batch usage vs. single-call usage) since the whole
    point of batching is that ONE call now covers what used to be N —
    conflating the two log lines would hide that the cache-write cost is
    being paid once per batch instead of once per clip.

    select_clip_batch is exclusively part of the cost-priority select_clip
    path (there is no long-form batch entry point), so thinking is always
    disabled here — same real hard zero as _call_model's disable_thinking,
    not a parameter, because this call site has no other caller that would
    ever need it on.
    """
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": _BATCH_RESPONSE_SCHEMA}},
        messages=[{"role": "user", "content": user_text}],
    )
    _log_usage(f"batch[{','.join(video_ids)}]", model, response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError(f"select: batch model refused the request ({response.stop_details})")
    label = f"batch[{','.join(video_ids)}]"
    return json.loads(_extract_text_or_raise(response, label))


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
    dynamic_template, static_prompt = split_cacheable_prompt(template)
    meta = _source_metadata(wd)

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    collected: list[dict] = []
    for win in windows:
        user_prompt = render_prompt(
            dynamic_template,
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
        payload = _call_model(client, video_id, static_prompt, user_prompt, model)
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


def select_clip(video_id: str, *, force: bool = False, model: str = SELECT_CLIP_MODEL) -> dict:
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
    dynamic_template, static_prompt = split_cacheable_prompt(template)
    meta = _source_metadata(wd)
    tier, view_count = resolve_audience_tier(wd)

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    user_prompt = render_prompt(
        dynamic_template,
        window_index="1",
        window_total="1",
        window_start=format_hmmss(0.0),
        window_end=format_hmmss(clip_duration),
        transcript_window=render_transcript(words),
        audience_tier=describe_tier(tier, view_count),
        **meta,
    )
    log.info(
        "select_clip: %s (%.1fs, %d words, audience tier %s) -> 1 LLM call to %s",
        video_id,
        clip_duration,
        len(words),
        describe_tier(tier, view_count),
        model,
    )
    response = _call_model(
        client, video_id, static_prompt, user_prompt, model, disable_thinking=True
    )
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


def _strip_wrapper_output_example(static_prompt: str) -> tuple[str, str]:
    """Split select_clip.md's static half into (everything before the
    single-clip Output section, the "Field rules:" block onward).

    select_clip_batch needs the exact same judgment rules (Task, Narrator
    commentary, Narration mode, Tone, Loop potential, Thumbnail/upload) and
    the exact same per-field rules (word counts, what `confidence` means,
    etc.) — those apply identically per candidate in a batch. The ONLY thing
    that has to differ is the wrapper: single-clip's `{"segments": [...]}` at
    the top level vs. batch's `{"results": [{"clip_index": N, "segments":
    [...]}]}`. Reconstructing the rules text in a second file would risk
    exactly the stale-instruction drift this project has hit before (see
    CLAUDE.md's stale-instruction sweep note) — so the batch prompt is built
    by surgically swapping only the wrapper example, from the one real
    source file, every time it's assembled.
    """
    output_at = static_prompt.find("\n## Output\n")
    field_rules_at = static_prompt.find("\nField rules:\n")
    if output_at == -1 or field_rules_at == -1 or field_rules_at < output_at:
        raise ValueError(
            "_strip_wrapper_output_example: couldn't find both '## Output' and "
            "'Field rules:' in prompts/select_clip.md — the batch prompt can't "
            "be assembled safely without them"
        )
    before_output = static_prompt[:output_at]
    field_rules_onward = static_prompt[field_rules_at + 1 :]  # drop the leading \n
    return before_output, field_rules_onward


def _render_candidate_block(index: int, total: int, video_id: str) -> tuple[str, dict, float]:
    """One "### Candidate N of M" block for select_clip_batch's user turn,
    reusing select_clip.md's own per-clip dynamic template — the exact same
    Source/transcript rendering a single-clip call would use, just labeled
    and concatenated instead of sent alone.

    Returns (rendered_block, words, clip_duration). Raises FileNotFoundError
    if this video hasn't been transcribed — same precondition select_clip
    enforces for a single clip.
    """
    wd = work_dir(video_id)
    words_path = wd / "words.json"
    if not words_path.exists():
        raise FileNotFoundError(
            f"select_clip_batch: {words_path} missing — run stage 2 (transcribe) first."
        )
    payload = json.loads(words_path.read_text())
    words = payload.get("words", [])
    if not words:
        raise ValueError(f"select_clip_batch: {words_path} has no words")
    clip_duration = float(payload.get("duration") or words[-1]["end"])

    template = (PROMPTS_DIR / "select_clip.md").read_text()
    dynamic_template, _ = split_cacheable_prompt(template)
    meta = _source_metadata(wd)
    tier, view_count = resolve_audience_tier(wd)

    block = render_prompt(
        dynamic_template,
        window_index="1",
        window_total="1",
        window_start=format_hmmss(0.0),
        window_end=format_hmmss(clip_duration),
        transcript_window=render_transcript(words),
        audience_tier=describe_tier(tier, view_count),
        **meta,
    )
    header = f"### Candidate {index} of {total}\n\n"
    return header + block, words, clip_duration


def select_clip_batch(
    video_ids: list[str], *, force: bool = False, model: str = SELECT_CLIP_MODEL
) -> dict:
    """Batched select_clip: judge up to several pre-selected clips in ONE LLM
    call instead of one call per clip, each judged independently (see
    prompts/select_clip_batch.md's explicit anti-ranking instruction — this
    is not "pick the best of N").

    The real cost problem this solves: select_clip's fixed instructions run
    ~9,300 tokens, cached at $6.25/MTok on write and $0.50/MTok on read
    (Opus 5). Real measurement this session showed that cache write, not
    thinking, was landing repeatedly — calls spread minutes apart outlive the
    ephemeral cache's TTL and each pay the FULL write cost again. Batching N
    clips into one call pays that write once and amortizes it across all N,
    the same way a single call already amortizes its own fixed cost across
    its one candidate — just at a coarser grain.

    Same caching discipline as every other stage: a video_id whose
    segments.json already exists is skipped and its cached result returned,
    same as select_clip. Only the video_ids that actually need a call go
    into the batch — if every video_id is cached, no call happens at all.

    Returns {video_id: {"video_id": ..., "segments": [...]}, ...}, one entry
    per input video_id, same per-item shape select_clip returns.
    """
    if not video_ids:
        raise ValueError("select_clip_batch: no video_ids given")

    results: dict[str, dict] = {}
    to_call: list[str] = []

    for video_id in video_ids:
        wd = work_dir(video_id)
        segments_path = wd / "segments.json"
        if segments_path.exists() and not force:
            log.info(
                "select_clip_batch: cache hit for %s (%s), skipping the LLM",
                video_id,
                segments_path,
            )
            results[video_id] = json.loads(segments_path.read_text())
            continue
        to_call.append(video_id)

    if not to_call:
        return results

    blocks: list[str] = []
    words_by_id: dict[str, list[dict]] = {}
    below_floor: list[str] = []
    total = len(to_call)
    for i, video_id in enumerate(to_call, start=1):
        block, words, clip_duration = _render_candidate_block(i, total, video_id)
        if clip_duration < MIN_DURATION:
            log.warning(
                "select_clip_batch: %s is %.1fs, under the %.0fs floor — nothing to trim up to, "
                "rejecting without spending it a slot in the batch call",
                video_id,
                clip_duration,
                MIN_DURATION,
            )
            below_floor.append(video_id)
            continue
        blocks.append(block)
        words_by_id[video_id] = words

    # Below-floor candidates never got a rendered block above and are
    # rejected the same way select_clip rejects them: no model call spent.
    for video_id in below_floor:
        out = {"video_id": video_id, "segments": []}
        (work_dir(video_id) / "segments.json").write_text(json.dumps(out, indent=2))
        results[video_id] = out

    called_ids = [v for v in to_call if v not in below_floor]
    if not called_ids:
        return results

    template = (PROMPTS_DIR / "select_clip.md").read_text()
    _, static_prompt = split_cacheable_prompt(template)
    before_output, field_rules_onward = _strip_wrapper_output_example(static_prompt)
    batch_addendum = (PROMPTS_DIR / "select_clip_batch.md").read_text()
    # The single-clip Field rules' closing line talks about a top-level
    # `{"segments": []}` — true in spirit for a batch result too (a rejected
    # candidate's own "segments" is still []), but the shape is now nested
    # under that candidate's result object, not the top level. One targeted
    # swap rather than forking the whole Field rules block over one line.
    field_rules_onward = field_rules_onward.replace(
        'Return `{"segments": []}` if the clip doesn\'t qualify.',
        'That candidate\'s result gets `"segments": []` if it doesn\'t qualify — '
        "still inside its own result object, per the shape in Output above, "
        "never by omitting the result entirely.",
    )
    batch_static_prompt = before_output.rstrip() + "\n\n" + batch_addendum + field_rules_onward

    user_prompt = "\n\n".join(blocks)

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    log.info(
        "select_clip_batch: %d candidate(s) (%s) -> 1 LLM call to %s",
        len(called_ids),
        ", ".join(called_ids),
        model,
    )
    response = _call_batch_model(client, called_ids, batch_static_prompt, user_prompt, model)
    raw_results = response.get("results", [])

    seen_indices: set[int] = set()
    for raw_result in raw_results:
        clip_index = raw_result.get("clip_index")
        if not isinstance(clip_index, int) or not (1 <= clip_index <= len(called_ids)):
            log.warning(
                "select_clip_batch: result has out-of-range clip_index=%r for %d candidate(s) "
                "— dropped",
                clip_index,
                len(called_ids),
            )
            continue
        if clip_index in seen_indices:
            log.warning(
                "select_clip_batch: duplicate clip_index=%d — later result dropped", clip_index
            )
            continue
        seen_indices.add(clip_index)

        video_id = called_ids[clip_index - 1]
        words = words_by_id[video_id]
        raw_segments = raw_result.get("segments", [])

        collected: list[dict] = []
        for raw in raw_segments:
            seg = validate_segment(raw, words)
            if seg is not None:
                collected.append(seg)
        for n, seg in enumerate(collected):
            seg["n"] = n

        out = {"video_id": video_id, "segments": collected}
        (work_dir(video_id) / "segments.json").write_text(json.dumps(out, indent=2))
        log.info("select_clip_batch: wrote %d segment(s) for %s", len(collected), video_id)
        results[video_id] = out

    missing = [v for i, v in enumerate(called_ids, start=1) if i not in seen_indices]
    for video_id in missing:
        log.warning(
            "select_clip_batch: %s got no result back from the model — treating as reject, "
            "not caching (so a re-run retries it rather than silently sticking with empty)",
            video_id,
        )
        results[video_id] = {"video_id": video_id, "segments": []}

    return results
