"""`clipper experiment ...` — status, verify, power, analyze.

Read-only against the pipeline: none of these call an LLM or touch work/. The
only files written are the analysis outputs (results.json, decision_memo.md)
inside the experiment's own directory.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from . import experiment as ex


def add_experiment_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("experiment", help="pre-registered A/B experiment tools")
    p.add_argument("--name", help="experiment name (default: the only one that exists)")
    es = p.add_subparsers(dest="exp_command", required=True)

    es.add_parser("status", help="flag state, assignment counts, funnel, what is due")
    es.add_parser("verify", help="recompute every assignment from the seed and check the log")

    pw = es.add_parser("power", help="sample size and calendar time for a given effect")
    pw.add_argument("--mde", type=float, default=10.0, help="smallest effect, in points")
    pw.add_argument("--sd", type=float, default=12.0, help="per-clip SD of the metric")
    pw.add_argument("--alpha", type=float, default=0.05)
    pw.add_argument("--power", type=float, default=0.8)
    pw.add_argument("--rates", type=float, nargs="+", default=[1, 2, 3, 5], help="clips/week")
    pw.add_argument("--measured-fraction", type=float, default=0.9)

    an = es.add_parser("analyze", help="run the pre-registered analysis and write the memo")
    an.add_argument("--n-boot", type=int, default=10_000)


def _pick(name: str | None) -> ex.Config:
    configs = ex.load_configs()
    if name:
        for c in configs:
            if c.name == name:
                return c
        raise SystemExit(f"no experiment named {name!r} (have: {[c.name for c in configs]})")
    if len(configs) == 1:
        return configs[0]
    raise SystemExit(
        "pass --name; experiments found: " + (", ".join(c.name for c in configs) or "none")
    )


def _analysis():
    try:
        from . import experiment_analysis as ea
    except ImportError as exc:  # numpy/scipy are in an optional dependency group
        raise SystemExit(
            "the experiment analysis needs numpy/scipy: run `uv sync --group experiment`"
        ) from exc
    return ea


def cmd_status(cfg: ex.Config, *, today: date | None = None) -> int:
    ea = _analysis()
    today = today or date.today()
    arms = list(cfg.arms)
    print(f"experiment: {cfg.name}   status: {cfg.status}   launch_date: {cfg.launch_date}")
    if cfg.launch_date:
        launch = date.fromisoformat(str(cfg.launch_date))
        stop = launch + timedelta(weeks=int(cfg.design["max_duration_weeks"]))
        elapsed = (today - launch).days / 7
        print(f"week {elapsed:.1f} of {cfg.design['max_duration_weeks']}   hard stop: {stop}")
    outcomes = ea.load_outcomes(cfg.dir / "outcomes.csv")
    rows = ea.build_rows(cfg, outcomes)
    fun = ea.funnel(rows, arms)
    print(f"\n{'arm':<12}{'assigned':>9}{'accepted':>9}{'published':>10}{'measured':>9}")
    for a in arms:
        f = fun[a]
        print(f"{a:<12}{f['assigned']:>9}{f['accepted']:>9}{f['published']:>10}{f['measured']:>9}")
    planned = int(cfg.design["planned_n_per_arm"])
    print(f"\nplanned: {planned} measured clips per arm ({2 * planned} total)")

    lo = int(cfg.design["measurement_window_days"][0])
    unpublished = [r["video_id"] for r in rows if r["accepted"] and not r["published"]]
    due = []
    for r in outcomes:
        if r.get("published_at") and r.get("stayed_to_watch_pct") is None:
            pub = date.fromisoformat(r["published_at"][:10])
            if (today - pub).days >= lo:
                due.append(r["video_id"])
    if unpublished:
        print(f"\naccepted but not yet in outcomes.csv ({len(unpublished)}): {unpublished}")
    if due:
        print(f"snapshot due (published >= {lo} days ago, no stayed_to_watch_pct): {due}")
    problems = ex.verify(cfg)
    state = "OK" if not problems else f"{len(problems)} problem(s) — run verify"
    print(f"\nlog integrity: {state}")
    return 0


def cmd_verify(cfg: ex.Config) -> int:
    problems = ex.verify(cfg)
    n = len(ex.read_assignments(cfg))
    if problems:
        print(f"{cfg.name}: {len(problems)} problem(s) in {n} assignment(s)")
        for p in problems:
            print("  -", p)
        return 1
    print(f"{cfg.name}: OK — {n} assignment(s) match the seed; pre-registration/design unchanged")
    return 0


def cmd_power(args: argparse.Namespace) -> int:
    from . import stats

    n_t = stats.n_per_arm(args.mde, args.sd, args.alpha, args.power)
    n_r = stats.n_per_arm_mann_whitney(args.mde, args.sd, args.alpha, args.power)
    print(
        f"MDE {args.mde:g} pts, SD {args.sd:g}, alpha {args.alpha:g}, power {args.power:g}, "
        f"d = {args.mde / args.sd:.2f}"
    )
    print(f"  t-test:        {n_t} per arm ({2 * n_t} total)")
    print(f"  rank test:     {n_r} per arm ({2 * n_r} total)   <- planned n")
    print(f"weeks to reach {2 * n_r} measured clips ({args.measured_fraction:.0%} measured):")
    for r in args.rates:
        w = stats.weeks_to_complete(2 * n_r, r, args.measured_fraction)
        print(f"  {r:g} clip(s)/week -> {w:.0f} weeks")
    return 0


def cmd_analyze(cfg: ex.Config, n_boot: int) -> int:
    ea = _analysis()
    problems = ex.verify(cfg)
    if problems:
        print("refusing to analyze: the assignment log failed verification")
        for p in problems:
            print("  -", p)
        return 1
    rows = ea.build_rows(cfg, ea.load_outcomes(cfg.dir / "outcomes.csv"))
    res = ea.analyze(cfg, rows, n_boot=n_boot)
    (cfg.dir / "results.json").write_text(json.dumps(res, indent=2, default=float) + "\n")
    (cfg.dir / "decision_memo.md").write_text(ea.render_memo(res))
    print(res["decision"]["label"])
    if "primary" in res:
        p = res["primary"]
        b = p["bootstrap"]
        print(
            f"{res['treatment']} - {res['control']}: {p['welch']['diff']:+.1f} pts "
            f"(95% CI {b['lo']:+.1f} to {b['hi']:+.1f}), Welch p = {p['welch']['p']:.3f}"
        )
    print(f"wrote {cfg.dir / 'results.json'} and {cfg.dir / 'decision_memo.md'}")
    return 0


def run_experiment(args: argparse.Namespace) -> int:
    if args.exp_command == "power":
        return cmd_power(args)
    cfg = _pick(args.name)
    if args.exp_command == "status":
        return cmd_status(cfg)
    if args.exp_command == "verify":
        return cmd_verify(cfg)
    if args.exp_command == "analyze":
        return cmd_analyze(cfg, args.n_boot)
    raise SystemExit(f"unknown experiment command {args.exp_command!r}")
