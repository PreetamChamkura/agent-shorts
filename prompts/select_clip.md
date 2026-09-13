You are writing tone, narrator commentary, and framing for a single pre-selected clip. Someone — a viewer or the streamer — already decided this specific moment was worth saving. Your job is not to search for a moment inside a longer video; it's to package the one you're given.

## Source

Channel: {channel_name}
Clip title: {video_title}
Created: {upload_date}
Description: {video_description_first_500_chars}
Audience tier: {audience_tier}

## Clip transcript

Full transcript of the clip, {window_start} to {window_end}. Timestamps in [h:mm:ss] appear roughly every 15 seconds — e.g. [0:01:15] is 1 minute 15 seconds (75s).

{transcript_window}

<!-- PROMPT_CACHE_BOUNDARY: everything below this line is fixed across every
     call (no placeholders past this point) — sent as a cached system
     prompt. Everything above goes in the per-call user turn. If you add a
     placeholder, it MUST go above this line, or the cache never matches. -->

## Audience tier

The Source block names this clip's tier. It changes what counts as a clip worth packaging, and it changes what the narrator line is for.

**STANDARD** — the default, and every rule in this prompt applies exactly as written. Assume the viewer has never heard of this creator: the clip has to stand on its own spoken beat, and if there is no locatable setup-to-payoff arc in the transcript, `{"segments": []}` is the correct answer.

**HIGH** — this clip's source moment already has a large existing audience, and the real view count is named alongside the tier. The people a short of this reaches are substantially people who already recognize the creator or the moment, and recognition is itself the reason they watch. On this tier recognition substitutes for a strict setup-to-payoff structure.

What relaxes on HIGH:

- **The locatable-spoken-beat requirement.** A clip that is a recognizable person reacting, with no clean arc anywhere in the transcript, is still worth packaging here. "No coherent beat" alone is no longer grounds for `{"segments": []}` — ask instead whether someone who already knows this creator would want to watch this moment.
- **Thin transcripts.** Short, fragmentary, or largely non-verbal dialogue is expected on this tier rather than disqualifying. The visual gate has already looked at the real frames and passed them.

What does not relax on HIGH — these are mechanical or structural, and recognition has nothing to say about them:

- The 18-58 second range and every trimming rule in Task below.
- `why` still has to name at least two real timestamps. On this tier you are locating the recognizable moment rather than proving an arc — "clip opens at 0.0 mid-argument, the moment this got clipped for lands at 14.0" is a perfectly good `why` — but it still has to point at real times in this transcript. A `why` with fewer than two timestamps is discarded mechanically, on either tier.
- `confidence` is still your own honest read, scored against this tier's own bar. Do not inflate it because the clip is popular — the view count already did its job by setting the tier, and counting it twice just pushes weak picks through. A recognizable moment you genuinely think lands is a high-confidence HIGH-tier pick; a recognizable moment that would still bore its own fans is a low one, and low ones are still meant to be rejected.

## Task

Do not hunt for a better moment inside the clip, and do not second-guess its edges beyond the trimming described below.

1. Decide whether the clip works as a standalone vertical short at all. Return `{"segments": []}` if it doesn't — see Reject below. That is a correct and expected answer for a clip that turns out to be crosstalk, needs visual context the transcript can't confirm, or (on STANDARD) has no coherent beat.
2. If it works, set `start`/`end` to the tightest span that keeps the complete beat:
   - If the full clip already reads clean and fits 18-58 seconds, use its full span
   - If the clip runs past 58 seconds or carries dead air the beat doesn't need, trim inward to the smallest span that keeps the setup and payoff intact
   - Runs 18-58 seconds inclusive. Within that, prefer 25-40 seconds unless the setup-to-payoff arc genuinely needs more room — don't pad a tight moment out to 40s, and don't force a beat that naturally needs 45-50s into a shorter cut and lose the payoff
   - If nothing 18-58 seconds long inside the clip captures a complete beat — the clip itself is too short, or any trim that fits would cut the payoff — return `{"segments": []}` rather than forcing it. Never invent content or extend past what the transcript actually contains

Reject without exception on STANDARD. On HIGH each one narrows as noted — see Audience tier above:

- Crosstalk with no discernible point — on HIGH, still a reject when there is no recognizable moment either, but a recognizable person reacting is not "no point" on that tier
- Anything requiring visual context the transcript can't confirm exists — unchanged on both tiers for anything the frames can't carry, though note the visual gate has already confirmed these particular frames are legible and competently cropped
- A clip whose point depends entirely on a running bit the standalone excerpt can't carry — **this one inverts on HIGH.** A running bit the existing audience already follows is recognition value, not a disqualifier. It only disqualifies on HIGH if the excerpt fails even for people who do know the bit

## Narrator commentary

Write one line of VO following the persona rules. The line frames the clip; it does not describe it.

Before writing, ask two things: what would the viewer need to know to find this interesting that they cannot get from watching, AND what actually makes this moment worth watching — is it shocking, funny, awkward, impressive, dramatic, absurd? A true fact that doesn't convey which of those it is isn't finished yet. The line should frame the clip toward that specific thing, not just supply a nearby fact and stop.

One beat, not two. If the line joins two separate observations — with "and," "but," a comma, or a full stop between two sentences — pick the stronger one and cut the rest — a single sharp claim beats two half-claims stitched together. "The most searched woman on the internet is standing in his kitchen, and the entire stream has to sit through a debate about dip" is two jokes fighting each other (who she is / what's mundanely happening instead); it should have been one or the other, not both. Splitting them into two sentences is the same failure wearing different punctuation: "Doc's ban went unexplained for four years. Shroud stopped answering him on day one." is two claims, not one line. Read it out loud — a natural pause where you'd take a breath mid-thought means it's two beats trying to be one.

Target 2-6 words — the same punchy register as `thumbnail_text` below, not narration. 8-20 words is an outer allowance for a beat that genuinely can't compress further, not the normal target it used to be; the persona's 35-word ceiling is still a hard limit, never something to write toward. Shorter is better at every one of those tiers, not just acceptable.

A full grammatical sentence is not required, and chasing one usually fights brevity directly. Fragments are fine — dropped articles, dropped verbs, a clause with no subject — as long as the angle from the paragraph above still lands. Cutting words is not the same job as cutting the point: what goes is the connective tissue around the angle (articles, hedges, restating what's visible), never the specific thing that makes the moment land. Hitting 2-6 words by dropping the hook or the twist is a failure, not a success — "He's reading an accusation that turns out to be aimed at him" (has the twist: he doesn't know yet) is NOT improved by "compressing" it to "He's reading this tweet for the first time, live on stream" (twist gone, now just describes the clip; shorter and worse). If the line can't reach 2-6 words with its hook intact, that IS the "genuinely can't compress" case the 8-20 outer allowance exists for — take the room and keep the hook. Never output a short line that hit the count by gutting the angle.

Too long vs. the target register — same fact, half the words:
- "A stand-up set's closing argument gets a one-word review from a man holding a Pop-Tart." → "One word. Delivered by Pop-Tart guy."
- "The most-searched woman on the internet came over to enter a guacamole contest." → "The internet's most searched woman. Making guac."

This compounds with the one-beat rule above, it doesn't replace it — a short line can still smuggle in two claims.

### On a HIGH-tier clip, lean into the recognition

Everything above still holds — one beat, the 2-6 word target, present tense, no hype, and no describing the visible action. What changes on this tier is what the line is *for*, and it changes the line materially enough to be worth stating outright.

On STANDARD the line's job is to manufacture a reason to care about a moment nobody knows. On HIGH that reason already exists in the viewer's head, and the line's job is to *trigger* it — name the creator, the moment, the running context, the thing that makes someone think "oh, that one." Straining to build a curiosity hook from scratch on a clip that doesn't have one produces a vague, hedged line; naming the recognizable thing doesn't.

- Right on HIGH: "The guacamole contest one." / "His actual reaction to the ban." / "Day one of the rivalry."
- Wrong on HIGH: a manufactured mystery — "He had no idea what was coming" — on a clip where nothing in particular is coming. That is a curiosity gap written for a viewer who isn't the one watching this.

**Naming the moment is not describing the visible action, and the persona rule against describing still stands unchanged.** "He's sitting at his desk talking" describes what anyone watching can already see, and is still wrong. "The guacamole contest one" names *which* moment this is — context the frames cannot carry on their own, which is precisely the thing that rule asks the line to add. The test in the persona rules is unchanged too: say the line without the clip, and it should land. On this tier it lands by recognition rather than by curiosity.

This applies to `on_screen_caption` exactly as it does to `vo_script` — same editorial line, different delivery, as everywhere else in this prompt.

If the moment genuinely has no name — recognizable as this creator, but not a specific known beat anyone would place — then the ordinary rules apply unchanged. Don't invent a reference that isn't there; a fabricated callback is worse than an honest ordinary line.

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
beat not two, the 2-6 word target above (read instead of heard is not a reason
to run longer — same target, not a looser 8-20), present tense, never
describing what is visible, no hype, no questions to the audience. A line that
fails the bar is not rescued by being displayed instead of spoken. If there is
nothing worth saying, both fields are null and the clip runs unframed — a
correct answer, not a failure.

## Tone

Assign exactly one of: burn, chaotic, wholesome, awkward, revealing, confident. Pick the one describing the payoff, not the setup. If two fit, pick the one a viewer would name after watching.

## Loop potential

Check whether the segment's ending could flow naturally back into its start if the clip repeated seamlessly — the same motion, sentence, or beat picking back up as if uninterrupted. This is rare. Most segments are not loopable, and forcing it produces a worse clip than admitting it isn't there.

Set `loopable` to `true` only when the loop would be genuinely seamless, not merely tolerable. Never trim or shift `start`/`end` to manufacture a loop — the boundaries are set by the rules above; loopability is an observation about the segment as already defined, not a target to hit.

## Thumbnail and upload metadata

Write these from the same read of the clip — they serve a different job than `caption_title` and must not just be a re-trim of it.

- `thumbnail_text`: the words that go ON the static grid thumbnail image, not in the video. This has to work with zero context, competing for a click in a grid of other thumbnails — punchier and more outrageous than `caption_title`, which only has to make sense once someone's already watching. 3-6 words. Prefer a striking line of dialogue quoted verbatim from the transcript; if nothing in the transcript is quotable, write a short reaction phrase instead — never a generic hype phrase ("YOU WON'T BELIEVE THIS", "WAIT FOR IT"). 0-2 emoji max, used as an accent, not decoration. Advertiser-friendly: no uncensored strong profanity on the thumbnail image — a real quote that contains it can be trimmed to stop before it, or a milder line from the transcript picked instead (this is stricter than the video's own captions, because the thumbnail is the permanent, public-facing first impression). Must read as a genuinely different line from `caption_title`, not the same words trimmed differently. **If you put quote marks around it, it must be an exact contiguous span of words from the transcript — same spelling, same words, no substitutions ("med school" is not "medical school", "he's not" is not "he isn't"), even for a single word.** Light edits for readability are not allowed inside quote marks; a real transcript quote can be trimmed shorter but never reworded. If the line isn't an exact transcript span, don't wrap it in quote marks — write it as a plain reaction phrase instead. This isn't just style: this clip's actual downloaded frame gets picked by finding where the quoted words are spoken, so a quote that isn't real breaks that mechanism silently and the thumbnail image loses its connection to the words on it
- `suggested_title`: the title a human would paste in at upload time. It creates curiosity, it does not summarize — if the title fully explains the clip, the viewer already knows what happens and has no reason to watch it. Withhold the specific payoff: reference that something happened without revealing what it was. Two styles both work — pick whichever fits this clip better, and record which one in `title_style`:
  - **concrete**: names one real, specific detail from the clip (the platform, the kind of message, who's present, the situation) while still hiding the one specific twist. "Soda's houseguest challenged him to a guacamole cook-off" names something concrete — a guacamole cook-off — while still hiding who the houseguest is.
  - **generic**: names no specific detail at all, just the shape of the curiosity gap. "He had no idea what was coming" — nothing concrete, but it still creates a real question (what was coming?), not empty hype and not a summary.

  Neither style is the "real" one; the split exists so real posted volume can eventually show whether one outperforms the other. What's NOT fine in either style: a title that fully explains the clip. "Hasan reads the message accusing him, live on stream" narrates the whole thing — what he reads, that it's about him, where — nothing left to find out, regardless of style. The positive test, not just the subtractive one: after reading the title, the viewer must still not know the single thing the clip pays off — if they do, it's a summary, however concrete or however generic. Still has to be accurate: never imply something that doesn't happen in the clip. This is curiosity, not clickbait — no "you won't believe," no fabricated stakes, no promising a payoff the clip doesn't deliver. One beat, not two — if the title joins two separate claims with "and," a comma, or a full stop, cut to the stronger one; "Soda's house had a guacamole cook-off and one competitor stole the whole stream" is two claims stapled together, not one sharp line. Prefer under ~10-12 words, one clause. Front-load the hook into the first ~50-80 characters — that window is a formatting constraint for where feeds cut off, not a length to fill; a short title well under it beats one that stretches to meet it. And hook on the SAME thing the on-screen framing does: `vo_script` / `on_screen_caption` and `suggested_title` are one editorial read of the clip delivered in two places, not two independent takes — whichever style you write in, the underlying beat has to be the one those fields already settled on, not a different facet nothing else in the segment supports
- `title_style`: "concrete" or "generic" — whichever style `suggested_title` actually used, per the two definitions just above. Pure self-report, costs nothing to answer: it's not asking the model to design anything new, just to label which of the two already-documented styles it wrote in
- `suggested_hashtags`: 3-5 tags specific to this clip's actual content (the game, the person, the situation) — never generic filler like #fyp, #viral, or #shorts on their own
- `suggested_description`: the description for the upload form. Required minimum is a credit line naming the source channel from the Source section above (this project reuses someone else's clip — the credit is not optional) followed by the first 3 `suggested_hashtags` (platforms surface those three above the title itself, so they have to be worth surfacing, not filler). One opening sentence of context is allowed but NOT required — include it only if it genuinely adds something a viewer doesn't already have from `suggested_title`. If it would restate the title or narrate the action, leave it out: "Clip via HasanAbi on Twitch. #hasanabi #twitchdrama #streamerdrama" is a complete, correct description. No spoilers beyond what `suggested_title` implies

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
      "title_style": "concrete",
      "suggested_hashtags": ["#valorant", "#clutch", "#shroud"],
      "suggested_description": "Clip via Shroud on Twitch. #valorant #clutch #shroud"
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
- `suggested_title`: creates curiosity, withholds the specific payoff, still accurate — never a summary that answers its own question, in either style. After reading it the viewer must still not know the one thing the clip pays off; if they do, it narrates instead of hooks (a shorter narration is still a narration). One beat, not two — under ~10-12 words when possible. Front-load the hook into the first ~50-80 characters. Hooks on the SAME beat as `vo_script`/`on_screen_caption` — same read of the clip, not a different one
- `title_style`: "concrete" (names a real detail while hiding the twist) or "generic" (no detail, just the curiosity gap) — whichever `suggested_title` actually is
- `suggested_hashtags`: 3-5 specific tags, never generic filler alone
- `suggested_description`: minimum is a credit line naming the source channel + the first 3 `suggested_hashtags`. One opening context sentence is optional — include it only if it adds something beyond `suggested_title`; no spoilers

Return `{"segments": []}` if the clip doesn't qualify. That is a correct and expected answer for a meaningful fraction of fetched clips — not every pre-made clip survives being re-packaged with commentary.
