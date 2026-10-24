"""Favourites coming back (lab/comebacks.py) on synthetic matches (no network)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import comebacks as cb
from soccer_stats.team_totals import _outcomes, _score_probs


def test_in_play_basics():
    assert cb.in_play(1.5, 1.0, 1, 0.0) == (0.0, 0.0)  # no time left: a trailing side loses
    win, draw = cb.in_play(1.2, 1.2, 0, 1.0)  # level and equal: win = loss
    assert win == pytest.approx((1 - draw) / 2)
    w1, _ = cb.in_play(2.0, 1.0, 1, 0.5)
    w2, _ = cb.in_play(2.0, 1.0, 2, 0.5)
    assert w1 > w2 > 0  # a bigger deficit is harder to overturn


def test_own_goal_convention_and_score_check():
    fav = pd.DataFrame(
        {
            "league": "E0",
            "date": pd.Timestamp("2020-01-01"),
            "home": ["A", "C"],
            "away": ["B", "D"],
            "fthg": [1, 2],
            "ftag": [1, 0],
            "fav_home": True,
        }
    )
    # Own goals listed under the side credited with them: A's own goal counts for B.
    us = pd.DataFrame(
        {
            "league": "E0",
            "date": pd.Timestamp("2020-01-02"),
            "home": ["A", "C"],
            "away": ["B", "D"],
            "events": [[(10, "a", True), (50, "h", False)], [(5, "h", False), (80, "h", False)]],
        }
    )
    m, counts = cb.attach_goals(fav, us)
    assert counts["own_goal_listed_under_scorers_side"] and counts["score_rebuilt"] == 2
    assert m.loc[0, "goals"][0] == (10, "a")
    # A rebuilt score that disagrees with football-data is dropped and counted.
    fav.loc[1, "fthg"] = 3
    m, counts = cb.attach_goals(fav, us)
    assert counts["score_mismatch_dropped"] == 1 and len(m) == 1


def _league(n=2500, seed=0):
    """Matches whose goals really follow the fair model: Poisson means, uniform minutes."""
    rng = np.random.default_rng(seed)
    fd, us = [], []
    for i in range(n):
        lh, la = rng.uniform(0.6, 2.6), rng.uniform(0.4, 1.6)
        o = _outcomes(_score_probs(lh, la))
        gh, ga = rng.poisson(lh), rng.poisson(la)
        ev = sorted(
            [(int(rng.uniform(0, 90)), "h", False) for _ in range(gh)]
            + [(int(rng.uniform(0, 90)), "a", False) for _ in range(ga)]
        )
        date = pd.Timestamp("2019-08-01") + pd.Timedelta(days=i % 300)
        fd.append(
            {
                "league": "E0",
                "season_start": 2019,
                "date": date,
                "home": f"H{i}",
                "away": f"A{i}",
                "fthg": gh,
                "ftag": ga,
                "hthg": sum(m < 45 for m, s, _ in ev if s == "h"),
                "htag": sum(m < 45 for m, s, _ in ev if s == "a"),
                "ph": 0.97 / o["home"],
                "pd": 0.97 / o["draw"],
                "pa": 0.97 / o["away"],
                "pov": 0.97 / o["over25"],
                "pun": 0.97 / o["under25"],
            }
        )
        us.append({"league": "E0", "date": date, "home": f"H{i}", "away": f"A{i}", "events": ev})
    return pd.DataFrame(fd), pd.DataFrame(us)


def test_a_fair_world_shows_no_gap():
    fd, us = _league()
    fav = cb.favourites(fd)
    assert fav["p_fav"].min() >= 0.45 and set(fav["band"]) <= {0, 1, 2, 3}
    m, counts = cb.attach_goals(fav, us)
    assert counts["score_mismatch_dropped"] == 0
    a = cb.add_predictions(cb.event_a(m))
    b = cb.add_predictions(cb.event_b(fav))
    assert len(a) > 300 and len(b) > 300
    assert (a["deficit"] == 1).all() and (b["deficit"] >= 1).all()
    res = cb.analyse(a, b, level=0.99)
    assert res["tests"] == 18 and len(res["event_a"]) == 12 and len(res["event_b"]) == 4
    # The model is the truth here, so the pooled win residuals sit near 0.
    for k in ("event_a_pooled", "event_b_pooled"):
        lo, hi = res[k]["win_residual_range"]
        assert lo < 0 < hi and abs(res[k]["win_residual"]) < 0.03
    assert res["verdict"] in {"no gap shown", "gap found", "below the model"}
    assert "VERDICT" in cb.report(res, counts)


def test_holdout_season_is_dropped(monkeypatch, tmp_path):
    raw = pd.DataFrame(
        {
            "Date": ["10/05/2025", "10/08/2025"],
            "HomeTeam": ["A", "C"],
            "AwayTeam": ["B", "D"],
            "FTHG": [1, 0],
            "FTAG": [0, 0],
            "HTHG": [0, 0],
            "HTAG": [0, 0],
        }
    )
    p = tmp_path / "x.csv"
    raw.to_csv(p, index=False)
    monkeypatch.setattr(cb, "download", lambda lg, y, raw_dir=None: p)
    df = cb.load_fd("E0", years=[2024])
    assert len(df) == 1 and df["date"].max() < cb.HOLDOUT_START
