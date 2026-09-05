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
  vo_{n}.wav         3.5  narrate (absent when vo_script is null)
  caps_{n}.ass       4    render, kept for debugging
  out_{n}.mp4        4    render
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
    "vo_placement": "cold_open",
    "why": "setup 412.8 establishing his position, turn at 431, payoff 437.1, reaction to 441.2",
    "confidence": 0.82
  }]
}
```

`start` and `end` MUST be snapped to word boundaries from words.json after the model returns. Model timestamps drift 200-600ms and a clip starting mid-word feels broken.

Validate `why` contains two parseable timestamps. If not, drop the segment — it means the model didn't actually locate the beat.

`loopable` is true only for the small minority of segments whose ending flows naturally back into their start. When true, stage 4 should end cleanly on the last frame — no fade, no hard cut — so a platform's auto-repeat creates the loop illusion. This does not apply to ordinary segments.

## Narrator persona

One voice, permanently. Never randomize the voice, name, or register. This is channel identity and it only compounds if it stays fixed.

### Voice

A friend who has already seen the clip and hands it to you with one line of setup. Dry, slightly amused, never impressed by its own joke. Register is texting a friend — not documentary narration, not YouTuber hype.

### Rules

- 12-30 words, hard ceiling 35
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

One Dialogue line per word. The full phrase shows; the active word recolors to the tone's highlight and scales 118% easing to 100% over 90ms.

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

### Tail, both compositions

```
[comp]ass=work/{id}/caps_{n}.ass:fontsdir=./fonts[capped];
[capped]drawtext=fontfile=./fonts/Montserrat-Bold.ttf:text='@HANDLE':
  fontcolor=white@0.45:fontsize=40:x=(w-tw)/2:y=1400[vout]
```

Gameplay audio is always discarded. Gameplay video comes from `assets/gameplay/*.mp4` at a random offset seeded by `{video_id}:{n}` so renders are reproducible and consecutive uploads differ.

## Voiceover mixing

- Stage 3.5 calls TTS with `vo_script`. Cache by sha256 of the script text. Never pay twice for the same line.
- Two overlapping voices don't work at any relative volume — VO and source speech are never audible at the same time. Behavior splits by `vo_placement`:
  - `cold_open`: an actual pre-roll. Freeze on the clip's first frame (dimmed and blurred) for the VO's duration, source audio fully silent, then cut to full clip playback and full source audio once narration ends. The VO plays before the clip starts, not under it — this lengthens the final render by the VO's duration, which still counts toward the 18-58s duration bound.
  - `pre_punchline`: full silence on the source track during the VO window (`volume=0`, not ducked), resuming full volume immediately after. Short enough that a hard cut to silence and back isn't jarring.
- `amix` the VO into the silenced window (`pre_punchline`) or concatenate it ahead of the clip (`cold_open`), then `loudnorm` on the result. Never loudnorm before mixing or concatenating.
- `vo_script: null` is a normal path, not an error. Render with no VO track.
- If TTS fails, render without VO and flag it in segments.json rather than failing the job.

## Out of scope

Do not build: publishing/upload code, scheduling, analytics ingestion, automated compilations, face tracking, or a hosted web app. Ask first.

Automated compilations specifically are off the table — they're the most-cited example of reused content and a compilation of reuploads is the worst possible artifact to hand a policy reviewer.

## Model switching

Default model for this project is Sonnet 5. Two situations call for Opus 5
instead, and in both cases: stop, tell me explicitly to run
`/model claude-opus-5`, wait for my confirmation before continuing, and tell
me to switch back to `/model claude-sonnet-5` once that specific task is done.

Switch for:
- Designing or revising the stage 3 selection prompt (prompts/select.md) —
  segment-selection quality and the `why`-field validation logic benefit
  more from stronger reasoning than routine coding does
- Debugging that has failed on 2+ attempts under Sonnet — a gnarly ffmpeg
  filter-graph bug, an audio-sync issue, anything where the first couple of
  fixes didn't hold

Don't switch for routine work: stage scaffolding, file I/O, subprocess
wrappers, config, tests, straightforward bug fixes. That's the majority of
this codebase and Sonnet handles it well.

## Conventions

- Python 3.11+, `uv` for deps, `ruff` for lint
- All ffmpeg calls go through one `run_ffmpeg()` helper that logs the full command and raises on non-zero with the last 40 lines of stderr. ffmpeg fails silently and weirdly — never call subprocess directly
- No network in tests
- Secrets in `.env`, never committed, `.env.example` current
