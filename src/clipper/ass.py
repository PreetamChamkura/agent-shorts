"""ASS caption generation.

`build_ass` is a PURE function: word list + phrase group size + style dict in,
ASS file contents out. No ffmpeg, no filesystem, no network — unit-testable in
isolation. Times in the word list are clip-relative seconds (the render stage
shifts absolute transcript times before calling this).

Layout, colours, and the per-word highlight animation follow the "ASS captions"
section of CLAUDE.md exactly. One Dialogue line per word; the phrase group
builds up progressively as each word is spoken (a word never renders — not
even unhighlighted — before its own start time) and the active word recolours
to the tone highlight while it scales 118% -> 100% over 90ms.

`build_ass` also carries the optional held caption for the `caption_only`
narration mode (CLAUDE.md "Narration modes"): one static, non-animated line of
narrator framing that shares this file with the karaoke captions rather than
living in a separate drawtext pass. It gets its own [V4+ Styles] entry derived
from the SAME tone block (`held_caption_style`), so it stays a tone lookup and
never a second styling system — see that function for exactly which properties
are inherited and which are deliberately overridden.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

# Sentence-ending punctuation triggers an early phrase-group break (CLAUDE.md:
# "Break phrase groups on sentence-ending punctuation ... even when short").
_SENTENCE_END = (".", "!", "?", "…")


class Word(TypedDict):
    word: str
    start: float  # clip-relative seconds
    end: float


# House baseline ("Pop" style from CLAUDE.md). Every tone in styles.yaml only
# states its deltas from this dict; clipper.styles.style_for_tone supplies the
# override, build_ass merges it in. An empty override reproduces this exactly.
DEFAULT_STYLE: dict[str, object] = {
    "name": "Pop",
    "fontname": "Montserrat ExtraBold",
    "fontsize": 104,
    "primary": "&H00FFFFFF",  # white
    "highlight": "&H0000E5FF",  # yellow, active word
    "alignment": 5,  # middle-center
    "marginv": 0,
    "marginl": 70,
    "marginr": 70,
    "outline": 9,  # do not reduce
    "shadow": 5,
    "bold": 1,
    "uppercase": True,  # pop-caption look; matches uppercase caption_title convention
}


def format_ass_time(seconds: float) -> str:
    """Seconds -> ASS timestamp `H:MM:SS.cc` (centiseconds, single-digit hour)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = round(seconds * 100)
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def group_words(words: Sequence[Word], group_size: int) -> list[list[Word]]:
    """Chunk words into phrase groups.

    Flush a group when it reaches ``group_size`` OR when a word ends a sentence
    (trailing . ! ? …), whichever comes first.
    """
    if group_size < 1:
        raise ValueError(f"group_size must be >= 1, got {group_size}")

    groups: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        current.append(w)
        ends_sentence = w["word"].rstrip().endswith(_SENTENCE_END)
        if len(current) >= group_size or ends_sentence:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _style_line(style: dict[str, object]) -> str:
    # Invariant fields from the spec's Pop line; variable fields from `style`.
    return (
        f"Style: {style['name']},{style['fontname']},{style['fontsize']},"
        f"{style['primary']},&H000000FF,&H00000000,&HA0000000,"
        f"{style['bold']},0,0,0,100,100,0,0,1,"
        f"{style['outline']},{style['shadow']},{style['alignment']},"
        f"{style['marginl']},{style['marginr']},{style['marginv']},1"
    )


def _dialogue_text(group: list[Word], active: int, highlight: str, uppercase: bool) -> str:
    parts: list[str] = []
    for i, w in enumerate(group):
        token = w["word"].strip()
        if uppercase:
            token = token.upper()
        if i == active:
            # Recolour + scale 118->100 over 90ms, then {\r} back to style default
            # so the following words render plain.
            parts.append(
                f"{{\\c{highlight}&\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)}}{token}{{\\r}}"
            )
        else:
            parts.append(token)
    return " ".join(parts)


class HeldCaption(TypedDict):
    """One static line of narrator framing, held on screen (caption_only)."""

    text: str
    start: float  # clip-relative seconds
    end: float


# Held-caption geometry. Every number here is derived from CLAUDE.md's safe
# zones and from where the karaoke line actually sits, so the two text blocks
# can never occupy the same band — see held_caption_style for the derivation.
HELD_STYLE_NAME = "Held"
HELD_FONTSIZE = 56  # secondary to the karaoke's 104; a read line, not a punch
HELD_MARGIN_X = 180  # x 180-900: inside CLAUDE.md's x 0-900, still centred on 540
HELD_MARGIN_TOP = 250  # top safe edge
HELD_MARGIN_BOTTOM = 580  # 1920-1340: block bottom 60px clear of the y=1400 watermark

# WrapStyle 2 means libass does NOT wrap for us — only an explicit \N breaks a
# line. The karaoke groups are 2-5 words and have always been short enough for
# that to go unnoticed; a held line of 8-20 words is not, so it is wrapped here,
# deterministically, instead of hoping the renderer does it.
# 720px of usable width / (56px * ~0.58em average advance for Montserrat
# ExtraBold in sentence case) ~= 22 characters. Confirmed against a real render.
HELD_MAX_CHARS_PER_LINE = 22

# Reading pace for the hold window: ~150 wpm (0.4s/word) rather than a silent
# reader's ~250, because this line competes with moving video and the clip's
# own karaoke captions, plus a second to notice it at all. Bounded so a
# three-word line still lands and a long one still gets out of the way — the
# persona's own "set up, then get out" rule applies to a held line too.
HELD_SECONDS_PER_WORD = 0.4
HELD_LEAD_OUT_SECONDS = 1.0
HELD_MIN_SECONDS = 2.5
HELD_MAX_SECONDS = 8.0


def held_caption_duration(text: str) -> float:
    """How long a held caption stays on screen, derived from its own length.

    Deliberately computed from the text rather than taken as another
    model-supplied timestamp: this pipeline has already had two real
    timestamp-drift bugs (`end` short of the payoff quote, `punchline_at`
    5.3s off it), and a hold window is the one piece of timing that does not
    need the model's opinion at all.
    """
    words = len(text.split())
    raw = words * HELD_SECONDS_PER_WORD + HELD_LEAD_OUT_SECONDS
    return round(min(max(raw, HELD_MIN_SECONDS), HELD_MAX_SECONDS), 2)


def wrap_held_text(text: str, max_chars: int = HELD_MAX_CHARS_PER_LINE) -> str:
    r"""Greedy word-wrap into ASS hard breaks (``\N``). See HELD_MAX_CHARS_PER_LINE."""
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\\N".join(lines)


def held_caption_style(base: dict[str, object] | None = None) -> dict[str, object]:
    """Derive the held caption's style from the SAME tone block the karaoke uses.

    Inherited from the tone, so this stays a lookup and never a second styling
    system: font family, `primary` colour, and `bold`. Everything else is
    deliberately overridden, and each override is a decision:

    - `fontsize` 56, not 104. A 20-word line at 104 is six lines of shouting.
    - `alignment`/`marginv` put it in the band the karaoke is NOT in. Every tone
      but `chaotic` runs the karaoke middle-centred (worst case y 772-1147 for a
      three-line group), so the held line goes to the top band from y=250.
      `chaotic` runs its karaoke top-centred (y 260-510), so its held line goes
      to the bottom band instead, ending at y=1340. The two blocks are always in
      different thirds of the frame — for any tone, present together or not.
    - `marginl`/`marginr` 180, not 70: a line held for seconds has to clear the
      right-hand like/comment/share rail (CLAUDE.md's x 0-900), which a
      single karaoke word flashing past does not.
    - `outline`/`shadow` scale with the smaller font (9 and 5 are tuned for
      104px glyphs; kept literally they would swallow 56px ones).
    - `highlight` is unused — nothing animates — and `uppercase` is off: this is
      a sentence of context, and the spoken equivalent is not shouted either.
    """
    st = {**DEFAULT_STYLE, **(base or {})}
    karaoke_at_top = int(st["alignment"]) in (7, 8, 9)
    scale = HELD_FONTSIZE / float(DEFAULT_STYLE["fontsize"])  # type: ignore[arg-type]
    return {
        **st,
        "name": HELD_STYLE_NAME,
        "fontsize": HELD_FONTSIZE,
        "alignment": 2 if karaoke_at_top else 8,
        "marginv": HELD_MARGIN_BOTTOM if karaoke_at_top else HELD_MARGIN_TOP,
        "marginl": HELD_MARGIN_X,
        "marginr": HELD_MARGIN_X,
        "outline": round(int(st["outline"]) * scale),  # type: ignore[arg-type]
        "shadow": round(int(st["shadow"]) * scale),  # type: ignore[arg-type]
        "uppercase": False,
    }


def build_ass(
    words: Sequence[Word],
    group_size: int,
    style: dict[str, object] | None = None,
    held: HeldCaption | None = None,
) -> str:
    """Return the full ASS file contents for ``words``.

    Pure: same inputs always yield the same string. ``words`` carry
    clip-relative start/end seconds. ``group_size`` is the phrase-group length.
    ``style`` overrides :data:`DEFAULT_STYLE` field-by-field.

    ``held`` adds one static, non-animated line of narrator framing in its own
    style and its own band (``caption_only`` — see :func:`held_caption_style`).
    It is never combined with a cold_open VO: those are mutually exclusive
    narration modes, so nothing here has to reconcile the two.
    """
    st = {**DEFAULT_STYLE, **(style or {})}
    highlight = str(st["highlight"])
    uppercase = bool(st["uppercase"])

    style_lines = [_style_line(st)]
    if held is not None:
        style_lines.append(_style_line(held_caption_style(style)))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        + "\n".join(style_lines)
        + "\n"
        "\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    style_name = str(st["name"])
    lines: list[str] = []
    if held is not None:
        # Emitted first so it sits under the karaoke line in ASS draw order.
        # They never share a band (held_caption_style), so this only matters
        # if the geometry is ever loosened.
        lines.append(
            f"Dialogue: 0,{format_ass_time(held['start'])},{format_ass_time(held['end'])},"
            f"{HELD_STYLE_NAME},,0,0,0,,{wrap_held_text(held['text'].strip())}"
        )
    for group in group_words(words, group_size):
        for i, w in enumerate(group):
            # Keep spoken-so-far words visible: this word's line runs until
            # the next word starts; the last word runs to its own end.
            start = float(w["start"])
            end = float(group[i + 1]["start"]) if i + 1 < len(group) else float(w["end"])
            if end < start:
                end = start
            # Only words spoken so far (0..i) render at all — a word must
            # not appear on screen, even unhighlighted, before its own start
            # time. Passing the full group here was the bug: whenever a
            # group's first word was followed by a real gap in natural
            # speech, words 2/3 sat on screen for that whole gap before
            # either was actually said.
            text = _dialogue_text(group[: i + 1], i, highlight, uppercase)
            lines.append(
                f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},"
                f"{style_name},,0,0,0,,{text}"
            )

    return header + "\n".join(lines) + ("\n" if lines else "")
