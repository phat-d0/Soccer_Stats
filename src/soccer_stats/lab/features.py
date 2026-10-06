"""Pre-match features for the bake-off, from earlier matches only.

Every value for a match comes from matches played before its date (same-day matches
never see each other). Parameters are fixed in docs/lab.md before any run, not tuned.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ELO_START = 1500.0
ELO_NEW = 1420.0  # a team's first match (promoted sides start below the league mean)
ELO_K = 20.0
ELO_HOME = 60.0
ELO_CARRY = 0.8  # between seasons, ratings keep 80% of their distance from 1500
WINDOWS = (6, 20)
MIN_FORM = 3
REST_CAP = 14

FEATURES = [
    "elo_home",
    "elo_away",
    "elo_diff",
    *[f"{s}_{c}{w}" for w in WINDOWS for s in ("home", "away") for c in ("xgf", "xga", "gf", "ga")],
    "home_rest",
    "away_rest",
]


def elo(matches: pd.DataFrame) -> pd.DataFrame:
    """Elo ratings going into each match (goal-difference multiplier, home edge)."""
    m = matches.sort_values("date", kind="stable")
    rating: dict[str, float] = {}
    season_of: dict[str, object] = {}
    out_h, out_a = np.empty(len(m)), np.empty(len(m))
    pending: list[tuple[str, float]] = []
    day = None
    for i, r in enumerate(m.itertuples(index=False)):
        if r.date != day:  # apply the previous day's updates only once the day is over
            for team, delta in pending:
                rating[team] += delta
            pending, day = [], r.date
        for team in (r.home, r.away):
            if team not in rating:
                rating[team] = ELO_NEW
            elif season_of.get(team) != r.season:
                rating[team] = ELO_START + ELO_CARRY * (rating[team] - ELO_START)
            season_of[team] = r.season
        rh, ra = rating[r.home], rating[r.away]
        out_h[i], out_a[i] = rh, ra
        exp_h = 1 / (1 + 10 ** ((ra - rh - ELO_HOME) / 400))
        gd = r.home_goals - r.away_goals
        score = 1.0 if gd > 0 else 0.5 if gd == 0 else 0.0
        mult = np.log(abs(gd) + 1) + 1 if gd else 1.0
        d = ELO_K * mult * (score - exp_h)
        pending += [(r.home, d), (r.away, -d)]
    return pd.DataFrame({"elo_home": out_h, "elo_away": out_a}, index=m.index).loc[matches.index]


def form(matches: pd.DataFrame) -> pd.DataFrame:
    """Rolling per-game xG for/against and goals for/against, and rest days."""
    m = matches.sort_values("date", kind="stable")
    sides = []
    for side, other in (("home", "away"), ("away", "home")):
        sides.append(
            pd.DataFrame(
                {
                    "row": m.index,
                    "date": m["date"],
                    "team": m[side],
                    "side": side,
                    "xgf": m[f"{side}_xg"],
                    "xga": m[f"{other}_xg"],
                    "gf": m[f"{side}_goals"].astype(float),
                    "ga": m[f"{other}_goals"].astype(float),
                }
            )
        )
    long = pd.concat(sides).sort_values(["date", "row"], kind="stable")
    g = long.groupby("team", sort=False)
    # Earlier days only: a team plays once a day, so shift(1) excludes the match itself.
    for c in ("xgf", "xga", "gf", "ga"):
        for w in WINDOWS:
            long[f"{c}{w}"] = g[c].transform(
                lambda s, w=w: s.shift(1).rolling(w, min_periods=MIN_FORM).mean()
            )
    long["rest"] = (long["date"] - g["date"].shift(1)).dt.days.clip(upper=REST_CAP)
    long["rest"] = long["rest"].fillna(REST_CAP)
    out = pd.DataFrame(index=matches.index)
    for side in ("home", "away"):
        s = long[long["side"] == side].set_index("row")
        for w in WINDOWS:
            for c in ("xgf", "xga", "gf", "ga"):
                out[f"{side}_{c}{w}"] = s[f"{c}{w}"]
        out[f"{side}_rest"] = s["rest"]
    return out


def build(matches: pd.DataFrame) -> pd.DataFrame:
    """`matches` (date, season, home, away, goals, xG) plus FEATURES and `y` (1X2)."""
    m = matches.copy()
    m = pd.concat([m, elo(m), form(m)], axis=1)
    m["elo_diff"] = m["elo_home"] - m["elo_away"]
    hg, ag = m["home_goals"].to_numpy(), m["away_goals"].to_numpy()
    m["y"] = np.select([hg > ag, hg == ag], [0, 1], 2)
    return m
