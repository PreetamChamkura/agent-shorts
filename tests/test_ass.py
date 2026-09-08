"""Unit tests for the pure ASS generator. No ffmpeg, no network, no files."""

from __future__ import annotations

import re

from clipper.ass import (
    DEFAULT_STYLE,
    HELD_FONTSIZE,
    HELD_MARGIN_BOTTOM,
    HELD_MARGIN_TOP,
    HELD_MARGIN_X,
    HELD_MAX_CHARS_PER_LINE,
    HELD_STYLE_NAME,
    build_ass,
    format_ass_time,
    group_words,
    held_caption_duration,
    held_caption_style,
    wrap_held_text,
)


def test_format_ass_time_basics():
    assert format_ass_time(0) == "0:00:00.00"
    assert format_ass_time(1.5) == "0:00:01.50"
    assert format_ass_time(61.234) == "0:01:01.23"
    assert format_ass_time(3661.9) == "1:01:01.90"


def test_format_ass_time_clamps_negative():
    assert format_ass_time(-3) == "0:00:00.00"


def test_group_words_by_size():
    words = [
        {"word": w, "start": i, "end": i + 0.5} for i, w in enumerate(["a", "b", "c", "d", "e"])
    ]
    groups = group_words(words, 3)
    assert [len(g) for g in groups] == [3, 2]


def test_group_words_breaks_on_sentence_end():
    words = [
        {"word": "so", "start": 0, "end": 0.4},
        {"word": "yeah.", "start": 0.5, "end": 0.9},
        {"word": "then", "start": 1.0, "end": 1.4},
    ]
    groups = group_words(words, 5)  # size would keep them together; punctuation splits
    assert [[w["word"] for w in g] for g in groups] == [["so", "yeah."], ["then"]]


def test_group_words_rejects_bad_size():
    try:
        group_words([], 0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for group_size 0")


def _words():
    return [
        {"word": "he", "start": 0.0, "end": 0.30},
        {"word": "had", "start": 0.30, "end": 0.60},
        {"word": "no", "start": 0.60, "end": 0.90},
        {"word": "idea", "start": 0.90, "end": 1.40},
    ]


def test_build_ass_header_and_style():
    out = build_ass(_words(), 3)
    assert "ScriptType: v4.00+" in out
    assert "PlayResX: 1080" in out
    assert "PlayResY: 1920" in out
    # Spec Pop style line reproduced from DEFAULT_STYLE.
    assert (
        "Style: Pop,Montserrat ExtraBold,104,&H00FFFFFF,&H000000FF,&H00000000,"
        "&HA0000000,1,0,0,0,100,100,0,0,1,9,5,5,70,70,0,1" in out
    )


def test_build_ass_one_dialogue_per_word():
    out = build_ass(_words(), 3)
    dialogues = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == len(_words())


def test_build_ass_highlights_active_word_only():
    out = build_ass(_words(), 4)  # single group of 4
    dialogues = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
    # Each line highlights exactly one word with the animation override.
    for ln in dialogues:
        assert ln.count("\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)") == 1
        assert "&H0000E5FF" in ln  # yellow highlight
    # Words are uppercased in the pop style.
    assert "IDEA" in out and "idea" not in out


def test_build_ass_phrase_stays_visible():
    # In a group, a word's line ends when the next word begins (phrase persists).
    out = build_ass(_words(), 4)
    first = next(ln for ln in out.splitlines() if ln.startswith("Dialogue:"))
    # first word 0.00 -> next word start 0.30
    assert ",0:00:00.00,0:00:00.30," in first


def test_build_ass_word_not_visible_before_its_own_start():
    # Regression: a group's later words must not render — not even
    # unhighlighted — before their own spoken time. A real gap between
    # words (natural speech) used to leave them sitting on screen early.
    group = [
        {"word": "Hey,", "start": 0.0, "end": 0.36},
        {"word": "what's", "start": 3.54, "end": 3.60},
        {"word": "going", "start": 3.60, "end": 3.72},
    ]
    out = build_ass(group, 3)
    dialogues = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]

    # First line spans the whole 3.54s gap before "what's" is spoken —
    # neither later word may appear in its text at all.
    first = dialogues[0]
    assert ",0:00:00.00,0:00:03.54," in first
    assert "WHAT'S" not in first
    assert "GOING" not in first
    assert "HEY," in first

    # Second line begins exactly when "what's" is spoken, and now shows
    # both words spoken so far, but not the third.
    second = dialogues[1]
    assert ",0:00:03.54,0:00:03.60," in second
    assert "HEY," in second
    assert "WHAT'S" in second
    assert "GOING" not in second

    # Third line: all three words now spoken, all visible.
    third = dialogues[2]
    assert "HEY," in third and "WHAT'S" in third and "GOING" in third


def test_build_ass_accumulates_progressively_for_larger_groups():
    # Same invariant holds for group sizes beyond 3: word i's line must
    # contain exactly i+1 words (0..i), never more.
    words = _words()  # 4 words: he, had, no, idea.
    out = build_ass(words, 4)
    dialogues = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 4
    # crude word count per line: split on the plain-text uppercase tokens
    expected_word_counts = [1, 2, 3, 4]
    for line, expected in zip(dialogues, expected_word_counts, strict=True):
        text = line.split(",,", 1)[1]
        bare = re.sub(r"\{[^}]*\}", "", text)  # strip ASS override tags
        assert len(bare.split()) == expected


def test_build_ass_empty_words():
    out = build_ass([], 3)
    assert "[Events]" in out
    assert not [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]


# --- held caption (caption_only narration mode) ------------------------------


_HELD_LINE = "The most-searched woman on the internet came over to enter a guacamole contest."


def test_held_caption_duration_scales_with_length_and_clamps():
    # ~0.4s/word + 1.0s lead-out, bounded 2.5-8.0 (clipper.ass).
    assert held_caption_duration("three words here") == 2.5  # floor, not 2.2
    assert held_caption_duration(_HELD_LINE) == 6.2  # 13 words
    assert held_caption_duration(" ".join(["word"] * 40)) == 8.0  # ceiling


def test_wrap_held_text_breaks_on_ass_hard_breaks():
    wrapped = wrap_held_text(_HELD_LINE)
    assert "\\N" in wrapped
    # Wrapping never loses or reorders words, and never splits one.
    assert wrapped.replace("\\N", " ").split() == _HELD_LINE.split()
    assert all(len(line) <= HELD_MAX_CHARS_PER_LINE for line in wrapped.split("\\N"))


def test_wrap_held_text_leaves_a_short_line_alone():
    assert wrap_held_text("short enough") == "short enough"


def test_wrap_held_text_keeps_an_overlong_word_whole():
    # A single word longer than the limit gets its own line rather than being cut.
    assert wrap_held_text("a supercalifragilisticexpialidocious b", 10) == (
        "a\\Nsupercalifragilisticexpialidocious\\Nb"
    )


def test_held_caption_style_goes_to_the_top_band_under_a_middle_karaoke():
    # Five of six tones run the karaoke middle-centred (alignment 5).
    st = held_caption_style({"alignment": 5, "marginv": 0})
    assert st["alignment"] == 8  # top-centre
    assert st["marginv"] == HELD_MARGIN_TOP


def test_held_caption_style_goes_to_the_bottom_band_under_a_top_karaoke():
    # `chaotic` puts the karaoke top-centred, so the held line must move away.
    st = held_caption_style({"alignment": 8, "marginv": 260})
    assert st["alignment"] == 2  # bottom-centre
    assert st["marginv"] == HELD_MARGIN_BOTTOM


def test_held_caption_style_inherits_the_tone_colour_but_not_its_size_or_case():
    st = held_caption_style({"primary": "&H00D0FDFF", "alignment": 5})
    assert st["primary"] == "&H00D0FDFF"  # wholesome cream, from the same tone block
    assert st["fontname"] == DEFAULT_STYLE["fontname"]
    assert st["fontsize"] == HELD_FONTSIZE  # not the karaoke 104
    assert st["uppercase"] is False
    assert st["marginl"] == st["marginr"] == HELD_MARGIN_X


def test_held_caption_style_scales_outline_and_shadow_with_the_font():
    st = held_caption_style({"alignment": 5})
    # 9 and 5 are tuned for 104px glyphs; kept literally they'd swallow 56px ones.
    assert st["outline"] == 5
    assert st["shadow"] == 3
    assert st["outline"] < DEFAULT_STYLE["outline"]


def test_build_ass_with_held_emits_a_second_style_and_one_static_line():
    out = build_ass(_words(), 3, None, {"text": _HELD_LINE, "start": 0.0, "end": 6.2})
    styles = [ln for ln in out.splitlines() if ln.startswith("Style:")]
    assert len(styles) == 2
    assert styles[0].startswith("Style: Pop,")
    assert styles[1].startswith(f"Style: {HELD_STYLE_NAME},")

    held_lines = [
        ln for ln in out.splitlines() if ln.startswith("Dialogue: 0,") and HELD_STYLE_NAME in ln
    ]
    assert len(held_lines) == 1
    assert held_lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:06.20,Held,")
    # Static: no highlight recolour, no scale animation, unlike every karaoke line.
    assert "\\t(" not in held_lines[0]
    assert "\\fscx" not in held_lines[0]


def test_build_ass_without_held_is_byte_identical_to_before():
    # The held caption must be strictly additive — no drift for the VO/silent modes.
    assert build_ass(_words(), 3) == build_ass(_words(), 3, None, None)
    assert "Style: Held" not in build_ass(_words(), 3)
