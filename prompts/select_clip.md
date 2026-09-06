You are writing tone, narrator commentary, and framing for a single pre-selected clip. Someone — a viewer or the streamer — already decided this specific moment was worth saving. Your job is not to search for a moment inside a longer video; it's to package the one you're given.

## Source

Channel: {channel_name}
Clip title: {video_title}
Created: {upload_date}
Description: {video_description_first_500_chars}

## Clip transcript

Full transcript of the clip, {window_start} to {window_end}. Timestamps in [h:mm:ss] appear roughly every 15 seconds — e.g. [0:01:15] is 1 minute 15 seconds (75s).

{transcript_window}

## Task

Do not hunt for a better moment inside the clip, and do not second-guess its edges beyond the trimming described below.

1. Decide whether the clip works as a standalone vertical short at all. Return `{"segments": []}` if it doesn't — see Reject below. That is a correct and expected answer for a clip that turns out to be crosstalk, needs visual context the transcript can't confirm, or has no coherent beat.
2. If it works, set `start`/`end` to the tightest span that keeps the complete beat:
   - If the full clip already reads clean and fits 18-58 seconds, use its full span
   - If the clip runs past 58 seconds or carries dead air the beat doesn't need, trim inward to the smallest span that keeps the setup and payoff intact
   - Runs 18-58 seconds inclusive. Within that, prefer 25-40 seconds unless the setup-to-payoff arc genuinely needs more room — don't pad a tight moment out to 40s, and don't force a beat that naturally needs 45-50s into a shorter cut and lose the payoff
   - If nothing 18-58 seconds long inside the clip captures a complete beat — the clip itself is too short, or any trim that fits would cut the payoff — return `{"segments": []}` rather than forcing it. Never invent content or extend past what the transcript actually contains

Reject without exception:
- Crosstalk with no discernible point
- Anything requiring visual context the transcript can't confirm exists
- A clip whose point depends entirely on a running bit the standalone excerpt can't carry

## Narrator commentary

Write one line of VO following the persona rules. The line frames the clip; it does not describe it.

Before writing, ask: what would the viewer need to know to find this interesting that they cannot get from watching? That is the line.

If there is nothing — the clip is entirely self-explanatory — set `vo_script` to null rather than writing filler. A clip with no VO is fine. A clip with an empty VO is not.

## Tone

Assign exactly one of: burn, chaotic, wholesome, awkward, revealing, confident. Pick the one describing the payoff, not the setup. If two fit, pick the one a viewer would name after watching.

## Loop potential

Check whether the segment's ending could flow naturally back into its start if the clip repeated seamlessly — the same motion, sentence, or beat picking back up as if uninterrupted. This is rare. Most segments are not loopable, and forcing it produces a worse clip than admitting it isn't there.

Set `loopable` to `true` only when the loop would be genuinely seamless, not merely tolerable. Never trim or shift `start`/`end` to manufacture a loop — the boundaries are set by the rules above; loopability is an observation about the segment as already defined, not a target to hit.

## Output

Return only a JSON object. No prose, no markdown fences, no preamble.

{
  "segments": [
    {
      "start": 0.0,
      "end": 34.6,
      "punchline_at": 31.2,
      "tone": "revealing",
      "loopable": false,
      "hook": "six-word description",
      "caption_title": "SHORT PUNCHY TITLE",
      "vo_script": "the narrator line, or null",
      "vo_placement": "cold_open",
      "why": "setup at 2.0 establishing X, turn at 24.0, payoff at 31.2, reaction to 34.6",
      "confidence": 0.82
    }
  ]
}

Field rules:

- `start`, `end`, `punchline_at`: seconds, one decimal, absolute within this clip's own transcript (0.0 is the clip's own start)
- `punchline_at` must fall in the last third of the segment
- `why` must name the setup and payoff timestamps **in plain decimal seconds** (e.g. "payoff at 31.2", not "0:00:31.2" — the [h:mm:ss] markers above are transcript landmarks only, not the format for this field) and say what each contains. Vague entries mean the segment wasn't located and will be rejected
- `confidence` 0.0-1.0, be harsh. 0.9+ means you'd bet it performs. 0.7 is the floor. Below 0.7, omit entirely rather than returning a low score
- `caption_title`: max 5 words, no emoji, no clickbait phrasing. Write your own from the clip's content — never reuse or lightly edit the clip's own source title above, even if it happens to already be clean and short. That title is context for understanding the clip, not a draft; a viewer-submitted Twitch clip title is often crude, inside-joke-dependent, or simply not what this beat is actually about
- `vo_placement`: "cold_open" or "pre_punchline"
- `loopable`: boolean, default `false`. `true` only for a genuinely seamless repeat — most segments should stay `false`

Return `{"segments": []}` if the clip doesn't qualify. That is a correct and expected answer for a meaningful fraction of fetched clips — not every pre-made clip survives being re-packaged with commentary.
