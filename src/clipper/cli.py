"""Command-line entry point.

Wires stages together; each stage lives in its own module and no stage imports
another. Three subcommands:

  clipper select VIDEO_ID [--force]
      stage 3 only. Calls the LLM once per transcript window, writes
      segments.json. Cached — never re-called unless --force.

  clipper render VIDEO_ID [--n N] [--composition C] [--force]
                          [--start S --end E --tone T]
      stage 4 only. With no --start/--end/--tone, reads segment --n from
      segments.json. Never re-downloads, re-transcribes, or re-calls the LLM.

  clipper clip URL [...same clip args...]
      ingest -> transcribe -> (select, if no manual args) -> render.

--start/--end/--tone are given together or not at all; a partial set is an
error rather than a half-manual guess. --tone accepts any string, and an
unknown tone fails loudly inside render() (see clipper.styles).
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


def _spec_from_segments(video_id: str, n: int) -> tuple[float, float, str]:
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
    return float(match["start"]), float(match["end"]), str(match["tone"])


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(prog="clipper")
    sub = parser.add_subparsers(dest="command", required=True)

    clip_p = sub.add_parser("clip", help="ingest + transcribe + select + render")
    clip_p.add_argument("url", help="YouTube URL")
    _add_clip_args(clip_p)

    render_p = sub.add_parser("render", help="render only (no download / transcribe / LLM)")
    render_p.add_argument("video_id", help="video id of an already-ingested video")
    _add_clip_args(render_p)

    select_p = sub.add_parser("select", help="stage 3 only: LLM segment selection")
    select_p.add_argument("video_id", help="video id of an already-transcribed video")
    select_p.add_argument("--force", action="store_true", help="re-call the LLM, ignoring cache")

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

    elif args.command == "clip":
        from .ingest import ingest
        from .render import render
        from .transcribe import transcribe

        manual = _manual_spec(args)
        video_id = ingest(args.url, force=args.force)
        transcribe(video_id, force=args.force)

        if manual:
            start, end, tone = args.start, args.end, args.tone
        else:
            from .select import select

            select(video_id, force=args.force)
            start, end, tone = _spec_from_segments(video_id, args.n)

        out = render(
            video_id,
            args.n,
            start,
            end,
            tone,
            composition=args.composition,
            force=args.force,
        )
        print(out)

    elif args.command == "render":
        from .render import render

        if _manual_spec(args):
            start, end, tone = args.start, args.end, args.tone
        else:
            start, end, tone = _spec_from_segments(args.video_id, args.n)

        out = render(
            args.video_id,
            args.n,
            start,
            end,
            tone,
            composition=args.composition,
            force=args.force,
        )
        print(out)

    else:  # argparse enforces required=True, but be explicit rather than silent.
        parser.error(f"unknown command {args.command!r}")
        sys.exit(2)


if __name__ == "__main__":
    main()
