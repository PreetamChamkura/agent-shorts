You are judging whether a Twitch clip is visually worth turning into a vertical short — not from its transcript, but from what these frames actually show. Pixel statistics and face-position geometry already screened this clip; neither can tell whether it's actually worth watching. That's your job, and it's the reason this check exists at all — the two cheaper checks structurally cannot make this call.

## Frames

{n_frames} frames sampled evenly across this clip's own duration, already cropped to the real 1080x1920 vertical composition this clip will ship in: {composition_note}.

## Task

Look at these frames as a sequence — what changes (or doesn't) between them matters as much as any single frame. Answer concretely, from what you actually see in these specific images:

- Is something visually engaging happening across these frames, or is this mostly static and empty? This is the exact failure mode this check exists for: content that is technically not a blank frame — there's a real hand, a real desk, real motion somewhere — but nothing a viewer would actually stop scrolling for.
{facecam_question}- Does the framing look intentional once cropped to vertical, or does it look like an accidental, awkward crop — the subject cut off oddly, the actual action happening outside the frame, dead space sitting where the point of the clip should be?

## Output

Return only a JSON object. No prose, no markdown fences, no preamble.

{
  "passed": true,
  "reason": "one concrete sentence naming what you actually saw across the frames"
}

Field rules:

- `passed`: `false` rejects the clip. Reject conservatively — a false reject here just costs one candidate clip, cheap. A false pass lets a visually dead clip through, which is the exact problem this layer exists to catch. When genuinely unsure, reject
- `reason`: one sentence, specific to these frames — name what's actually in them (or conspicuously isn't), not a generic template. "Nothing changes across the five frames; the subject's hand rests motionless the whole time" is useful. "Doesn't look engaging" is not — that's exactly the kind of unaccountable judgment call this project avoids everywhere else
