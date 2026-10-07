"""The learned minimum edge (lab.thresholds) on synthetic bets."""

import numpy as np
import pandas as pd

from soccer_stats.lab import thresholds as th


def _bets(n=6000, real_from=None, margin=0.05, seasons=("2324", "2425", "2526"), seed=0):
    """Bets with claimed edges 0-30%. The bookmaker's chance is fair minus a margin.

    Below `real_from` the model's extra chance is noise (bets win at the bookmaker's
    fair rate, so they lose the margin); from `real_from` up the claimed edge is real.
    real_from=None: no edge anywhere.
    """
    rng = np.random.default_rng(seed)
    fair = rng.uniform(0.25, 0.6, n)
    odds = 1 / (fair * (1 + margin))
    edge = rng.uniform(0.001, 0.30, n)
    p = (1 + edge) / odds
    real = np.zeros(n, bool) if real_from is None else edge >= real_from
    true = np.where(real, np.minimum(p, 0.97), fair)
    won = (rng.random(n) < true).astype(float)
    season = np.array(seasons)[np.arange(n) * len(seasons) // n]
    t = pd.Timestamp("2023-08-01", tz="UTC") + pd.to_timedelta(np.arange(n) // 4, "D")
    return pd.DataFrame(
        {
            "edge": edge,
            "p": p,
            "odds": odds,
            "won": won,
            "group": np.arange(n) // 2,  # two bets a match
            "season": season,
            "time": t,
        }
    )


def test_a_planted_edge_gives_a_threshold_near_where_it_starts():
    res = th.edge_threshold(_bets(n=20000, real_from=0.10), "bets")
    assert res["min_edge"] is not None and 0.07 <= res["min_edge"] <= 0.16
    assert res["check"]["roi"] > 0
    assert res["seasons"] == {"development": ["2324", "2425"], "check": ["2526"]}
    assert "paid off" in res["note"]
    b = {r["edge_lo"]: r for r in res["by_bucket"]}
    assert b[0.0]["realized"] < b[0.0]["model"]  # the small claimed edges were noise
    assert b[0.2]["roi"] > 0 and b[0.2]["realized_lo"] <= b[0.2]["realized"]
    assert {
        "edge_lo",
        "edge_hi",
        "n",
        "implied",
        "model",
        "realized",
        "realized_lo",
        "realized_hi",
    } <= set(res["by_bucket"][0])


def test_no_edge_gives_null_with_a_plain_reason():
    res = th.edge_threshold(_bets(real_from=None), "match bets")
    assert res["min_edge"] is None
    assert res["note"].startswith("No minimum edge works")
    assert "match bets" in res["note"]
    assert res["confidence"] == th.CONFIDENCE and res["n_bets"] > 5000


def test_an_edge_that_vanishes_later_is_not_published():
    early = _bets(n=14000, real_from=0.10, seasons=("2324", "2425"), seed=1)
    late = _bets(real_from=None, n=3000, seasons=("2526",), seed=2)
    late["time"] = late["time"] + pd.Timedelta(days=800)
    late["group"] = late["group"] + 10**6
    res = th.edge_threshold(pd.concat([early, late], ignore_index=True))
    assert res["min_edge"] is None and "did not hold up" in res["note"]
    assert res["check"]["roi"] <= 0


def test_one_season_splits_in_time_and_voids_are_left_out():
    b = _bets(n=20000, real_from=0.10, seasons=("2526",))
    b.loc[::10, "won"] = np.nan  # void
    res = th.edge_threshold(b)
    assert res["seasons"]["development"] == ["2526 first half"]
    assert res["n_bets"] == int(b["won"].notna().sum())


def test_too_few_bets_and_from_trades():
    res = th.edge_threshold(_bets(n=40))
    assert res["min_edge"] is None and res["note"].startswith("Too few")
    trades = pd.DataFrame(
        {
            "edge": [0.1, 0.2, 0.05],
            "model_p": [0.5, 0.4, 0.3],
            "odds": [2.2, 3.0, 3.5],
            "status": ["won", "lost", "void"],
            "season": ["2526"] * 3,
            "home": ["A", "B", "C"],
            "away": ["X", "Y", "Z"],
            "kickoff": pd.to_datetime(["2025-08-16", "2025-08-17", "2025-08-18"], utc=True),
        }
    )
    b = th.from_trades(trades)
    assert list(b["won"].fillna(-1)) == [1, 0, -1]
    assert b["group"].nunique() == 3
