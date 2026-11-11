"""Feature-flag randomization for pre-registered clip A/B experiments.

An experiment lives in experiments/<name>/ with a config.yaml (the flag), a
PREREGISTRATION.md, and append-only logs. This module is the only code that
decides which arm a clip is in, and it is deliberately boring:

* The k-th enrolled clip gets the k-th element of a sequence derived purely
  from the seed (permuted blocks), so an assignment can be *recomputed* and
  checked, never just trusted (see ``verify``).
* Assignment is idempotent — a clip already in the log keeps its arm. A forced
  re-selection must never re-roll the draw.
* Every assignment is appended with the hashes of the pre-registration and of
  the design block, so editing either after launch is detectable.

Stdlib + pyyaml only: the selection stage imports this, and it must not pull
the numpy/scipy analysis stack into the pipeline's hot path.

Single-writer assumption: the CLI is run by one person at a time, so the log is
a plain append with no file locking.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import paths

STATUS_ACTIVE = "active"
STATUS_FROZEN = "frozen"

ASSIGNMENTS_FILE = "assignments.jsonl"
SELECTIONS_FILE = "selection_outcomes.jsonl"

_REQUIRED_RECORD_KEYS = (
    "index", "experiment", "video_id", "arm", "block", "pos", "seed",
    "prereg_sha256", "design_sha256", "ts",
)  # fmt: skip

NO_EXPERIMENT = "NONE"


class ExperimentError(Exception):
    """A config or log problem serious enough that randomizing would be unsafe."""


@dataclass(frozen=True)
class Config:
    name: str
    status: str
    launch_date: str | None
    preregistration: str
    design: dict
    dir: Path

    @property
    def seed(self) -> int:
        return int(self.design["seed"])

    @property
    def block_size(self) -> int:
        return int(self.design["block_size"])

    @property
    def arms(self) -> dict[str, dict]:
        return self.design["arms"]

    @property
    def control_arm(self) -> str:
        return self.design["control_arm"]

    @property
    def assignments_path(self) -> Path:
        return self.dir / ASSIGNMENTS_FILE

    @property
    def selections_path(self) -> Path:
        return self.dir / SELECTIONS_FILE


@dataclass(frozen=True)
class Enrollment:
    """A clip's place in a running experiment."""

    experiment: str
    arm: str
    hook_style: str
    index: int

    def as_dict(self) -> dict:
        return {"name": self.experiment, "arm": self.arm, "index": self.index}


# --- config -----------------------------------------------------------------


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    try:
        cfg = Config(
            name=raw["name"],
            status=raw["status"],
            launch_date=raw.get("launch_date"),
            preregistration=raw["preregistration"],
            design=raw["design"],
            dir=path.parent,
        )
        weights = [int(a["weight"]) for a in cfg.arms.values()]
        _ = cfg.seed, cfg.block_size, cfg.control_arm
    except (KeyError, TypeError, ValueError) as exc:
        raise ExperimentError(f"{path}: malformed experiment config ({exc!r})") from exc
    if cfg.control_arm not in cfg.arms:
        raise ExperimentError(f"{path}: control_arm {cfg.control_arm!r} is not one of the arms")
    if cfg.block_size % sum(weights) != 0:
        raise ExperimentError(
            f"{path}: block_size {cfg.block_size} must be a multiple of the total arm weight "
            f"{sum(weights)} so every block is balanced"
        )
    return cfg


def load_configs() -> list[Config]:
    root = paths.EXPERIMENTS_DIR
    if not root.exists():
        return []
    return [load_config(p) for p in sorted(root.glob("*/config.yaml"))]


def running_config() -> Config | None:
    """The one experiment that is active or frozen, if any.

    More than one at a time would let two experiments contaminate each other's
    clips, so that is an error rather than a silent pick.
    """
    live = [c for c in load_configs() if c.status in (STATUS_ACTIVE, STATUS_FROZEN)]
    if len(live) > 1:
        raise ExperimentError(
            "more than one experiment is active/frozen: " + ", ".join(c.name for c in live)
        )
    return live[0] if live else None


def design_sha256(design: dict) -> str:
    canon = json.dumps(design, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def prereg_sha256(cfg: Config) -> str:
    return hashlib.sha256((cfg.dir / cfg.preregistration).read_bytes()).hexdigest()


# --- the deterministic sequence --------------------------------------------


def _block_arms(cfg: Config, block: int) -> list[str]:
    """The shuffled arm order of one block, derived only from (seed, block)."""
    total = sum(int(a["weight"]) for a in cfg.arms.values())
    per_unit = cfg.block_size // total
    slots = [name for name, a in cfg.arms.items() for _ in range(int(a["weight"]) * per_unit)]
    # A str seed is hashed with sha512 by random.Random — stable across runs,
    # platforms, and PYTHONHASHSEED, unlike hash().
    random.Random(f"{cfg.seed}:{block}").shuffle(slots)
    return slots


def arm_at(cfg: Config, index: int) -> tuple[str, int, int]:
    """(arm, block, position-in-block) for the index-th enrolled clip."""
    block, pos = divmod(index, cfg.block_size)
    return _block_arms(cfg, block)[pos], block, pos


def arm_sequence(cfg: Config, n: int) -> list[str]:
    return [arm_at(cfg, i)[0] for i in range(n)]


# --- the log ----------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_assignments(cfg: Config) -> list[dict]:
    return _read_jsonl(cfg.assignments_path)


def read_selections(cfg: Config) -> list[dict]:
    return _read_jsonl(cfg.selections_path)


def lookup(cfg: Config, video_id: str) -> dict | None:
    for rec in read_assignments(cfg):
        if rec["video_id"] == video_id:
            return rec
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def assign(cfg: Config, video_id: str) -> dict:
    """Enrol ``video_id`` and return its assignment record (idempotent)."""
    existing = lookup(cfg, video_id)
    if existing is not None:
        return existing
    index = len(read_assignments(cfg))
    arm, block, pos = arm_at(cfg, index)
    record = {
        "index": index,
        "experiment": cfg.name,
        "video_id": video_id,
        "arm": arm,
        "block": block,
        "pos": pos,
        "seed": cfg.seed,
        "prereg_sha256": prereg_sha256(cfg),
        "design_sha256": design_sha256(cfg.design),
        "ts": _now(),
    }
    _append_jsonl(cfg.assignments_path, record)
    return record


def enroll(video_id: str, *, tier: str) -> Enrollment | None:
    """What the selection stage calls for each candidate it is about to send.

    None means "not in an experiment": no experiment is running, the flag is
    off (preregistered), the clip is outside the eligibility tier, or enrolment
    is frozen. A clip that is already in the log keeps its arm even when the
    experiment is frozen.
    """
    cfg = running_config()
    if cfg is None:
        return None
    rec = lookup(cfg, video_id)
    if rec is None:
        if cfg.status != STATUS_ACTIVE:
            return None
        if tier != cfg.design.get("eligible_tier", tier):
            return None
        rec = assign(cfg, video_id)
    return Enrollment(
        experiment=cfg.name,
        arm=rec["arm"],
        hook_style=cfg.arms[rec["arm"]]["hook_style"],
        index=rec["index"],
    )


def hook_style_label(enrollment: Enrollment | None) -> str:
    """The value of the prompt's per-clip ``Hook style:`` line."""
    return enrollment.hook_style if enrollment else NO_EXPERIMENT


# --- the manipulation and its check ----------------------------------------

_SPOKEN = ("cold_open", "pre_punchline")


def hook_line(segment: dict) -> str | None:
    """The line the experiment manipulates: the spoken line for spoken
    placements, the held on-screen caption for ``caption_only``."""
    if segment.get("vo_placement") in _SPOKEN:
        line = segment.get("vo_script")
    else:
        line = segment.get("on_screen_caption")
    line = (line or "").strip()
    return line or None


def check_compliance(hook_style: str, line: str | None) -> bool | None:
    """Did the model do what the arm asked? None when there is no line at all
    (which is itself a protocol deviation, but not one a punctuation check can
    classify)."""
    if line is None:
        return None
    if hook_style == "QUESTION":
        return line.rstrip().endswith("?")
    if hook_style == "STATEMENT":
        return "?" not in line
    return None


def record_selection(enrollment: Enrollment | None, video_id: str, segment: dict | None) -> None:
    """Log what selection did with an enrolled clip: accepted or rejected, the
    hook line, whether it complied with its arm, and whether the title happens
    to be a question. Later records for the same clip supersede earlier ones (a
    forced re-selection is a fresh model draw under the same arm).
    """
    if enrollment is None:
        return
    cfg = running_config()
    if cfg is None:
        return
    line = hook_line(segment) if segment else None
    title = (segment or {}).get("suggested_title", "") or ""
    _append_jsonl(
        cfg.selections_path,
        {
            "ts": _now(),
            "experiment": enrollment.experiment,
            "video_id": video_id,
            "arm": enrollment.arm,
            "accepted": segment is not None,
            "hook_line": line,
            "compliant": check_compliance(enrollment.hook_style, line) if segment else None,
            "title_is_question": title.rstrip().endswith("?") if segment else None,
        },
    )


# --- integrity --------------------------------------------------------------


def verify(cfg: Config) -> list[str]:
    """Problems found in the assignment log; an empty list means it is sound.

    Recomputes every arm from the seed rather than trusting the log, and
    checks that neither the pre-registration nor the design block has been
    edited since the first assignment.
    """
    problems: list[str] = []
    want_prereg, want_design = prereg_sha256(cfg), design_sha256(cfg.design)
    seen: set[str] = set()
    for i, rec in enumerate(read_assignments(cfg)):
        missing = [k for k in _REQUIRED_RECORD_KEYS if k not in rec]
        if missing:
            problems.append(f"line {i}: missing keys {missing}")
            continue
        if rec["index"] != i:
            problems.append(f"line {i}: index is {rec['index']}, expected {i}")
        if rec["video_id"] in seen:
            problems.append(f"line {i}: {rec['video_id']} assigned more than once")
        seen.add(rec["video_id"])
        expected, _, _ = arm_at(cfg, i)
        if rec["arm"] != expected:
            problems.append(
                f"line {i}: {rec['video_id']} logged as {rec['arm']!r} but the seed "
                f"prescribes {expected!r}"
            )
        if rec["seed"] != cfg.seed:
            problems.append(f"line {i}: seed {rec['seed']} != config seed {cfg.seed}")
        if rec["experiment"] != cfg.name:
            problems.append(f"line {i}: experiment {rec['experiment']!r} != {cfg.name!r}")
        if rec["prereg_sha256"] != want_prereg:
            problems.append(f"line {i}: pre-registration changed since this assignment")
        if rec["design_sha256"] != want_design:
            problems.append(f"line {i}: design block changed since this assignment")
    return problems
