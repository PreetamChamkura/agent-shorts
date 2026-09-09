# Project: vertical clip pipeline

Turns long-form video into 1080x1920 short-form clips with animated word-by-word captions, tone-matched styling, AI narrator commentary, and a filler gameplay bed. Output is a finished mp4. Upload is manual — there is no publishing code in this project and none should be added without asking.

## Non-negotiable output spec

Do not change these without being explicitly asked.

- Resolution 1080x1920, SAR 1:1, 30fps
- Video: libx264, `-preset medium -crf 20 -pix_fmt yuv420p`
- Audio: aac 192k, final pass `loudnorm=I=-14:TP=-1.5:LRA=11`
- Container mp4, `-movflags +faststart`
- Duration 18-58s inclusive. Hard fail outside.
- Fonts vendored in `./fonts/`. Always pass `fontsdir=./fonts` to the `ass` filter and explicit `fontfile=` to `drawtext`. Never rely on system fontconfig — it substitutes silently and output looks wrong.

## Safe zones

Platform UI overlays the frame. Nothing load-bearing outside these bounds:

- Horizontal: x 0-900. Right 180px is like/comment/share/remix.
- Vertical: y 250-1520. Top is channel header, bottom is title, subscribe button, and scrubber.
- Panel seam (split composition) sits at y≈960.
- Watermark at y=1400, centered.

## Stage contract

Stages are separate modules. Each reads only files written by earlier stages and writes only its own outputs. No stage calls another directly. Every stage skips work when its output exists unless `--force` is passed.

```
work/{video_id}/
  source.mp4         1    ingest
  source.info.json   1    yt-dlp metadata
  words.json         2    transcribe
  segments.json      3    select
  vo_{n}.wav         3.5  narrate (absent when vo_script is null — including every caption_only segment)
  vo_{n}.words.json  3.5  narrate, whisper timing for cold_open VO captions (same condition)
  caps_{n}.ass       4    render, kept for debugging
  out_{n}.mp4        4    render
  thumbnail_{n}.jpg  4    thumbnail (standalone `clipper thumbnail`, not yet wired into clip/render by default)
  metadata_{n}.json  4    thumbnail (same command)
  review.json        5    approve/reject
```

Caching is load-bearing. Renders get re-run dozens of times while tuning. Re-running stage 4 must never re-download, re-transcribe, or re-call an LLM.

### words.json

```json
{
  "language": "en",
  "duration": 2841.3,
  "words": [{"word": "so", "start": 12.44, "end": 12.61}]
}
```

### segments.json

```json
{
  "video_id": "abc123",
  "segments": [{
    "n": 0,
    "start": 412.8,
    "end": 441.2,
    "punchline_at": 437.1,
    "tone": "revealing",
    "loopable": false,
    "hook": "prediction that aged badly",
    "caption_title": "HE HAD NO IDEA",
    "vo_script": "He said this three weeks before the exact thing he ruled out happened.",
    "on_screen_caption": null,
    "vo_placement": "cold_open",
    "narration_mode_reason": "the clip opens on dead air, so a spoken line costs nothing",
    "why": "setup 412.8 establishing his position, turn at 431, payoff 437.1, reaction to 441.2",
    "confidence": 0.82,
    "thumbnail_text": "\"HE RULED IT OUT\"",
    "suggested_title": "He publicly ruled this out three weeks before it happened",
    "suggested_hashtags": ["#valorant", "#clutch", "#shroud"]
  }]
}
```

`start` and `end` MUST be snapped to word boundaries from words.json after the model returns. Model timestamps drift 200-600ms and a clip starting mid-word feels broken.

Validate `why` contains two parseable timestamps. If not, drop the segment — it means the model didn't actually locate the beat.

`end` must also cover the payoff phrase `why` quotes verbatim, when one can be reliably located — extend forward if short (as long as the result still fits 18-58s), otherwise drop the segment. The model's own numeric "payoff at N" marker is not sufficient on its own: `end` can already exceed that marker while still truncating the quoted payoff, because the marker itself can be short of where the quote actually finishes.

`loopable` is true only for the small minority of segments whose ending flows naturally back into their start. When true, stage 4 should end cleanly on the last frame — no fade, no hard cut — so a platform's auto-repeat creates the loop illusion. This does not apply to ordinary segments. Stage 4 re-snaps `end` to a word boundary and confirms the frame at that exact timestamp is stable (not mid-blink or motion-blurred) before freezing on it, stepping back a few frames if it isn't.

## Sources beyond a single YouTube upload

`clipper clip URL` accepts any yt-dlp-supported URL — YouTube or Twitch — and routes automatically based on the URL's own shape, never a flag the caller has to remember.

**Livestreams (YouTube and Twitch).** This pipeline is batch and needs a finished file. The supported path is the VOD (`live_status: was_live`) — wait for the broadcast to end, ingest the recording like any other video, no special handling needed. A still-airing stream (`is_live`) fails loudly rather than silently hanging: yt-dlp follows the live edge in real time, so ingesting one blocks for however long the stream runs. Recording from the start is available via an explicit `live_from_start` opt-in, which still blocks until the broadcast ends and is documented upstream as experimental.

**Twitch VODs** need no code beyond the above — same generic `ingest()`, same `live_status` gate. Window count (stage 3) scales with source length (~8 windows/hour); a multi-hour stream costs proportionally more LLM calls than a short upload, logged up front before spending.

**Twitch clips** are a distinct, simpler path: a clip is already a human-curated moment, so stage 3's long-form window-hunting is skipped entirely.

- `clipper fetch-clips CHANNEL` — Helix API discovery (`/helix/clips`, client-credentials app token, no user login/scopes needed for this read-only lookup). Lists candidates (view count, date, title) before anything downloads. Sortable by view count, filterable by date range.
- `clipper fetch-trending` — discovery with no channel given up front. `/helix/clips` has no unfiltered/global mode (confirmed against Twitch's own API reference: it requires `broadcaster_id`, `game_id`, or `id`), so this goes through `/helix/streams` first — which does support a zero-filter call, confirmed for real against a live request, and comes back sorted by viewer count — to find the top currently-live channels, then calls the same `fetch_clips()` per channel. Every clip is tagged `clip["discovery"] = {"mode": "trending"|"channel", "channel": ..., "viewer_count_at_discovery": ..., "follower_count_at_discovery": ..., "language": ...}` (the channel-specific path tags its own clips the same way, minus the stream-specific fields) so which mode found a clip stays visible in the printed list and any `--out` manifest, for comparing how trending-sourced clips perform against channel-specific ones later.
  - Raw viewer-count-right-now ordering skews toward whatever language happens to be biggest on Twitch at that moment and toward one-off viral spikes rather than channels with an existing audience, so two filters narrow `/helix/streams` before any clip is ever fetched: `--language` (default `en`, Helix's own server-side `language` param on `/helix/streams`, confirmed with a real call that it actually filters rather than just being echoed back) and `--min-followers` (default `250000`, checked via `/helix/channels/followers` — confirmed for real that an app access token gets the total-follower-count-only response the docs describe for a non-moderator caller, no user token needed). The 250k default came from a real survey of the then-current top 20 live channels: 18 sat between 294k-11.7M followers, and the one clear outlier was a streamer at 4,400 followers riding a one-off viral spike — 250k separates that case from genuine established channels on this sample. Pass `--language ""` / `--min-followers 0` to disable either.
- The visual quality gate (below) screens candidates before/after download — identically for both discovery modes.
- `select_clip()` + `prompts/select_clip.md` replace `select()`/`select.md`: one LLM call over the whole clip instead of one per window, boundaries already given rather than searched for. Same output schema and the same `validate_segment()` as the long-form path — payoff-quote cross-check, loopable rules, the 18-58s range and 25-40s soft preference all apply unchanged. A clip under 18s is rejected before ever calling the model.

### Visual quality gate

Twitch clips get no visual screening from stage 3 (it only ever sees a transcript), so a clip can be selected purely on its words while being visually dead. Three layers, cheapest first, each thinning the batch before the next spends more:

| layer | what | gates or informs |
|---|---|---|
| 1 — thumbnail pre-filter | Helix's own `thumbnail_url`, scored before any download/transcribe | hard-rejects only a near-blank/placeholder frame (variance floor) — see below for why it's this narrow |
| 2 — facecam detection | OpenCV Haar cascade across several sampled frames of the downloaded clip, clustered by position to reject one-off false positives | feeds render's composition choice (facecam_split vs tight_crop), never rejects a clip |
| 3 — vision-model judgment | Claude Sonnet 5, 5 frames sampled evenly across the clip, cropped through the SAME filter graph render() would actually use | wired into `clipper clip` (Twitch clip URLs only): runs right after transcribe, before select_clip, so a rejected clip never reaches the selection LLM call either |

Layer 1 turned out narrower than first scoped, and that's a real finding, not a shortcut: variance, dark-pixel-fraction, and edge-density (whole-frame and per-quadrant) were tested against real fetched thumbnails and none reliably separates "visually empty" from "legitimately dark/moody content" — a hand on a desk mat can score *higher* contrast than a genuinely dark dungeon-crawler screenshot. A single static frame's pixel statistics can't make that call; it's semantic, not statistical, which is exactly why layer 3 exists. Layer 1 therefore only catches the case it safely can (a blank/placeholder frame) and logs every candidate's scores regardless of outcome for visibility.

**Testing gap, not yet closed:** layer 3's original positive-control test case (the "Doc" clip — a hand on a desk mat, facecam a small box in the corner) now *passes* under its real shipped composition, because `facecam_split` (built earlier the same session as layer 3) already gives the facecam its own full panel with visible reactions — genuinely fixing that specific case, confirmed by forcing the clip back onto its old `tight_crop` rendering and watching layer 3 reject it with the original complaint almost verbatim. That means there is currently no known real clip that reliably exercises layer 3's rejection path under its real, current composition. Before trusting this layer against a wider batch, find or construct one: a facecam clip that's dead even *with* its own dedicated panel — person visibly present, but not reacting, flat expression, nothing happening across the sampled frames.

## Narrator persona

One voice, permanently. Never randomize the voice, name, or register. This is channel identity and it only compounds if it stays fixed.

### Voice

A friend who has already seen the clip and hands it to you with one line of setup. Dry, slightly amused, never impressed by its own joke. Register is texting a friend — not documentary narration, not YouTuber hype.

### Find the angle before writing

Before writing the line, name what actually makes this specific moment worth watching: is it shocking, funny, awkward, impressive, dramatic, absurd? The line has to frame the clip toward that specific thing — not just supply a nearby true fact and stop there.

A fact that passes the backstory bar below can still be incomplete if it doesn't convey why the moment lands. "The person quietly doing X in the background is [notable person], and chat has no idea" states a fact — it doesn't tell the viewer whether to expect a laugh, a reveal, or a disaster. Naming the actual angle turns context into a frame: what about this fact makes it funny, or awkward, or dramatic? That's the piece a merely-true fact can still be missing.

This works with the rules below, not instead of them — still no hype, still no describing visible action, still no rhetorical questions, still the word count. The point isn't a second, bigger rule; it's that the fact chosen should be the one that makes the moment land, not just any true fact that happens to fit.

### Rules

- 8-20 words is the practical target — write to land one beat, not to fill a range. 35 is a hard ceiling, never something to write toward; a line has been drifting toward that ceiling by default, which is the wrong direction
- One beat, not two. If the line joins two separate observations — with "and," "but," a comma, or a full stop between two sentences — pick the stronger one and cut the rest — a single sharp claim beats two half-claims stitched together. ("The most searched woman on the internet is standing in his kitchen, and the entire stream has to sit through a debate about dip" is two jokes fighting each other — who she is, and what's mundanely happening instead — pick one)
- Present tense
- Never describe what is visibly happening. The viewer has eyes. Add what they can't see: context, history, stakes, what happened next
- Never open with "POV", "wait for it", "this is insane", "he cooked", or any variant. If the line could sit on any clip, it's a bad line
- No questions to the audience, no calls to action
- One joke maximum, only if it lands. A flat factual setup beats a bad punchline
- Never explain the joke that's coming. Set up, then get out
- Mild profanity at most; advertiser-friendly by default
- Name the source when known and it adds context
- Use punctuation for pacing, not for hype. A short pause via an em-dash or ellipsis before the key word helps TTS delivery land naturally — but the words on either side of the pause still have to follow every other rule here

### The test

Read the line without the clip. Slightly curious means it works. If it only makes sense after seeing the clip, it's describing rather than framing.

Stronger bar: could you tell someone one true fact about this moment's backstory, history, or context that they wouldn't get from the clip alone? If the line only re-angles what's already visible, it's not there yet — that's the difference between commentary and captioning with extra words.

Two sentences are the same failure wearing different punctuation — a full stop between two independent claims is still two beats, and it slips past a rule that only names "and," "but," and commas (found in real output the first time `caption_only` fired). Read it out loud. A natural pause where you'd take a breath mid-thought means it's probably two beats trying to be one — cut to the stronger half rather than keeping both.

Every rule above applies to a held `on_screen_caption` exactly as it does to a spoken `vo_script` — see Narration modes. Written for the eye instead of the ear is a delivery difference, never a different editorial bar.

### Good

- "This was recorded three weeks before the exact thing he's ruling out happened."
- "He has been asked this forty times and has never once answered it."
- "Nobody in this room knows he already told the other side."

### Bad

- "You won't believe what happens next"          empty hype
- "He looks at the camera and starts laughing"   describing the video
- "This is why I love these guys"                says nothing
- "Ever wonder what happens when..."             rhetorical question

## Tone routing

Style is a lookup on `tone`. Never random. Same house look every upload — same font family, same watermark position, same layout geometry. Style varies caption color, phrase group size, vertical position, and cut rhythm only. A viewer should recognize the channel in two seconds regardless of tone.

Defined in `styles.yaml`, one block per tone. Adding a tone means adding a block, never touching the renderer.

| tone      | when                                | treatment                    |
|-----------|-------------------------------------|------------------------------|
| burn      | someone gets got                    | red highlight, 3-word groups |
| chaotic   | everyone talking, nothing lands     | 2-word groups, high position |
| wholesome | genuine or sincere moment           | soft cream, 4-word groups    |
| awkward   | silence, someone caught out         | white, 5-word groups, slow   |
| revealing | information the audience lacked     | amber, 3-word groups         |
| confident | a claim, prediction, flat statement | white heavy, 3-word groups   |

If stage 3 returns a tone absent from styles.yaml, fail loudly. Don't fall back to a default — drift between prompt and style file should surface immediately.

## ASS captions

Generate ASS and burn in one pass. Never stack `drawtext` for captions.

```
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Pop,Montserrat ExtraBold,104,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,1,0,0,0,100,100,0,0,1,9,5,5,70,70,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
```

- Colors are `&HAABBGGRR`. Highlight yellow `&H0000E5FF`, burn red `&H003B3BE2`, amber `&H00279FEF`
- `Alignment: 5` is middle-center
- Outline 9 / shadow 5 keeps text legible over any footage. Don't reduce
- Timestamps `H:MM:SS.cc`, single-digit hour

One Dialogue line per word. The phrase builds up progressively as each word is spoken — a word must never appear on screen, even unhighlighted, before its own start time, since natural speech leaves real gaps between words that would otherwise show later words early. The active word recolors to the tone's highlight and scales 118% easing to 100% over 90ms.

```
{\c&H0000E5FF&\fscx118\fscy118\t(0,90,\fscx100\fscy100)}WORD
```

Break phrase groups on sentence-ending punctuation from the transcript even when the group is short. Reads noticeably better than fixed chunking.

## Filter graphs

### Blur fill

```
[0:v]split=2[bg][fg];
[bg]scale=1080:1920:force_original_aspect_ratio=increase,
    crop=1080:1920,gblur=sigma=28[bgb];
[fg]scale=1080:-2[fgs];
[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp]
```

### Split screen with gameplay

```
color=c=black:s=1080x1920:r=30[base];
[0:v]scale=1080:-2[src];
[1:v]scale=1080:864:force_original_aspect_ratio=increase,
    crop=1080:864,setsar=1[game];
[base][src]overlay=0:250[t1];
[t1][game]overlay=0:1056[comp]
```

16:9 at 1080 wide is 607 tall, occupying y 250-857. Gameplay 1056-1920.

Gameplay scales to cover the 1080x864 panel, then center-crops. A plain
`scale=-2:864` only reaches 1080 wide at 5:4 or wider — 16:9 and vertical
footage would land short and leave the panel unfilled.

### Facecam split (Twitch clips)

```
color=c=black:s=1080x1920:r=30[base];
[0:v]split=2[main_src][face_src];
[main_src]scale=1080:864:force_original_aspect_ratio=increase,
    crop=1080:864:x={0 or in_w-out_w}:y=0,setsar=1[main];
[face_src]crop={panel_w}:{panel_h}:{corner_x}:{corner_y},
    scale=1080:864:force_original_aspect_ratio=increase,
    crop=1080:864,setsar=1[face];
[base][main]overlay=0:250[t1];
[t1][face]overlay=0:1056[comp]
```

Both panels come from the SAME source (no separate gameplay file, unlike split screen above). Panel position is fixed — main content always top, facecam always bottom — regardless of which corner the source's own facecam sits in; consistency across every facecam-style upload matters more than mirroring each clip's own layout. `{corner_x}/{corner_y}` anchor a `FACECAM_PANEL_FRACTION` (0.35) crop flush to whichever corner the detected facecam center falls nearest — not a tight crop of the detected face box alone, since a real overlay (background, shoulders, headset) runs well past the face itself. The main panel's crop `x` offset is chosen to fall on whichever side is farthest from the facecam's corner, so the same overlay doesn't visibly repeat in both panels.

Selected automatically — not a manual `--composition` choice like split_screen above. `render()` runs face detection (`clipper.face_detect`) against the clip's own footage whenever `composition` is left unset; `facecam_split` is used only when a small corner facecam is actually found, otherwise `tight_crop`. A podcast-style source (subject fills the frame, e.g. a wide two-shot with no PIP overlay) always falls back to `tight_crop` unchanged.

Known limitation (see Known issues below): corner detection currently checks whether the facecam's *center* falls within `CORNER_MARGIN_FRACTION` (0.25) of both a horizontal and vertical edge. A real facecam positioned with some vertical margin above the game's own HUD (confirmed on a real clip) can fail this check and fall back to `tight_crop` — a missed optimization, not a broken render, and the safer failure direction versus the alternative (incorrectly split-screening a real podcast face, confirmed as a real false-positive risk during testing). Revisit with more real facecam layouts before loosening the margin.

### Tail, both compositions

```
[comp]ass=work/{id}/caps_{n}.ass:fontsdir=./fonts[capped];
[capped]drawtext=fontfile=./fonts/Montserrat-Bold.ttf:text='@FrameByFrameMoments1':
  fontcolor=white@0.45:fontsize=40:x=(w-tw)/2:y=1400[vout]
```

Gameplay audio is always discarded. Gameplay video comes from `assets/gameplay/*.mp4` at a random offset seeded by `{video_id}:{n}` so renders are reproducible and consecutive uploads differ.

## Narration modes

Three ways a segment's framing reaches the viewer, selected by `vo_placement`
in stage 3 and mutually exclusive by construction:

| mode | field | what it costs |
|---|---|---|
| `cold_open` | `vo_script` | audio — the source is silenced for `[0, vo_duration)` |
| `pre_punchline` | `vo_script` | audio — the source is silenced from 1.2s before `punchline_at` |
| `caption_only` | `on_screen_caption` | screen — nothing is silenced; a held line shares the frame with the clip's own captions |

Both delivery fields null is the fourth, normal state: the clip needs no
framing. `vo_placement` is unused then and should read `cold_open`.

`vo_placement` is the authority when the model contradicts itself. Stage 3's
`resolve_narration` nulls whichever delivery field doesn't belong to the chosen
mode and keeps the segment — same warn-and-keep posture as the VO word ceiling.
A segment must never ship with both, or the viewer reads one sentence while
hearing a different one. The field name is a legacy wart: it predates there
being a non-VO mode, and renaming it would invalidate every segments.json
already on disk for no behavioural gain.

`narration_mode_reason` is required and must name the actual trade — the
specific sound a caption is protecting, or why speech beats text here. Same
accountability mechanism as `why`'s located timestamps and visual_judge's
`best_frame_reason`, for the same reason: an unjustified judgment field drifts.
Warn-only; a thin reason is a signal to fix the prompt, never grounds to drop a
real segment.

**`caption_only` exists for one reason: some clips' own audio IS the payoff.**
A laugh breaking, a scream, a voice cracking, a room going quiet. Both spoken
placements silence a window of source audio, and that window can land directly
on the thing being framed. A held line pays in screen space instead. The
selection criteria live in prompts/select.md and prompts/select_clip.md —
default is a spoken line, since the narrator is channel identity (see Narrator
persona) and a caption is the exception taken for a stateable reason.

`on_screen_caption` answers to every narrator-persona rule `vo_script` does —
find the angle, one beat, 8-20 words, no hype. It is the same editorial line
delivered to the eye instead of the ear, never a lower bar.

**A caption is never a fallback for failed VO.** A `tts_failed` or
`vo_duration_exceeded` segment renders silent, not captioned: a line written to
be heard hasn't been checked against the held line's constraints, and promoting
it would put an unreviewed sentence on screen.

**Testing gap, not yet closed** (same shape as the visual gate's layer-3 note
above). `caption_only` has fired exactly once on a real clip — the Doc ban clip
(work/1932625054), rejected for VO because its opening carries the whole setup
and the rest runs wall-to-wall with no window to silence, which is criterion 2
verbatim. Re-running the same clip after the one-beat rule was tightened chose
`cold_open` instead, with an equally defensible reason ("the payoff is a spoken
line, not a sound"). Both reads are correct; the clip is genuinely borderline.
So the mode is confirmed reachable and confirmed not to swallow everything
(three real clips, two chose VO), but its real selection rate is uncharacterised
and no clip is yet known to choose it reliably. Before trusting the criteria at
batch scale, find one that should be unambiguous: a clip whose payoff is purely
a sound — a laugh breaking, a scream, a room going quiet — with no spoken
punchline anywhere in it.

### Held caption geometry

Rendered as a second ASS style in the same caption file, not a `drawtext` pass —
`clipper.ass.held_caption_style` derives it from the tone's own styles.yaml
block so it stays a tone lookup (font family, `primary` colour, `bold`
inherited). Deliberate overrides, each load-bearing:

- **Font 56, not 104.** A 20-word line at caption size is six lines of shouting.
- **Its own band, derived from where the karaoke line sits.** Unlike a
  `cold_open` VO — which silences the source and therefore correctly suppresses
  the clip's own captions during its window — `caption_only` mutes nothing, so
  the dialogue captions must keep running. The two blocks ARE on screen
  together, always; the geometry is load-bearing, not defensive. Every tone but
  `chaotic` runs its karaoke middle-centred (worst case y 772-1147), so the held
  line takes the top band from y=250. `chaotic` runs its karaoke top-centred
  (y 260-510), so its held line takes the bottom band instead, ending at y=1340,
  60px clear of the y=1400 watermark. Always different thirds of the frame.
- **Margins 180/180, not 70/70.** x 180-900, inside the horizontal safe zone and
  still optically centred on 540. A line held for seconds has to clear the
  right-hand like/comment/share rail; a single karaoke word flashing past does
  not. (The karaoke's own 70/70 margins predate this and do reach x=1010 — a
  real, pre-existing inconsistency with the Safe zones section, left alone here.)
- **Outline/shadow scale with the font.** 9 and 5 are tuned for 104px glyphs.
- **No uppercase, no animation.** It is a sentence of context, and its spoken
  equivalent isn't shouted either. Nothing scales or recolours: the highlight
  colour means "this word is being said right now" everywhere else in the
  system, and reusing it here would collide semantically.
- **Wrapped explicitly with `\N`.** `WrapStyle: 2` means libass does no word
  wrapping of its own — only a hard break splits a line. A karaoke group can't
  use the same `\N` fix: its text grows word-by-word as the line is spoken
  (`clipper.ass.build_ass`'s progressive reveal), so a break computed from the
  final group would land wrong for every shorter prefix on the way there. Held
  captions don't have that constraint — the whole line is static from the
  first frame — so `clipper.ass.wrap_held_text` wraps it deterministically
  instead of hoping the renderer does.

  **This DID surface for karaoke groups, confirmed on a real render**: `awkward`'s
  fixed 5-word groups (styles.yaml) overflowed off both edges of the frame on
  a real transcript line ("forms from the anthropological approach", 39
  chars) — `group_size` was a word COUNT with no ceiling on rendered width.
  Fixed differently from the held caption, because it had to be:
  `clipper.ass.group_words` now also flushes a group early once its estimated
  rendered width (a real-measured px-per-character factor for this font/size,
  see the constant's own comment — NOT the same constant `clipper.thumbnail`
  uses, which is a different rasterizer at a different size) would exceed the
  resolved style's real usable width. `group_size` in styles.yaml stays the
  target and the upper bound; this only ever makes a group smaller, protecting
  every tone automatically, not just `awkward`. Confirmed fixed on the same
  real overflowing line via a real render.

**Hold window is derived from the text, not from the model.** From the clip's
first frame for `words * 0.4s + 1.0s`, clamped to 2.5-8.0s, then it clears —
"set up, then get out" applies to a held line too. Deliberately not another
model-supplied timestamp: this pipeline has already had two real
timestamp-drift bugs (`end` short of the payoff quote, `punchline_at` 5.3s off
it), and a hold window is the one piece of timing that needs no judgment at all.
Constants live in `clipper.ass`; switch to a whole-clip hold there if a real
render argues for it.


## Voiceover mixing

- Stage 3.5 calls TTS with `vo_script`. Cache by sha256 of the script text. Never pay twice for the same line.
- Two overlapping voices don't work at any relative volume — VO and source speech are never audible at the same time. But the video never freezes for it either: a static frame is dead time, exactly the kind of pause that loses viewers on Shorts/Reels. Both `vo_placement` values share the same mechanism — full silence on the source track during the VO window (`volume=0`, not ducked), source video playing normally throughout, resuming full audio immediately after — differing only in where the window sits:
  - `cold_open`: the window is `[0, vo_duration)` — the clip's own opening seconds. The source video keeps moving under the narration; nothing is prepended, so this does not lengthen the render.
  - `pre_punchline`: the window starts 1.2s before `punchline_at`. Short enough that a hard cut to silence and back isn't jarring.
- `cold_open` additionally captions the VO's own line during its window, with the same karaoke engine and style as the rest of the clip, so the viewer can read the narrator as well as hear them. The timing comes from a whisper transcription of the synthesized VO audio (`vo_{n}.words.json`, stage 3.5 — reusing the same transcription wrapper stage 2 uses, not a new mechanism), not an invented even-split. `pre_punchline` does not caption its VO — it's a short interjection, not a window with room for a second caption track.
- `amix` the VO into the silenced window, then `loudnorm` on the mixed result. Never loudnorm before mixing.
- `vo_script: null` is a normal path, not an error. Render with no VO track.
- If TTS fails, render without VO and flag it in segments.json rather than failing the job.
- Because `cold_open` no longer lengthens the render, the only way its VO still can't be used is if it's long enough to consume the clip's own duration outright. Stage 3.5 checks this once it knows the real synthesized length; if the VO doesn't leave a few seconds of the clip's own content after it, the VO is dropped for that render and the segment is flagged `vo_duration_exceeded` in segments.json — exactly like a TTS failure, never a silent bad render. The cached audio itself is untouched; only this segment's use of it as a cold open is unusable.

## Thumbnail + upload metadata

Static grid-thumbnail image and upload text, generated stage-4-adjacent per clip — not an automated upload. 2026 platform research finding that drives this design: YouTube Shorts' custom-thumbnail feature (July 2026) is YPP + Studio-desktop only, and the Data API's `thumbnails.set` is undocumented/unreliable for Shorts. So this produces an asset the human copies in at manual upload time, same as the video file itself — never a publishing call.

- `thumbnail_text`, `suggested_title`, `suggested_hashtags` are added to the SAME `select`/`select_clip` schema and LLM call that already produces `caption_title`/`vo_script` — context is already loaded, no second call. `thumbnail_text` must read as genuinely different from `caption_title`: punchier, more outrageous, meant to compete for a click with zero context, ideally a striking quoted line of dialogue. None of the three can drop an otherwise-good segment on their own — a blank or malformed value only warns and keeps the segment, same posture as an over-long `vo_script` getting nulled rather than sinking the clip.
- `clipper thumbnail VIDEO_ID [--n N] [--force]` (standalone CLI command, `src/clipper/thumbnail.py`) generates `thumbnail_{n}.jpg` (1080x1920) and `metadata_{n}.json` from an already-selected segment. **Not wired into `clip`/`render` by default yet** — built and tested standalone first, on purpose.
- Frame selection prefers layer 3's own already-computed judgment (`visual_judge.json`'s `best_frame_timestamp` — the vision model that already looked at several frames to decide pass/reject also names which one is most worth using as a thumbnail, extending that same call's schema rather than paying for a second one) when it falls inside the final segment's own `[start, end]`. Falls back to a cheap pixel-stats scan (reusing `clipper.frames.edge_density`, same proxy visual_gate's layer 1 uses) when layer 3 never ran (the long-form YouTube path) or its pick got trimmed out of range by `select_clip`. For `facecam_split`, the fallback scores only the facecam panel's own source region, not the whole frame.
- Text and its background scrim stay inside a centered 1080x1080 safe square (Reels crops the cover to 4:5/3:4, TikTok's grid does its own crop) and clear of the `facecam_split` panel seam at y≈960. Text color is the tone's own caption highlight color (`styles.yaml`) for visual consistency with the video's own captions.
- **Emoji confirmed broken, stripped before rendering.** `drawtext` + the vendored Montserrat font renders emoji as tofu boxes, not the glyph — confirmed against a real render, not assumed. Montserrat has no emoji coverage and `drawtext` has no font-fallback mechanism (this project never relies on fontconfig substitution for exactly this kind of silent breakage). `thumbnail_text` may still carry up to 2 emoji per the prompt rules — they stay in `metadata.json` verbatim for a human assembling a thumbnail by hand elsewhere, but the auto-rendered overlay strips them first.

## Out of scope

Do not build: publishing/upload code, scheduling, analytics ingestion, automated compilations, face tracking, or a hosted web app. Ask first.

Automated compilations specifically are off the table — they're the most-cited example of reused content and a compilation of reuploads is the worst possible artifact to hand a policy reviewer.

## Model switching

Default model for this project is Sonnet 5. Two situations call for Opus 5
instead, and in both cases: stop, tell me explicitly to run
`/model claude-opus-5`, wait for my confirmation before continuing, and tell
me to switch back to `/model claude-sonnet-5` once that specific task is done.

Switch for:
- Designing new judgment criteria in the stage 3 selection prompts
  (prompts/select.md, prompts/select_clip.md) — open-ended questions like
  "is this moment self-contained," "did the payoff land," the `why`-field
  validation logic — where the model has to weigh ambiguous, competing
  signals and stronger reasoning changes the actual quality of the
  judgment
- Debugging that has failed on 2+ attempts under Sonnet — a gnarly ffmpeg
  filter-graph bug, an audio-sync issue, anything where the first couple of
  fixes didn't hold

Don't switch for: stage scaffolding, file I/O, subprocess wrappers, config,
tests, straightforward bug fixes, OR adding a fully-specified field to an
existing prompt — every constraint (word count, format, what to exclude)
already decided in advance, so writing it in is transcription, not judgment
design (e.g. `thumbnail_text`/`suggested_title`/`suggested_hashtags`: word
counts, emoji limits, character windows, and the hashtag rule were all
dictated before the prompt text was written — nothing about the addition
asked the model to weigh anything the spec hadn't already settled). The
line: did this addition ask "how should the model decide," or just "state
this already-decided rule in the prompt"? Only the former needs Opus. If a
fully-specified field's real output comes back generic or low-quality once
tested, that's the trigger to revisit it on Opus with the actual bad
examples in hand — not a reason to route every such addition through Opus
pre-emptively.

## Known issues

Real, confirmed limitations left as-is on purpose — not TODOs, not forgotten. Recorded here so they're not rediscovered from scratch.

- **`is_small_corner_facecam`'s corner-proximity check misses some real facecams.** A facecam positioned with enough vertical margin above the game's own HUD can fall outside `CORNER_MARGIN_FRACTION` (0.25) and fall back to `tight_crop` instead of `facecam_split` — confirmed on a real clip. Loosening the margin was tried and rejected: it reintroduces the exact false positive layer 2 was built to avoid (a real podcast face misclassifying as a facecam). The two real cases don't separate cleanly on corner-center distance alone. Left as-is because the failure direction is the safe one — a missed optimization (graceful fallback to a known-good composition), not a broken render. `render()` logs this distinctly (`"failed the corner-proximity check"`) whenever it fires, so real frequency is visible over time. Revisit with more real facecam layouts before touching the margin again.

## Conventions

- Python 3.11+, `uv` for deps, `ruff` for lint
- All ffmpeg calls go through one `run_ffmpeg()` helper that logs the full command and raises on non-zero with the last 40 lines of stderr. ffmpeg fails silently and weirdly — never call subprocess directly
- No network in tests
- Secrets in `.env`, never committed, `.env.example` current
- `output_config`'s `json_schema` format rejects some ordinary-looking JSON Schema constraints — confirmed for real, twice, as 400s from live calls: `"minimum"` on an `integer` type, and array `"minItems"` for any value other than 0 or 1. Enforce ranges/counts in Python after parsing instead of in the schema itself when adding a new structured-output field.
