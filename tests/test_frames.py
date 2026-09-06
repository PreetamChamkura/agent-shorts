"""Unit tests for clipper.frames — dependency-free PGM parsing and
grayscale-frame metrics shared by render.py's loop check and the Twitch
visual gate."""

from __future__ import annotations

import pytest

from clipper.frames import (
    dark_pixel_fraction,
    edge_density,
    mean_abs_diff,
    pixel_variance,
    read_pgm_gray,
)


def test_read_pgm_gray_parses_minimal_header(tmp_path):
    # 2x2 binary grayscale PGM: "P5\n2 2\n255\n" + 4 pixel bytes.
    path = tmp_path / "f.pgm"
    path.write_bytes(b"P5\n2 2\n255\n" + bytes([10, 20, 30, 40]))
    assert read_pgm_gray(path) == bytes([10, 20, 30, 40])


def test_read_pgm_gray_rejects_non_pgm(tmp_path):
    path = tmp_path / "f.pgm"
    path.write_bytes(b"not a pgm")
    with pytest.raises(ValueError):
        read_pgm_gray(path)


def test_read_pgm_gray_skips_comment_line(tmp_path):
    path = tmp_path / "f.pgm"
    path.write_bytes(b"P5\n# a comment\n2 1\n255\n" + bytes([1, 2]))
    assert read_pgm_gray(path) == bytes([1, 2])


def test_mean_abs_diff_identical_is_zero():
    assert mean_abs_diff(bytes([5, 10]), bytes([5, 10])) == 0.0


def test_mean_abs_diff_measures_magnitude():
    assert mean_abs_diff(bytes([0, 0]), bytes([10, 20])) == 15.0


def test_pixel_variance_zero_for_uniform_frame():
    assert pixel_variance(bytes([128] * 100)) == 0.0


def test_pixel_variance_positive_for_varied_frame():
    assert pixel_variance(bytes([0, 255, 0, 255])) > 0


def test_pixel_variance_empty_is_zero():
    assert pixel_variance(b"") == 0.0


def test_dark_pixel_fraction_all_dark():
    assert dark_pixel_fraction(bytes([0, 5, 10]), threshold=30) == 1.0


def test_dark_pixel_fraction_none_dark():
    assert dark_pixel_fraction(bytes([200, 210, 220]), threshold=30) == 0.0


def test_dark_pixel_fraction_mixed():
    assert dark_pixel_fraction(bytes([0, 200]), threshold=30) == 0.5


def test_dark_pixel_fraction_empty_is_zero():
    assert dark_pixel_fraction(b"", threshold=30) == 0.0


def test_edge_density_zero_for_flat_frame():
    # 4x4 all-identical pixels — no adjacent difference anywhere.
    assert edge_density(bytes([50] * 16), 4, 4) == 0.0


def test_edge_density_positive_for_checkerboard():
    # 2x2: rows of [0, 255] — every horizontal neighbor differs by 255.
    px = bytes([0, 255, 0, 255])
    assert edge_density(px, 2, 2) == 255.0


def test_edge_density_handles_undersized_input_gracefully():
    assert edge_density(bytes([1, 2]), 10, 10) == 0.0
    assert edge_density(b"", 1, 1) == 0.0
