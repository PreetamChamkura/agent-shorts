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
import tempfile
from pathlib import Path

from .face_detect import find_face_region, is_small_corner_facecam
from .ffmpeg import probe_duration, run_ffmpeg
from .paths import PROMPTS_DIR, work_dir
from .render import _facecam_split_video, _tight_crop_video

log = logging.getLogger("clipper.visual_judge")

JUDGE_MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096

N_SAMPLE_FRAMES = 5

# Half of the real 1080x1920 output. Judging composition and motion doesn't
# need full resolution, and this keeps image tokens down — roughly
# (540*960)/750 ~= 690 tokens/frame, ~3.5k tokens total for 5 frames.
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
    },
    "required": ["passed", "reason"],
    "additionalProperties": False,
}


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
    """One frame at ``ts``, run through ``video_fc`` (one of render.py's
    video-only filter chains, ending in [comp]) and downscaled to preview
    size — the REAL vertical composition this clip will ship in, not a
    guess about it.

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
    """
    with tempfile.TemporaryDirectory(prefix="clipper_warmup_") as td:
        pattern = Path(td) / "w_%02d.png"
        run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{ts:.3f}",
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
    return (
        template.replace("{n_frames}", str(n_frames))
        .replace("{composition_note}", composition_note)
        .replace("{facecam_question}", facecam_question)
    )


def _call_model(client, prompt: str, image_paths: list[Path], model: str) -> dict:
    """The only networked call in this module. Returns the parsed JSON verdict."""
    content: list[dict] = [{"type": "text", "text": prompt}]
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
        output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
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
    "frames_sampled": int}.
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

    import anthropic  # imported lazily so the pure helpers stay import-light

    client = anthropic.Anthropic()

    with tempfile.TemporaryDirectory(prefix="clipper_judge_") as td:
        tmp_dir = Path(td)
        frame_paths = []
        for i, ts in enumerate(timestamps):
            out_path = tmp_dir / f"frame_{i:02d}.png"
            _extract_preview_frame(str(source), ts, video_fc, out_path)
            if out_path.exists():
                frame_paths.append(out_path)

        if not frame_paths:
            raise RuntimeError(f"visual_judge: no preview frames extracted for {video_id}")

        prompt = _build_prompt(composition, len(frame_paths))
        verdict = _call_model(client, prompt, frame_paths, model)

    result = {
        "passed": bool(verdict["passed"]),
        "reason": str(verdict["reason"]),
        "composition": composition,
        "frames_sampled": len(frame_paths),
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
