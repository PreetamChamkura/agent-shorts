You are selecting short-form clips from a long-form video transcript and writing one line of narrator commentary for each.

## Source

Title: {video_title}
Channel: {channel_name}
Uploaded: {upload_date}
Description: {video_description_first_500_chars}

## Transcript window

Window {window_index} of {window_total}, covering {window_start} to {window_end}. Timestamps in [h:mm:ss] appear roughly every 15 seconds — e.g. [1:15:45] is 1 hour, 15 minutes, 45 seconds (4545s absolute), not "115 minutes."

{transcript_window}

## Task

Find up to 3 self-contained moments that would work as standalone vertical shorts. Return fewer — including zero — if the window doesn't contain them. Most windows contain zero or one. Three mediocre segments is worse than one good one.

A moment qualifies only if all are true:

- Comprehensible to someone who hasn't seen the rest of the video. No dangling pronouns pointing outside the window, no callbacks to jokes established elsewhere
- Has beat structure: something builds, then something lands. A single funny sentence with no setup is not a clip
- The payoff falls in the last third. If the best moment is at the start, move the boundaries so it isn't
- Runs 18-58 seconds inclusive. Within that range, prefer 25-40 seconds unless the content genuinely needs more room to complete its setup-to-payoff arc — don't pad a tight moment out to 40s, and don't force a beat that naturally needs 45-50s into a shorter cut and lose the payoff. This is a soft preference: the 18-58s range above and the reject-rather-than-pad rule are what actually gate a segment

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

## Thumbnail and upload metadata

Write these from the same read of the segment — they serve a different job than `caption_title` and must not just be a re-trim of it.

- `thumbnail_text`: the words that go ON the static grid thumbnail image, not in the video. This has to work with zero context, competing for a click in a grid of other thumbnails — punchier and more outrageous than `caption_title`, which only has to make sense once someone's already watching. 3-6 words. Prefer a striking line of dialogue quoted verbatim from the transcript; if nothing in the transcript is quotable, write a short reaction phrase instead — never a generic hype phrase ("YOU WON'T BELIEVE THIS", "WAIT FOR IT"). 0-2 emoji max, used as an accent, not decoration. Must read as a genuinely different line from `caption_title`, not the same words trimmed differently. **If you put quote marks around it, it must be an exact contiguous span of words from the transcript — same spelling, same words, no substitutions ("med school" is not "medical school", "he's not" is not "he isn't"), even for a single word.** Light edits for readability are not allowed inside quote marks; a real transcript quote can be trimmed shorter but never reworded. If the line isn't an exact transcript span, don't wrap it in quote marks — write it as a plain reaction phrase instead. This isn't just style: this clip's actual downloaded frame gets picked by finding where the quoted words are spoken, so a quote that isn't real breaks that mechanism silently and the thumbnail image loses its connection to the words on it
- `suggested_title`: the title a human would paste in at upload time. Front-load the actual hook into the first ~50-80 characters — feeds cut titles off past that window, so anything after it should be allowed to be lost without the title losing its point
- `suggested_hashtags`: 3-5 tags specific to this clip's actual content (the game, the person, the situation) — never generic filler like #fyp, #viral, or #shorts on their own

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
      "confidence": 0.82,
      "thumbnail_text": "\"I DIDN'T EVEN SEE HIM\"",
      "suggested_title": "He ruled this out three weeks before it happened",
      "suggested_hashtags": ["#valorant", "#clutch", "#shroud"]
    }
  ]
}

Field rules:

- `start`, `end`, `punchline_at`: seconds, one decimal, absolute within the full video, not relative to the window
- `punchline_at` must fall in the last third of the segment
- `why` must name the setup and payoff timestamps **in plain decimal seconds** (e.g. "payoff at 437.1", not "0:07:17.1" — the [h:mm:ss] markers above are transcript landmarks only, not the format for this field) and say what each contains. Vague entries mean the segment wasn't located and will be rejected
- `confidence` 0.0-1.0, be harsh. 0.9+ means you'd bet it performs. 0.7 is the floor. Below 0.7, omit entirely rather than returning a low score
- `caption_title`: max 5 words, no emoji, no clickbait phrasing. Write your own from the segment's content — never reuse or lightly edit the source video's own title above, even if it happens to already be clean and short. That title is context, not a draft
- `vo_placement`: "cold_open" or "pre_punchline"
- `loopable`: boolean, default `false`. `true` only for a genuinely seamless repeat — most segments should stay `false`
- `thumbnail_text`: 3-6 words, must differ genuinely from `caption_title` — see "Thumbnail and upload metadata" above. Quote marks mean an exact transcript span, word for word — never use them around a paraphrase
- `suggested_title`: front-load the hook into the first ~50-80 characters
- `suggested_hashtags`: 3-5 specific tags, never generic filler alone

Return {"segments": []} if nothing qualifies. That is a correct and expected answer for most windows.
