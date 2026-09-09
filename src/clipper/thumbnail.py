"""Stage 4 (alongside the video render) — static thumbnail + upload metadata.

Reads: work/{video_id}/source.mp4, work/{video_id}/visual_judge.json
       (optional — Twitch clips only, see select_thumbnail_timestamp)
Writes: work/{video_id}/thumbnail_{n}.jpg, work/{video_id}/metadata_{n}.json,
        work/{video_id}/upload_{n}.txt (plain-text title+description, for
        pasting directly into an upload form — metadata_{n}.json's quoting
        and braces aren't)

Why this exists at all (2026 platform research, see CLAUDE.md): YouTube
Shorts' custom-thumbnail feature (July 2026) is YPP + Studio-desktop only —
the Data API's thumbnails.set is undocumented/unreliable for Shorts. So this
is NOT an automated-upload feature; it generates the same kind of asset the
video file itself is — something the human copies in at manual upload time.
No publishing code, per CLAUDE.md's Out of scope.

## Frame selection

Prefers Layer 3's own already-computed judgment (clipper.visual_judge's
best_frame_timestamp) over re-sampling and re-judging: that layer already
looked at several frames of this exact clip to decide pass/reject, so it
also names which one is most worth using as a thumbnail — see
select_thumbnail_timestamp. That data only exists for Twitch clips (Layer 3
never runs on the long-form YouTube path) and can fall outside the FINAL
segment's own [start, end] (select_clip can trim tighter than the full clip
Layer 3 sampled across) — both cases fall back to a cheap pixel-stats scan
within [start, end] (_pick_engaging_frame_cheap), reusing clipper.frames'
edge_density the same way visual_gate's layer 1 does. That fallback is a
proxy, not a judgment — it cannot tell "engaging" from "busy" any more than
layer 1 can tell "blank" from "dark and moody" (CLAUDE.md), so it's
explicitly the second choice, not the primary mechanism.

For facecam_split, the fallback scores ONLY the facecam panel's own source
region (clipper.render._facecam_panel_crop_box) rather than the whole
frame — the point is catching a visible reaction, and the main-content
panel's own motion would otherwise dominate the score for a reason that has
nothing to do with the facecam.

For every other composition (tight_crop in practice), the fallback requires
a CONFIRMED-recurring face — find_face_region()'s own temporal clustering
over the search window, not a bare single-frame Haar check — before a
candidate is even scored, searched in a window centered on an ANCHOR
timestamp rather than spread across the whole segment. Three real bugs
found from real output, in sequence:

1. An unconditional highest-edge_density pick chose a visually busy but
   completely person-free B-roll shot (a product shelf) near the payoff,
   since thumbnail_text is usually a quoted line of dialogue that needs a
   visible speaker to read as anything.
2. A bare single-frame Haar "does this exact frame contain a face" check,
   added to fix that, itself let the SAME B-roll frame through — Haar threw
   4 false-positive hits on bottle labels/caps in that one frame, and
   because it was also the busiest-looking frame, edge_density picked it
   right back. Requiring the hit to match an ALREADY-confirmed recurring
   position (find_face_region's own clustering over the search window, see
   _pick_engaging_frame_tight_crop / _frame_matches_confirmed_face) fixed
   this: a one-off texture misfire doesn't recur at the same spot across
   temporally-spread samples the way a real, present subject does.
3. Centering the search on punchline_at unconditionally: a real segment's
   thumbnail_text was a verbatim quote spoken 15+ seconds away from
   punchline_at (the payoff) in the same segment — the search picked a real
   person, but not the one actually saying the quoted line, because nothing
   had confirmed the two timestamps were even close. _resolve_anchor now
   locates thumbnail_text's own spoken span in the transcript (same
   contiguous-token-run matching clipper.select uses for why's quoted
   payoff phrase, see _locate_quoted_phrase) and anchors there when it's a
   genuine verbatim quote; only falls back to punchline_at when
   thumbnail_text is a paraphrase/reaction phrase instead (select.md
   explicitly allows either) and the quote can't be located.

This does not touch facecam_split's own logic at all — that panel is, by
construction, already where a face would be, and its fallback (on the rare
occasion it even runs) ignores both punchline_at and thumbnail_text.

## Composition + text treatment

Resolves composition (tight_crop vs facecam_split) independently via
clipper.face_detect, exactly like render() and visual_judge() each already
do — CLAUDE.md's stage-independence contract: sharing one detection call
across stages would couple them, so this small, cheap OpenCV pass runs a
third time rather than being threaded through as a shared argument.

Text overlay uses the tone's own highlight color (clipper.styles,
clipper.ass.DEFAULT_STYLE) for consistency with the video's own captions,
Montserrat ExtraBold (the same vendored caption font), and a black
translucent scrim placed behind the text band for legibility regardless of
the source frame's own brightness — no existing "pre-roll dim" treatment
was found elsewhere in this codebase to reuse (checked before building:
narrate.py's cold_open change removed a prepended pre-roll rather than
adding a dim filter), so this scrim is a new, small treatment built for
this specific need. Text and scrim both stay inside a centered 1080x1080
safe square (CLAUDE.md safe zones: Reels/TikTok grid-crop the cover image).
For facecam_split specifically, the text band is further restricted to the
main panel's own visible range and NEVER overlaps the facecam panel at all
(_text_band_center / _text_safe_bounds) — a real bug found from real output:
an earlier version used one fixed band for every composition, which for
facecam_split sat inside the facecam panel itself, directly over the
visible face's mouth.

Text is passed to ffmpeg via a temp textfile (drawtext's `textfile=`), not
inlined into the filter-graph string — thumbnail_text can contain quotes,
colons, and emoji, exactly the characters that make ffmpeg filter-string
escaping "fail silently and weirdly" per CLAUDE.md; writing it to a file and
referencing the path sidesteps that entirely, the same reason captions are
written to caps_{n}.ass and referenced by path rather than inlined.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path

from .ass import DEFAULT_STYLE
from .face_detect import (
    DETECT_WIDTH,
    _load_cascade,
    detect_faces_in_frame,
    find_face_region,
    is_small_corner_facecam,
)
from .ffmpeg import run_ffmpeg, run_ffprobe
from .frames import edge_density, read_pgm_gray
from .paths import CAPTION_FONT, work_dir
from .render import (
    FACECAM_PANEL_Y,
    _facecam_panel_crop_box,
    _facecam_split_video,
    _tight_crop_video,
)
from .styles import style_for_tone

log = logging.getLogger("clipper.thumbnail")

THUMB_WIDTH = 1080
THUMB_HEIGHT = 1920

# Centered safe square (CLAUDE.md: Reels crops the cover to 4:5 in-feed and
# 3:4 on the profile grid, TikTok's grid does its own crop — content outside
# a centered square gets cut on at least one platform).
SAFE_SQUARE_TOP = (THUMB_HEIGHT - THUMB_WIDTH) // 2  # 420
SAFE_SQUARE_BOTTOM = SAFE_SQUARE_TOP + THUMB_WIDTH  # 1500

# Text band vertical center for compositions with no facecam panel to avoid
# (tight_crop, blur_fill): inside the safe square, in its lower half — the
# conventional thumbnail-text position.
#
# facecam_split uses a DIFFERENT band — see TEXT_BAND_CENTER_Y_FACECAM_SPLIT
# and _text_safe_bounds below. Real bug found from real output: an earlier
# version used this same fixed band for every composition, which for
# facecam_split sat inside the facecam panel itself (FACECAM_PANEL_Y=1056
# downward), directly over the visible face's mouth. Text must never
# overlap that panel at all for facecam_split.
TEXT_BAND_CENTER_Y_DEFAULT = 1300

# facecam_split's own band: centered within the intersection of the safe
# square and the main panel's actually-visible range [SAFE_SQUARE_TOP,
# FACECAM_PANEL_Y) — never touching the facecam panel. (420 + 1056) // 2.
TEXT_BAND_CENTER_Y_FACECAM_SPLIT = (SAFE_SQUARE_TOP + FACECAM_PANEL_Y) // 2

TEXT_FONTSIZE = 88
TEXT_MARGIN_X = 60  # each side, inside the 1080-wide frame
# Rough average glyph width for Montserrat ExtraBold as a fraction of
# fontsize — untuned, a wrapping estimate rather than a measured value (no
# text-measurement library in this project's dependency set). Errs toward
# wrapping a little early rather than overflowing the safe square.
_AVG_CHAR_WIDTH_FACTOR = 0.62
LINE_GAP = 14
SCRIM_ALPHA = 0.55
SCRIM_PAD_Y = 36

# Cheap-fallback frame scoring (only used when Layer 3's data is unavailable
# or falls outside the final segment window — see module docstring).
_N_FALLBACK_CANDIDATES = 6
_FRAME_SCORE_SIZE = 96

# tight_crop's fallback face-gate (facecam_split is untouched by this — see
# _pick_engaging_frame_cheap). Real bug found from real output: the
# unconditional highest-edge_density pick chose a visually busy but
# completely person-free B-roll shot (a product shelf) near the payoff — a
# quoted line of dialogue on a thumbnail with no one visibly saying it
# doesn't read as anything. Search starts centered on punchline_at (where
# the quoted line usually IS, per CLAUDE.md's last-third payoff placement)
# rather than spread across the whole segment, and requires a detected face
# before a candidate is even scored. Widens once before giving up and
# falling back to the best faceless frame in the full segment span.
_FACE_SEARCH_INITIAL_RADIUS = 3.0
_FACE_SEARCH_WIDENED_RADIUS = 8.0
_FACE_SEARCH_N_CANDIDATES = 6

# Confirmed for real (not assumed): ffmpeg drawtext + this vendored Montserrat
# font renders emoji as broken tofu boxes, not the glyph — Montserrat is a
# text font with no emoji coverage, and drawtext has no font-fallback
# mechanism (CLAUDE.md is explicit that this project never relies on
# fontconfig substitution for exactly this kind of silent breakage). The
# select.md/select_clip.md prompt still allows thumbnail_text up to 2 emoji
# as an accent — those stay in metadata.json for a human assembling a
# thumbnail by hand in other software, which can render them fine — but the
# auto-generated overlay strips them first, since a real glyph would beat a
# tofu box and a missing character is far less noticeable than a broken one.
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"  # emoticons, transport, symbols & pictographs (+ extended-A)
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator letters (flag emoji)
    "\U0000fe0f"  # variation selector-16 (emoji presentation)
    "]+"
)


def _strip_emoji(text: str) -> str:
    """Remove emoji before the thumbnail overlay renders it — see _EMOJI_RE."""
    return _EMOJI_RE.sub("", text)


def _text_band_center(composition: str) -> int:
    """Vertical center of the thumbnail text band for ``composition`` — see
    TEXT_BAND_CENTER_Y_FACECAM_SPLIT for why facecam_split gets its own."""
    if composition == "facecam_split":
        return TEXT_BAND_CENTER_Y_FACECAM_SPLIT
    return TEXT_BAND_CENTER_Y_DEFAULT


def _text_safe_bounds(composition: str) -> tuple[int, int]:
    """(top, bottom): the vertical range thumbnail text is allowed to occupy
    for ``composition``.

    facecam_split gets its own, narrower range — the intersection of the
    safe square with the main panel's own visible band, ending strictly at
    FACECAM_PANEL_Y and never inside it. Every other composition uses the
    full safe square.
    """
    if composition == "facecam_split":
        return SAFE_SQUARE_TOP, FACECAM_PANEL_Y
    return SAFE_SQUARE_TOP, SAFE_SQUARE_BOTTOM


# See _extract_full_frame: render()'s multi-branch filter graphs (a real
# seeked input plus a synthetic `color=` source feeding an overlay chain) can
# return a solid-black first output frame before the two sync up — the exact
# issue clipper.visual_judge's _extract_preview_frame already found and
# fixed for its own (downscaled) still-frame extraction. Duplicated here
# rather than shared, per this project's stage-independence convention
# (render.py duplicates rather than imports select.py's word-snap logic for
# the same reason) — kept at the same value since it fixed the real failure
# there and this is the same underlying ffmpeg behavior.
#
# _extract_full_frame seeks _WARMUP_FRAMES/30s BEFORE the requested timestamp
# and keeps the LAST frame of the burst, landing ON the timestamp — not a
# naive seek-to-ts-then-keep-last, which lands _WARMUP_FRAMES/30s (~0.27s)
# PAST it. That forward drift was a real bug, not theoretical: it's what
# actually produced a visibly duller facecam-panel frame than Layer 3's own
# chosen index, confirmed by extracting both and comparing them directly
# against this exact clip. Mirrors render.py's pick_loop_freeze_end /
# _extract_loop_candidate_frames, which already solves the identical
# seek-then-land-exactly-here problem the same way (seek backward by the
# lookahead window, decode forward to the target).
_WARMUP_FRAMES = 8


def _wrap_lines(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap. A single word longer than ``max_chars`` still gets
    its own line rather than being split mid-word."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = f"{current} {w}".strip()
        if not current or len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _ass_to_ffmpeg_hex(ass_color: str) -> str:
    """'&HAABBGGRR' (ASS/SSA byte order) -> '0xRRGGBB' (ffmpeg drawtext).

    Alpha is dropped — the scrim behind the text is what actually handles
    legibility (see module docstring); the text itself renders fully opaque.
    """
    hexpart = ass_color.upper().removeprefix("&H")
    if len(hexpart) != 8:
        raise ValueError(f"not a valid &HAABBGGRR color: {ass_color!r}")
    _aa, bb, gg, rr = hexpart[0:2], hexpart[2:4], hexpart[4:6], hexpart[6:8]
    return f"0x{rr}{gg}{bb}"


def _tone_accent_hex(tone: str) -> str:
    """The tone's own highlight color (clipper.styles), as an ffmpeg hex —
    same color the video's own active-word captions use, for the thumbnail
    text to look like it belongs to the same clip."""
    overrides, _group_size = style_for_tone(tone)
    style = {**DEFAULT_STYLE, **overrides}
    return _ass_to_ffmpeg_hex(str(style["highlight"]))


def _extract_score_frame(
    source: str, ts: float, crop_box: tuple[int, int, int, int] | None
) -> bytes | None:
    """Small grayscale pixels for one fallback candidate frame at ``ts``,
    optionally cropped to ``crop_box`` (w, h, x, y) first — used to score
    just the facecam panel's own source region for facecam_split, not the
    whole frame (see module docstring). Returns None on extraction failure
    (e.g. ``ts`` past the source's actual end) rather than raising — a
    missing candidate simply doesn't compete for the pick.
    """
    vf = ""
    if crop_box is not None:
        w, h, x, y = crop_box
        vf += f"crop={w}:{h}:{x}:{y},"
    vf += f"scale={_FRAME_SCORE_SIZE}:{_FRAME_SCORE_SIZE},format=gray"

    with tempfile.TemporaryDirectory(prefix="clipper_thumbpick_") as td:
        out = Path(td) / "f.pgm"
        run_ffmpeg(["-y", "-ss", f"{ts:.3f}", "-i", source, "-frames:v", "1", "-vf", vf, str(out)])
        if not out.exists():
            return None
        return read_pgm_gray(out)


def _evenly_spaced(start: float, end: float, n: int) -> list[float]:
    """n absolute timestamps evenly spread across [start, end], inset from
    the very edges. Mirrors the sampling pattern clipper.visual_judge and
    clipper.face_detect each already use — duplicated rather than imported,
    same stage-independence reasoning as _WARMUP_FRAMES above."""
    span = end - start
    if n < 1 or span <= 0:
        return []
    inset = span * 0.05
    lo, hi = start + inset, end - inset
    if n == 1:
        return [(lo + hi) / 2]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def _best_by_edge_density(
    source: str, timestamps: list[float], crop_box: tuple[int, int, int, int] | None
) -> float | None:
    """Highest-edge_density timestamp among ``timestamps`` (optionally
    cropped first), or None if every one failed to extract. Factored out of
    _pick_engaging_frame_cheap so both the facecam_split path and
    tight_crop's face-gated search share the same scoring step."""
    best_ts: float | None = None
    best_score = -1.0
    for ts in timestamps:
        pixels = _extract_score_frame(source, ts, crop_box)
        if pixels is None:
            continue
        score = edge_density(pixels, _FRAME_SCORE_SIZE, _FRAME_SCORE_SIZE)
        if score > best_score:
            best_score, best_ts = score, ts
    return best_ts


def _detect_faces_at(source: str, ts: float) -> list[tuple[int, int, int, int]]:
    """Raw Haar hits at ``ts``, in DETECT_WIDTH's downscaled coordinate
    space — the same detection machinery layer 2 uses (detect_faces_in_frame,
    _load_cascade), on a single frame. No clustering — see
    _frame_matches_confirmed_face for why a bare single-frame hit isn't
    trusted on its own."""
    import cv2

    with tempfile.TemporaryDirectory(prefix="clipper_facecheck_") as td:
        out = Path(td) / "f.png"
        run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{ts:.3f}",
                "-i",
                source,
                "-frames:v",
                "1",
                "-vf",
                f"scale={DETECT_WIDTH}:-2",
                str(out),
            ]
        )
        if not out.exists():
            return []
        img = cv2.imread(str(out))
        if img is None:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return detect_faces_in_frame(gray, _load_cascade())


def _frame_matches_confirmed_face(source: str, ts: float, confirmed: dict) -> bool:
    """True if a Haar hit at ``ts`` lands near ``confirmed``'s already-
    verified position (within face_detect.CLUSTER_DISTANCE_PX, in
    DETECT_WIDTH's coordinate space), not just any hit anywhere in the frame.

    ``confirmed`` is a find_face_region() result — SOURCE pixel coordinates,
    scaled down here to match _detect_faces_at's downscaled space. Requiring
    a match against an ALREADY temporally-confirmed position (rather than
    trusting a bare single-frame Haar hit on its own) is the fix for a real,
    confirmed failure: a single-frame check alone accepted a pure product
    B-roll shot as "has a face" — Haar threw 4 false-positive hits on bottle
    labels/caps in that exact frame, and because it was also the busiest-
    looking frame, edge_density then picked it anyway. A one-off false
    positive on a specific frame's texture won't recur at the SAME position
    across the temporally-spread samples find_face_region already checked,
    the same reasoning that makes find_face_region itself trustworthy for
    the facecam case (face_detect.py's own module docstring: a single frame
    threw 3 hits, only one real; sampling and requiring recurrence fixed it).
    """
    from .face_detect import CLUSTER_DISTANCE_PX

    scale = DETECT_WIDTH / confirmed["frame_width"]
    confirmed_cx = (confirmed["x"] + confirmed["w"] / 2) * scale
    confirmed_cy = (confirmed["y"] + confirmed["h"] / 2) * scale

    for x, y, w, h in _detect_faces_at(source, ts):
        cx, cy = x + w / 2, y + h / 2
        if ((cx - confirmed_cx) ** 2 + (cy - confirmed_cy) ** 2) ** 0.5 <= CLUSTER_DISTANCE_PX:
            return True
    return False


def _pick_engaging_frame_tight_crop(
    source: str, start: float, end: float, anchor_hint: float | None
) -> float:
    """tight_crop's fallback pick: highest edge_density among candidates
    that actually show a CONFIRMED-recurring face, searched in a window
    centered on ``anchor_hint`` — widened once before giving up and falling
    back to the best faceless frame across the whole segment.

    ``anchor_hint`` is resolved by the caller (select_thumbnail_timestamp /
    _resolve_anchor): thumbnail_text's own spoken timestamp when it's a
    verbatim quote locatable in the transcript, else punchline_at. This
    function doesn't know or care which — it just centers the search on
    whatever timestamp it's given, defaulting to the segment midpoint if
    None. Real bug found from real output that made the distinction matter:
    thumbnail_text's quoted line and punchline_at's payoff can be 15+
    seconds apart in the same segment — centering blindly on punchline_at
    picked a frame in the gap between them, showing a real person but not
    the one saying the quoted line.

    "Confirmed-recurring" means find_face_region() over the search window
    itself first — its own temporal clustering, not a bare single-frame
    Haar check (see _frame_matches_confirmed_face for the real failure that
    required this). A window where nothing recurs (find_face_region returns
    None) is treated exactly like a window with no face candidates at all.
    """
    anchor = anchor_hint if anchor_hint is not None else (start + end) / 2
    for radius in (_FACE_SEARCH_INITIAL_RADIUS, _FACE_SEARCH_WIDENED_RADIUS):
        window_start = max(start, anchor - radius)
        window_end = min(end, anchor + radius)
        if window_end <= window_start:
            continue

        confirmed = find_face_region(source, window_start, window_end)
        if confirmed is None:
            continue

        candidates = _evenly_spaced(window_start, window_end, _FACE_SEARCH_N_CANDIDATES)
        face_candidates = [
            ts for ts in candidates if _frame_matches_confirmed_face(source, ts, confirmed)
        ]
        if face_candidates:
            best_ts = _best_by_edge_density(source, face_candidates, None)
            if best_ts is not None:
                log.info(
                    "thumbnail: confirmed a recurring face within %.1fs of the anchor "
                    "(%.2fs) — using %.2fs",
                    radius,
                    anchor,
                    best_ts,
                )
                return best_ts

    log.warning(
        "thumbnail: no confirmed-recurring face found within %.1fs of the anchor (%.2fs) in "
        "[%.2f, %.2f] even after widening — falling back to the best faceless frame in the "
        "full segment span",
        _FACE_SEARCH_WIDENED_RADIUS,
        anchor,
        start,
        end,
    )
    fallback = _best_by_edge_density(
        source, _evenly_spaced(start, end, _N_FALLBACK_CANDIDATES), None
    )
    return fallback if fallback is not None else (start + end) / 2


def _pick_engaging_frame_cheap(
    source: str,
    start: float,
    end: float,
    composition: str,
    face_region: dict | None,
    anchor_hint: float | None = None,
) -> float:
    """Fallback frame pick when Layer 3's data isn't usable (see module
    docstring): an untuned pixel-stats heuristic, not a vision judgment,
    same caveat as visual_gate's layer 1 (CLAUDE.md) — the best available
    signal without paying for another vision-model call on every long-form
    render.

    facecam_split scores ONLY the facecam panel's own source region
    (unchanged by the tight_crop fix below, and ``anchor_hint`` is unused
    for it — that panel is, by construction, where a face would be, and
    Layer 3 already covers this composition's normal case, so this rarely
    even runs for it). Every other composition goes through
    _pick_engaging_frame_tight_crop's face-gated, anchor-centered search
    instead of a plain full-segment scan.
    """
    if composition == "facecam_split":
        crop_box = _facecam_panel_crop_box(face_region) if face_region else None
        best_ts = _best_by_edge_density(
            source, _evenly_spaced(start, end, _N_FALLBACK_CANDIDATES), crop_box
        )
        if best_ts is not None:
            return best_ts
        log.warning(
            "thumbnail: every fallback candidate frame failed to extract for %.2f-%.2fs — "
            "using the segment midpoint",
            start,
            end,
        )
        return (start + end) / 2

    return _pick_engaging_frame_tight_crop(source, start, end, anchor_hint)


def _normalize_phrase_token(tok: str) -> str:
    """Same normalization clipper.select uses for its own why-field phrase
    matching (_normalize_token) — duplicated per this project's stage-
    independence convention rather than imported across stage modules."""
    return re.sub(r"[^a-z0-9']", "", tok.lower())


def _locate_quoted_phrase(words: list[dict], phrase: str) -> tuple[float, float] | None:
    """(start, end) of ``phrase`` as a contiguous token run in ``words``, or
    None if it isn't found verbatim.

    Same approach as clipper.select._find_phrase_span (why's quoted payoff
    cross-check), applied here to thumbnail_text instead — duplicated
    rather than imported, same stage-independence reasoning as
    _WARMUP_FRAMES/_evenly_spaced above. thumbnail_text is often wrapped in
    literal quote marks ("...") as a styling convention, not part of the
    spoken words — those get stripped by the same token normalization as
    any other punctuation. Returns None when thumbnail_text is a paraphrase
    or reaction phrase rather than a verbatim quote (select.md explicitly
    allows either) — no reliable location is possible then, same posture as
    the why-field case: callers fall back rather than guess.
    """
    tokens = [t for t in (_normalize_phrase_token(w) for w in phrase.split()) if t]
    if not tokens:
        return None
    norm_words = [_normalize_phrase_token(w["word"]) for w in words]
    n = len(tokens)
    for i in range(len(norm_words) - n + 1):
        if norm_words[i : i + n] == tokens:
            return float(words[i]["start"]), float(words[i + n - 1]["end"])
    return None


def _resolve_anchor(
    video_id: str,
    start: float,
    end: float,
    thumbnail_text: str,
    punchline_at: float | None,
) -> tuple[float, str]:
    """(anchor timestamp, source tag — "quote" or "punchline") to center
    tight_crop's fallback face search on.

    Prefers thumbnail_text's own spoken moment when it's a verbatim quote
    locatable in the transcript: the thumbnail should show the person
    actually saying the quoted line, not just someone visible near the
    payoff. Falls back to punchline_at (or the segment midpoint, if that's
    also unavailable) when thumbnail_text is a paraphrase/reaction phrase
    instead — the normal case select.md explicitly allows, not an error.

    Real bug found from real output: a segment's thumbnail_text ("Where did
    you go to med school?") was spoken at 101-102s, while its punchline_at
    (the payoff, "he's not the best, but he's very affordable") landed at
    117-119s — 15+ seconds apart in the same segment. Centering blindly on
    punchline_at picked a frame in the gap between them: a real person, but
    not the one saying the quoted line.
    """
    words_path = work_dir(video_id) / "words.json"
    if words_path.exists() and thumbnail_text.strip():
        all_words = json.loads(words_path.read_text()).get("words", [])
        segment_words = [w for w in all_words if start <= float(w["start"]) <= end]
        located = _locate_quoted_phrase(segment_words, thumbnail_text)
        if located is not None:
            phrase_start, phrase_end = located
            log.info(
                "thumbnail: %s thumbnail_text located verbatim at %.2f-%.2fs — anchoring the "
                "frame search there instead of punchline_at",
                video_id,
                phrase_start,
                phrase_end,
            )
            return (phrase_start + phrase_end) / 2, "quote"

    return (punchline_at if punchline_at is not None else (start + end) / 2), "punchline"


def select_thumbnail_timestamp(
    video_id: str,
    start: float,
    end: float,
    composition: str,
    face_region: dict | None,
    punchline_at: float | None = None,
    thumbnail_text: str = "",
) -> tuple[float, str]:
    """Absolute source.mp4 timestamp for this clip's thumbnail, and a short
    tag naming how it was picked — "layer3", "fallback:quote" (tight_crop's
    fallback, anchored on thumbnail_text's own located timestamp), or
    "fallback:punchline" (anchored on punchline_at instead, see
    _resolve_anchor) — for logging and tests, not shown to any end user.
    See module docstring for the preference order. facecam_split's fallback
    ignores both punchline_at and thumbnail_text (unaffected by this).
    """
    wd = work_dir(video_id)
    judge_path = wd / "visual_judge.json"
    if judge_path.exists():
        cached = json.loads(judge_path.read_text())
        ts = cached.get("best_frame_timestamp")
        if ts is not None:
            if start <= float(ts) <= end:
                return float(ts), "layer3"
            log.info(
                "thumbnail: %s Layer 3 best frame %.2fs falls outside the final segment "
                "%.2f-%.2fs (select_clip trimmed past it) — falling back to a cheap scan",
                video_id,
                ts,
                start,
                end,
            )

    source = str(wd / "source.mp4")
    if composition == "facecam_split":
        # Unaffected by this fix — see _pick_engaging_frame_cheap's own
        # docstring on why facecam_split never uses an anchor at all.
        ts = _pick_engaging_frame_cheap(source, start, end, composition, face_region, None)
        return ts, "fallback"

    anchor, anchor_source = _resolve_anchor(video_id, start, end, thumbnail_text, punchline_at)
    ts = _pick_engaging_frame_cheap(source, start, end, composition, face_region, anchor)
    return ts, f"fallback:{anchor_source}"


def _extract_full_frame(source: str, ts: float, video_fc: str, out_path: Path) -> None:
    """One full-resolution (1080x1920) frame landing ON ``ts`` (not past it —
    see _WARMUP_FRAMES), composited through ``video_fc`` (render.py's real
    video-only filter chain)."""
    lead = _WARMUP_FRAMES / 30.0
    seek_ts = max(0.0, ts - lead)
    with tempfile.TemporaryDirectory(prefix="clipper_thumbframe_") as td:
        pattern = Path(td) / "w_%02d.png"
        run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{seek_ts:.3f}",
                "-i",
                source,
                "-frames:v",
                str(_WARMUP_FRAMES),
                "-filter_complex",
                f"{video_fc};[comp]scale={THUMB_WIDTH}:{THUMB_HEIGHT}[out]",
                "-map",
                "[out]",
                str(pattern),
            ]
        )
        produced = sorted(Path(td).glob("w_*.png"))
        if produced:
            produced[-1].replace(out_path)


def _resolve_composition(source: str, start: float, end: float) -> tuple[str, dict | None]:
    """Same detection render() runs when composition is left unset — see
    module docstring on why this isn't shared/threaded through instead."""
    face_region = find_face_region(source, start, end)
    if face_region and is_small_corner_facecam(face_region):
        return "facecam_split", face_region
    return "tight_crop", None


def _validate_thumbnail(out_path: str) -> None:
    proc = run_ffprobe(
        ["-v", "error", "-show_entries", "stream=width,height", "-of", "json", out_path]
    )
    streams = json.loads(proc.stdout).get("streams", [])
    if not streams or (streams[0].get("width"), streams[0].get("height")) != (
        THUMB_WIDTH,
        THUMB_HEIGHT,
    ):
        raise ValueError(f"thumbnail: {out_path} is not {THUMB_WIDTH}x{THUMB_HEIGHT}")


def generate_thumbnail(
    video_id: str,
    n: int,
    start: float,
    end: float,
    tone: str,
    thumbnail_text: str,
    *,
    punchline_at: float | None = None,
    composition: str | None = None,
    force: bool = False,
) -> str:
    """Generate work/{video_id}/thumbnail_{n}.jpg: a static 1080x1920 image,
    the real composited frame plus a tone-colored, scrim-backed
    ``thumbnail_text`` overlay kept inside the centered safe square.

    ``composition`` mirrors render()'s own parameter: leave unset to
    auto-detect (tight_crop vs facecam_split) via the same clipper.face_detect
    logic render() uses. ``punchline_at`` centers tight_crop's fallback
    frame search when Layer 3 data isn't available or usable (see
    select_thumbnail_timestamp / _pick_engaging_frame_tight_crop) — pass
    segments.json's own punchline_at; harmless to omit, it just widens the
    fallback search to the segment midpoint instead. Cached like every
    other stage output — skipped when thumbnail_{n}.jpg already exists,
    unless ``force``.
    """
    wd = work_dir(video_id)
    source = wd / "source.mp4"
    out_path = wd / f"thumbnail_{n}.jpg"

    if not source.exists():
        raise FileNotFoundError(f"thumbnail: {source} missing — run stage 1 (ingest) first.")
    if out_path.exists() and not force:
        log.info("thumbnail: cache hit for n=%d (%s), skipping", n, out_path)
        return str(out_path)
    if not thumbnail_text.strip():
        raise ValueError(f"thumbnail: n={n} has no thumbnail_text to render")

    face_region = None
    if composition is None:
        composition, face_region = _resolve_composition(str(source), start, end)
    elif composition == "facecam_split":
        face_region = find_face_region(str(source), start, end)
        if not face_region or not is_small_corner_facecam(face_region):
            raise ValueError(
                f"thumbnail: n={n} composition='facecam_split' was requested explicitly, "
                "but no small corner facecam was detected — pass tight_crop, or leave "
                "composition unset to auto-detect."
            )

    ts, pick_source = select_thumbnail_timestamp(
        video_id, start, end, composition, face_region, punchline_at, thumbnail_text
    )
    log.info(
        "thumbnail: n=%d composition=%s frame=%.2fs (source=%s)", n, composition, ts, pick_source
    )

    video_fc = (
        _facecam_split_video(face_region) if composition == "facecam_split" else _tight_crop_video()
    )

    accent_hex = _tone_accent_hex(tone)
    render_text = _strip_emoji(thumbnail_text.strip().upper()).strip()
    if render_text != thumbnail_text.strip().upper():
        log.info(
            "thumbnail: n=%d stripped emoji from thumbnail_text for the rendered overlay "
            "(kept in metadata_%d.json): %r -> %r",
            n,
            n,
            thumbnail_text,
            render_text,
        )
    usable_width = THUMB_WIDTH - 2 * TEXT_MARGIN_X
    max_chars = max(4, int(usable_width / (TEXT_FONTSIZE * _AVG_CHAR_WIDTH_FACTOR)))
    lines = _wrap_lines(render_text, max_chars)
    line_height = TEXT_FONTSIZE * 1.25
    text_block_h = len(lines) * line_height
    box_h = round(text_block_h + 2 * SCRIM_PAD_Y)
    band_center_y = _text_band_center(composition)
    box_y = round(band_center_y - box_h / 2)
    text_y = round(box_y + SCRIM_PAD_Y)

    band_top, band_bottom = _text_safe_bounds(composition)
    if box_y < band_top or box_y + box_h > band_bottom:
        log.warning(
            "thumbnail: n=%d text band (%d-%d) doesn't fully fit the %d-%d safe zone for "
            "%s — %d line(s) of thumbnail_text may be long for this layout",
            n,
            box_y,
            box_y + box_h,
            band_top,
            band_bottom,
            composition,
            len(lines),
        )

    with tempfile.TemporaryDirectory(prefix="clipper_thumb_") as td:
        tmp_dir = Path(td)
        frame_path = tmp_dir / "frame.png"
        _extract_full_frame(str(source), ts, video_fc, frame_path)
        if not frame_path.exists():
            raise RuntimeError(f"thumbnail: n={n} failed to extract a frame at {ts:.2f}s")

        text_path = tmp_dir / "text.txt"
        text_path.write_text("\n".join(lines), encoding="utf-8")

        vf = (
            "eq=contrast=1.05:brightness=-0.02,"
            f"drawbox=x=0:y={box_y}:w={THUMB_WIDTH}:h={box_h}:color=black@{SCRIM_ALPHA}:t=fill,"
            f"drawtext=fontfile={CAPTION_FONT}:textfile={text_path}:fontcolor={accent_hex}:"
            f"fontsize={TEXT_FONTSIZE}:bordercolor=black:borderw=10:line_spacing={LINE_GAP}:"
            f"x=(w-text_w)/2:y={text_y}"
        )
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(frame_path),
                "-vf",
                vf,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_path),
            ]
        )

    try:
        _validate_thumbnail(str(out_path))
    except Exception:
        out_path.unlink(missing_ok=True)
        raise

    log.info(
        "thumbnail: wrote %s (%d word(s), %d line(s))",
        out_path,
        len(thumbnail_text.split()),
        len(lines),
    )
    return str(out_path)


def _format_upload_text(seg: dict) -> str:
    """Plain-text, copy-paste-ready rendering of the upload fields — no
    quotes, no JSON syntax, nothing a form field would choke on. Labeled
    blocks rather than one undifferentiated blob so "select from after
    'TITLE:' to the blank line" works as cleanly as "select all".

    Title and description ONLY — deliberately not everything
    metadata_{n}.json carries. thumbnail_text, caption_title, and tone are
    internal/render-use fields (thumbnail_text drives the burned-in
    overlay, caption_title is the video's own on-screen title, tone drives
    styling) and were never meant to be read at upload time; including them
    here would make this file something a human has to edit before
    pasting, defeating the point of a copy-paste-ready file.
    """
    title = str(seg.get("suggested_title", "")).strip()
    description = str(seg.get("suggested_description", "")).strip()
    return f"TITLE:\n{title}\n\nDESCRIPTION:\n{description}\n"


def write_metadata(video_id: str, n: int, seg: dict, *, force: bool = False) -> tuple[str, str]:
    """Write work/{video_id}/metadata_{n}.json (suggested_title,
    thumbnail_text, suggested_hashtags, suggested_description from
    segments.json — structured, for tooling) and work/{video_id}/upload_{n}.txt
    (title + description as plain text — for a human to select-all and paste
    directly into YouTube Studio's own form fields, which the JSON's quoting
    and braces are not directly usable in). Neither is embedded anywhere in
    the video or thumbnail; upload stays manual per CLAUDE.md's Out of scope.

    Returns (metadata_json_path, upload_txt_path).
    """
    wd = work_dir(video_id)
    out_path = wd / f"metadata_{n}.json"
    txt_path = wd / f"upload_{n}.txt"
    if out_path.exists() and txt_path.exists() and not force:
        log.info("thumbnail: metadata cache hit for n=%d (%s), skipping", n, out_path)
        return str(out_path), str(txt_path)

    payload = {
        "video_id": video_id,
        "n": n,
        "suggested_title": seg.get("suggested_title", ""),
        "thumbnail_text": seg.get("thumbnail_text", ""),
        "suggested_hashtags": seg.get("suggested_hashtags", []),
        "suggested_description": seg.get("suggested_description", ""),
        "caption_title": seg.get("caption_title", ""),
        "tone": seg.get("tone", ""),
    }
    out_path.write_text(json.dumps(payload, indent=2))
    txt_path.write_text(_format_upload_text(seg))
    log.info("thumbnail: wrote %s and %s", out_path, txt_path)
    return str(out_path), str(txt_path)
