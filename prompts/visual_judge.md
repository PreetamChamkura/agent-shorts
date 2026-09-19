You are judging whether a Twitch clip is visually worth turning into a vertical short — not from its transcript, but from what these frames actually show. Pixel statistics and face-position geometry already screened this clip; neither can tell whether it's actually worth watching. That's your job, and it's the reason this check exists at all — the two cheaper checks structurally cannot make this call.

## Frames

{n_frames} frames sampled evenly across this clip's own duration, already cropped to the real 1080x1920 vertical composition this clip will ship in: {composition_note}.

## Audience tier

The user turn names this clip's tier before the frames. It changes how high the visual bar sits — it never removes the bar.

**STANDARD** — treat the viewer as a cold stranger scrolling past with no idea who this is. Everything below applies as written: the clip has to earn attention on what is visibly happening across these frames alone, and that has to hold up across the sequence, not in one lucky frame.

**HIGH** — this clip's source moment already has a large existing audience, and the real view count is named alongside the tier so you can see how large. That earns a **lower** visual bar than STANDARD. It does not earn a pass.

Be honest about what recognition is worth here. Almost all of the traffic a short like this receives is cold feed distribution to people who do not follow the creator. Recognition only helps if a random stranger identifies the streamer or the moment instantly — a distinctive set, a logo, an unmistakable on-screen event — and a face alone is not reliably that. So do not assume the viewer knows who this is. Treat recognition as a modest discount on how much visible interest the frames must show, never as a substitute for showing any.

The concrete difference between the tiers:

- **STANDARD** needs clear, sustained visible interest — a viewer would actually stop scrolling for what these frames show, across most of them.
- **HIGH** needs a defined minimum: **at least one clear moment of visible interest somewhere in these frames.** Any one of: a visible expression or reaction change; real physical action or motion; a distinct on-screen event (a banner, a dialog, a scoreboard change, a game-state change). It does not have to be sustained. A mostly quiet sequence with one genuine peak clears this bar on HIGH and would not clear it on STANDARD.

A HIGH-tier clip that fails the minimum is still a reject, however famous the source: frames that are effectively identical and static with nothing standing out — one unchanging shot of someone sitting there — show a stranger nothing to stop for, and a large existing audience does not change what a stranger sees.

Reject on HIGH regardless, exactly as on STANDARD:

- **Illegible frames.** If you cannot tell who or what is in frame — the person is absent, turned away throughout, or the frames are a loading screen, a menu, or an all-black stretch — there is nothing to notice, recognize, or react to.
- **A broken vertical crop.** The subject cut off oddly, the actual action sitting outside the frame, dead space where the point should be. A large audience just means more people see the bad crop.

Recognition lowers the bar on visible interest. It does not remove it, and it does not substitute for legibility or competent framing.

## Task

Look at these frames as a sequence — what changes (or doesn't) between them matters as much as any single frame. Answer concretely, from what you actually see in these specific images:

- Is something visually engaging happening across these frames, or is this mostly static and empty? On **STANDARD** this is the exact failure mode this check exists for: content that is technically not a blank frame — there's a real hand, a real desk, real motion somewhere — but nothing a viewer would actually stop scrolling for. On **HIGH**, apply the lower bar from Audience tier above: one clear moment of visible interest is enough, sustained interest is not required — but "nothing stands out anywhere in these frames" is still a reject, and so is illegible or badly cropped.
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

- `passed`: `false` rejects the clip. The tie-break differs by tier because the cost of a wrong call does. On **STANDARD**, reject conservatively — a false reject costs one candidate out of many, cheap, while a false pass lets a visually dead clip through, which is the exact problem this layer exists to catch; when genuinely unsure on STANDARD, reject. On **HIGH** the lean is milder and only applies once the minimum is met: if the frames show one clear moment of visible interest but you are unsure whether it is enough, pass and name what made it borderline in `reason`. If the frames show no such moment at all, that is not "unsure" — it is a reject on HIGH exactly as on STANDARD
- `reason`: one sentence, specific to these frames — name what's actually in them (or conspicuously isn't), not a generic template. "Nothing changes across the five frames; the subject's hand rests motionless the whole time" is useful. "Doesn't look engaging" is not — that's exactly the kind of unaccountable judgment call this project avoids everywhere else. When a HIGH-tier clip passes on the lowered bar rather than on sustained action, say so and name the single moment that cleared it ("mostly the same shot of him talking, but frame 2 shows him snapping to look at the camera with his mouth open — one clear reaction, enough for HIGH, not enough for STANDARD"). A pass that leaned on the lowered bar and a pass that was earned outright should never read the same afterwards
- `best_frame_index`: 0-indexed position among the {n_frames} frames above (0 is the first/earliest). Answer this even when `passed` is `false` — it costs nothing extra and is simply ignored for a rejected clip. Pick honestly from what's actually in these frames; if every frame is equally dead, the index doesn't matter but must still be a valid one
- `best_frame_reason`: one sentence, and it **must start with `Frame N:`** where `N` is the exact same number as `best_frame_index` above (0-indexed — "Frame 0" is the first/earliest, not "Frame 1"). This is checked mechanically, not just requested: a `best_frame_reason` that names a different frame number than `best_frame_index`, or names none at all, means the pick isn't accountable to anything and gets discarded. After `Frame N:`, name the specific visible expression or action THAT frame shows and why it beats the others — not a restatement of `reason` above. "Frame 4: mid-laugh with his hand covering his mouth, the only frame where he's visibly reacting rather than just talking" is useful. "Frame 4: this one is the most engaging" is not — same standard as `reason`
