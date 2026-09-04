"""Filesystem layout shared by every stage.

One source of truth for where the repo root, work dirs, fonts, and assets live.
Absolute paths so ffmpeg is never sensitive to the process cwd.
"""

from __future__ import annotations

from pathlib import Path

# src/clipper/paths.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]

WORK_DIR = REPO_ROOT / "work"
FONTS_DIR = REPO_ROOT / "fonts"
ASSETS_DIR = REPO_ROOT / "assets"
GAMEPLAY_DIR = ASSETS_DIR / "gameplay"
PROMPTS_DIR = REPO_ROOT / "prompts"

# Fonts vendored per CLAUDE.md. Milestone 1 needs exactly these two.
CAPTION_FONT = FONTS_DIR / "Montserrat-ExtraBold.ttf"  # ASS "Montserrat ExtraBold"
WATERMARK_FONT = FONTS_DIR / "Montserrat-Bold.ttf"  # drawtext tail


def work_dir(video_id: str) -> Path:
    """work/{video_id}/, created if absent."""
    d = WORK_DIR / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d
