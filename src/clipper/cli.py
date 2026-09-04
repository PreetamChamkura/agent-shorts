"""Command-line entry point.

Wires stages together; each stage lives in its own module and no stage imports
another. Milestone 1 exposes two subcommands:

  clipper clip URL --start S --end E [--n N] [--force]
      ingest -> transcribe -> render (skips stage 3 selection).

  clipper render VIDEO_ID --start S --end E [--n N] [--force]
      render only. Never re-downloads or re-transcribes (caching is stage-local).
"""

from __future__ import annotations

import argparse
import logging
import sys


def _add_clip_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", type=float, required=True, help="clip start, seconds")
    p.add_argument("--end", type=float, required=True, help="clip end, seconds")
    p.add_argument("--n", type=int, default=0, help="clip index within the video")
    p.add_argument(
        "--composition",
        choices=["tight_crop", "blur_fill", "split_screen"],
        default="tight_crop",
        help="filter graph to render with (default: tight_crop)",
    )
    p.add_argument("--force", action="store_true", help="ignore cached outputs for this stage")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(prog="clipper")
    sub = parser.add_subparsers(dest="command", required=True)

    clip_p = sub.add_parser("clip", help="ingest + transcribe + render one clip")
    clip_p.add_argument("url", help="YouTube URL")
    _add_clip_args(clip_p)

    render_p = sub.add_parser("render", help="render only (no download / transcribe)")
    render_p.add_argument("video_id", help="video id of an already-ingested video")
    _add_clip_args(render_p)

    args = parser.parse_args(argv)

    if args.command == "clip":
        from .ingest import ingest
        from .render import render
        from .transcribe import transcribe

        video_id = ingest(args.url, force=args.force)
        transcribe(video_id, force=args.force)
        out = render(
            video_id, args.n, args.start, args.end, composition=args.composition, force=args.force
        )
        print(out)
    elif args.command == "render":
        from .render import render

        out = render(
            args.video_id,
            args.n,
            args.start,
            args.end,
            composition=args.composition,
            force=args.force,
        )
        print(out)
    else:  # argparse enforces required=True, but be explicit rather than silent.
        parser.error(f"unknown command {args.command!r}")
        sys.exit(2)


if __name__ == "__main__":
    main()
