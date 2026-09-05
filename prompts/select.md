You are selecting short-form clips from a long-form video transcript and writing one line of narrator commentary for each.

## Source

Title: {video_title}
Channel: {channel_name}
Uploaded: {upload_date}
Description: {video_description_first_500_chars}

## Transcript window

Window {window_index} of {window_total}, covering {window_start} to {window_end}. Timestamps in [mm:ss] appear roughly every 15 seconds.

{transcript_window}

## Task

Find up to 3 self-contained moments that would work as standalone vertical shorts. Return fewer — including zero — if the window doesn't contain them. Most windows contain zero or one. Three mediocre segments is worse than one good one.

A moment qualifies only if all are true:

- Comprehensible to someone who hasn't seen the rest of the video. No dangling pronouns pointing outside the window, no callbacks to jokes established elsewhere
- Has beat structure: something builds, then something lands. A single funny sentence with no setup is not a clip
- The payoff falls in the last third. If the best moment is at the start, move the boundaries so it isn't
- Runs 18-58 seconds inclusive

Reject without exception:
- Crosstalk with no discernible point
- Anything requiring visual context the transcript can't confirm exists
- Sponsor reads, intros, outros, subscribe requests
- Moments whose humor depends entirely on a running bit from elsewhere

## Narrator commentary

Write one line of VO per segment following the persona rules. The line frames the clip; it does not describe it.

Before writing, ask: what would the viewer need to know to find this interesting that they cannot get from watching? That is the line.

If there is nothing — the clip is entirely self-explanatory — set `vo_script` to null rather than writing filler. A clip with no VO is fine. A clip with an empty VO is not.

## Tone

Assign exactly one of: burn, chaotic, wholesome, awkward, revealing, confident. Pick the one describing the payoff, not the setup. If two fit, pick the one a viewer would name after watching.

## Loop potential

Check whether the segment's ending could flow naturally back into its start if the clip repeated seamlessly — the same motion, sentence, or beat picking back up as if uninterrupted. This is rare. Most segments are not loopable, and forcing it produces a worse clip than admitting it isn't there.

Set `loopable` to `true` only when the loop would be genuinely seamless, not merely tolerable. Never trim or shift `start`/`end` to manufacture a loop — the boundaries are set by the beat-structure rules above; loopability is an observation about the segment as already defined, not a target to hit.

## Output

Return only a JSON object. No prose, no markdown fences, no preamble.

{
  "segments": [
    {
      "start": 412.8,
      "end": 441.2,
      "punchline_at": 437.1,
      "tone": "revealing",
      "loopable": false,
      "hook": "six-word description",
      "caption_title": "SHORT PUNCHY TITLE",
      "vo_script": "the narrator line, or null",
      "vo_placement": "cold_open",
      "why": "setup at 412.8 establishing X, turn at 431, payoff at 437.1, reaction to 441.2",
      "confidence": 0.82
    }
  ]
}

Field rules:

- `start`, `end`, `punchline_at`: seconds, one decimal, absolute within the full video, not relative to the window
- `punchline_at` must fall in the last third of the segment
- `why` must name the setup and payoff timestamps and what each contains. Vague entries mean the segment wasn't located and will be rejected
- `confidence` 0.0-1.0, be harsh. 0.9+ means you'd bet it performs. 0.7 is the floor. Below 0.7, omit entirely rather than returning a low score
- `caption_title`: max 5 words, no emoji, no clickbait phrasing
- `vo_placement`: "cold_open" or "pre_punchline"
- `loopable`: boolean, default `false`. `true` only for a genuinely seamless repeat — most segments should stay `false`

Return {"segments": []} if nothing qualifies. That is a correct and expected answer for most windows.
