"""Command-line entry point.

Wires stages together; each stage lives in its own module and no stage imports
another. Subcommands:

  clipper select VIDEO_ID [--force]
      stage 3 only. Calls the LLM once per transcript window, writes
      segments.json. Cached — never re-called unless --force.

  clipper select-clip VIDEO_ID [--force]
      stage 3, clip variant (Twitch clips — CLAUDE.md step 2b). One LLM call
      against the clip's whole transcript instead of windowing a long-form
      video; same segments.json schema and validation as `select`.

  clipper narrate VIDEO_ID [--force]
      stage 3.5 only. Synthesizes vo_{n}.wav for every non-null vo_script in
      segments.json. Cached two ways — see clipper.narrate.

  clipper render VIDEO_ID [--n N] [--composition C] [--force]
                          [--start S --end E --tone T]
      stage 4 only. With no --start/--end/--tone, reads segment --n from
      segments.json (including its VO window, if vo_{n}.wav exists). Never
      re-downloads, re-transcribes, or re-calls the LLM.

  clipper clip URL [...same clip args...]
      ingest -> transcribe -> (select|select-clip + narrate, if no manual
      args) -> render -> thumbnail. Routes to select-clip automatically
      when URL is a Twitch clip (detected from the URL itself, not a flag)
      — see clipper.twitch.is_twitch_clip_url. For Twitch clips only, the
      visual gate's layer 3 (clipper.visual_judge) runs right before
      select-clip and stops the pipeline (SystemExit) on a reject — never
      reaches the selection LLM call for a clip the vision judgment
      rejects. The final thumbnail step only runs when segments.json drove
      the render (skipped for manual --start/--end/--tone, which has no
      thumbnail_text to work with) and never fails the run — a thumbnail
      generation error is logged and the already-rendered out_{n}.mp4 is
      still the command's result, same posture as narrate's "TTS fails,
      render without VO" rule.

  clipper thumbnail VIDEO_ID [--n N] [--force]
      generates work/{video_id}/thumbnail_{n}.jpg, metadata_{n}.json, and
      upload_{n}.txt from an already-selected segment (thumbnail_text/
      suggested_title/suggested_hashtags/suggested_description — added to
      segments.json's schema alongside caption_title/vo_script, CLAUDE.md
      thumbnail generation step). metadata_{n}.json is structured for
      tooling; upload_{n}.txt is the same title/description as plain text —
      no quotes, no JSON syntax — for pasting straight into an upload form.
      `clip` now runs this automatically as its last step; this standalone
      command stays for regenerating a thumbnail alone — e.g. rejecting a
      thumbnail in review but keeping the video — without re-rendering
      out_{n}.mp4.
      Reads work/{video_id}/segments.json for segment `n`'s
      start/end/tone/thumbnail_text/punchline_at; reuses Layer 3's own
      best_frame_timestamp (clipper.visual_judge) when available and it
      falls inside the segment's final [start, end], else locates
      thumbnail_text's own spoken timestamp in the transcript when it's a
      verbatim quote, else centers on punchline_at. Never touches
      out_{n}.mp4.

  clipper review VIDEO_ID [--port N]
      stage 5 only. Starts a local review server (http://127.0.0.1:PORT/)
      listing every rendered out_{n}.mp4 with its metadata, an editorial
      checklist, and approve/reject. Writes review.json. Runs until Ctrl+C.

  clipper fetch-gameplay [--limit N]
      setup tool, not a stage. Downloads filler footage into assets/gameplay/
      from the Pexels and Pixabay video APIs and writes sources.json.

  clipper fetch-clips CHANNEL [--limit N] [--since T] [--until T] [--out PATH]
                              [--no-visual-gate]
      setup tool, not a stage. Lists a Twitch channel's clips via the Helix
      API (id, view count, created date, title) — nothing downloaded. Runs
      the layer-1 visual pre-filter (clipper.visual_gate) on each candidate's
      thumbnail unless --no-visual-gate is passed; rejects only near-blank
      frames, logs scores for everything. Sanity-check the list before
      running `clipper clip` on any of them.

  clipper fetch-trending [--limit N] [--top-channels N] [--per-channel-limit N]
                         [--since T] [--until T] [--out PATH] [--no-visual-gate]
                         [--language L] [--min-followers N]
      setup tool, not a stage. Trending counterpart to fetch-clips — no
      channel given up front. Helix's /helix/clips has no unfiltered/global
      mode (confirmed against the API reference), so this queries
      /helix/streams (which does support a zero-filter call, sorted by
      viewer count) for the top currently-live channels, then calls
      clipper.twitch.fetch_clips() per channel — same downstream pipeline
      as fetch-clips, just a different discovery source. Each clip is
      tagged clip["discovery"] = {"mode": "trending", "channel": ...,
      "viewer_count_at_discovery": ..., "follower_count_at_discovery": ...,
      "language": ...}; fetch-clips tags its own clips {"mode": "channel",
      "channel": ...} the same way, so which mode found a clip stays visible
      in the printed list and any --out manifest.

      --language (default "en") filters /helix/streams server-side, so a
      non-English candidate is never fetched only to be discarded. Pass ""
      to pull every language. --min-followers (default 250000) filters out
      channels below that floor — Helix has no such filter, so this costs
      one /helix/channels/followers lookup per candidate channel (an app
      access token gets the total-follower-count-only response, confirmed
      for real). Pass 0 to disable and use pure current-viewer-count
      ordering. Both exist because "trending" by live viewer count alone can
      surface a small streamer's one-off viral spike rather than a channel
      with a real, established audience — see clipper.twitch.
      get_top_live_streams for the real follower-count survey behind the
      250000 default.

--start/--end/--tone are given together or not at all; a partial set is an
error rather than a half-manual guess. Manual mode never mixes in VO (there is
no punchline_at to place it against) — that's the segments.json path only.
--tone accepts any string, and an unknown tone fails loudly inside render()
(see clipper.styles).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

log = logging.getLogger("clipper.cli")


def _add_clip_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", type=float, help="clip start, seconds (with --end and --tone)")
    p.add_argument("--end", type=float, help="clip end, seconds (with --start and --tone)")
    p.add_argument("--tone", help="tone key from styles.yaml, e.g. 'confident'")
    p.add_argument("--n", type=int, default=0, help="clip index within the video")
    p.add_argument(
        "--composition",
        choices=["tight_crop", "blur_fill", "split_screen", "facecam_split"],
        default=None,
        help=(
            "filter graph to render with. Default: auto-detect — runs the visual "
            "gate's facecam detection and uses facecam_split only if a small corner "
            "facecam is actually found; falls back to tight_crop otherwise. Pass "
            "one explicitly to skip detection and force that composition."
        ),
    )
    p.add_argument("--force", action="store_true", help="ignore cached outputs for this stage")


def _manual_spec(args) -> bool:
    """True if the clip window was given by hand. Errors on a partial set."""
    given = [args.start is not None, args.end is not None, args.tone is not None]
    if all(given):
        return True
    if any(given):
        raise SystemExit(
            "clipper: --start, --end and --tone must be given together "
            "(or all omitted, to use segments.json)"
        )
    return False


def _segment_from_file(video_id: str, n: int) -> dict:
    from .paths import work_dir

    seg_path = work_dir(video_id) / "segments.json"
    if not seg_path.exists():
        raise SystemExit(
            f"clipper: {seg_path} missing — run `clipper select {video_id}` first, "
            "or pass --start/--end/--tone by hand."
        )
    segments = json.loads(seg_path.read_text()).get("segments", [])
    match = next((s for s in segments if s.get("n") == n), None)
    if match is None:
        raise SystemExit(
            f"clipper: no segment n={n} in {seg_path} ({len(segments)} segment(s) available)."
        )
    return match


def _narration_kwargs(seg: dict) -> dict:
    """render() kwargs for this segment's narration mode, or {} for silence.

    Three shapes, one per mode (CLAUDE.md "Narration modes"):
      - spoken: {"punchline_at", "vo_placement"} — render() also checks that
        vo_{n}.wav actually exists before mixing
      - held:   {"on_screen_caption"} — no audio touched, so none of the VO
        failure flags apply to it
      - silent: {}

    A null vo_script, a stage-3.5 tts_failed flag, or a vo_duration_exceeded
    flag (the VO would consume the clip's own duration) all fall back to
    silence rather than to a caption: a line written to be heard hasn't been
    checked against the held line's very different constraints, and quietly
    promoting it would put an unreviewed sentence on screen.
    """
    from .select import CAPTION_ONLY

    if str(seg.get("vo_placement", "")) == CAPTION_ONLY:
        caption = seg.get("on_screen_caption")
        return {"on_screen_caption": str(caption)} if caption else {}
    if seg.get("vo_script") is None or seg.get("tts_failed") or seg.get("vo_duration_exceeded"):
        return {}
    return {"punchline_at": float(seg["punchline_at"]), "vo_placement": str(seg["vo_placement"])}


def _narration_summary(seg: dict) -> str:
    """One-line narration description for the select/select-clip listings."""
    from .select import CAPTION_ONLY

    if str(seg.get("vo_placement", "")) == CAPTION_ONLY and seg.get("on_screen_caption"):
        return f"[caption] {seg['on_screen_caption']}"
    if seg.get("vo_script"):
        return f"[{seg.get('vo_placement', 'vo')}] {seg['vo_script']}"
    return "(no narration)"


def _generate_thumbnail_safely(video_id: str, n: int, seg: dict, *, force: bool) -> None:
    """Thumbnail + metadata as the `clip` command's last step. Never raises:
    a failure here must not fail an otherwise-successful render, same
    posture as narrate's "TTS fails, render without VO" rule — the
    already-rendered out_{n}.mp4 is still the command's real result. Logged
    clearly (not silently swallowed) so a real failure rate stays visible.
    """
    from .thumbnail import generate_thumbnail, write_metadata

    try:
        generate_thumbnail(
            video_id,
            n,
            float(seg["start"]),
            float(seg["end"]),
            str(seg["tone"]),
            str(seg.get("thumbnail_text", "")),
            punchline_at=float(seg["punchline_at"])
            if seg.get("punchline_at") is not None
            else None,
            force=force,
        )
        write_metadata(video_id, n, seg, force=force)
    except Exception as exc:
        log.warning(
            "clipper: thumbnail generation failed for %s n=%d: %s — rendering without a "
            "thumbnail (out_%d.mp4 is unaffected; retry with `clipper thumbnail %s --n %d`)",
            video_id,
            n,
            exc,
            n,
            video_id,
            n,
        )


def _require_twitch_client_id() -> str:
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
        raise SystemExit(
            "clipper: TWITCH_CLIENT_ID not set in .env (see .env.example) — "
            "register an app at dev.twitch.tv/console/apps"
        )
    return client_id


def _print_clip_list(clips: list[dict]) -> int:
    """One line per clip — visual-gate layer 1 status (if run), discovery
    mode (channel login for both "channel" and "trending" modes), then the
    usual id/views/date/duration/title. Returns the layer-1 rejected count,
    0 if the gate wasn't run. Shared by fetch-clips and fetch-trending so
    the two commands' output never drifts apart.
    """
    n_rejected = 0
    for c in clips:
        gate = (c.get("visual_gate") or {}).get("layer_1")
        if gate is None:
            status = ""
        elif gate["passed"]:
            status = (
                f"[pass  var={gate['variance']:.0f} dark={gate['dark_fraction']:.2f} "
                f"edge={gate['edge_density']:.1f}] "
            )
        else:
            status = "[REJECT layer1] "
            n_rejected += 1

        discovery = c.get("discovery")
        if discovery:
            disc_tag = f"[{discovery['mode']}:{discovery['channel']}]"
            extras = []
            if discovery.get("language"):
                extras.append(f"lang={discovery['language']}")
            if "viewer_count_at_discovery" in discovery:
                extras.append(f"viewers={discovery['viewer_count_at_discovery']:,}")
            if discovery.get("follower_count_at_discovery") is not None:
                extras.append(f"followers={discovery['follower_count_at_discovery']:,}")
            if extras:
                disc_tag += "[" + " ".join(extras) + "]"
            disc_tag += " "
        else:
            disc_tag = ""

        print(
            f"{status}{disc_tag}{c['id']:<28} views={c['view_count']:>8,}  {c['created_at']}  "
            f"{c['duration']:>4.0f}s  {c['title'][:55]}"
        )
    return n_rejected


def _write_clips_out(out_path: str | None, clips: list[dict], *, channel: str | None) -> None:
    if not out_path:
        return
    from pathlib import Path

    payload: dict = {"clips": clips}
    if channel is not None:
        payload["channel"] = channel
    Path(out_path).write_text(json.dumps(payload, indent=2))
    print(f"wrote {out_path}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(prog="clipper")
    sub = parser.add_subparsers(dest="command", required=True)

    clip_p = sub.add_parser("clip", help="ingest + transcribe + select + narrate + render")
    clip_p.add_argument("url", help="video URL (YouTube or Twitch)")
    clip_p.add_argument(
        "--live-from-start",
        action="store_true",
        help=(
            "record a still-airing stream from its start. BLOCKS until the broadcast "
            "ends; experimental in yt-dlp. Normally you want the VOD instead."
        ),
    )
    _add_clip_args(clip_p)

    render_p = sub.add_parser("render", help="render only (no download / transcribe / LLM)")
    render_p.add_argument("video_id", help="video id of an already-ingested video")
    _add_clip_args(render_p)

    select_p = sub.add_parser("select", help="stage 3 only: LLM segment selection")
    select_p.add_argument("video_id", help="video id of an already-transcribed video")
    select_p.add_argument("--force", action="store_true", help="re-call the LLM, ignoring cache")

    select_clip_p = sub.add_parser(
        "select-clip", help="stage 3 (clip variant): one LLM call, no window-hunting"
    )
    select_clip_p.add_argument("video_id", help="video id of an already-transcribed clip")
    select_clip_p.add_argument(
        "--force", action="store_true", help="re-call the LLM, ignoring cache"
    )

    narrate_p = sub.add_parser("narrate", help="stage 3.5 only: synthesize VO for segments.json")
    narrate_p.add_argument("video_id", help="video id of an already-selected video")
    narrate_p.add_argument(
        "--force", action="store_true", help="re-copy vo_{n}.wav (never re-pays for cached text)"
    )

    thumbnail_p = sub.add_parser(
        "thumbnail",
        help="generate thumbnail_{n}.jpg + metadata_{n}.json + upload_{n}.txt for an "
        "already-selected segment (runs automatically as clip's last step; standalone here "
        "for regenerating without re-rendering)",
    )
    thumbnail_p.add_argument("video_id", help="video id of an already-selected video")
    thumbnail_p.add_argument("--n", type=int, default=0, help="clip index within the video")
    thumbnail_p.add_argument(
        "--force", action="store_true", help="ignore cached thumbnail/metadata outputs"
    )

    review_p = sub.add_parser("review", help="stage 5 only: local approve/reject UI")
    review_p.add_argument("video_id", help="video id with at least one rendered out_{n}.mp4")
    review_p.add_argument("--port", type=int, default=8765, help="local server port (default 8765)")

    fetch_p = sub.add_parser(
        "fetch-gameplay", help="setup tool: download filler footage into assets/gameplay/"
    )
    fetch_p.add_argument("--limit", type=int, default=12, help="max clips to download (default 12)")

    fetch_clips_p = sub.add_parser(
        "fetch-clips",
        help="setup tool: list a Twitch channel's clips (stage 2b discovery, no download)",
    )
    fetch_clips_p.add_argument("channel", help="Twitch channel login, e.g. 'shroud'")
    fetch_clips_p.add_argument(
        "--limit", type=int, default=10, help="max clips to list (default 10)"
    )
    fetch_clips_p.add_argument(
        "--since", help="RFC3339 timestamp — only clips created at/after this"
    )
    fetch_clips_p.add_argument("--until", help="RFC3339 timestamp — only clips created before this")
    fetch_clips_p.add_argument("--out", help="also write the fetched list as JSON to this path")
    fetch_clips_p.add_argument(
        "--no-visual-gate",
        action="store_true",
        help="skip the layer-1 thumbnail pre-filter (stage 2b visual gate)",
    )

    fetch_trending_p = sub.add_parser(
        "fetch-trending",
        help="setup tool: list clips from currently-trending channels (no channel needed)",
    )
    fetch_trending_p.add_argument(
        "--limit", type=int, default=10, help="max clips to list overall (default 10)"
    )
    fetch_trending_p.add_argument(
        "--top-channels",
        type=int,
        default=15,
        help="how many top live channels to pull clips from (default 15)",
    )
    fetch_trending_p.add_argument(
        "--per-channel-limit",
        type=int,
        default=5,
        help="max clips to pull from any one channel (default 5)",
    )
    fetch_trending_p.add_argument(
        "--since", help="RFC3339 timestamp — only clips created at/after this"
    )
    fetch_trending_p.add_argument(
        "--until", help="RFC3339 timestamp — only clips created before this"
    )
    fetch_trending_p.add_argument("--out", help="also write the fetched list as JSON to this path")
    fetch_trending_p.add_argument(
        "--no-visual-gate",
        action="store_true",
        help="skip the layer-1 thumbnail pre-filter (stage 2b visual gate)",
    )
    fetch_trending_p.add_argument(
        "--language",
        default="en",
        help=(
            "ISO 639-1 code passed to Helix's own /helix/streams language filter "
            "(default 'en'). Pass '' to disable and pull every language."
        ),
    )
    fetch_trending_p.add_argument(
        "--min-followers",
        type=int,
        default=250_000,
        help=(
            "minimum follower count for a channel to be considered trending "
            "(default 250000) — filters out a small streamer's one-off viewer "
            "spike, not just whoever is highest-viewer right now. Pass 0 to disable."
        ),
    )

    from .experiment_cli import add_experiment_parser

    add_experiment_parser(sub)

    args = parser.parse_args(argv)

    if args.command == "experiment":
        from .experiment_cli import run_experiment

        code = run_experiment(args)
        if code:
            sys.exit(code)
    elif args.command == "select":
        from .select import select

        out = select(args.video_id, force=args.force)
        for seg in out["segments"]:
            vo = _narration_summary(seg)
            print(
                f"[{seg['n']}] {seg['start']:.1f}-{seg['end']:.1f}s  "
                f"{seg['tone']:<9} conf={seg['confidence']:.2f}  {seg['caption_title']}\n"
                f"    {vo}"
            )
        print(f"\n{len(out['segments'])} segment(s)")

    elif args.command == "select-clip":
        from .select import select_clip

        out = select_clip(args.video_id, force=args.force)
        for seg in out["segments"]:
            vo = _narration_summary(seg)
            print(
                f"[{seg['n']}] {seg['start']:.1f}-{seg['end']:.1f}s  "
                f"{seg['tone']:<9} conf={seg['confidence']:.2f}  {seg['caption_title']}\n"
                f"    {vo}"
            )
        print(f"\n{len(out['segments'])} segment(s)")

    elif args.command == "narrate":
        from .narrate import narrate

        out = narrate(args.video_id, force=args.force)
        for seg in out["segments"]:
            if seg.get("vo_script") is None:
                status = "(no VO)"
            elif seg.get("tts_failed"):
                status = "TTS FAILED — rendering without VO"
            elif seg.get("vo_duration_exceeded"):
                status = "VO EXCEEDS 58s CAP — rendering without VO"
            else:
                status = f"vo_{seg['n']}.wav"
            print(f"[{seg['n']}] {status}")

    elif args.command == "clip":
        from .ingest import ingest
        from .render import render
        from .transcribe import transcribe

        manual = _manual_spec(args)
        video_id = ingest(
            args.url, force=args.force, live_from_start=getattr(args, "live_from_start", False)
        )
        transcribe(video_id, force=args.force)

        seg = None  # stays None for manual mode — no segment, no thumbnail_text to work with
        if manual:
            start, end, tone, vo_kwargs, loopable = args.start, args.end, args.tone, {}, False
        else:
            from .narrate import narrate
            from .select import select, select_clip
            from .twitch import is_twitch_clip_url

            # A Twitch clip is already a curated, fixed-boundary moment — the
            # window-hunting path (select) would be wasted effort on it (see
            # CLAUDE.md step 2b). Detected from the URL's own shape, not a
            # flag, so this never needs remembering to pass anything extra.
            if is_twitch_clip_url(args.url):
                from .visual_judge import judge_clip

                # Visual gate layer 3: the one layer with real per-clip
                # cost, so it runs here — after transcribe, before the
                # ALSO-costly selection LLM call — rather than after
                # select_clip. A visually-rejected clip never reaches
                # select_clip at all.
                verdict = judge_clip(video_id, force=args.force)
                if not verdict["passed"]:
                    raise SystemExit(
                        f"clipper: {video_id} rejected by the visual gate "
                        f"(layer 3, {verdict['composition']}): {verdict['reason']}"
                    )
                select_clip(video_id, force=args.force)
            else:
                select(video_id, force=args.force)
            narrate(video_id, force=args.force)
            seg = _segment_from_file(video_id, args.n)
            start, end, tone = float(seg["start"]), float(seg["end"]), str(seg["tone"])
            vo_kwargs = _narration_kwargs(seg)
            loopable = bool(seg.get("loopable", False))

        out = render(
            video_id,
            args.n,
            start,
            end,
            tone,
            composition=args.composition,
            loopable=loopable,
            force=args.force,
            **vo_kwargs,
        )
        print(out)

        # Thumbnail is the pipeline's last step, and only runs when a real
        # segment drove the render — manual --start/--end/--tone has no
        # thumbnail_text to work with, same reason manual mode never mixes
        # in VO. Never fails the run: see _generate_thumbnail_safely.
        if seg is not None:
            _generate_thumbnail_safely(video_id, args.n, seg, force=args.force)

    elif args.command == "render":
        from .render import render

        if _manual_spec(args):
            start, end, tone, vo_kwargs, loopable = args.start, args.end, args.tone, {}, False
        else:
            seg = _segment_from_file(args.video_id, args.n)
            start, end, tone = float(seg["start"]), float(seg["end"]), str(seg["tone"])
            vo_kwargs = _narration_kwargs(seg)
            loopable = bool(seg.get("loopable", False))

        out = render(
            args.video_id,
            args.n,
            start,
            end,
            tone,
            composition=args.composition,
            loopable=loopable,
            force=args.force,
            **vo_kwargs,
        )
        print(out)

    elif args.command == "thumbnail":
        from .thumbnail import generate_thumbnail, write_metadata

        seg = _segment_from_file(args.video_id, args.n)
        thumb_path = generate_thumbnail(
            args.video_id,
            args.n,
            float(seg["start"]),
            float(seg["end"]),
            str(seg["tone"]),
            str(seg.get("thumbnail_text", "")),
            punchline_at=float(seg["punchline_at"])
            if seg.get("punchline_at") is not None
            else None,
            force=args.force,
        )
        meta_path, upload_txt_path = write_metadata(args.video_id, args.n, seg, force=args.force)
        print(thumb_path)
        print(meta_path)
        print(upload_txt_path)

    elif args.command == "review":
        from .review import review

        review(args.video_id, port=args.port)

    elif args.command == "fetch-gameplay":
        from .fetch_gameplay import fetch_gameplay

        manifest = fetch_gameplay(limit=args.limit)
        for clip in manifest["clips"]:
            print(
                f"{clip['file']:<28} {clip['duration']:>5.0f}s  {clip['resolution']:<10} "
                f"{clip['platform']}  {clip['source_url']}"
            )
        print(f"\n{len(manifest['clips'])} clip(s) — screen thumbnails before use")

    elif args.command == "fetch-clips":
        from .twitch import fetch_clips, get_app_access_token, get_broadcaster_id
        from .visual_gate import screen_clips_layer1

        client_id = _require_twitch_client_id()
        token = get_app_access_token(client_id=client_id)
        broadcaster_id = get_broadcaster_id(args.channel, token=token, client_id=client_id)
        clips = fetch_clips(
            broadcaster_id,
            token=token,
            client_id=client_id,
            limit=args.limit,
            started_at=args.since,
            ended_at=args.until,
        )
        # Same field fetch_trending_clips() stamps for its own clips (mode
        # "trending") — which discovery mode found a clip stays visible
        # wherever clip metadata already travels, not a new mechanism.
        for c in clips:
            c["discovery"] = {"mode": "channel", "channel": args.channel}

        if not args.no_visual_gate:
            clips = screen_clips_layer1(clips)
        n_rejected = _print_clip_list(clips)

        summary = f"\n{len(clips)} clip(s) for {args.channel}"
        if not args.no_visual_gate:
            summary += f" — {n_rejected} rejected by layer 1, {len(clips) - n_rejected} passed"
        print(summary + " — nothing downloaded yet")
        _write_clips_out(args.out, clips, channel=args.channel)

    elif args.command == "fetch-trending":
        from .twitch import fetch_trending_clips, get_app_access_token
        from .visual_gate import screen_clips_layer1

        client_id = _require_twitch_client_id()
        token = get_app_access_token(client_id=client_id)
        clips = fetch_trending_clips(
            token=token,
            client_id=client_id,
            limit=args.limit,
            top_channels=args.top_channels,
            per_channel_limit=args.per_channel_limit,
            started_at=args.since,
            ended_at=args.until,
            language=args.language or None,
            min_followers=args.min_followers or None,
        )
        if not args.no_visual_gate:
            clips = screen_clips_layer1(clips)
        n_rejected = _print_clip_list(clips)

        summary = f"\n{len(clips)} trending clip(s) across up to {args.top_channels} channel(s)"
        if not args.no_visual_gate:
            summary += f" — {n_rejected} rejected by layer 1, {len(clips) - n_rejected} passed"
        print(summary + " — nothing downloaded yet")
        _write_clips_out(args.out, clips, channel=None)

    else:  # argparse enforces required=True, but be explicit rather than silent.
        parser.error(f"unknown command {args.command!r}")
        sys.exit(2)


if __name__ == "__main__":
    main()
