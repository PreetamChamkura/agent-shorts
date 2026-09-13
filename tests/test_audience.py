"""Unit tests for the audience tier — the view-count signal both judgment
stages read to decide whether they're judging for a cold stranger or for an
audience that already recognizes the moment.

The threshold itself is checked against the real data it was derived from
(see clipper.audience's module docstring): the two clusters either side of
the single 20.4x gap in the 2026-09-11 batch must land on opposite tiers, or
the number has drifted away from what justified it.
"""

from __future__ import annotations

import json
import logging

from clipper.audience import (
    TIER_HIGH,
    TIER_STANDARD,
    VIEW_COUNT_HIGH_TIER,
    describe_tier,
    resolve_audience_tier,
    source_view_count,
    tier_for_view_count,
)

# --- the threshold, against the real distribution it came from --------------


def test_threshold_separates_the_two_real_clusters():
    # Real boundary values from the 2026-09-11 batch: the lower cluster's
    # ceiling and the upper cluster's floor, either side of the only
    # order-of-magnitude gap in 33 candidates.
    assert tier_for_view_count(23_341) == TIER_STANDARD
    assert tier_for_view_count(476_567) == TIER_HIGH


def test_threshold_sits_inside_that_gap():
    assert 23_341 < VIEW_COUNT_HIGH_TIER < 476_567


def test_tier_boundary_is_inclusive():
    assert tier_for_view_count(VIEW_COUNT_HIGH_TIER) == TIER_HIGH
    assert tier_for_view_count(VIEW_COUNT_HIGH_TIER - 1) == TIER_STANDARD


def test_real_viral_clip_is_high():
    # The "nahh" clip — 17.7M views, layer 3's real reject on record.
    assert tier_for_view_count(17_755_365) == TIER_HIGH


def test_unknown_view_count_is_standard_never_high():
    # An absent signal must not silently lower the bar — STANDARD is the
    # existing behaviour, so unknown changes nothing.
    assert tier_for_view_count(None) == TIER_STANDARD


# --- reading the count off disk --------------------------------------------


def _wd(tmp_path, info: dict | None, name: str = "vid1"):
    wd = tmp_path / name
    wd.mkdir(exist_ok=True)
    if info is not None:
        (wd / "source.info.json").write_text(json.dumps(info))
    return wd


def test_source_view_count_reads_the_real_field(tmp_path):
    wd = _wd(tmp_path, {"view_count": 17_755_365, "title": "nahh"})
    assert source_view_count(wd) == 17_755_365


def test_source_view_count_missing_info_json_is_none(tmp_path):
    assert source_view_count(_wd(tmp_path, None)) is None


def test_source_view_count_absent_field_is_none(tmp_path):
    assert source_view_count(_wd(tmp_path, {"title": "no count here"})) is None


def test_source_view_count_malformed_json_warns_and_returns_none(tmp_path, caplog):
    wd = tmp_path / "vid1"
    wd.mkdir()
    (wd / "source.info.json").write_text("{not json at all")
    with caplog.at_level(logging.WARNING):
        assert source_view_count(wd) is None
    assert "unreadable" in caplog.text


def test_source_view_count_non_numeric_warns_and_returns_none(tmp_path, caplog):
    wd = _wd(tmp_path, {"view_count": "loads"})
    with caplog.at_level(logging.WARNING):
        assert source_view_count(wd) is None
    assert "non-numeric" in caplog.text


def test_resolve_audience_tier_returns_both_halves(tmp_path):
    big = _wd(tmp_path, {"view_count": 500_000}, name="big")
    small = _wd(tmp_path, {"view_count": 900}, name="small")
    assert resolve_audience_tier(big) == (TIER_HIGH, 500_000)
    assert resolve_audience_tier(small) == (TIER_STANDARD, 900)


def test_resolve_audience_tier_without_info_json_is_standard(tmp_path):
    # The long-form YouTube path and any pre-existing work dir land here.
    assert resolve_audience_tier(_wd(tmp_path, None)) == (TIER_STANDARD, None)


# --- what the model is actually told ---------------------------------------


def test_describe_tier_names_the_real_number():
    # The label alone would be an assertion the model has to take on trust.
    out = describe_tier(TIER_HIGH, 17_755_365)
    assert "HIGH" in out
    assert "17,755,365" in out


def test_describe_tier_says_so_when_the_count_is_unknown():
    out = describe_tier(TIER_STANDARD, None)
    assert "STANDARD" in out
    assert "unknown" in out
