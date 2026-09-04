"""Tone -> style lookup. See CLAUDE.md's "Tone routing" section.

Adding a tone means adding a block to styles.yaml, never touching this module
or the renderer. If a tone is looked up here and has no block in styles.yaml,
that is a hard failure by design: drift between the selection prompt and the
style file must surface immediately, never fall back to a default.
"""

from __future__ import annotations

import yaml

from .paths import STYLES_PATH


class UnknownToneError(ValueError):
    """Raised when a tone has no block in styles.yaml. Never caught to fall
    back to a default — the whole point is to surface drift loudly."""


def load_styles() -> dict:
    if not STYLES_PATH.exists():
        raise FileNotFoundError(f"styles.yaml missing at {STYLES_PATH}")
    return yaml.safe_load(STYLES_PATH.read_text()) or {}


def style_for_tone(tone: str) -> tuple[dict, int]:
    """Return (ass style overrides, phrase group size) for ``tone``.

    Overrides are merged onto clipper.ass.DEFAULT_STYLE by build_ass. Raises
    UnknownToneError if ``tone`` has no block in styles.yaml.
    """
    styles = load_styles()
    if tone not in styles:
        raise UnknownToneError(
            f"tone {tone!r} has no block in styles.yaml. Known tones: "
            f"{sorted(styles)}. Add a block for it, or fix the caller."
        )

    block = dict(styles[tone])
    group_size = block.pop("group_size", None)
    if group_size is None:
        raise UnknownToneError(f"styles.yaml tone {tone!r} is missing required 'group_size'")
    return block, int(group_size)
