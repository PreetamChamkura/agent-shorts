"""Unit tests for the pure ASS generator. No ffmpeg, no network, no files."""

from __future__ import annotations

import re

import pytest

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


# --- width-based early split (real bug: awkward's 5-word groups overflowed) --


def _long_words():
    # The real transcript group that overflowed off both frame edges on a
    # real render before this fix — see clipper.ass module docstring.
    return [
        {"word": "forms", "start": 0.0, "end": 0.5},
        {"word": "from", "start": 0.5, "end": 0.9},
        {"word": "the", "start": 0.9, "end": 1.1},
        {"word": "anthropological", "start": 1.1, "end": 2.0},
        {"word": "approach.", "start": 2.0, "end": 2.6},
    ]


# Default style: fontsize 104, marginl/marginr 70/70 -> 940px usable width.
_DEFAULT_USABLE_WIDTH = 1080 - 70 - 70
_DEFAULT_FONTSIZE = 104.0


def test_group_words_requires_width_and_fontsize_together():
    with pytest.raises(ValueError, match="together"):
        group_words(_long_words(), 5, max_width_px=940)
    with pytest.raises(ValueError, match="together"):
        group_words(_long_words(), 5, fontsize=104)


def test_group_words_without_width_args_is_unchanged():
    # No max_width_px/fontsize -> pure count-based grouping, exactly as
    # before this fix existed (every pre-existing caller and test).
    groups = group_words(_long_words(), 5)
    assert [len(g) for g in groups] == [5]


def test_group_words_splits_early_when_the_group_would_overflow():
    # The real overflow case: 5 words, group_size=5 (awkward's own config),
    # at the real karaoke fontsize/margins. Must not stay a single group.
    groups = group_words(
        _long_words(), 5, max_width_px=_DEFAULT_USABLE_WIDTH, fontsize=_DEFAULT_FONTSIZE
    )
    assert len(groups) > 1
    assert all(len(g) < 5 for g in groups)
    # Still every word, still in order — a split, never a drop.
    assert [w["word"] for g in groups for w in g] == [w["word"] for w in _long_words()]


def test_group_words_width_split_never_exceeds_group_size():
    # The width check can only make a group smaller than group_size, never
    # larger — group_size is still the ceiling.
    groups = group_words(
        _long_words(), 5, max_width_px=_DEFAULT_USABLE_WIDTH, fontsize=_DEFAULT_FONTSIZE
    )
    assert all(1 <= len(g) <= 5 for g in groups)


def test_group_words_short_words_are_unaffected_by_the_width_check():
    # An ordinary short group must not be split just because the width check
    # is switched on — only genuinely long groups should ever trigger it.
    words = [
        {"word": w, "start": i, "end": i + 0.3} for i, w in enumerate(["he", "had", "no", "idea"])
    ]
    groups = group_words(
        words, 4, max_width_px=_DEFAULT_USABLE_WIDTH, fontsize=_DEFAULT_FONTSIZE
    )
    assert len(groups) == 1
    assert len(groups[0]) == 4


def test_group_words_width_check_still_honors_sentence_end():
    # Sentence-end punctuation and the width check are independent triggers;
    # a short sentence-ending group must still flush on the period alone.
    words = [
        {"word": "so", "start": 0, "end": 0.4},
        {"word": "yeah.", "start": 0.5, "end": 0.9},
        {"word": "then", "start": 1.0, "end": 1.4},
    ]
    groups = group_words(
        words, 5, max_width_px=_DEFAULT_USABLE_WIDTH, fontsize=_DEFAULT_FONTSIZE
    )
    assert [[w["word"] for w in g] for g in groups] == [["so", "yeah."], ["then"]]


def test_build_ass_awkward_style_splits_the_real_overflow_group():
    # Integration: build_ass always resolves the real style's margins/
    # fontsize and passes them through — awkward's own styles.yaml config
    # (group_size=5, no margin override) is exactly the real bug's shape.
    out = build_ass(_long_words(), 5, {"primary": "&H00FFFFFF"})
    # 5 words, one Dialogue line per word as always — but the LAST line
    # (all 5 words spoken) must not contain all 5 words' plain text if the
    # group was split; check the final line's text has fewer than 5 tokens.
    dialogues = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
    last_text = dialogues[-1].split(",,", 1)[1]
    bare = re.sub(r"\{[^}]*\}", "", last_text)
    assert len(bare.split()) < 5


def test_build_ass_without_width_info_would_have_overflowed_regression():
    # Documents the actual bug this fix closes: forcing the OLD, count-only
    # behavior (bypassing build_ass's own width resolution) on the same real
    # words produces one ungrouped 5-word line — the confirmed-overflowing
    # shape from the real render before this fix.
    groups = group_words(_long_words(), 5)
    assert len(groups) == 1
    assert len(groups[0]) == 5


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
    #
    # Deliberately short words: this test is about the visibility-timing
    # invariant, not width-based splitting (see the "FORMS FROM THE
    # ANTHROPOLOGICAL APPROACH" tests below) — long enough words at
    # group_size 3 can now legitimately split the group early, which would
    # make this test about two things at once.
    group = [
        {"word": "Hi,", "start": 0.0, "end": 0.36},
        {"word": "ok", "start": 3.54, "end": 3.60},
        {"word": "now", "start": 3.60, "end": 3.72},
    ]
    out = build_ass(group, 3)
    dialogues = [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 3  # confirms no width-based split fired here

    # First line spans the whole 3.54s gap before "ok" is spoken — neither
    # later word may appear in its text at all.
    first = dialogues[0]
    assert ",0:00:00.00,0:00:03.54," in first
    assert "OK" not in first
    assert "NOW" not in first
    assert "HI," in first

    # Second line begins exactly when "ok" is spoken, and now shows both
    # words spoken so far, but not the third.
    second = dialogues[1]
    assert ",0:00:03.54,0:00:03.60," in second
    assert "HI," in second
    assert "OK" in second
    assert "NOW" not in second

    # Third line: all three words now spoken, all visible.
    third = dialogues[2]
    assert "HI," in third and "OK" in third and "NOW" in third


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
