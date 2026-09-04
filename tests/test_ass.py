"""Unit tests for the pure ASS generator. No ffmpeg, no network, no files."""

from __future__ import annotations

from clipper.ass import build_ass, format_ass_time, group_words


def test_format_ass_time_basics():
    assert format_ass_time(0) == "0:00:00.00"
    assert format_ass_time(1.5) == "0:00:01.50"
    assert format_ass_time(61.234) == "0:01:01.23"
    assert format_ass_time(3661.9) == "1:01:01.90"


def test_format_ass_time_clamps_negative():
    assert format_ass_time(-3) == "0:00:00.00"


def test_group_words_by_size():
    words = [{"word": w, "start": i, "end": i + 0.5} for i, w in enumerate("a b c d e".split())]
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


def test_build_ass_empty_words():
    out = build_ass([], 3)
    assert "[Events]" in out
    assert not [ln for ln in out.splitlines() if ln.startswith("Dialogue:")]
