You are writing tone, narrator commentary, and framing for a single pre-selected clip. Someone — a viewer or the streamer — already decided this specific moment was worth saving. Your job is not to search for a moment inside a longer video; it's to package the one you're given.

## Source

Channel: {channel_name}
Clip title: {video_title}
Created: {upload_date}
Description: {video_description_first_500_chars}

## Clip transcript

Full transcript of the clip, {window_start} to {window_end}. Timestamps in [h:mm:ss] appear roughly every 15 seconds — e.g. [0:01:15] is 1 minute 15 seconds (75s).

{transcript_window}

<!-- PROMPT_CACHE_BOUNDARY: everything below this line is fixed across every
     call (no placeholders past this point) — sent as a cached system
     prompt. Everything above goes in the per-call user turn. If you add a
     placeholder, it MUST go above this line, or the cache never matches. -->

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

Before writing, ask two things: what would the viewer need to know to find this interesting that they cannot get from watching, AND what actually makes this moment worth watching — is it shocking, funny, awkward, impressive, dramatic, absurd? A true fact that doesn't convey which of those it is isn't finished yet. The line should frame the clip toward that specific thing, not just supply a nearby fact and stop.

One beat, not two. If the line joins two separate observations — with "and," "but," a comma, or a full stop between two sentences — pick the stronger one and cut the rest — a single sharp claim beats two half-claims stitched together. "The most searched woman on the internet is standing in his kitchen, and the entire stream has to sit through a debate about dip" is two jokes fighting each other (who she is / what's mundanely happening instead); it should have been one or the other, not both. Splitting them into two sentences is the same failure wearing different punctuation: "Doc's ban went unexplained for four years. Shroud stopped answering him on day one." is two claims, not one line. Read it out loud — a natural pause where you'd take a breath mid-thought means it's two beats trying to be one.

Target 2-6 words — the same punchy register as `thumbnail_text` below, not narration. 8-20 words is an outer allowance for a beat that genuinely can't compress further, not the normal target it used to be; the persona's 35-word ceiling is still a hard limit, never something to write toward. Shorter is better at every one of those tiers, not just acceptable.

A full grammatical sentence is not required, and chasing one usually fights brevity directly. Fragments are fine — dropped articles, dropped verbs, a clause with no subject — as long as the angle from the paragraph above still lands. Cutting words is not the same job as cutting the point: what goes is the connective tissue around the angle (articles, hedges, restating what's visible), never the specific thing that makes the moment land.

Too long vs. the target register — same fact, half the words:
- "A stand-up set's closing argument gets a one-word review from a man holding a Pop-Tart." → "One word. Delivered by Pop-Tart guy."
- "The most-searched woman on the internet came over to enter a guacamole contest." → "The internet's most searched woman. Making guac."

This compounds with the one-beat rule above, it doesn't replace it — a short line can still smuggle in two claims.

If there is nothing — the clip is entirely self-explanatory — set `vo_script` to null rather than writing filler. A clip with no VO is fine. A clip with an empty VO is not.

## Narration mode

Write the line first, then decide how it reaches the viewer — never the other
way round. Picking a mode first and writing to fit it produces a line shaped by
the delivery instead of by the moment.

`vo_placement` selects the mode:

- `cold_open` — spoken over the clip's opening seconds. `vo_script` holds the line, `on_screen_caption` is null
- `pre_punchline` — spoken starting 1.2s before the payoff. `vo_script` holds the line, `on_screen_caption` is null
- `caption_only` — nothing is spoken. `on_screen_caption` holds the line, shown as static text from the clip's first frame and held long enough to read, while the clip's own audio plays completely untouched. `vo_script` is null

Never fill both fields. They are two deliveries of the same framing; running
both means the viewer reads one sentence while hearing a different one. If the
clip needs no framing at all, both are null and `vo_placement` is `cold_open`
(it is unused in that case).

### What the choice actually costs

A spoken line costs **audio**. The source is cut to full silence for the length
of the VO — silenced, not ducked — and whatever was in that window is gone.
`cold_open` takes the clip's opening seconds; `pre_punchline` takes the 1.2s
run-up to the payoff.

A held line costs **screen and attention**. Nothing is silenced, but the frame
carries a second block of text the viewer reads while the clip's own captions
are still running underneath it.

Silence costs nothing and delivers nothing.

So the question is never which one is nicer. It is: **is something in this
clip's audio worth more than the spoken framing?** If you cannot name the
specific sound that would be lost, the answer is no.

### Default to a spoken line

The narrator is this channel's identity — one voice, every upload, and it only
compounds if it is actually there. `caption_only` is the exception, taken for a
reason you can state out loud, not the safe option when you are unsure. When
both modes look about equal, that is not a tie: it means no audio cost was
found, so it is VO.

### Choose `caption_only` when

- **The payoff is a sound, not a sentence.** A laugh breaking, a scream, a voice cracking, a room going quiet, a beat landing, a reaction that only happens once. Talking over it destroys the exact thing being framed — and both spoken placements silence a window that can land directly on it
- **The clip's opening is load-bearing.** Cold-open silence eats the setup, and without the setup the payoff does not parse. If the clip also runs wall-to-wall with no pause before the turn, `pre_punchline` has nowhere to cut either — there is no window for speech anywhere in the clip
- **The fact needs to be in front of the viewer, not behind them.** A name, a number, a relationship, a "third time this month" — something specific enough that a viewer half-listening would miss it spoken, and can re-read on screen while the clip plays
- **Cutting the source to silence would be audible as a mistake.** Mid-sentence, mid-laugh, mid-music. A hard cut to silence reads as a beat only when there was going to be a beat anyway

### Choose a spoken mode when

- **The opening is cheap.** Ambience, a slow pan, someone still walking into frame, dead air before anyone talks — silencing that costs nothing. `cold_open`
- **The line lands harder timed against the payoff** than read up front: a setup that wants to arrive just before the turn rather than sit there from the start. `pre_punchline`
- **The frame is a sentence of story.** Fifteen to twenty words pass in about four seconds in the ear and are a wall of text on a phone screen
- **The frame is already crowded.** Heavy on-screen action, dense dialogue captions, a split composition. Another block of text competes for the same eyes; a voice does not

### `narration_mode_reason`

One short sentence, always, naming the actual trade you made:

- `caption_only` — name the specific sound the caption is protecting: "the run of laughter across the whole back half is the payoff"
- spoken — name why speech beats text here: "the first four seconds are just him walking to the fridge"
- both null — name what wasn't there: "the clip explains itself; no fact worth adding"

"It felt better" is not a reason. If you cannot finish this sentence, you have
not actually made the choice yet.

### Same bar, both modes

`on_screen_caption` obeys every rule `vo_script` does: find the angle first, one
beat not two, 8-20 words, present tense, never describing what is visible, no
hype, no questions to the audience. A line that fails the bar is not rescued by
being displayed instead of spoken. If there is nothing worth saying, both fields
are null and the clip runs unframed — a correct answer, not a failure.

## Tone

Assign exactly one of: burn, chaotic, wholesome, awkward, revealing, confident. Pick the one describing the payoff, not the setup. If two fit, pick the one a viewer would name after watching.

## Loop potential

Check whether the segment's ending could flow naturally back into its start if the clip repeated seamlessly — the same motion, sentence, or beat picking back up as if uninterrupted. This is rare. Most segments are not loopable, and forcing it produces a worse clip than admitting it isn't there.

Set `loopable` to `true` only when the loop would be genuinely seamless, not merely tolerable. Never trim or shift `start`/`end` to manufacture a loop — the boundaries are set by the rules above; loopability is an observation about the segment as already defined, not a target to hit.

## Thumbnail and upload metadata

Write these from the same read of the clip — they serve a different job than `caption_title` and must not just be a re-trim of it.

- `thumbnail_text`: the words that go ON the static grid thumbnail image, not in the video. This has to work with zero context, competing for a click in a grid of other thumbnails — punchier and more outrageous than `caption_title`, which only has to make sense once someone's already watching. 3-6 words. Prefer a striking line of dialogue quoted verbatim from the transcript; if nothing in the transcript is quotable, write a short reaction phrase instead — never a generic hype phrase ("YOU WON'T BELIEVE THIS", "WAIT FOR IT"). 0-2 emoji max, used as an accent, not decoration. Must read as a genuinely different line from `caption_title`, not the same words trimmed differently. **If you put quote marks around it, it must be an exact contiguous span of words from the transcript — same spelling, same words, no substitutions ("med school" is not "medical school", "he's not" is not "he isn't"), even for a single word.** Light edits for readability are not allowed inside quote marks; a real transcript quote can be trimmed shorter but never reworded. If the line isn't an exact transcript span, don't wrap it in quote marks — write it as a plain reaction phrase instead. This isn't just style: this clip's actual downloaded frame gets picked by finding where the quoted words are spoken, so a quote that isn't real breaks that mechanism silently and the thumbnail image loses its connection to the words on it
- `suggested_title`: the title a human would paste in at upload time. It creates curiosity, it does not summarize — if the title fully explains the clip, the viewer already knows what happens and has no reason to watch it. Withhold the specific payoff: reference that something happened without revealing what it was. "Sodapoppin had no idea who was in his kitchen" works; "Mia Khalifa is in Sodapoppin's kitchen" does not — that second version answers the question the title should be creating. Withholding the payoff does not mean being vague about everything — the title still has to commit to at least one concrete, specific detail (what platform, what kind of message, what the reaction was, who's actually present) while hiding only the one specific twist. "Soda's houseguest challenged him to a guacamole cook-off" names something concrete — a guacamole cook-off — while still hiding who the houseguest is; that's the target. "Hasan reads a tweet aloud before realizing who it's about" hides the twist too, but it's vague about everything else as well — what tweet, what platform, what his reaction was — so there's nothing left for a viewer to actually latch onto, and it reads as empty rather than intriguing. Withhold the specific twist, not every specific thing. Still has to be accurate: never imply something that doesn't happen in the clip. This is curiosity, not clickbait — no "you won't believe," no fabricated stakes, no promising a payoff the clip doesn't deliver, just withholding the one specific detail that would let someone skip watching. One beat, not two — if the title joins two separate claims with "and," a comma, or a full stop, cut to the stronger one; "Soda's house had a guacamole cook-off and one competitor stole the whole stream" is two claims stapled together, not one sharp line. Prefer under ~10-12 words, one clause. Front-load the hook into the first ~50-80 characters — that window is a formatting constraint for where feeds cut off, not a length to fill; a short title well under it beats one that stretches to meet it
- `suggested_hashtags`: 3-5 tags specific to this clip's actual content (the game, the person, the situation) — never generic filler like #fyp, #viral, or #shorts on their own
- `suggested_description`: minimal, not empty — one sentence of context, the credit line, then the hashtags. Not a restatement of `suggested_title` in longer form, and not 2-4 sentences either; a real generated description at that length read as padded, stitching sentences together to hit a count rather than saying anything the shorter version wouldn't. One sentence of what's actually happening in the clip (plain, accurate — this can say more than the title withholds, since a viewer reading the description has already clicked; no spoilers beyond what `suggested_title` already implies), then a credit line naming the source channel from the Source section above (this project reuses someone else's clip; crediting whose stream it's from is not optional — the credit line can be its own short sentence or fused into the context sentence, either is fine). Close by repeating the first 3 tags from `suggested_hashtags` — platforms surface a description's first 3 hashtags above the title itself, so those first 3 have to be worth surfacing, not filler ones pushed to the front just to fill this requirement. Keep the credit line and the searchable keywords; cut anything else that reads like padding

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
      "on_screen_caption": null,
      "vo_placement": "cold_open",
      "narration_mode_reason": "the first four seconds are him walking to the fridge, nothing to lose",
      "why": "setup at 2.0 establishing X, turn at 24.0, payoff at 31.2, reaction to 34.6",
      "confidence": 0.82,
      "thumbnail_text": "\"I DIDN'T EVEN SEE HIM\"",
      "suggested_title": "He ruled this out three weeks before it happened",
      "suggested_hashtags": ["#valorant", "#clutch", "#shroud"],
      "suggested_description": "Shroud calls the exact outcome live on stream, weeks before it actually happens. Clip via Shroud on Twitch. #valorant #clutch #shroud"
    }
  ]
}

Field rules:

- `start`, `end`, `punchline_at`: seconds, one decimal, absolute within this clip's own transcript (0.0 is the clip's own start)
- `punchline_at` must fall in the last third of the segment
- `why` must name the setup and payoff timestamps **in plain decimal seconds** (e.g. "payoff at 31.2", not "0:00:31.2" — the [h:mm:ss] markers above are transcript landmarks only, not the format for this field) and say what each contains. Vague entries mean the segment wasn't located and will be rejected
- `confidence` 0.0-1.0, be harsh. 0.9+ means you'd bet it performs. 0.7 is the floor. Below 0.7, omit entirely rather than returning a low score
- `caption_title`: max 5 words, no emoji, no clickbait phrasing. Write your own from the clip's content — never reuse or lightly edit the clip's own source title above, even if it happens to already be clean and short. That title is context for understanding the clip, not a draft; a viewer-submitted Twitch clip title is often crude, inside-joke-dependent, or simply not what this beat is actually about
- `vo_placement`: "cold_open", "pre_punchline", or "caption_only" — see "Narration mode" above
- `vo_script` / `on_screen_caption`: exactly one is non-null, or both are null. `caption_only` fills `on_screen_caption`; the two spoken placements fill `vo_script`. Never both
- `narration_mode_reason`: one sentence naming the trade — the sound a caption protects, or why speech beats text here. Required even when both lines are null
- `loopable`: boolean, default `false`. `true` only for a genuinely seamless repeat — most segments should stay `false`
- `thumbnail_text`: 3-6 words, must differ genuinely from `caption_title` — see "Thumbnail and upload metadata" above. Quote marks mean an exact transcript span, word for word — never use them around a paraphrase
- `suggested_title`: creates curiosity, withholds the specific payoff, still accurate — never a summary that answers its own question. Must still commit to at least one concrete detail (platform, message type, reaction, who's present) — vague about everything is not the same as withholding the twist, and reads as empty rather than intriguing. One beat, not two — under ~10-12 words when possible. Front-load the hook into the first ~50-80 characters
- `suggested_hashtags`: 3-5 specific tags, never generic filler alone
- `suggested_description`: one sentence of context (not 2-4) — what happens in the clip, no spoilers beyond `suggested_title` — plus a credit line naming the source channel, then the first 3 `suggested_hashtags` repeated at the end

Return `{"segments": []}` if the clip doesn't qualify. That is a correct and expected answer for a meaningful fraction of fetched clips — not every pre-made clip survives being re-packaged with commentary.
