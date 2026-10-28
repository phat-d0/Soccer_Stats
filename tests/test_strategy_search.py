import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import strategy_search as ss
from soccer_stats.lab.harness import Holdout, HoldoutLocked


def side_table(n_matches=60, seed=1):
    """A synthetic side table: 1X2 sides for E0 and E1 matches, with a random edge."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_matches):
        lg = "E0" if i % 2 else "E1"
        date = pd.Timestamp("2021-08-07") + pd.Timedelta(days=7 * (i // 4) + (i % 3))
        y = rng.integers(0, 3)
        for j, side in enumerate(("home", "draw", "away")):
            odds = (2.2, 3.3, 3.6)[j]
            rows.append(
                {
                    "league": lg,
                    "season": "2122",
                    "date": date,
                    "home": f"H{i}",
                    "away": f"A{i}",
                    "market": "1x2",
                    "side": side,
                    "odds": odds,
                    "close_odds": odds * (1 + rng.normal(0, 0.02)),
                    "fair_early": (0.43, 0.29, 0.28)[j],
                    "fair_close": (0.43, 0.29, 0.28)[j] + rng.normal(0, 0.01),
                    "p_model": (0.43, 0.29, 0.28)[j] + rng.normal(0, 0.05),
                    "p_blend": np.nan,
                    "z": rng.normal(),
                    "z_market": rng.normal(),
                    "profit": odds - 1 if y == j else -1.0,
                    "profit_close": odds - 1 if y == j else -1.0,
                    "gap": abs(rng.normal(0.2, 0.1)),
                    "time": "15:00" if i % 4 else "20:00",
                }
            )
    return ss.add_features(pd.DataFrame(rows))


def test_features_mark_favourites_slots_days_and_one_best_side_per_match():
    df = side_table()
    assert (df.groupby("key")["best_edge"].sum() == 1).all()
    assert (df[df["side"] == "home"]["fav"] == "fav").all()
    assert set(df["day"]) <= {"weekend", "midweek"}
    assert set(df["slot"].dropna()) <= {"lone", "shared"}
    np.testing.assert_allclose(df["clv"], df["odds"] * df["fair_close"] - 1)


def test_arms_count_every_combination_of_two_different_filters():
    df = side_table()
    levels = ss.filter_levels(df)
    every = ss.arms(df, levels)
    one = [a for a in every if a["market"] == "1x2" and a["base"] == "every side"]
    opts = [
        (n, lv) for n in ss.FILTERS for lv, _ in levels.get(n, [])
        if ss._applies("every side", "1x2", n, lv)
    ]  # fmt: skip
    pairs = sum(1 for i, a in enumerate(opts) for b in opts[i + 1 :] if a[0] != b[0])
    assert len(one) == 1 + len(opts) + pairs
    # Filters of the same kind never combine, and bands only follow their own base.
    for a in every:
        kinds = [f.split("=")[0] for f in a["filters"]]
        assert len(kinds) == len(set(kinds))
        if a["base"] == "every side":
            assert "band" not in kinds and "driver" not in kinds


def test_score_means_ranges_and_the_reality_check():
    df = side_table(200)
    rows = np.flatnonzero((df["side"] == "home").to_numpy())
    arm = {"family": "early", "market": "1x2", "base": "x", "filters": [], "rows": rows}
    w = ss.weights(int(pd.factorize(df["key"])[0].max()) + 1, n_boot=300)
    maxima = ss.score([arm], df, w, {"clv": "clv", "roi": "profit"}, universe_min=50)
    assert arm["n_roi"] == len(rows)
    assert arm["roi"] == pytest.approx(df["profit"].to_numpy()[rows].mean(), rel=1e-5)
    lo, hi = arm["roi_ci95"]
    assert lo < arm["roi"] < hi
    # One arm: its recentred maximum is centred on 0, so its own mean's p is about the
    # share of draws above it; a value far above every draw has p 0.
    assert ss.adjusted_p(10.0, maxima["roi"]["final"]) == 0.0
    assert 0.3 < ss.adjusted_p(0.0, maxima["roi"]["final"]) < 0.7
    assert ss.null_share(maxima["roi"])["mean"] == pytest.approx(0.5, abs=0.1)


def test_the_bootstrap_draws_are_reproducible():
    a, b = ss.weights(50, 20), ss.weights(50, 20)
    np.testing.assert_array_equal(a, b)
    assert (a.sum(0) == 50).all()


def test_search_runs_end_to_end_and_finds_nothing_in_noise():
    df = side_table(240)
    res, every = ss.search(df, n_boot=200)
    assert res["arms_tried"]["total"] == len(every)
    assert res["arms_tried"]["move"] > 0
    assert res["finalists"] == [] and res["verdict"].startswith("no strategy found")
    assert set(res["by_market"]) == set(ss.MARKETS)
    assert res["by_market"]["1x2"]["n_roi"] == len(df)


def test_the_holdout_refuses_rows_from_2024_07_01():
    h = Holdout(ss.HOLDOUT_START)
    h.check(pd.Series(pd.to_datetime(["2024-06-30"])))
    with pytest.raises(HoldoutLocked):
        h.check(pd.Series(pd.to_datetime(["2024-07-01"])))


def test_sizing_stops_a_season_after_a_20_unit_drawdown():
    bets = pd.DataFrame(
        {
            "date": pd.date_range("2021-08-01", periods=30),
            "season": "2122",
            "profit": [-1.0] * 25 + [5.0] * 5,
            "odds": 2.0,
            "p_blend": np.nan,
        }
    )
    out = ss.sizing(bets)
    assert out["flat"]["profit"] == 0.0
    assert out["stop_20"] == {"bets_staked": 20, "profit": -20.0}
    assert "note" in out["kelly_quarter"]


def test_the_app_file_has_the_research_shape():
    df = side_table(240)
    res, _ = ss.search(df, n_boot=100)
    res["leagues"] = {"E0": {"seasons": ["2122"]}, "E1": {"seasons": ["2122"]}}
    app = ss.app_file(res, {"search": "1"})
    for key in ("title", "question", "rule", "verdict", "verdict_text", "cost", "runs",
                "preregistered", "designs", "bets", "notes"):  # fmt: skip
        assert key in app
    d = app["designs"]["search"]
    assert d["seasons"] == ["2021/22"] and d["arms"]["total"] == res["arms_tried"]["total"]
    if app["bets"]:
        assert [b["date"] for b in app["bets"]] == sorted(b["date"] for b in app["bets"])
