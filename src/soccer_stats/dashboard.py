"""Data shaping for the Streamlit app, kept free of Streamlit so it can be tested."""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import backtest
from soccer_stats.markets import btts, match_odds, over_under
from soccer_stats.models import DixonColes
from soccer_stats.odds import devig_shin

LOW_DATA_MATCHES = 6


def fit_model(
    matches: pd.DataFrame, as_of: pd.Timestamp | None = None, xg_weight: float = 0.0
) -> DixonColes:
    as_of = as_of or matches["date"].max() + pd.Timedelta(days=1)
    return DixonColes(xg_weight=xg_weight).fit(matches[matches["date"] < as_of], as_of=as_of)


def match_counts(matches: pd.DataFrame, current_teams: list[str]) -> pd.Series:
    counts = pd.concat([matches["home"], matches["away"]]).value_counts()
    return counts.reindex(current_teams, fill_value=0)


def predict_match(model: DixonColes, home: str, away: str) -> dict:
    m = model.score_matrix(home, away)
    xg_h, xg_a = model.expected_goals(home, away)
    p = match_odds(m)
    return {
        "xg_home": xg_h,
        "xg_away": xg_a,
        "p_home": p[0],
        "p_draw": p[1],
        "p_away": p[2],
        "p_over25": over_under(m, 2.5)[0],
        "p_btts": btts(m)[0],
        "matrix": m,
    }


_PICKS = [
    ("home", "p_home", "odds_home"),
    ("draw", "p_draw", "odds_draw"),
    ("away", "p_away", "odds_away"),
    ("over 2.5", "p_over25", "odds_over25"),
    ("under 2.5", "p_under25", "odds_under25"),
]


def predict_fixtures(model: DixonColes, fixtures: pd.DataFrame, counts: pd.Series) -> pd.DataFrame:
    """Model probabilities, edges and the best value pick for each upcoming fixture."""
    rows = []
    for fx in fixtures.itertuples(index=False):
        pred = predict_match(model, fx.home, fx.away)
        pred.pop("matrix")
        row = {**fx._asdict(), **pred, "p_under25": 1 - pred["p_over25"]}
        row["low_data"] = min(counts.get(fx.home, 0), counts.get(fx.away, 0)) < LOW_DATA_MATCHES
        best = ("", 0.0, np.nan)  # no pick
        for label, p_col, o_col in _PICKS:
            price = row.get(o_col)
            e = row[p_col] * price - 1 if pd.notna(price) else np.nan
            row[f"edge_{label}"] = e
            if pd.notna(e) and e > best[1]:
                best = (label, e, price)
        row["pick"], row["pick_edge"], row["pick_odds"] = best
        if all(pd.notna(row[c]) for c in ("odds_home", "odds_draw", "odds_away")):
            mk = devig_shin(np.array([row["odds_home"], row["odds_draw"], row["odds_away"]]))
            row["mkt_home"], row["mkt_draw"], row["mkt_away"] = mk
        rows.append(row)
    return pd.DataFrame(rows)


def season_xg(matches: pd.DataFrame, since: str | pd.Timestamp) -> pd.DataFrame:
    """Per-team average xG for and against per game since `since` (empty if no xG)."""
    df = matches[matches["date"] >= pd.Timestamp(since)]
    if "home_xg" not in df or df["home_xg"].isna().all():
        return pd.DataFrame(columns=["xg_for", "xg_against"])
    df = df.dropna(subset=["home_xg", "away_xg"])
    long = pd.concat(
        [
            pd.DataFrame(
                {"team": df["home"], "xg_for": df["home_xg"], "xg_against": df["away_xg"]}
            ),
            pd.DataFrame(
                {"team": df["away"], "xg_for": df["away_xg"], "xg_against": df["home_xg"]}
            ),
        ]
    )
    return long.groupby("team")[["xg_for", "xg_against"]].mean()


def team_ratings(model: DixonColes, counts: pd.Series, teams: list[str]) -> pd.DataFrame:
    """Goals each team would score / concede per game against an average opponent, neutral venue."""
    c = model.params["intercept"] + model.params["home_adv"] / 2
    df = pd.DataFrame(
        {
            "team": teams,
            "goals_for": [np.exp(c + model.attack.get(t, 0.0)) for t in teams],
            "goals_against": [np.exp(c + model.defence.get(t, 0.0)) for t in teams],
            "matches": [int(counts.get(t, 0)) for t in teams],
        }
    )
    df["goal_diff"] = df["goals_for"] - df["goals_against"]
    df = df.sort_values("goal_diff", ascending=False).reset_index(drop=True)
    df.index += 1
    return df


def replay(matches: pd.DataFrame, start: pd.Timestamp, xg_weight: float = 0.0) -> pd.DataFrame:
    """Walk-forward predictions from `start` with de-vigged closing probabilities (slow)."""
    preds = backtest.walk_forward(
        matches, start=start, model_factory=lambda: DixonColes(xg_weight=xg_weight)
    )
    return backtest.add_market_probs(preds) if not preds.empty else preds


def track_record(preds: pd.DataFrame, min_edge: float) -> dict:
    """Scores, bets and a cumulative profit series for replayed predictions (fast)."""
    if preds.empty:
        return {"scores": pd.DataFrame(), "bets": pd.DataFrame(), "summary": {}}
    bets = backtest.simulate_bets(preds, min_edge=min_edge)
    bets["cum_profit"] = bets["profit"].cumsum()
    return {
        "scores": backtest.score(preds),
        "bets": bets,
        "summary": backtest.summarize_bets(bets),
    }


def top_scorelines(matrix: np.ndarray, n: int = 5) -> list[tuple[str, float]]:
    flat = np.argsort(matrix, axis=None)[::-1][:n]
    return [
        (f"{i}-{j}", float(matrix[i, j]))
        for i, j in zip(*np.unravel_index(flat, matrix.shape), strict=True)
    ]
