"""Anytime goalscorer, stage 1 (free data, no odds): features, walk-forward and scoring.

The model (models/player_goals.GoalscorerModel) gives P(scores >= 1 | plays). It is
judged walk-forward, refitted weekly on appearances before the week, against two
season-to-date benchmarks: his goals per appearance this season and his xG per
appearance this season (Poisson, position average before his first appearance).

The specification (GOAL_FACTORS, l2, refit and lookback) was fixed before any real
data was scored, and nothing is tuned on the results. 2025/26 is the locked holdout:
it is reported on its own beside the earlier seasons ("development"), and the season
in progress separately ("live"). Ranges resample whole matches (players in one match
are not independent). The research lab's shared harness (src/soccer_stats/lab/) will
take over the evaluation once it is merged; this keeps to its rules (time-ordered
splits, locked holdout, bootstrap ranges).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.edge.stats import bootstrap_mean
from soccer_stats.factors import PRIOR_90S, before_kickoff, ewsum_prior
from soccer_stats.models.player_goals import GOAL_FACTORS, GoalscorerModel

PRIOR_XG = 8.0  # shrink goals / xG toward 1 by this much xG
DEFAULT_POS_XG = {"GK": 0.002, "DEF": 0.05, "MID": 0.12, "FWD": 0.35}  # xG per 90
HOLDOUT = "2526"
CAL_BINS = [0, 0.05, 0.1, 0.2, 0.3, 0.45, 1.0]


def goal_features(feats: pd.DataFrame) -> pd.DataFrame:
    """Add goal features and benchmarks to factors.build_features output.

    Every value uses only matches that kicked off strictly earlier (rows are in time
    order; a player has one row per match).
    """
    df = feats.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True).copy()
    for col in ("goals", "xg"):
        df[col] = df[col].fillna(0).astype(float) if col in df else 0.0
    pid = df["player_id"]
    df["_90s"] = df["minutes"] / 90
    df["_one"] = 1.0
    pos = before_kickoff(df, ["position"], ["xg", "goals", "_90s", "_one"])
    default = df["position"].map(DEFAULT_POS_XG).fillna(0.1)
    pos_xg = (pos["xg"] / pos["_90s"].replace(0, np.nan)).fillna(default)
    s_xg, s_g = ewsum_prior(df["xg"], pid), ewsum_prior(df["goals"], pid)
    s_90 = ewsum_prior(df["_90s"], pid)
    df["xg_rate"] = (s_xg + pos_xg * PRIOR_90S) / (s_90 + PRIOR_90S)
    df["log_xg_rate"] = np.log(df["xg_rate"].clip(lower=0.002))
    df["log_finish"] = np.log(((s_g + PRIOR_XG) / (s_xg + PRIOR_XG)).clip(0.5, 2))

    # Benchmarks: season-to-date goals and xG per appearance.
    g = df.groupby(["player_id", "season"])
    n = g.cumcount()
    pos_apps = pos["_one"].replace(0, np.nan)
    for col in ("goals", "xg"):
        prior = g[col].cumsum() - df[col]
        pos_avg = (pos[col] / pos_apps).fillna(df["position"].map(DEFAULT_POS_XG).fillna(0.1))
        df[f"season_avg_{col}"] = np.where(n > 0, prior / n.replace(0, 1), pos_avg)
    return df.drop(columns=["_90s", "_one"])


def walk_forward(
    feats: pd.DataFrame,
    start: str | pd.Timestamp,
    lineup_known: bool = False,
    factors: list[str] | None = None,
    refit_every: str = "7D",
    lookback_days: int = 730,
    min_prev_apps: int = 3,
) -> pd.DataFrame:
    """Out-of-sample P(scores) for each appearance from `start` on (goal_features input).

    Each week's model is fitted only on appearances before the week; players with fewer
    than `min_prev_apps` earlier appearances are left out, as for shots.
    """
    start = pd.Timestamp(start)
    start = start.tz_localize("UTC") if start.tz is None else start
    end = feats["kickoff"].max() + pd.Timedelta(refit_every)
    weeks = pd.date_range(start, end, freq=refit_every)
    out = []
    for lo, hi in zip(weeks[:-1], weeks[1:], strict=True):
        test = feats[(feats["kickoff"] >= lo) & (feats["kickoff"] < hi)]
        test = test[test["prev_apps"] >= min_prev_apps]
        if test.empty:
            continue
        train = feats[
            (feats["kickoff"] < lo) & (feats["kickoff"] >= lo - pd.Timedelta(days=lookback_days))
        ]
        if len(train) < 200 or train["goals"].sum() < 10:
            continue
        model = GoalscorerModel(factors).fit(train)
        rec = test[
            [
                "match_id",
                "season",
                "kickoff",
                "team",
                "opponent",
                "player_id",
                "player",
                "position",
                "started",
                "minutes",
                "goals",
                "xg",
            ]
        ].copy()
        rec["p_model"] = model.prob_score(test, lineup_known)
        rec["p_base_goals"] = 1 - np.exp(-test["season_avg_goals"].clip(lower=1e-4))
        rec["p_base_xg"] = 1 - np.exp(-test["season_avg_xg"].clip(lower=1e-4))
        rec["scored"] = (test["goals"] > 0).astype(int)
        out.append(rec)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def calibration(preds: pd.DataFrame, col: str = "p_model") -> list[dict]:
    """Predicted vs actual scoring rate by predicted-chance bucket."""
    g = preds.groupby(pd.cut(preds[col], CAL_BINS), observed=True)
    return [
        {
            "bucket": f"{b.left:.0%}-{b.right:.0%}",
            "n": len(x),
            "predicted": round(float(x[col].mean()), 4),
            "scored": round(float(x["scored"].mean()), 4),
        }
        for b, x in g
    ]


def score(preds: pd.DataFrame, seed: int = 0) -> dict:
    """Log loss and Brier score, model vs both benchmarks, with match-resampled ranges
    for the log-loss differences (negative = the model is better)."""
    if preds.empty:
        return {"n": 0}
    y = preds["scored"].to_numpy()
    ll = {k: _ll(preds[c].to_numpy(), y) for k, c in COLUMNS.items()}
    out = {
        "n": len(preds),
        "matches": int(preds["match_id"].nunique()),
        "scored_rate": round(float(y.mean()), 4),
        "predicted_rate": round(float(preds["p_model"].mean()), 4),
    }
    for k, c in COLUMNS.items():
        out[k] = {
            "log_loss": round(float(ll[k].mean()), 5),
            "brier": round(float(((preds[c] - y) ** 2).mean()), 5),
        }
    for base in ("season_goals", "season_xg"):
        d = ll["model"] - ll[base]
        rng = bootstrap_mean(d, preds["match_id"], seed=seed)
        out[f"vs_{base}"] = {
            "diff": round(float(d.mean()), 5),
            "range95": [round(v, 5) for v in rng] if rng else None,
            "beats": bool(rng is not None and rng[1] < 0),
        }
    out["calibration"] = calibration(preds)
    return out


COLUMNS = {"model": "p_model", "season_goals": "p_base_goals", "season_xg": "p_base_xg"}


def split(preds: pd.DataFrame, holdout: str = HOLDOUT) -> dict[str, pd.DataFrame]:
    """development (before the holdout), holdout, live (after it)."""
    s = preds["season"].astype(str)
    parts = {"development": preds[s < holdout], "holdout": preds[s == holdout]}
    parts["live"] = preds[s > holdout]
    return {k: v for k, v in parts.items() if not v.empty}


def report(before: pd.DataFrame, known: pd.DataFrame | None = None) -> dict:
    """Stage-1 result: scores per split, before lineups and (optionally) lineup known.

    The gate (shown, not yet used to switch anything on) is that the model beats both
    benchmarks on the holdout before lineups: the upper end of each range below 0.
    """
    out = {"factors": list(GOAL_FACTORS), "holdout": HOLDOUT, "before_lineups": {}}
    for k, p in split(before).items():
        out["before_lineups"][k] = score(p)
    if known is not None and not known.empty:
        out["lineup_known"] = {k: score(p) for k, p in split(known).items()}
    h = out["before_lineups"].get("holdout", {})
    out["gate"] = {
        "passed": bool(
            h.get("vs_season_goals", {}).get("beats") and h.get("vs_season_xg", {}).get("beats")
        ),
        "rule": "beats both season-to-date benchmarks on the 2025/26 holdout (95% range < 0)",
    }
    return out
