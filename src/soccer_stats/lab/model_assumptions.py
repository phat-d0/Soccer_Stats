"""Stage 1 of model evaluation: do the models' assumptions hold? (docs/lab.md "Model
evaluation, stage 1: method selection"; owner via the lead, 8 Nov.)

Out of sample, development seasons only (2017/18-2023/24; nothing at or after 2024-07-01):
- goals (the match model, Dixon-Coles + xG, walk-forward as live): raw variance-to-mean,
  dispersion given the model's own means (Pearson, Poisson = 1), the home-away correlation
  of the residuals (the model assumes independence apart from the low-score correction), and
  the low-score cells 0-0, 1-0, 0-1, 1-1 observed vs the model (with rho) vs independent
  Poisson on the same means (rho = 0);
- corners (model (f), refitted every 28 days on the 730 days before, as live): dispersion
  under Poisson and under the fitted NB2 (= 1 when NB2 is right), and the alpha fitted;
- data size per league-season.
No prices, no bets, no key, 0 credits.
"""

from __future__ import annotations

import argparse
import functools
import json
import os

import numpy as np
import pandas as pd
from scipy.stats import poisson

from soccer_stats.lab.harness import Holdout

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
FIRST, LAST = 2017, 2023
LOCK = pd.Timestamp("2024-07-01")
CELLS = ((0, 0), (1, 0), (0, 1), (1, 1))
N_BOOT = 2000
SEED = 0
REFIT_CORNERS = "28D"


def boot_stat(groups: np.ndarray, fn, n_boot: int = N_BOOT, seed: int = SEED):
    """Range of fn(rows) resampling rows (each row one match)."""
    groups = np.asarray(groups)
    if len(groups) < 2:
        return None
    rng = np.random.default_rng(seed)
    vals = [fn(groups[rng.integers(0, len(groups), len(groups))]) for _ in range(n_boot)]
    return [float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))]


def goals_checks(preds: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """preds: walk-forward rows with home_goals, away_goals, exp_home, exp_away, matrix,
    rho (the fit's)."""
    hg, ag = preds["home_goals"].to_numpy(float), preds["away_goals"].to_numpy(float)
    lh, la = preds["exp_home"].to_numpy(float), preds["exp_away"].to_numpy(float)
    n = len(preds)
    rows = np.column_stack([hg, ag, lh, la])

    def disp(r):
        return float(
            np.mean(np.r_[(r[:, 0] - r[:, 2]) ** 2 / r[:, 2], (r[:, 1] - r[:, 3]) ** 2 / r[:, 3]])
        )

    def corr(r):
        return float(np.corrcoef(r[:, 0] - r[:, 2], r[:, 1] - r[:, 3])[0, 1])

    cells = {}
    for i, j in CELLS:
        obs = ((hg == i) & (ag == j)).astype(float)
        model = np.array([m[i, j] for m in preds["matrix"]])
        indep = poisson.pmf(i, lh) * poisson.pmf(j, la)
        both = np.column_stack([obs, model, indep])
        cells[f"{i}-{j}"] = {
            "observed": float(obs.mean()),
            "model": float(model.mean()),
            "independent": float(indep.mean()),
            "obs_minus_model": float((obs - model).mean()),
            "obs_minus_model_ci95": boot_stat(both, lambda r: (r[:, 0] - r[:, 1]).mean(), n_boot),
            "obs_minus_indep": float((obs - indep).mean()),
            "obs_minus_indep_ci95": boot_stat(both, lambda r: (r[:, 0] - r[:, 2]).mean(), n_boot),
        }
    return {
        "matches": n,
        "goals_per_match": float((hg + ag).mean()),
        "var_to_mean": {
            "home": float(hg.var() / hg.mean()),
            "away": float(ag.var() / ag.mean()),
            "total": float((hg + ag).var() / (hg + ag).mean()),
        },
        "dispersion_given_model": disp(rows),
        "dispersion_ci95": boot_stat(rows, disp, n_boot),
        "residual_corr": corr(rows),
        "residual_corr_ci95": boot_stat(rows, corr, n_boot),
        "low_score_cells": cells,
    }


def corner_walk(
    cm: pd.DataFrame, start, end, refit: str = REFIT_CORNERS, model=None
) -> pd.DataFrame:
    """Model (f)'s out-of-sample means and alpha for matches in [start, end): refitted every
    `refit` on matches before the block (TeamCornersF keeps the 730 days before). `model`
    is any class with TeamCornersF's fit/rates/alpha_ interface (default TeamCornersF; the
    corners team's candidates, e.g. edge.corner_alpha.CandidateFOos)."""
    from soccer_stats.corners_live import TeamCornersF

    model = model or TeamCornersF
    from soccer_stats.lab.harness import blocks

    cm = cm.sort_values("date").reset_index(drop=True)
    d = pd.to_datetime(cm["date"])
    out = []
    for lo, hi in blocks(start, end, refit):
        test = cm[(d >= lo) & (d < hi)]
        if test.empty:
            continue
        try:
            f = model().fit(cm[d < lo])
        except ValueError:
            continue
        lh, la = f.rates(test["home"], test["away"])
        t = test.copy()
        t["m_home"], t["m_away"] = lh, la
        t["alpha_home"], t["alpha_away"] = f.alpha_
        t["fit_to"] = d[d < lo].max()
        out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def corner_checks(rows: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    y = np.r_[rows["home_corners"].to_numpy(float), rows["away_corners"].to_numpy(float)]
    m = np.r_[rows["m_home"].to_numpy(float), rows["m_away"].to_numpy(float)]
    a = np.r_[rows["alpha_home"].to_numpy(float), rows["alpha_away"].to_numpy(float)]
    n = len(rows)
    arr = np.column_stack([y[:n], y[n:], m[:n], m[n:], a[:n], a[n:]])

    def pois(r):
        return float(
            np.mean(np.r_[(r[:, 0] - r[:, 2]) ** 2 / r[:, 2], (r[:, 1] - r[:, 3]) ** 2 / r[:, 3]])
        )

    def nb(r):
        vh = r[:, 2] + r[:, 4] * r[:, 2] ** 2
        va = r[:, 3] + r[:, 5] * r[:, 3] ** 2
        return float(np.mean(np.r_[(r[:, 0] - r[:, 2]) ** 2 / vh, (r[:, 1] - r[:, 3]) ** 2 / va]))

    def corr(r):
        return float(np.corrcoef(r[:, 0] - r[:, 2], r[:, 1] - r[:, 3])[0, 1])

    return {
        "matches": n,
        "corners_per_team": float(y.mean()),
        "var_to_mean": float(y.var() / y.mean()),
        "dispersion_poisson": pois(arr),
        "dispersion_poisson_ci95": boot_stat(arr, pois, n_boot),
        "dispersion_nb2": nb(arr),
        "dispersion_nb2_ci95": boot_stat(arr, nb, n_boot),
        "alpha_mean": {"home": float(a[:n].mean()), "away": float(a[n:].mean())},
        "residual_corr": corr(arr),
        "residual_corr_ci95": boot_stat(arr, corr, n_boot),
    }


def sizes(matches: pd.DataFrame) -> dict:
    return {str(s): int(len(g)) for s, g in matches.groupby(matches["season"].astype(str))}


def run_league(code: str, n_boot: int = N_BOOT) -> dict:
    from soccer_stats import backtest
    from soccer_stats import leagues as lgs
    from soccer_stats.corners_live import corner_matches
    from soccer_stats.data import load_matches
    from soccer_stats.models import DixonColes
    from soccer_stats.publish import XG_WEIGHT
    from soccer_stats.xg import with_xg

    holdout = Holdout(LOCK)
    m = load_matches([code], range(FIRST - 2, LAST + 1))
    m, err = with_xg(m)
    weight = XG_WEIGHT if m["home_xg"].notna().any() else 0.0
    if lgs.LEAGUES[code].understat and not weight:
        raise RuntimeError(f"no Understat xG attached ({err})")
    m = m[m["date"] < LOCK]
    holdout.check(m["date"])

    preds = backtest.walk_forward(
        m,
        start=f"{FIRST}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=weight),
        keep_matrix=True,
    )
    preds = preds[pd.to_datetime(preds["date"]) < LOCK]
    res = {
        "league": code,
        "xg_weight": weight,
        "sizes": sizes(m[pd.to_datetime(m["date"]) >= pd.Timestamp(f"{FIRST}-07-01")]),
        "goals": goals_checks(preds, n_boot),
    }
    try:
        cm = corner_matches(code, seasons=range(FIRST - 2, LAST + 1))
        cm = cm[pd.to_datetime(cm["date"]) < LOCK]
        holdout.check(cm["date"])
        cw = corner_walk(cm, f"{FIRST}-07-01", LOCK)
        res["corners"] = corner_checks(cw, n_boot) if len(cw) else {"error": "no corner rows"}
    except Exception as exc:  # noqa: BLE001 - reported
        res["corners"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return res


def summary_lines(res: dict) -> list[str]:
    out = []
    for code, r in res["leagues"].items():
        if "error" in r:
            out.append(f"{code}: error {r['error']}")
            continue
        g = r["goals"]
        c = g["low_score_cells"]
        out.append(
            f"{code} goals: {g['matches']} matches, dispersion {g['dispersion_given_model']:.3f} "
            f"{g['dispersion_ci95']}, corr {g['residual_corr']:+.3f} {g['residual_corr_ci95']}, "
            f"0-0 obs-model {c['0-0']['obs_minus_model']:+.4f}, "
            f"1-1 {c['1-1']['obs_minus_model']:+.4f}"
        )
        k = r.get("corners", {})
        if "error" not in k:
            out.append(
                f"{code} corners: {k['matches']} matches, Poisson {k['dispersion_poisson']:.3f}, "
                f"NB2 {k['dispersion_nb2']:.3f} {k['dispersion_nb2_ci95']}, alpha {k['alpha_mean']}"
            )
        else:
            out.append(f"{code} corners: {k['error']}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default=",".join(LEAGUES), help="comma list of codes")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    p.add_argument("--json", default="", help="write the result here too")
    a = p.parse_args(argv)
    leagues = {}
    for code in [c.strip() for c in a.leagues.split(",") if c.strip()]:
        try:
            leagues[code] = run_league(code, a.boot)
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            leagues[code] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        print(f"{code} done", flush=True)
    res = {
        "kind": "model_assumptions",
        "seasons": f"{FIRST}/{(FIRST + 1) % 100:02d}-{LAST}/{(LAST + 1) % 100:02d}",
        "locked_from": str(LOCK.date()),
        "leagues": leagues,
        "runs": {"model_assumptions": os.environ.get("GITHUB_RUN_ID")},
    }
    print("\n".join(summary_lines(res)))
    text = json.dumps(res, default=str)
    print(f"MODEL_ASSUMPTIONS_JSON {text}")
    if a.json:
        with open(a.json, "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()
