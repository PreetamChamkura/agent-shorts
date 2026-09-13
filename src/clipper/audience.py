"""Audience tier — how much pre-existing recognition a clip's moment already
has, derived from the view count ingest already records.

Both judgment stages (visual gate layer 3, and select_clip) evaluate a clip as
if the viewer has no context: would a scrolling stranger stop for this. That is
the right bar for an unknown moment and the wrong one for a moment hundreds of
thousands of people have already watched — there, the real audience isn't a
cold stranger, it's someone who recognizes the creator or the moment and will
watch on recognition alone. That's a different, lower, more honest bar, and
until now the signal that distinguishes the two cases was fetched and thrown
away.

The threshold is not a round number picked by feel. Across the 33 distinct real
candidates fetched on 2026-09-11, sorted by view count, there is exactly one
order-of-magnitude gap: 23,341 -> 476,567, a 20.4x jump. The next largest gaps
are 7.75x (17.7M -> 2.29M, inside the upper cluster) and 1.85x (inside the
lower one). The split is structural rather than a sampling artifact — Helix
returns two genuinely different populations here, a channel's all-time top
clips (the 476K+ cluster) and clips created in the last few days (the <=23K
one). 100,000 sits at the geometric midpoint of that gap (sqrt(23,341 *
476,567) = 105,468), leaving near-balanced log-scale margin on both sides:
4.8x below the upper cluster's floor, 4.3x above the lower cluster's ceiling.
On that real sample it splits 18 HIGH / 15 STANDARD.

One line to change if real output shows it's wrong. Revisit it against a real
batch, not in the abstract.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("clipper.audience")

VIEW_COUNT_HIGH_TIER = 100_000

TIER_HIGH = "HIGH"
TIER_STANDARD = "STANDARD"


def source_view_count(wd: Path) -> int | None:
    """The clip's own view count from yt-dlp's info json, or None.

    Not the Helix `view_count` the discovery step printed: the two are fetched
    at different moments and drift by a few hundred on a viral clip (real
    example: 17,755,365 here vs 17,755,788 from discovery minutes earlier).
    Irrelevant at this threshold's granularity, and reading it from the work
    dir means every stage gets the number without discovery having to hand it
    down — a re-run days later still knows the tier.
    """
    info_path = wd / "source.info.json"
    if not info_path.exists():
        return None
    try:
        info = json.loads(info_path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("audience: %s unreadable — treating view count as unknown", info_path)
        return None
    raw = info.get("view_count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("audience: %s has a non-numeric view_count %r", info_path, raw)
        return None


def tier_for_view_count(view_count: int | None) -> str:
    """HIGH once a clip has a real pre-existing audience, else STANDARD.

    Unknown counts are STANDARD, never HIGH: the long-form YouTube path has no
    equivalent signal, and an absent info json shouldn't silently lower the
    bar. STANDARD is the current behaviour, so the unknown case changes
    nothing.
    """
    if view_count is None:
        return TIER_STANDARD
    return TIER_HIGH if view_count >= VIEW_COUNT_HIGH_TIER else TIER_STANDARD


def resolve_audience_tier(wd: Path) -> tuple[str, int | None]:
    """(tier, view_count) for a work dir — what both judgment stages call."""
    view_count = source_view_count(wd)
    return tier_for_view_count(view_count), view_count


def describe_tier(tier: str, view_count: int | None) -> str:
    """The one line handed to the model naming this clip's tier.

    Deliberately states the real number alongside the label: "HIGH" alone is
    an assertion the model has to take on trust, and this project's whole
    posture is that a judgment field names the thing it's judging on.
    """
    if view_count is None:
        return f"{tier} (source view count unknown)"
    return f"{tier} ({view_count:,} views on the source clip)"
