"""Dependency-free grayscale-frame analysis, shared across stages.

PGM (P5) is a trivial fixed binary format — width/height/maxval header, then
raw pixel bytes — so ffmpeg can produce frames for pure-Python analysis with
no imaging library (PIL/OpenCV/numpy) needed at all. Generic infra, not
stage-specific behavior: render.py's loop-stability check (stage 4) and the
Twitch-clip visual gate's thumbnail scoring (stage 2b pre-filter) both parse
the exact same wire format and would otherwise duplicate this byte-level
parsing verbatim.
"""

from __future__ import annotations

from pathlib import Path


def read_pgm_gray(path: Path | str) -> bytes:
    """Parse a binary grayscale PGM (P5) file into raw pixel bytes."""
    data = Path(path).read_bytes()
    if not data.startswith(b"P5"):
        raise ValueError(f"not a binary PGM: {path}")
    tokens: list[bytes] = []
    i = 2
    while len(tokens) < 3:
        while data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            while data[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not data[j : j + 1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    width, height, _maxval = (int(t) for t in tokens)
    i += 1  # the single whitespace byte separating the header from pixel data
    return data[i : i + width * height]


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-pixel difference between two equal-length grayscale
    frames — a coarse but adequate "how much moved" signal."""
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def pixel_variance(pixels: bytes) -> float:
    """Population variance of grayscale pixel values (0-255 each).

    Low variance means the frame is close to a single flat tone throughout —
    a strong signal for "visually empty" (a dark desk mat, a solid loading
    screen) as opposed to a frame with real detail (gameplay HUD, a busy
    scene, facial features).
    """
    if not pixels:
        return 0.0
    n = len(pixels)
    mean = sum(pixels) / n
    return sum((p - mean) ** 2 for p in pixels) / n


def edge_density(pixels: bytes, width: int, height: int) -> float:
    """Mean absolute difference between horizontally-adjacent pixels.

    A cheap texture/detail proxy: two large flat regions meeting at one sharp
    boundary (e.g. a hand against a dark backdrop) can still show high global
    variance (pixel_variance) despite being visually mostly empty, because
    variance only measures spread around the mean, not how that spread is
    laid out. Edge density instead scores how much the image actually
    changes from pixel to pixel — high across a busy scene with real detail
    (HUD text, foliage, a character model), low across large uniform
    stretches even when a big all-or-nothing jump sits between them.
    """
    if width < 2 or height < 1 or len(pixels) < width * height:
        return 0.0
    total = 0
    count = 0
    for row in range(height):
        offset = row * width
        for col in range(width - 1):
            total += abs(pixels[offset + col] - pixels[offset + col + 1])
            count += 1
    return total / count if count else 0.0


def dark_pixel_fraction(pixels: bytes, threshold: int = 30) -> float:
    """Fraction of pixels at or below ``threshold`` (0-255, low = dark).

    Catches the case variance alone can miss: a frame that's mostly dark but
    has one small bright patch (e.g. a facecam in a corner) can still show
    moderate variance from that one patch alone, while still being
    overwhelmingly empty screen space.
    """
    if not pixels:
        return 0.0
    return sum(1 for p in pixels if p <= threshold) / len(pixels)
