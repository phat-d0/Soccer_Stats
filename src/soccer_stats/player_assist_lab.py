"""Assists and "score or assist": a free development look (lead, 26 Nov 2026; scoping only).

Question: can goalscorer B's structure, given assist features from Understat's own match
files (assists, expected assists xA and key passes per player), beat a season-to-date
baseline on log loss? And the same for "score or assist" (goals + assists >= 1).

- **Assists model:** an NB2 count model with B's mixture (starts and substitute minutes,
  lineup known) on `ASSIST_FACTORS`: his shrunk xA and key passes per 90, assists per xA
  (shrunk hard toward 1), position, the match model's team expected goals, the opponent's
  conceding and home / game state. **Score or assist:** the same on goals + assists with B's
  goal factors and the assist ones together (G + A >= 1 exactly when he scores or assists).
- **Baselines:** season-to-date assists per appearance and (goals + assists) per
  appearance, P(>= 1) = 1 - exp(-mean), the position average before his first appearance.
- **Scoring:** walk-forward with the lab harness (refit every 28 days on the 730 days
  before the block, players with 3+ earlier appearances), starters with the lineup known,
  log-loss gain over the baseline with a 95% range resampling whole matches. Development
  seasons 2023/24 and 2024/25 are the read; 2025/26 is reported apart, descriptive (it
  has been seen); 2026/27 is never touched (`FORWARD_START`).
- **Base rates** for the market probe's Yes-only prices: per match, distinct players who
  assist, who score or assist, and who are booked (Understat's yellow and red cards).

Nothing here reads a price. Features use only earlier kickoffs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.factors import PRIOR_90S, before_kickoff, ewsum_prior
from soccer_stats.lab import harness
from soccer_stats.lab.harness import Holdout
from soccer_stats.lab.metrics import boot_range
from soccer_stats.models.player_counts import NBRegression
from soccer_stats.models.player_goals import FIT_OPTIONS, GOAL_FACTORS, p_zero
from soccer_stats.player_goals import FORWARD_START

PRIOR_XA = 4.0  # shrink assists / xA toward 1 by this much xA
DEFAULT_POS_XA = {"GK": 0.005, "DEF": 0.06, "MID": 0.13, "FWD": 0.15}  # xA per 90
DEFAULT_POS_KP = {"GK": 0.05, "DEF": 0.6, "MID": 1.2, "FWD": 1.1}  # key passes per 90
ASSIST_FEATS = ["log_xa_rate", "log_kp_rate", "log_assist_conv"]
ASSIST_FACTORS = [
    *ASSIST_FEATS,
    "pos_def",
    "pos_fwd",
    "log_team_xg",
    "log_opp_conceded",
    "home",
    "game_state",
]
GA_FACTORS = [*GOAL_FACTORS, *ASSIST_FEATS]
TARGETS = {"assists": ASSIST_FACTORS, "ga": GA_FACTORS}
REFIT = "28D"
LOOKBACK_DAYS = 730
MIN_PREV_APPS = 3
DEV_SEASONS = ("2324", "2425")
SEEN_SEASON = "2526"


def assist_features(feats: pd.DataFrame) -> pd.DataFrame:
    """Add the assist features and both baselines to goal_features output (rows in time
    order, one per player and match; every value from strictly earlier kickoffs)."""
    df = feats.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True).copy()
    for col in ("assists", "xa", "key_passes", "goals"):
        df[col] = df[col].fillna(0).astype(float) if col in df else 0.0
    df["ga"] = df["goals"] + df["assists"]
    pid = df["player_id"]
    df["_90s"] = df["minutes"] / 90
    df["_one"] = 1.0
    pos = before_kickoff(df, ["position"], ["xa", "key_passes", "assists", "ga", "_90s", "_one"])
    pos_xa = (pos["xa"] / pos["_90s"].replace(0, np.nan)).fillna(
        df["position"].map(DEFAULT_POS_XA).fillna(0.1)
    )
    pos_kp = (pos["key_passes"] / pos["_90s"].replace(0, np.nan)).fillna(
        df["position"].map(DEFAULT_POS_KP).fillna(1.0)
    )
    s_xa, s_a = ewsum_prior(df["xa"], pid), ewsum_prior(df["assists"], pid)
    s_kp, s_90 = ewsum_prior(df["key_passes"], pid), ewsum_prior(df["_90s"], pid)
    df["log_xa_rate"] = np.log(((s_xa + pos_xa * PRIOR_90S) / (s_90 + PRIOR_90S)).clip(lower=0.002))
    df["log_kp_rate"] = np.log(((s_kp + pos_kp * PRIOR_90S) / (s_90 + PRIOR_90S)).clip(lower=0.01))
    df["log_assist_conv"] = np.log(((s_a + PRIOR_XA) / (s_xa + PRIOR_XA)).clip(0.5, 2))
    g = df.groupby(["player_id", "season"])
    n = g.cumcount()
    apps = pos["_one"].replace(0, np.nan)
    for col in ("assists", "ga"):
        prior = g[col].cumsum() - df[col]
        pos_avg = (pos[col] / apps).fillna(0.1)
        df[f"season_avg_{col}"] = np.where(n > 0, prior / n.replace(0, 1), pos_avg)
    return df.drop(columns=["_90s", "_one"])


class CountCandidate:
    """B's mixture on any count target, for lab.harness: [P(0), P(>= 1)] with the lineup
    known; NaN for players with fewer than MIN_PREV_APPS earlier appearances."""

    def __init__(self, target: str, factors: list[str]):
        self.target, self.factors, self.train = target, list(factors), None

    def fit(self, train):
        self.train = train

    def predict(self, test):
        lo = test["kickoff"].min()
        tr = self.train[self.train["kickoff"] >= lo - pd.Timedelta(days=LOOKBACK_DAYS)]
        p = np.full(len(test), np.nan)
        ok = (test["prev_apps"] >= MIN_PREV_APPS).to_numpy()
        if len(tr) < 200 or tr[self.target].sum() < 10 or not ok.any():
            return np.column_stack([1 - p, p])
        t = test[ok]
        m = NBRegression(self.factors, 2.0, options=FIT_OPTIONS)
        m.fit(tr, tr[self.target].to_numpy(), tr["minutes"].to_numpy() / 90)
        rate = m.rate(t)
        ps = t["started"].astype(float).to_numpy()
        st = rate * t["start_minutes"].to_numpy() / 90
        sb = rate * t["sub_minutes"].to_numpy() / 90
        p[ok] = ps * (1 - p_zero(st, m.alpha_)) + (1 - ps) * (1 - p_zero(sb, m.alpha_))
        return np.column_stack([1 - p, p])


def predict(feats: pd.DataFrame, target: str, start, end, holdout=None) -> pd.Series:
    pr = harness.walk_forward(
        feats,
        lambda **kw: CountCandidate(**kw),
        {"target": target, "factors": TARGETS[target]},
        start,
        end,
        holdout=holdout,
        time_col="kickoff",
        refit=REFIT,
        min_train=1,
    )
    return pr["p_1"] if not pr.empty else pd.Series(dtype=float)


def scored_rows(feats: pd.DataFrame, start, end, holdout=None) -> pd.DataFrame:
    """Starters with both models' chances, the outcomes and both baselines."""
    pa = predict(feats, "assists", start, end, holdout)
    pg = predict(feats, "ga", start, end, holdout)
    idx = pa.dropna().index.intersection(pg.dropna().index)
    r = feats.loc[idx]
    out = pd.DataFrame(
        {
            "season": r["season"].astype(str).to_numpy(),
            "match_id": r["match_id"].astype(str).to_numpy(),
            "started": r["started"].astype(bool).to_numpy(),
            "assisted": (r["assists"] > 0).astype(int).to_numpy(),
            "ga_hit": (r["ga"] > 0).astype(int).to_numpy(),
            "p_assist": pa.loc[idx].to_numpy(),
            "p_ga": pg.loc[idx].to_numpy(),
            "bench_assist": 1 - np.exp(-r["season_avg_assists"].clip(lower=1e-4).to_numpy()),
            "bench_ga": 1 - np.exp(-r["season_avg_ga"].clip(lower=1e-4).to_numpy()),
        }
    )
    return out[out["started"]].reset_index(drop=True)


def _ll(p, y) -> np.ndarray:
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def compare(rows: pd.DataFrame, model: str, bench: str, outcome: str) -> dict:
    """Log loss of model and baseline, the gain (baseline minus model) with a 95% range
    resampling matches, and calibration in three bands of the model's chance."""
    if rows.empty:
        return {"rows": 0}
    y = rows[outcome].to_numpy()
    lm, lb = _ll(rows[model], y), _ll(rows[bench], y)
    d = lb - lm
    rng = boot_range(d, rows["match_id"].to_numpy())
    bands = []
    for lo, hi in ((0, 0.1), (0.1, 0.25), (0.25, 1.0)):
        x = rows[(rows[model] >= lo) & (rows[model] < hi)]
        if len(x):
            bands.append(
                {
                    "band": f"{lo:.0%}-{hi:.0%}",
                    "n": len(x),
                    "model": round(float(x[model].mean()), 4),
                    "won": round(float(x[outcome].mean()), 4),
                }
            )
    return {
        "rows": len(rows),
        "matches": int(rows["match_id"].nunique()),
        "rate": round(float(y.mean()), 4),
        "ll_model": round(float(lm.mean()), 5),
        "ll_bench": round(float(lb.mean()), 5),
        "gain": round(float(d.mean()), 5),
        "range": [round(v, 5) for v in rng] if rng else None,
        "beats": bool(rng and rng[0] > 0),
        "calibration": bands,
    }


def report(rows: pd.DataFrame) -> dict:
    """Development (2023/24-2024/25 pooled, then each) and 2025/26 apart (seen)."""
    out = {}
    for name, seasons in (("development", DEV_SEASONS), *((s, (s,)) for s in DEV_SEASONS)):
        r = rows[rows["season"].isin(seasons)]
        out[name] = {
            "assists": compare(r, "p_assist", "bench_assist", "assisted"),
            "score_or_assist": compare(r, "p_ga", "bench_ga", "ga_hit"),
        }
    r = rows[rows["season"] == SEEN_SEASON]
    out[f"{SEEN_SEASON}_seen"] = {
        "assists": compare(r, "p_assist", "bench_assist", "assisted"),
        "score_or_assist": compare(r, "p_ga", "bench_ga", "ga_hit"),
        "note": "2025/26 has been seen: described, not a test",
    }
    return out


def base_rates(apps: pd.DataFrame, seasons=DEV_SEASONS) -> dict:
    """Per match: distinct players who assist, who score or assist, and who are booked;
    and per starter the shares (for the probe's Yes-only prices)."""
    a = apps[apps["season"].astype(str).isin(seasons)]
    if a.empty:
        return {}
    by = a.groupby("match_id")
    starters = a[a["started"]]
    card = (
        (a.get("yellow", 0) + a.get("red", 0)) > 0 if "yellow" in a else pd.Series(False, a.index)
    )
    return {
        "matches": int(by.ngroups),
        "assisters_per_match": round(float(by["assists"].apply(lambda s: (s > 0).sum()).mean()), 3),
        "scorers_or_assisters_per_match": round(
            float(
                a.assign(ga=a["goals"] + a["assists"])
                .groupby("match_id")["ga"]
                .apply(lambda s: (s > 0).sum())
                .mean()
            ),
            3,
        ),
        "booked_per_match": round(float(card.groupby(a["match_id"]).sum().mean()), 3),
        "starter_assist_rate": round(float((starters["assists"] > 0).mean()), 4),
        "starter_ga_rate": round(float(((starters["goals"] + starters["assists"]) > 0).mean()), 4),
    }


def holdout() -> Holdout:
    """2026/27's forward window stays locked: nothing from FORWARD_START is scored."""
    return Holdout(FORWARD_START)
