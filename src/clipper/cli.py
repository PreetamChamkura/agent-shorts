"""Command-line entry point.

Wires stages together; each stage lives in its own module and no stage imports
another. Subcommands:

  clipper select VIDEO_ID [--force]
      stage 3 only. Calls the LLM once per transcript window, writes
      segments.json. Cached — never re-called unless --force.

  clipper narrate VIDEO_ID [--force]
      stage 3.5 only. Synthesizes vo_{n}.wav for every non-null vo_script in
      segments.json. Cached two ways — see clipper.narrate.

  clipper render VIDEO_ID [--n N] [--composition C] [--force]
                          [--start S --end E --tone T]
      stage 4 only. With no --start/--end/--tone, reads segment --n from
      segments.json (including its VO window, if vo_{n}.wav exists). Never
      re-downloads, re-transcribes, or re-calls the LLM.

  clipper clip URL [...same clip args...]
      ingest -> transcribe -> (select + narrate, if no manual args) -> render.

  clipper review VIDEO_ID [--port N]
      stage 5 only. Starts a local review server (http://127.0.0.1:PORT/)
      listing every rendered out_{n}.mp4 with its metadata, an editorial
      checklist, and approve/reject. Writes review.json. Runs until Ctrl+C.

  clipper fetch-gameplay [--limit N]
      setup tool, not a stage. Downloads filler footage into assets/gameplay/
      from the Pexels and Pixabay video APIs and writes sources.json.

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
import sys


def _add_clip_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", type=float, help="clip start, seconds (with --end and --tone)")
    p.add_argument("--end", type=float, help="clip end, seconds (with --start and --tone)")
    p.add_argument("--tone", help="tone key from styles.yaml, e.g. 'confident'")
    p.add_argument("--n", type=int, default=0, help="clip index within the video")
    p.add_argument(
        "--composition",
        choices=["tight_crop", "blur_fill", "split_screen"],
        default="tight_crop",
        help="filter graph to render with (default: tight_crop)",
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


def _vo_kwargs(seg: dict) -> dict:
    """{"punchline_at", "vo_placement"} when the segment has usable VO, else {}.

    A null vo_script, a stage-3.5 tts_failed flag, or a vo_duration_exceeded
    flag (the cold_open pre-roll would push the render past 58s) all mean the
    normal no-VO path — render() itself also checks that vo_{n}.wav exists
    before mixing.
    """
    if seg.get("vo_script") is None or seg.get("tts_failed") or seg.get("vo_duration_exceeded"):
        return {}
    return {"punchline_at": float(seg["punchline_at"]), "vo_placement": str(seg["vo_placement"])}


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

    narrate_p = sub.add_parser("narrate", help="stage 3.5 only: synthesize VO for segments.json")
    narrate_p.add_argument("video_id", help="video id of an already-selected video")
    narrate_p.add_argument(
        "--force", action="store_true", help="re-copy vo_{n}.wav (never re-pays for cached text)"
    )

    review_p = sub.add_parser("review", help="stage 5 only: local approve/reject UI")
    review_p.add_argument("video_id", help="video id with at least one rendered out_{n}.mp4")
    review_p.add_argument("--port", type=int, default=8765, help="local server port (default 8765)")

    fetch_p = sub.add_parser(
        "fetch-gameplay", help="setup tool: download filler footage into assets/gameplay/"
    )
    fetch_p.add_argument("--limit", type=int, default=12, help="max clips to download (default 12)")

    args = parser.parse_args(argv)

    if args.command == "select":
        from .select import select

        out = select(args.video_id, force=args.force)
        for seg in out["segments"]:
            vo = seg["vo_script"] or "(no VO)"
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

        if manual:
            start, end, tone, vo_kwargs, loopable = args.start, args.end, args.tone, {}, False
        else:
            from .narrate import narrate
            from .select import select

            select(video_id, force=args.force)
            narrate(video_id, force=args.force)
            seg = _segment_from_file(video_id, args.n)
            start, end, tone = float(seg["start"]), float(seg["end"]), str(seg["tone"])
            vo_kwargs = _vo_kwargs(seg)
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

    elif args.command == "render":
        from .render import render

        if _manual_spec(args):
            start, end, tone, vo_kwargs, loopable = args.start, args.end, args.tone, {}, False
        else:
            seg = _segment_from_file(args.video_id, args.n)
            start, end, tone = float(seg["start"]), float(seg["end"]), str(seg["tone"])
            vo_kwargs = _vo_kwargs(seg)
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

    else:  # argparse enforces required=True, but be explicit rather than silent.
        parser.error(f"unknown command {args.command!r}")
        sys.exit(2)


if __name__ == "__main__":
    main()
