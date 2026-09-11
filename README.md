# clipper

Turns long-form video (or a Twitch clip) into a finished 1080x1920 short-form
clip: animated word-by-word captions, tone-matched styling, AI narrator
commentary, and a filler gameplay bed. Output is a ready-to-upload mp4 plus a
generated thumbnail and upload metadata (title/description). Upload itself is
manual — there is no publishing code in this project.

## How it works

A single video moves through a fixed pipeline, each stage caching its output
to disk so re-running never repeats a download, a transcription, or an LLM
call it doesn't have to:

| stage | what it does |
|---|---|
| 1. ingest | downloads the source (`yt-dlp`, YouTube or Twitch) |
| 2. transcribe | word-level transcript (`faster-whisper`) |
| 2b. visual gate *(Twitch clips only)* | thumbnail pre-filter → facecam detection → vision-model judgment, rejects visually dead clips before they reach selection |
| 3. select | Claude picks the best moment(s), writes hook/caption/thumbnail text/title/hashtags/description as one structured call |
| 3.5 narrate | synthesizes the narrator voiceover line (TTS), cached by script hash |
| 4. render | burns in animated ASS captions, composites the gameplay/facecam bed, mixes narration, exports the final mp4; also writes the thumbnail image and upload text |
| 5. review | a small local UI to approve/reject renders |

Twitch clips skip the long-form window-hunting in stage 3 — a clip is already
a human-curated moment, so one LLM call picks the framing rather than
searching for it across the whole video.

Every editorial decision (word count, tone, caption placement, title style)
is a fixed, versioned rule, not something the model freelances — see
[CLAUDE.md](CLAUDE.md) for the full spec this pipeline is built against.

## Setup

Requires Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and `ffmpeg` on
your `PATH` (with `libass`/`drawtext` support).

```bash
git clone <this-repo-url>
cd agentshorts
uv sync
cp .env.example .env
```

Then fill in `.env` with your own keys:

- `ANTHROPIC_API_KEY` — required, drives stage 3 selection (and the Twitch
  visual gate). Must be a workspace-scoped key.
- `TTS_API_KEY` — OpenAI key, used for stage 3.5 narration.
- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — required for the Twitch clip
  discovery commands (`fetch-clips`/`fetch-trending`). Register a
  client-credentials app at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) — no user OAuth needed.
  Not required for YouTube-only use.
- `PEXELS_API_KEY` / `PIXABAY_API_KEY` — optional, either source for
  `fetch-gameplay`'s filler footage; leave unset to skip that source.

One-time asset setup:

```bash
uv run clipper fetch-gameplay      # downloads filler gameplay footage into assets/gameplay/
```

Fonts are already vendored in `fonts/` — nothing to install there.

## Usage

```bash
# Full pipeline, start to finish
uv run clipper clip "https://www.twitch.tv/videos/..."
uv run clipper clip "https://www.youtube.com/watch?v=..."

# Twitch clip discovery (no download)
uv run clipper fetch-clips shroud --limit 10
uv run clipper fetch-trending --limit 10

# Re-render without re-downloading/re-transcribing/re-calling the LLM
uv run clipper render VIDEO_ID

# Regenerate just the thumbnail/title/description for an already-selected clip
uv run clipper thumbnail VIDEO_ID --force

# Approve/reject renders locally
uv run clipper review VIDEO_ID
```

Run `uv run clipper --help` (or `<subcommand> --help`) for the full flag list.

## Development

```bash
uv run pytest -q
uv run ruff check .
```

No network calls happen in tests. `work/`, `assets/gameplay/`, and generated
media are gitignored — they're per-machine pipeline state and downloaded/
generated content, never checked in.
