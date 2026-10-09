"""Lineup surprise (edge/lineups.py) on synthetic rosters and prices (no network)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats.edge import lineups


def _apps(n_matches=8, season="2324", drop=None, start="2023-08-12", team="A", opp="B"):
    """Team A: 11 starters p0..p10 every match, p9/p10 carry the xG (0.6 / 0.3), the
    rest 0.01 each. `drop` maps match index -> players who don't start (a sub p11
    starts instead)."""
    drop = drop or {}
    rows = []
    for i in range(n_matches):
        mid = f"{season}-{i}"
        ko = pd.Timestamp(start, tz="UTC") + pd.Timedelta(days=7 * i)
        out = set(drop.get(i, ()))
        xi = [f"p{k}" for k in range(11) if f"p{k}" not in out]
        xi += [f"p{11 + j}" for j in range(11 - len(xi))]
        for p in xi:
            xg = {"p9": 0.6, "p10": 0.3}.get(p, 0.01)
            rows.append((mid, season, ko, team, i % 2 == 0, p, True, xg))
        for k in range(11):  # the opponent, nothing missing
            rows.append((mid, season, ko, opp, i % 2 == 1, f"o{k}", True, 0.1))
    return pd.DataFrame(
        rows,
        columns=["match_id", "season", "kickoff", "team", "home", "player_id", "started", "xg"],
    )


def _a(s):
    return s[s["team"] == "A"].set_index("match_id")


def test_surprise_weights_missing_regulars_by_their_earlier_xg_share():
    apps = _apps(drop={5: ["p9"]})
    s = _a(lineups.team_surprise(apps))
    # The first three matches have fewer than 3 earlier ones: no value.
    assert s.loc[["2324-0", "2324-1", "2324-2"], "surprise"].isna().all()
    assert s.loc["2324-3", "surprise"] == 0 and s.loc["2324-3", "regulars"] == 11
    team_xg = 0.6 + 0.3 + 9 * 0.01
    assert s.loc["2324-5", "surprise"] == pytest.approx(0.6 / team_xg)
    assert s.loc["2324-5", "missing"] == 1
    # The next match: p9 started 4 of the last 5 (needs 4), so still a regular, back now.
    assert s.loc["2324-6", "surprise"] == 0


def test_surprise_uses_earlier_matches_only_and_resets_each_season():
    base = lineups.team_surprise(_apps(drop={5: ["p9"]}))
    later = _apps(drop={5: ["p9"], 7: ["p9", "p10"]})
    later.loc[later["match_id"] == "2324-7", "xg"] = 5.0  # the future changes
    changed = lineups.team_surprise(later)
    a, b = _a(base), _a(changed)
    early = [f"2324-{i}" for i in range(7)]
    pd.testing.assert_series_equal(a.loc[early, "surprise"], b.loc[early, "surprise"])
    # A new season starts from nothing: its first three matches have no value.
    two = pd.concat([_apps(), _apps(season="2425", start="2024-08-17")], ignore_index=True)
    s = _a(lineups.team_surprise(two))
    assert s.loc[["2425-0", "2425-1", "2425-2"], "surprise"].isna().all()
    assert s.loc["2425-3", "surprise"] == 0


def test_signal_is_away_minus_home():
    apps = _apps(drop={4: ["p9", "p10"], 5: ["p9", "p10"]})
    sig = lineups.match_signal(lineups.team_surprise(apps), apps).set_index("match_id")
    # Match 4: A at home (even index) is depleted -> x < 0. Match 5: A away -> x > 0.
    assert sig.loc["2324-4", "home"] == "A" and sig.loc["2324-4", "x"] < 0
    assert sig.loc["2324-5", "away"] == "A" and sig.loc["2324-5", "x"] > 0
    # Match 4 is in match 5's window, with the stars out, so their share is smaller.
    assert 0 < sig.loc["2324-5", "x"] < -sig.loc["2324-4", "x"]


def test_attach_pairs_on_teams_and_a_nearby_date():
    sig = pd.DataFrame(
        {
            "match_id": ["1", "2"],
            "home": ["A", "C"],
            "away": ["B", "D"],
            "kickoff": pd.to_datetime(["2023-08-12 19:00", "2023-08-20 12:00"], utc=True),
            "x": [0.3, -0.1],
            "home_surprise": [0.0, 0.1],
            "away_surprise": [0.3, 0.0],
            "home_missing": [0, 1],
            "away_missing": [1, 0],
        }
    )
    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2023-08-13", "2023-08-30", "2023-08-12"]),
            "home": ["A", "C", "E"],
            "away": ["B", "D", "F"],
        }
    )
    out = lineups.attach(prices, sig)
    assert out["x"].iloc[0] == 0.3 and np.isnan(out["x"].iloc[1]) and np.isnan(out["x"].iloc[2])


def test_walk_forward_blend_uses_earlier_rows_only():
    rng = np.random.default_rng(1)
    n = 900
    t = pd.date_range("2018-08-01", periods=n, freq="D")
    m = rng.dirichlet([4, 3, 3], size=n)
    x = rng.normal(0, 0.2, n)
    y = rng.integers(0, 3, n)
    p, _ = lineups.walk_forward(m, x, y, t)
    y2 = y.copy()
    y2[600:] = (y2[600:] + 1) % 3  # change only the later results
    p2, _ = lineups.walk_forward(m, x, y2, t)
    first_fit = np.flatnonzero(np.isfinite(p).all(1))[0]
    assert first_fit >= lineups.MIN_ROWS
    # Predictions made before day 600's block opens can't see the change.
    cut = np.flatnonzero(t < t[600] - pd.Timedelta(lineups.REFIT))[-1]
    np.testing.assert_allclose(p[:cut], p2[:cut])


def _synthetic_league(seed=3, absorb=True):
    """Prices for 2016/17-2024/25 where the true chance moves with x; the close knows
    it (absorb=True), the early price doesn't."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in range(2016, 2026):
        for i in range(300):
            date = pd.Timestamp(f"{season}-08-10") + pd.Timedelta(days=i)
            x = rng.choice([0.0, 0.0, 0.0, rng.normal(0, 0.3)])
            base = np.log(rng.dirichlet([6, 4, 4]))
            true = np.exp(base + np.array([x, 0, -x]))
            true /= true.sum()
            early = np.exp(base) / np.exp(base).sum()
            close = true if absorb else early
            y = rng.choice(3, p=true)
            rows.append(
                {
                    "date": date,
                    "home": f"H{i}",
                    "away": f"A{i}",
                    "season": f"{season % 100:02d}{(season + 1) % 100:02d}",
                    "home_goals": [2, 1, 0][y],
                    "away_goals": [0, 1, 2][y],
                    **{
                        f"pinnacle_early_{k}": 1 / (1.03 * early[j])
                        for j, k in enumerate(lineups.H2H)
                    },
                    **{
                        f"pinnacle_close_{k}": 1 / (1.02 * close[j])
                        for j, k in enumerate(lineups.H2H)
                    },
                    "x": x,
                    "home_surprise": 0.0,
                    "away_surprise": x,
                    "home_missing": 0,
                    "away_missing": int(x != 0),
                }
            )
    return pd.DataFrame(rows)


def test_run_finds_a_move_that_the_close_absorbs():
    df = lineups.prepare(_synthetic_league())
    assert df["date"].max() < lineups.HOLDOUT_START  # 2025/26 dropped first
    res = lineups.run(df, level=0.95)
    assert res["move"]["pass"] and res["move"]["slope"] > 0
    assert res["early"]["blend"]["c_range"][0] > 0  # x adds to the early price
    lo, _ = res["close"]["blend"]["c_range"]
    assert lo < 0 and not res["close"]["pass"]  # the close already has it
    text = lineups.report("E0", res, 0.95)
    assert "1 move" in text and "4 close" in text and "nan" not in text.lower()


def test_run_flags_news_the_close_misses():
    res = lineups.run(lineups.prepare(_synthetic_league(absorb=False)), level=0.95)
    assert not res["move"]["pass"]
    assert res["close"]["pass"]  # x still adds beside a close that ignored it


def test_family_size():
    assert lineups.TESTS == 20 and len(lineups.LEAGUES) == 5
