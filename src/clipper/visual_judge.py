"""Visual gate layer 3 — vision-model judgment (CLAUDE.md step: Twitch clip
visual quality gate).

The one layer with real per-clip cost, so it only ever runs on what survives
layers 1 (thumbnail pre-filter) and 2 (facecam detection) — never the raw
fetched batch. Wired into `clipper clip` as a real gate: cli.py calls this
right after transcribe() and before select_clip(), for Twitch clip URLs
only, so a visually-rejected clip never reaches the (also costly) selection
LLM call either.

This is the layer that actually catches "boring but visible" content — the
Doc clip's original failure mode under its old tight_crop rendering: a hand
on a desk mat, not blank, but nothing worth watching. (The Doc clip now
passes under its real, current composition — facecam_split, built earlier
in this same session, already gives the facecam its own full panel and
substantially fixed that specific case. Verified this layer still catches
the original failure mode by forcing tight_crop back on and confirming
rejection with the same complaint. The Doc clip is retired as this layer's
positive-control test case for exactly that reason — CLAUDE.md Known
issues names what a genuine one should look like: a facecam clip that's
dead even with its own dedicated panel, not fixed by better composition
alone.) Pixel statistics (layer 1) and face geometry (layer 2) both
structurally cannot make this call; it's semantic, not statistical, which
is exactly why a vision model sits here instead.

Samples N_SAMPLE_FRAMES evenly across the clip's own duration, cropped
through the SAME filter graph render() would actually use (tight_crop or
facecam_split — decided by the same clipper.face_detect logic layer 2
already runs) so the model judges the real vertical composition, not a
guess about it.

Uses Claude Sonnet 5, not Opus: this is a per-clip production call that
runs at volume, and the judgment needed (is this visually engaging, not a
deep reasoning task) doesn't need Opus-level reasoning.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import tempfile
from pathlib import Path

from .audience import TIER_HIGH, describe_tier, resolve_audience_tier
from .face_detect import find_face_region, is_small_corner_facecam
from .ffmpeg import probe_duration, run_ffmpeg
from .paths import PROMPTS_DIR, work_dir
from .render import _facecam_split_video, _tight_crop_video

log = logging.getLogger("clipper.visual_judge")

JUDGE_MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096

# Cost-priority reduction from 5 (project is self-funded, prior spend wasn't
# sustainable — no comparison eval run first, by explicit instruction).
# Image tokens scale with frame count, so this cuts roughly 2/5 of this
# call's real image cost directly. Rest of the layer's logic (composition
# detection, best-frame selection, the prompt itself) is unchanged.
N_SAMPLE_FRAMES = 3

# Half of the real 1080x1920 output. Judging composition and motion doesn't
# need full resolution, and this keeps image tokens down — roughly
# (540*960)/750 ~= 690 tokens/frame, ~2.1k tokens total for 3 frames.
PREVIEW_WIDTH = 540
PREVIEW_HEIGHT = 960

# See _extract_preview_frame: the first output frame from render.py's
# multi-branch filter graphs can be solid black before the synthetic
# `color=` source and the real seeked input sync up. 5 was enough to fix
# the real failure this was found against; kept with a small margin.
_WARMUP_FRAMES = 8

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reason": {"type": "string"},
        # No "minimum" constraint — confirmed for real that output_config's
        # json_schema rejects it on an integer type ("property 'minimum' is
        # not supported", a real 400 from a live call). Range is enforced in
        # Python instead (judge_clip clamps out-of-range values to 0).
        "best_frame_index": {"type": "integer"},
        "best_frame_reason": {"type": "string"},
    },
    "required": ["passed", "reason", "best_frame_index", "best_frame_reason"],
    "additionalProperties": False,
}

# best_frame_reason must open with "Frame N:" naming the SAME N as
# best_frame_index (prompts/visual_judge.md's field rule) — mirrors
# clipper.select's why-field cross-check (why_is_located / the payoff-quote
# search): the model must commit to a checkable claim about which frame it
# means, not just describe a frame in prose disconnected from the index it
# actually returned. Anchored at the start of the string (not searched
# anywhere in it) so a reason that mentions multiple frame numbers in
# passing can't accidentally "match" on the wrong one.
_FRAME_REF_RE = re.compile(r"^\s*frame\s+(\d+)\s*:", re.IGNORECASE)


def _referenced_frame_index(best_frame_reason: str) -> int | None:
    """The frame number best_frame_reason claims to justify, or None if it
    doesn't open with the required "Frame N:" — same "unparseable means
    untrustworthy" posture as clipper.select.why_is_located."""
    m = _FRAME_REF_RE.match(best_frame_reason or "")
    return int(m.group(1)) if m else None


def _sample_timestamps(duration: float, n: int) -> list[float]:
    """n timestamps evenly spread across [0, duration], inset from the very
    edges — mirrors clipper.face_detect._sample_timestamps."""
    if n < 1:
        return []
    inset = duration * 0.05
    lo, hi = inset, duration - inset
    if n == 1:
        return [(lo + hi) / 2]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def resolve_composition(source: str, duration: float) -> tuple[str, dict | None]:
    """Which composition render() would actually pick for this clip, and the
    face region if facecam_split. Same detection logic render() uses, run
    once here rather than duplicated or guessed at — the whole point of
    this layer is judging the REAL shipped composition."""
    face_region = find_face_region(source, 0.0, duration)
    if face_region and is_small_corner_facecam(face_region):
        return "facecam_split", face_region
    return "tight_crop", None


def _extract_preview_frame(source: str, ts: float, video_fc: str, out_path: Path) -> None:
    """One frame landing ON ``ts`` (not past it), run through ``video_fc``
    (one of render.py's video-only filter chains, ending in [comp]) and
    downscaled to preview size — the REAL vertical composition this clip
    will ship in, not a guess about it.

    Requests _WARMUP_FRAMES output frames and keeps only the last, not the
    first. render()'s filter graphs mix a real seeked input with a synthetic
    `color=` source (facecam_split's [base]) feeding a multi-branch overlay
    chain — real, reproduced behavior: the two don't necessarily sync on the
    very first output frame, so a naive `-frames:v 1` can silently return a
    solid black frame despite the source having real content at that exact
    timestamp (confirmed: frame 1 of an 5-frame burst at the same timestamp
    was black, frame 5 showed the correct composited image). render() itself
    never hits this — it always encodes the full clip duration, never a
    single frame — so this warm-up is specific to still-frame extraction.

    Seeks _WARMUP_FRAMES/30s BEFORE ``ts`` and decodes forward through the
    warm-up burst, landing ON ``ts`` — not a seek-to-ts-then-keep-last, which
    lands _WARMUP_FRAMES/30s (~0.27s) PAST it. That forward drift was a real,
    confirmed bug: this preview frame is what best_frame_index judges, so
    drift here means the frame the model actually saw (and later names via
    best_frame_timestamp) isn't quite the one at the timestamp recorded —
    compounding with clipper.thumbnail's own extraction of that same
    timestamp. Fixed the same way render.py's pick_loop_freeze_end /
    _extract_loop_candidate_frames already solves the identical
    seek-then-land-exactly-here problem: seek backward by the lookahead
    window, decode forward to the target.
    """
    lead = _WARMUP_FRAMES / 30.0
    seek_ts = max(0.0, ts - lead)
    with tempfile.TemporaryDirectory(prefix="clipper_warmup_") as td:
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
                f"{video_fc};[comp]scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}[out]",
                "-map",
                "[out]",
                str(pattern),
            ]
        )
        produced = sorted(Path(td).glob("w_*.png"))
        if produced:
            produced[-1].replace(out_path)


def _build_prompt(composition: str, n_frames: int) -> str:
    template = (PROMPTS_DIR / "visual_judge.md").read_text()
    facecam_question = (
        "- If a facecam is present in the bottom panel, is it showing something — a reaction, "
        "an expression — or is the person out of frame / not visible in these samples?\n"
        if composition == "facecam_split"
        else ""
    )
    composition_note = (
        "a main-content panel on top and a facecam panel on the bottom (facecam_split)"
        if composition == "facecam_split"
        else "a single cropped shot filling the frame (tight_crop)"
    )
    # Steers best_frame_index toward the facecam panel specifically for
    # facecam_split — this pick doubles as stage 4's thumbnail frame (CLAUDE.md
    # thumbnail generation: "for a facecam_split composition, use a frame
    # where the facecam shows visible expression/reaction, not a neutral
    # moment"), so the instruction has to be explicit here, not left implicit
    # in the general engagement question above.
    facecam_best_frame_note = (
        " For facecam_split specifically, weight this toward a frame where the facecam panel "
        "shows a visible reaction or expression, not a neutral/blank one — that panel is what "
        "this pick is really being used for."
        if composition == "facecam_split"
        else ""
    )
    return (
        template.replace("{n_frames}", str(n_frames))
        .replace("{composition_note}", composition_note)
        .replace("{facecam_question}", facecam_question)
        .replace("{facecam_best_frame_note}", facecam_best_frame_note)
    )


def _log_usage(video_id: str, model: str, usage) -> None:
    """Real per-call token usage, logged unconditionally — see CLAUDE.md's
    cost investigation. Duplicated from clipper.select's own _log_usage
    rather than imported, per this project's stage-independence convention
    (each stage module stays self-contained). `thinking_tokens` is billed as
    output but never appears in the visible response text; it's the exact
    gap that made an after-the-fact cost reconstruction only a floor."""
    thinking = usage.output_tokens_details.thinking_tokens if usage.output_tokens_details else 0
    log.info(
        "visual_judge: usage %s model=%s in=%d cache_read=%d cache_write=%d out=%d(thinking=%d)",
        video_id,
        model,
        usage.input_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
        usage.output_tokens,
        thinking,
    )


def _call_model(
    client,
    video_id: str,
    prompt: str,
    image_paths: list[Path],
    model: str,
    tier_line: str,
) -> dict:
    """The only networked call in this module. Returns the parsed JSON verdict.

    ``prompt`` goes in a cached `system` block, not the user turn: real
    measurement shows this text varies only by composition (2 real values)
    and occasionally by frame count (almost always N_SAMPLE_FRAMES) — effectively
    constant, not genuinely per-clip data — so it caches at ~0.1x on every
    call after the first with the same composition. Only the real,
    genuinely-unique-per-clip images go in the user turn, where they can
    never cache anyway.

    ``tier_line`` goes in the user turn for exactly that reason. The rules for
    BOTH tiers live in the cached system prompt — byte-identical on every
    call — while only this clip's own tier travels per-call. Putting the tier
    in the system block instead would fork the cache into 4 namespaces
    (composition x tier) to carry one word of real information.
    """
    content: list[dict] = [{"type": "text", "text": f"Audience tier for this clip: {tier_line}"}]
    for path in image_paths:
        b64 = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64},
            }
        )

    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    _log_usage(video_id, model, response.usage)
    if response.stop_reason == "refusal":
        raise RuntimeError(f"visual_judge: model refused the request ({response.stop_details})")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def judge_clip(video_id: str, *, model: str = JUDGE_MODEL, force: bool = False) -> dict:
    """Layer 3: vision-model judgment of a downloaded clip's own footage.

    Reads work/{video_id}/source.mp4 (must already exist — this is the
    real-cost layer, only meant to run on clips that already survived
    layers 1 and 2). Writes/reads work/{video_id}/visual_judge.json —
    this is a real per-clip API cost, so a re-run (retrying the pipeline,
    tuning a later stage) must never re-pay for the same clip's verdict
    unless force=True, same caching discipline as every other stage.
    Returns {"passed": bool, "reason": str, "composition": str,
    "frames_sampled": int, "best_frame_timestamp": float,
    "best_frame_reason": str}.
    ``best_frame_timestamp`` is an absolute second into source.mp4 — the model
    already looked at these frames to make the pass/reject call, so it also
    names which one is most worth using as a thumbnail (CLAUDE.md thumbnail
    generation step), reusing this judgment rather than re-sampling frames
    and asking again. Consumed by clipper.thumbnail when it's inside the
    final segment's own [start, end]; falls back to its own cheap heuristic
    otherwise (e.g. select_clip trimmed the segment past this timestamp).

    ``best_frame_reason`` must open with "Frame N:" naming the same N as the
    index actually used (prompts/visual_judge.md's field rule) — cross-checked
    here the same way clipper.select checks `why` against its own payoff
    quote: a reason that names a different frame, or no frame at all, means
    the pick isn't accountable to its own stated reasoning, so best_index
    falls back to 0 rather than trusting it. This never affects `passed` —
    only which frame clipper.thumbnail is told to prefer.
    """
    wd = work_dir(video_id)
    source = wd / "source.mp4"
    cache_path = wd / "visual_judge.json"

    if cache_path.exists() and not force:
        cached = json.loads(cache_path.read_text())
        log.info(
            "visual_judge: cache hit for %s (%s) — %s",
            video_id,
            cache_path,
            "PASS" if cached["passed"] else "REJECT",
        )
        return cached

    if not source.exists():
        raise FileNotFoundError(f"visual_judge: {source} missing — run stage 1 (ingest) first.")

    duration = probe_duration(str(source))
    composition, face_region = resolve_composition(str(source), duration)
    video_fc = (
        _facecam_split_video(face_region) if composition == "facecam_split" else _tight_crop_video()
    )

    timestamps = _sample_timestamps(duration, N_SAMPLE_FRAMES)

    tier, view_count = resolve_audience_tier(wd)
    log.info(
        "visual_judge: %s audience tier %s — %s",
        video_id,
        describe_tier(tier, view_count),
        "lowered visual bar (one clear moment suffices)"
        if tier == TIER_HIGH
        else "full visual bar (sustained interest required)",
    )

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    with tempfile.TemporaryDirectory(prefix="clipper_judge_") as td:
        tmp_dir = Path(td)
        frame_paths = []
        sampled_timestamps = []  # parallel to frame_paths — see best_frame_index mapping below
        for i, ts in enumerate(timestamps):
            out_path = tmp_dir / f"frame_{i:02d}.png"
            _extract_preview_frame(str(source), ts, video_fc, out_path)
            if out_path.exists():
                frame_paths.append(out_path)
                sampled_timestamps.append(ts)

        if not frame_paths:
            raise RuntimeError(f"visual_judge: no preview frames extracted for {video_id}")

        prompt = _build_prompt(composition, len(frame_paths))
        verdict = _call_model(
            client, video_id, prompt, frame_paths, model, describe_tier(tier, view_count)
        )

    # best_frame_index refers to the model's own numbering of the images it
    # was actually shown (0..len(frame_paths)-1) — that's frame_paths' order,
    # not the original `timestamps` list, since a failed extraction can leave
    # gaps (see the loop above: only successfully-extracted frames are
    # appended). sampled_timestamps was built in lockstep with frame_paths
    # specifically so this index always lands on the real timestamp the model
    # was actually looking at.
    raw_best_index = int(verdict.get("best_frame_index", 0))
    best_frame_reason = str(verdict.get("best_frame_reason", "")).strip()
    referenced_index = _referenced_frame_index(best_frame_reason)

    # Two independent, distinctly-logged reasons a pick can't be trusted —
    # same "unparseable/inconsistent means untrustworthy" posture as
    # clipper.select's why-field checks, and the same "log distinctly so
    # real frequency is visible" convention as render.py's corner-check
    # fallback. Both fall back to frame 0 rather than crashing a passing
    # verdict over a secondary (thumbnail-only) data point.
    if not (0 <= raw_best_index < len(sampled_timestamps)):
        log.warning(
            "visual_judge: %s best_frame_index=%d out of range for %d sampled frame(s) — using 0",
            video_id,
            raw_best_index,
            len(sampled_timestamps),
        )
        best_index = 0
    elif referenced_index != raw_best_index:
        log.warning(
            "visual_judge: %s best_frame_reason doesn't name best_frame_index=%d (parsed "
            "reference: %s) — pick isn't accountable to its own reasoning, using frame 0. "
            "best_frame_reason: %r",
            video_id,
            raw_best_index,
            referenced_index,
            best_frame_reason,
        )
        best_index = 0
    else:
        best_index = raw_best_index
    best_frame_timestamp = sampled_timestamps[best_index]

    result = {
        "passed": bool(verdict["passed"]),
        "reason": str(verdict["reason"]),
        "composition": composition,
        "frames_sampled": len(frame_paths),
        "best_frame_timestamp": best_frame_timestamp,
        "best_frame_reason": best_frame_reason,
        # Recorded, not just used: a verdict reached under the lowered HIGH
        # bar is a different judgment from one reached under the full bar,
        # and six months from now the cached json is the only place that says
        # which was applied.
        "audience_tier": tier,
        "source_view_count": view_count,
    }
    log.info(
        "visual_judge: %s %s (%s, %d frame(s)) — %s",
        video_id,
        "PASS" if result["passed"] else "REJECT",
        composition,
        len(frame_paths),
        result["reason"],
    )
    cache_path.write_text(json.dumps(result, indent=2))
    return result
