"""Walk-forward backtest: refit periodically, predict only future matches, compare to market.

Two questions matter, in this order:
1. Calibration: is the model's log loss close to (or better than) the de-vigged closing line?
2. Value: betting at the available (opening) price where the model sees an edge,
   do we get positive ROI and, more reliably, positive closing line value (CLV)?
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.markets import match_odds
from soccer_stats.models import DixonColes
from soccer_stats.odds import devig_shin

OUTCOMES = ["home", "draw", "away"]


def walk_forward(
    matches: pd.DataFrame,
    start: str | pd.Timestamp,
    refit_every: str = "7D",
    lookback_days: int = 730,
    min_team_matches: int = 6,
    model_factory=DixonColes,
) -> pd.DataFrame:
    """Predict 1X2 probabilities for every match on/after `start` using only prior data.

    Matches involving a team with fewer than `min_team_matches` games in the
    training window are skipped (ratings for them are mostly guesswork).
    """
    matches = matches.sort_values("date").reset_index(drop=True)
    start = pd.Timestamp(start)
    windows = pd.date_range(start, matches["date"].max() + pd.Timedelta(days=1), freq=refit_every)

    out = []
    for lo, hi in zip(windows[:-1], windows[1:], strict=True):
        test = matches[(matches["date"] >= lo) & (matches["date"] < hi)]
        if test.empty:
            continue
        train = matches[
            (matches["date"] < lo) & (matches["date"] >= lo - pd.Timedelta(days=lookback_days))
        ]
        counts = pd.concat([train["home"], train["away"]]).value_counts()
        model = model_factory().fit(train, as_of=lo)

        for row in test.itertuples(index=False):
            if min(counts.get(row.home, 0), counts.get(row.away, 0)) < min_team_matches:
                continue
            p = match_odds(model.score_matrix(row.home, row.away))
            out.append({**row._asdict(), "p_home": p[0], "p_draw": p[1], "p_away": p[2]})
    return pd.DataFrame(out)


def add_market_probs(preds: pd.DataFrame, prefix: str = "close") -> pd.DataFrame:
    """Add de-vigged market probabilities (mkt_home/draw/away) from `{prefix}_*` odds."""
    df = preds.copy()
    cols = [f"{prefix}_{o}" for o in OUTCOMES]
    probs = np.full((len(df), 3), np.nan)
    ok = df[cols].notna().all(axis=1).to_numpy()
    for i in np.flatnonzero(ok):
        probs[i] = devig_shin(df[cols].iloc[i].to_numpy(dtype=float))
    df[["mkt_home", "mkt_draw", "mkt_away"]] = probs
    return df


def _result_index(df: pd.DataFrame) -> np.ndarray:
    return np.select(
        [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]], [0, 1], 2
    )


def score(preds: pd.DataFrame) -> pd.DataFrame:
    """Log loss and Brier score for the model and (if present) the market."""
    df = preds.dropna(subset=["mkt_home"]) if "mkt_home" in preds else preds
    y = np.eye(3)[_result_index(df)]
    rows = {}
    for name, cols in [
        ("model", ["p_home", "p_draw", "p_away"]),
        ("market", ["mkt_home", "mkt_draw", "mkt_away"]),
    ]:
        if not set(cols) <= set(df.columns):
            continue
        p = df[cols].to_numpy()
        rows[name] = {
            "log_loss": float(-np.mean(np.log(np.clip((p * y).sum(1), 1e-12, None)))),
            "brier": float(np.mean(((p - y) ** 2).sum(1))),
            "n": len(df),
        }
    return pd.DataFrame(rows).T


def simulate_bets(
    preds: pd.DataFrame, min_edge: float = 0.03, price_prefix: str = "odds", max_odds: float = 6.0
) -> pd.DataFrame:
    """Flat 1-unit bets wherever model edge at `price_prefix` odds exceeds `min_edge`.

    Returns one row per bet with profit and CLV versus the de-vigged closing line.
    """
    res = _result_index(preds)
    bets = []
    for i, o in enumerate(OUTCOMES):
        price = preds[f"{price_prefix}_{o}"]
        p = preds[f"p_{o}"]
        e = p * price - 1
        mask = (e > min_edge) & (price <= max_odds) & price.notna()
        sel = preds[mask]
        won = res[mask.to_numpy()] == i
        b = pd.DataFrame(
            {
                "date": sel["date"],
                "home": sel["home"],
                "away": sel["away"],
                "pick": o,
                "price": price[mask],
                "model_p": p[mask],
                "edge": e[mask],
                "profit": np.where(won, price[mask] - 1, -1.0),
            }
        )
        if f"mkt_{o}" in preds:
            b["clv"] = price[mask] * preds.loc[mask, f"mkt_{o}"] - 1
        bets.append(b)
    return pd.concat(bets).sort_values("date").reset_index(drop=True)


def summarize_bets(bets: pd.DataFrame) -> dict[str, float]:
    if bets.empty:
        return {"bets": 0}
    out = {
        "bets": len(bets),
        "staked": float(len(bets)),
        "profit": float(bets["profit"].sum()),
        "roi": float(bets["profit"].mean()),
        "avg_odds": float(bets["price"].mean()),
        "avg_edge": float(bets["edge"].mean()),
    }
    if "clv" in bets:
        out["avg_clv"] = float(bets["clv"].mean())
        out["pct_beat_close"] = float((bets["clv"] > 0).mean())
    return out
