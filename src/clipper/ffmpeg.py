"""The single choke point for every ffmpeg / ffprobe invocation.

Per CLAUDE.md: ffmpeg fails silently and weirdly. Nothing in this project may
call subprocess for ffmpeg directly. Everything goes through ``run_ffmpeg`` so
that the full command is always logged and a non-zero exit always raises with
enough stderr to see what happened.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence

log = logging.getLogger("clipper.ffmpeg")

# How much of ffmpeg's stderr tail to surface on failure.
STDERR_TAIL_LINES = 40


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe process exits non-zero.

    Carries the return code and the tail of stderr so callers and logs get the
    actual reason instead of a bare CalledProcessError.
    """

    def __init__(self, cmd: Sequence[str], returncode: int, stderr_tail: str) -> None:
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        super().__init__(
            f"{cmd[0]} exited {returncode}\n"
            f"command: {' '.join(cmd)}\n"
            f"--- last {STDERR_TAIL_LINES} lines of stderr ---\n"
            f"{stderr_tail}"
        )


def _tail(text: str, n: int = STDERR_TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def run_ffmpeg(args: Sequence[str], *, binary: str = "ffmpeg") -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg (or ffprobe) command, logging it and raising on failure.

    ``args`` is the argument list WITHOUT the leading binary name, e.g.
    ``["-y", "-i", "in.mp4", ..., "out.mp4"]``.

    Logs the full resolved command before running. On a non-zero exit, raises
    :class:`FFmpegError` carrying the last ``STDERR_TAIL_LINES`` lines of stderr.
    Never swallows failures and never falls back to a default — a broken render
    must surface loudly.

    The binary is resolved via ``CLIPPER_FFMPEG_BIN`` / ``CLIPPER_FFPROBE_BIN``
    if set (an explicit path pin), else looked up on PATH. The pin matters on
    machines with more than one ffmpeg where the PATH-first one may lack the
    ``ass``/``drawtext`` filters this pipeline requires.
    """
    env_override = {
        "ffmpeg": os.environ.get("CLIPPER_FFMPEG_BIN"),
        "ffprobe": os.environ.get("CLIPPER_FFPROBE_BIN"),
    }.get(binary)
    resolved = env_override or shutil.which(binary)
    if resolved is None:
        raise FFmpegError(
            [binary, *args],
            127,
            f"{binary} not found on PATH. Install ffmpeg and ensure it is on PATH.",
        )

    cmd = [resolved, *args]
    log.info("running: %s", " ".join(cmd))

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, _tail(proc.stderr))

    return proc


def run_ffprobe(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Convenience wrapper for ffprobe. Same logging and failure semantics."""
    return run_ffmpeg(args, binary="ffprobe")


def probe_duration(path: str) -> float:
    """Duration in seconds of any media file, via ffprobe. Shared across
    stages (render.py's clip/gameplay/VO probes, narrate.py's duration-fit
    check) — this is generic infra, not stage-specific behavior, so it lives
    here rather than being duplicated per stage."""
    proc = run_ffprobe(["-v", "error", "-show_entries", "format=duration", "-of", "json", path])
    return float(json.loads(proc.stdout)["format"]["duration"])
