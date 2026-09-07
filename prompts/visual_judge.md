You are judging whether a Twitch clip is visually worth turning into a vertical short — not from its transcript, but from what these frames actually show. Pixel statistics and face-position geometry already screened this clip; neither can tell whether it's actually worth watching. That's your job, and it's the reason this check exists at all — the two cheaper checks structurally cannot make this call.

## Frames

{n_frames} frames sampled evenly across this clip's own duration, already cropped to the real 1080x1920 vertical composition this clip will ship in: {composition_note}.

## Task

Look at these frames as a sequence — what changes (or doesn't) between them matters as much as any single frame. Answer concretely, from what you actually see in these specific images:

- Is something visually engaging happening across these frames, or is this mostly static and empty? This is the exact failure mode this check exists for: content that is technically not a blank frame — there's a real hand, a real desk, real motion somewhere — but nothing a viewer would actually stop scrolling for.
{facecam_question}- Does the framing look intentional once cropped to vertical, or does it look like an accidental, awkward crop — the subject cut off oddly, the actual action happening outside the frame, dead space sitting where the point of the clip should be?
- Which single frame, if someone saw only that one frame as a thumbnail, would make them want to click? Pick the most visually engaging one of the {n_frames} — the peak of whatever action or expression is happening, not a neutral or in-between moment.{facecam_best_frame_note} This is a separate, specific decision from the engagement question above — answer it on its own terms, not as a byproduct of your overall reasoning about the sequence.

## Output

Return only a JSON object. No prose, no markdown fences, no preamble.

{
  "passed": true,
  "reason": "one concrete sentence naming what you actually saw across the frames",
  "best_frame_index": 2,
  "best_frame_reason": "Frame 2: he leans back mid-laugh, mouth open and shoulders raised — the most animated moment of the five."
}

Field rules:

- `passed`: `false` rejects the clip. Reject conservatively — a false reject here just costs one candidate clip, cheap. A false pass lets a visually dead clip through, which is the exact problem this layer exists to catch. When genuinely unsure, reject
- `reason`: one sentence, specific to these frames — name what's actually in them (or conspicuously isn't), not a generic template. "Nothing changes across the five frames; the subject's hand rests motionless the whole time" is useful. "Doesn't look engaging" is not — that's exactly the kind of unaccountable judgment call this project avoids everywhere else
- `best_frame_index`: 0-indexed position among the {n_frames} frames above (0 is the first/earliest). Answer this even when `passed` is `false` — it costs nothing extra and is simply ignored for a rejected clip. Pick honestly from what's actually in these frames; if every frame is equally dead, the index doesn't matter but must still be a valid one
- `best_frame_reason`: one sentence, and it **must start with `Frame N:`** where `N` is the exact same number as `best_frame_index` above (0-indexed — "Frame 0" is the first/earliest, not "Frame 1"). This is checked mechanically, not just requested: a `best_frame_reason` that names a different frame number than `best_frame_index`, or names none at all, means the pick isn't accountable to anything and gets discarded. After `Frame N:`, name the specific visible expression or action THAT frame shows and why it beats the others — not a restatement of `reason` above. "Frame 4: mid-laugh with his hand covering his mouth, the only frame where he's visibly reacting rather than just talking" is useful. "Frame 4: this one is the most engaging" is not — same standard as `reason`
