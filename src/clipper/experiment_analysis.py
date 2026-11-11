"""Analysis of a pre-registered clip experiment: merge the assignment and
selection logs with hand-entered YouTube Studio outcomes, run the tests and
diagnostics PREREGISTRATION.md commits to, apply its decision rule mechanically,
and render the decision memo.

The decision is computed by ``decide`` from the numbers — not by whoever is
reading them — so "ship it" can't be argued into existence after the fact.
Needs the optional `experiment` dependency group (numpy, scipy).
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import numpy as np

from . import experiment as ex
from . import stats

ALPHA = 0.05
SRM_ALARM_P = 0.001  # design-integrity failure: withhold the decision
DIAGNOSTIC_FLAG_P = 0.01
MIN_COMPLIANCE = 0.80

_NUMERIC = {
    "views": int,
    "stayed_to_watch_pct": float,
    "avg_view_duration_s": float,
    "avg_view_pct": float,
}


# --- loading and merging ----------------------------------------------------


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s.strip()[:10]) if s and s.strip() else None


def load_outcomes(path: Path) -> list[dict]:
    """Rows of outcomes.csv with blanks as None and numbers parsed. A
    stayed_to_watch_pct outside 0-100 is a data-entry error and is rejected
    loudly rather than silently analysed."""
    if not path.exists():
        return []
    rows = []
    with path.open(newline="") as f:
        for n, raw in enumerate(csv.DictReader(f), start=2):
            row = {k: (v.strip() if v and v.strip() else None) for k, v in raw.items()}
            for col, cast in _NUMERIC.items():
                if row.get(col) is not None:
                    try:
                        row[col] = cast(row[col])
                    except ValueError as exc:
                        raise ValueError(f"{path}:{n}: bad {col} {row[col]!r}") from exc
            pct = row.get("stayed_to_watch_pct")
            if pct is not None and not 0 <= pct <= 100:
                raise ValueError(f"{path}:{n}: stayed_to_watch_pct {pct} is outside 0-100")
            rows.append(row)
    return rows


def _age_days(row: dict) -> int | None:
    pub, snap = _date(row.get("published_at")), _date(row.get("snapshot_date"))
    return (snap - pub).days if pub and snap else None


def build_rows(cfg: ex.Config, outcomes: list[dict]) -> list[dict]:
    """One row per ASSIGNED clip, in assignment order, with what selection did
    and what (if anything) was measured. Later outcome/selection records for the
    same clip supersede earlier ones."""
    out_by_id = {r["video_id"]: r for r in outcomes if r.get("video_id")}
    sel_by_id = {r["video_id"]: r for r in ex.read_selections(cfg)}
    lo, hi = cfg.design.get("measurement_window_days", [6, 10])
    rows = []
    for rec in ex.read_assignments(cfg):
        vid = rec["video_id"]
        sel, o = sel_by_id.get(vid), out_by_id.get(vid)
        age = _age_days(o) if o else None
        pct = o.get("stayed_to_watch_pct") if o else None
        rows.append(
            {
                "video_id": vid,
                "arm": rec["arm"],
                "accepted": bool(sel and sel["accepted"]),
                "compliant": bool(sel and sel.get("compliant") is True),
                "title_is_question": bool(sel and sel.get("title_is_question")),
                "hook_line": sel.get("hook_line") if sel else None,
                "published": bool(o and o.get("published_at")),
                "age_days": age,
                "measured": pct is not None and age is not None and lo <= age <= hi,
                "views": o.get("views") if o else None,
                "stayed_to_watch_pct": pct,
                "avg_view_duration_s": o.get("avg_view_duration_s") if o else None,
                "avg_view_pct": o.get("avg_view_pct") if o else None,
            }
        )
    return rows


def funnel(rows: list[dict], arms: list[str]) -> dict[str, dict]:
    return {
        a: {
            "assigned": sum(r["arm"] == a for r in rows),
            "accepted": sum(r["arm"] == a and r["accepted"] for r in rows),
            "published": sum(r["arm"] == a and r["published"] for r in rows),
            "measured": sum(r["arm"] == a and r["measured"] for r in rows),
        }
        for a in arms
    }


# --- helpers ----------------------------------------------------------------


def holm(pvalues: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adj[i] = running
    return adj


def blinded_reestimate(cfg: ex.Config, rows: list[dict]) -> dict | None:
    """The pre-registered blinded sample-size re-estimate: once enough clips are
    measured, pool the outcome ignoring arm, recompute n once, and allow the
    planned n to rise (never fall)."""
    d = cfg.design
    values = [r["stayed_to_watch_pct"] for r in rows if r["measured"]]
    if len(values) < int(d.get("blinded_reestimate_at_n", 16)):
        return None
    pooled = stats.blinded_pooled_sd(values)
    sd = stats.blinded_adjusted_sd(values, d["mde_points"])
    n_t = stats.n_per_arm(d["mde_points"], sd, d["alpha"], d["power"])
    n_mwu = stats.n_per_arm_mann_whitney(d["mde_points"], sd, d["alpha"], d["power"])
    before = int(d["planned_n_per_arm"])
    return {
        "n_measured": len(values),
        "pooled_sd": pooled,
        "adjusted_sd": sd,
        "planning_sd": float(d["planning_sd"]),
        "n_per_arm_t": n_t,
        "n_per_arm_rank": n_mwu,
        "planned_before": before,
        "planned_after": max(before, n_mwu),
    }


# --- the analysis -----------------------------------------------------------


def analyze(cfg: ex.Config, rows: list[dict], *, n_boot: int = 10_000) -> dict:
    """Run the pre-registered analysis. Effects are treatment minus control."""
    arms = list(cfg.arms)
    if len(arms) != 2:
        raise ValueError("analyze() supports exactly two arms")
    control = cfg.control_arm
    treatment = next(a for a in arms if a != control)
    weights = [float(cfg.arms[a]["weight"]) for a in (treatment, control)]

    fun = funnel(rows, [treatment, control])
    meas = [r for r in rows if r["measured"]]

    def vals(arm: str, key: str, pool=meas) -> list[float]:
        return [r[key] for r in pool if r["arm"] == arm and r[key] is not None]

    t, c = vals(treatment, "stayed_to_watch_pct"), vals(control, "stayed_to_watch_pct")
    reest = blinded_reestimate(cfg, rows)
    planned = reest["planned_after"] if reest else int(cfg.design["planned_n_per_arm"])

    res: dict = {
        "experiment": cfg.name,
        "treatment": treatment,
        "control": control,
        "planned_n_per_arm": planned,
        "funnel": fun,
        "blinded_reestimate": reest,
        "n": {treatment: len(t), control: len(c)},
        "insufficient": min(len(t), len(c)) < 2,
    }

    # --- diagnostics (computed even when the primary can't be) ---
    accepted = [fun[treatment]["accepted"], fun[control]["accepted"]]
    assigned = [fun[treatment]["assigned"], fun[control]["assigned"]]
    measured = [fun[treatment]["measured"], fun[control]["measured"]]
    # A clip can only be published if selection accepted it, so a measured clip
    # with no accepted selection record means the logs are inconsistent.
    measured_accepted = [
        sum(r["arm"] == a and r["measured"] and r["accepted"] for r in rows)
        for a in (treatment, control)
    ]
    orphaned = sum(measured) - sum(measured_accepted)
    comp_n = [
        sum(r["compliant"] for r in rows if r["arm"] == a and r["accepted"])
        for a in (treatment, control)
    ]
    res["diagnostics"] = {
        "srm_assigned": stats.srm_test(assigned, weights),
        "srm_analyzed": stats.srm_test(measured, weights),
        "acceptance": stats.attrition_test(accepted, assigned) if min(assigned) else None,
        "attrition": (
            stats.attrition_test(measured_accepted, accepted) if min(accepted) else None
        ),
        "measured_without_accepted_selection": orphaned,
        "compliance": {
            a: (comp_n[i] / accepted[i] if accepted[i] else None)
            for i, a in enumerate((treatment, control))
        },
        "title_is_question": {
            a: (
                sum(r["title_is_question"] for r in rows if r["arm"] == a and r["accepted"])
                / fun[a]["accepted"]
                if fun[a]["accepted"]
                else None
            )
            for a in (treatment, control)
        },
    }
    if res["insufficient"]:
        res["decision"] = decide(res)
        return res

    # --- primary + robustness ---
    seed = cfg.seed
    res["primary"] = {
        "metric": cfg.design["primary_metric"],
        "mean": {treatment: float(np.mean(t)), control: float(np.mean(c))},
        "welch": stats.welch(t, c),
        "mann_whitney": stats.mann_whitney(t, c),
        "bootstrap": stats.bootstrap_diff_ci(t, c, n_boot=n_boot, seed=seed),
        "hedges_g": stats.hedges_g(t, c),
    }

    # --- sensitivity (reported, never decisive) ---
    wt = [max(float(r["views"] or 0), 1.0) for r in meas if r["arm"] == treatment]
    wc = [max(float(r["views"] or 0), 1.0) for r in meas if r["arm"] == control]
    sens = {
        "views_weighted": stats.bootstrap_diff_ci(
            t, c, n_boot=n_boot, seed=seed, weights_treatment=wt, weights_control=wc
        )
    }
    comp = [r for r in meas if r["compliant"]]
    pt = vals(treatment, "stayed_to_watch_pct", comp)
    pc = vals(control, "stayed_to_watch_pct", comp)
    if min(len(pt), len(pc)) >= 2:
        sens["per_protocol"] = {
            "n": {treatment: len(pt), control: len(pc)},
            "welch": stats.welch(pt, pc),
        }
    res["sensitivity"] = sens

    # --- secondaries (descriptive; Holm across the three) ---
    sec, raw_p = {}, []
    for key, label, tf in (
        ("views", "views (log1p)", np.log1p),
        ("avg_view_duration_s", "avg view duration (s)", None),
        ("avg_view_pct", "avg % viewed", None),
    ):
        a, b = vals(treatment, key), vals(control, key)
        if min(len(a), len(b)) >= 2:
            aa, bb = (tf(a), tf(b)) if tf else (a, b)
            w = stats.welch(aa, bb)
            sec[key] = {"label": label, "diff": w["diff"], "p": w["p"]}
            raw_p.append(w["p"])
    for k, adj in zip(sec, holm(raw_p), strict=True):
        sec[k]["p_holm"] = adj
    res["secondary"] = sec

    res["decision"] = decide(res)
    return res


def decide(res: dict) -> dict:
    """Apply the pre-registered decision rule. Returns the decision, the flags
    that constrained it, and whether the experiment is underpowered."""
    t, c = res["treatment"], res["control"]
    diag = res["diagnostics"]
    flags: list[str] = []

    if res["insufficient"]:
        return {
            "decision": "no_decision",
            "label": "NO DECISION — not enough measured clips yet",
            "flags": [],
            "underpowered": True,
        }

    if diag["srm_analyzed"]["p"] < SRM_ALARM_P:
        return {
            "decision": "no_decision",
            "label": "NO DECISION — sample-ratio mismatch (design integrity failure)",
            "flags": [f"SRM p={diag['srm_analyzed']['p']:.2g} on the analysed arm counts"],
            "underpowered": False,
        }

    for name, key in (("selection acceptance", "acceptance"), ("attrition", "attrition")):
        d = diag.get(key)
        if d and d["p"] < DIAGNOSTIC_FLAG_P:
            flags.append(f"differential {name} by arm (Fisher p={d['p']:.3g})")
    if diag["measured_without_accepted_selection"]:
        flags.append(
            f"{diag['measured_without_accepted_selection']} measured clip(s) have no accepted "
            "selection record — the logs are inconsistent"
        )
    for arm, rate in diag["compliance"].items():
        if rate is not None and rate < MIN_COMPLIANCE:
            flags.append(f"compliance in {arm!r} arm is {rate:.0%} (< {MIN_COMPLIANCE:.0%})")

    p = res["primary"]
    welch, mwu, boot = p["welch"], p["mann_whitney"], p["bootstrap"]
    sig = welch["p"] < ALPHA and mwu["p"] < ALPHA and (boot["lo"] > 0 or boot["hi"] < 0)
    sign = np.sign(welch["diff"])
    concordant = sign == np.sign(boot["diff"]) and (boot["lo"] > 0 if sign > 0 else boot["hi"] < 0)

    n_min = min(res["n"].values())
    underpowered = n_min < res["planned_n_per_arm"]

    if sig and concordant and not flags:
        if welch["diff"] > 0:
            decision, label = "ship_treatment", f"SHIP {t.upper()}"
        else:
            decision, label = "keep_control", f"KEEP {c.upper()}"
    elif sig and flags:
        decision, label = "inconclusive", "INCONCLUSIVE — significant, but a diagnostic flag fired"
    else:
        decision, label = "inconclusive", "INCONCLUSIVE"
    if underpowered:
        label += f" (underpowered: {n_min} of {res['planned_n_per_arm']} planned per arm)"
    return {"decision": decision, "label": label, "flags": flags, "underpowered": underpowered}


# --- memo -------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _funnel_table(res: dict) -> list[str]:
    f, t, c = res["funnel"], res["treatment"], res["control"]
    lines = [
        "| arm | assigned | accepted by selection | published | measured |",
        "|---|---|---|---|---|",
    ]
    for a in (c, t):
        r = f[a]
        lines.append(
            f"| {a} | {r['assigned']} | {r['accepted']} | {r['published']} | {r['measured']} |"
        )
    return lines


def _primary_section(res: dict) -> list[str]:
    t, c, p = res["treatment"], res["control"], res["primary"]
    w, m, b = p["welch"], p["mann_whitney"], p["bootstrap"]
    out = [
        "## Primary result",
        "",
        f"`{p['metric']}`, {t} minus {c}.",
        "",
        f"- Mean: {t} {p['mean'][t]:.1f}% (n={res['n'][t]}) "
        f"vs {c} {p['mean'][c]:.1f}% (n={res['n'][c]})",
        f"- **Difference: {w['diff']:+.1f} points, "
        f"95% bootstrap CI [{b['lo']:+.1f}, {b['hi']:+.1f}]**",
        f"- Welch t-test p = {w['p']:.3f}; Mann-Whitney p = {m['p']:.3f}; "
        f"P({t} > {c}) = {m['prob_superiority']:.2f}; Hedges' g = {p['hedges_g']:+.2f}",
        "",
    ]
    if res["decision"]["underpowered"]:
        out += [
            "The planned sample size was not reached, so a non-significant result "
            "means *inconclusive*, not *no effect*.",
            "",
        ]
    sens = res.get("sensitivity", {})
    if sens:
        out += ["## Sensitivity (not decisive)", ""]
        if "views_weighted" in sens:
            v = sens["views_weighted"]
            out.append(
                f"- Views-weighted difference: {v['diff']:+.1f} points, "
                f"CI [{v['lo']:+.1f}, {v['hi']:+.1f}]"
            )
        if "per_protocol" in sens:
            pp = sens["per_protocol"]["welch"]
            out.append(
                f"- Per-protocol (compliant clips only): {pp['diff']:+.1f} points, "
                f"p = {pp['p']:.3f}"
            )
        out.append("")
    if res.get("secondary"):
        out += ["## Secondary outcomes (Holm-adjusted, no decision weight)", ""]
        for v in res["secondary"].values():
            out.append(
                f"- {v['label']}: diff {v['diff']:+.2f}, p = {v['p']:.3f}, "
                f"Holm p = {v['p_holm']:.3f}"
            )
        out.append("")
    return out


def _diagnostics_section(res: dict) -> list[str]:
    dg = res["diagnostics"]
    out = ["## Diagnostics", ""]
    for label, key in (("assigned", "srm_assigned"), ("analysed", "srm_analyzed")):
        d = dg[key]
        out.append(f"- Sample ratio ({label}): {d['counts']}, p = {d['p']:.3f}")
    for label, key in (
        ("Selection acceptance by arm", "acceptance"),
        ("Accepted -> measured by arm", "attrition"),
    ):
        d = dg[key]
        if d:
            rates = [_pct(x) for x in d["rate"]]
            out.append(f"- {label} {rates}: Fisher p = {d['p']:.3f}")
    comp = ", ".join(f"{a} {_pct(r)}" for a, r in dg["compliance"].items())
    out.append(f"- Compliance with arm: {comp}")
    out.append(
        "- Titles ending in '?': "
        + ", ".join(f"{a} {_pct(r)}" for a, r in dg["title_is_question"].items())
    )
    re_ = res.get("blinded_reestimate")
    if re_:
        out += [
            "",
            f"Blinded re-estimate at n={re_['n_measured']}: within-arm SD "
            f"{re_['adjusted_sd']:.1f} (pooled {re_['pooled_sd']:.1f}; planned with "
            f"{re_['planning_sd']:.0f}); planned n/arm "
            f"{re_['planned_before']} -> {re_['planned_after']}.",
        ]
    return out


def render_memo(res: dict, *, synthetic: bool = False) -> str:
    """The one-page decision memo. ``synthetic`` stamps a banner on output built
    from simulated data, so it can never be mistaken for a real result."""
    d = res["decision"]
    lines: list[str] = []
    if synthetic:
        lines += ["> **SYNTHETIC DATA — generated from a simulation, not a real experiment.**", ""]
    lines += [f"# Decision memo: {res['experiment']}", "", f"## Decision: {d['label']}", ""]
    if d["flags"]:
        lines += ["**Flags:** " + "; ".join(d["flags"]), ""]
    lines += ["## Funnel", "", *_funnel_table(res), ""]
    lines += [f"Planned: {res['planned_n_per_arm']} measured clips per arm.", ""]
    if "primary" in res:
        lines += _primary_section(res)
    lines += _diagnostics_section(res)
    lines += ["", "## What to test next", ""]
    lines += ["_To be written by the owner after reading the above._", ""]
    return "\n".join(lines)


# --- plotting (the one chart) -----------------------------------------------


def plot_primary(res: dict, rows: list[dict], ax=None):
    """Per-clip points by arm with the mean difference and its 95% CI in the
    title. Returns the axes, or None when there is nothing to plot."""
    import matplotlib.pyplot as plt

    if "primary" not in res:
        return None
    t, c, p = res["treatment"], res["control"], res["primary"]
    if ax is None:
        _, ax = plt.subplots(figsize=(7, 4.2))
    rng = np.random.default_rng(0)
    for i, arm in enumerate((c, t)):
        y = [r["stayed_to_watch_pct"] for r in rows if r["measured"] and r["arm"] == arm]
        ax.scatter(i + rng.uniform(-0.12, 0.12, len(y)), y, alpha=0.65, s=28, zorder=3)
        ax.hlines(p["mean"][arm], i - 0.25, i + 0.25, colors="black", lw=2.5, zorder=4)
    ax.set_xticks(
        [0, 1], [f"{c}\n(control, n={res['n'][c]})", f"{t}\n(treatment, n={res['n'][t]})"]
    )
    ax.set_ylabel("Stayed to watch (%)")
    b = p["bootstrap"]
    ax.set_title(
        f"{t} − {c}: {p['welch']['diff']:+.1f} pts  (95% CI {b['lo']:+.1f} to {b['hi']:+.1f})",
        fontsize=11,
    )
    ax.grid(axis="y", alpha=0.25)
    return ax
