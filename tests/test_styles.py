"""Unit tests for the tone -> style lookup. No ffmpeg, no network."""

from __future__ import annotations

import pytest

from clipper.styles import UnknownToneError, load_styles, style_for_tone

EXPECTED_TONES = {"burn", "chaotic", "wholesome", "awkward", "revealing", "confident"}


def test_all_six_tones_present():
    assert set(load_styles()) == EXPECTED_TONES


@pytest.mark.parametrize("tone", sorted(EXPECTED_TONES))
def test_every_tone_has_a_group_size(tone):
    _, group_size = style_for_tone(tone)
    assert isinstance(group_size, int)
    assert group_size >= 1


def test_unknown_tone_raises_loudly():
    with pytest.raises(UnknownToneError):
        style_for_tone("nonexistent_tone")


def test_burn_highlight_is_burn_red():
    overrides, group_size = style_for_tone("burn")
    assert overrides["highlight"] == "&H003B3BE2"
    assert group_size == 3


def test_revealing_highlight_is_amber():
    overrides, group_size = style_for_tone("revealing")
    assert overrides["highlight"] == "&H00279FEF"
    assert group_size == 3


def test_chaotic_is_high_position_two_word_groups():
    overrides, group_size = style_for_tone("chaotic")
    assert group_size == 2
    assert overrides["alignment"] == 8
    assert "highlight" not in overrides  # keeps house default yellow


def test_wholesome_is_soft_cream_four_word_groups():
    overrides, group_size = style_for_tone("wholesome")
    assert group_size == 4
    assert overrides["primary"] != "&H00FFFFFF"  # not plain white


def test_awkward_is_five_word_groups():
    _, group_size = style_for_tone("awkward")
    assert group_size == 5


def test_confident_is_heavier_outline_three_word_groups():
    overrides, group_size = style_for_tone("confident")
    assert group_size == 3
    assert overrides["outline"] > 9  # heavier than the house baseline


def test_style_for_tone_does_not_leak_group_size_into_overrides():
    overrides, _ = style_for_tone("burn")
    assert "group_size" not in overrides
