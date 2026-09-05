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


def build_ass(
    words: Sequence[Word], group_size: int, style: dict[str, object] | None = None
) -> str:
    """Return the full ASS file contents for ``words``.

    Pure: same inputs always yield the same string. ``words`` carry
    clip-relative start/end seconds. ``group_size`` is the phrase-group length.
    ``style`` overrides :data:`DEFAULT_STYLE` field-by-field.
    """
    st = {**DEFAULT_STYLE, **(style or {})}
    highlight = str(st["highlight"])
    uppercase = bool(st["uppercase"])

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
        f"{_style_line(st)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    style_name = str(st["name"])
    lines: list[str] = []
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
