"""Totals markets (edge/totals.py) on synthetic leagues (no network)."""

import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from soccer_stats.edge import totals


def test_trailing_mean_uses_strictly_earlier_rows_in_the_window():
    dates = pd.to_datetime(["2020-01-01", "2020-01-01", "2020-01-05", "2021-03-01"])
    v = [1.0, 3.0, 10.0, 7.0]
    m = totals.trailing_mean(dates, v, 30)
    assert np.isnan(m[0]) and np.isnan(m[1])  # same day: not earlier
    assert m[2] == pytest.approx(2.0)
    assert np.isnan(m[3])  # nothing in the 30 days before


def test_convolve_and_goal_probs_match_direct_sums():
    ph = np.array([[0.5, 0.5, 0.0]])
    pa = np.array([[0.2, 0.3, 0.5]])
    c = totals.convolve(ph, pa, kmax=2)
    assert c.sum() == pytest.approx(1) and c[0, 0] == pytest.approx(0.1)
    assert c[0, 2] == pytest.approx(1 - 0.1 - (0.5 * 0.3 + 0.5 * 0.2))  # tail bucket
    g = np.arange(11)
    m = np.outer(poisson.pmf(g, 1.6), poisson.pmf(g, 1.1))
    df = pd.DataFrame({"matrix": [m / m.sum()]})
    p = totals.goal_probs(df).iloc[0]
    assert p["goals_over_2.5"] == pytest.approx(1 - poisson.cdf(2, 2.7), abs=1e-6)
    assert p["home_over_0.5"] == pytest.approx(1 - poisson.pmf(0, 1.6), abs=1e-6)
    assert p["away_over_1.5"] == pytest.approx(1 - poisson.cdf(1, 1.1), abs=1e-6)


def test_corner_price_scan_ignores_goal_lines():
    raws = [pd.DataFrame(columns=["B365C>2.5", "PC<2.5", "AvgAHH", "HC", "Corners>9.5"])]
    raws.append(pd.DataFrame(columns=["XYZ>10.5", "P>2.5"]))
    assert totals.corner_price_columns(raws) == ["Corners>9.5", "XYZ>10.5"]


def _league(seed=0, seasons=range(2015, 2020)):
    """A double round robin per season; goals from a known Poisson matrix (so the
    'model' is the truth), corners NB with team-dependent means, Pinnacle 2.5 prices."""
    rng = np.random.default_rng(seed)
    teams = [f"T{i}" for i in range(20)]
    attack = dict(zip(teams, rng.normal(0, 0.25, 20), strict=True))
    corners = dict(zip(teams, rng.normal(0, 0.3, 20), strict=True))  # a team's corner tendency
    rows = []
    g = np.arange(11)
    for s in seasons:
        day = pd.Timestamp(f"{s}-08-10")
        pairs = [(h, a) for h in teams for a in teams if h != a]
        rng.shuffle(pairs)
        for i, (h, a) in enumerate(pairs):
            date = day + pd.Timedelta(days=i // 2)
            lh = np.exp(0.35 + attack[h] - attack[a])
            la = np.exp(0.1 + attack[a] - attack[h])
            m = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la))
            m /= m.sum()
            hg, ag = rng.poisson(lh), rng.poisson(la)
            ch = rng.negative_binomial(8, 8 / (8 + np.exp(1.7 + corners[h] + 0.5 * corners[a])))
            ca = rng.negative_binomial(8, 8 / (8 + np.exp(1.5 + corners[a] + 0.5 * corners[h])))
            over = 1 - poisson.cdf(2, lh + la)
            rows.append(
                {
                    "date": date,
                    "home": h,
                    "away": a,
                    "home_goals": hg,
                    "away_goals": ag,
                    "season": f"{s % 100:02d}{(s + 1) % 100:02d}",
                    "home_shots": rng.poisson(12 * np.exp(corners[h])),
                    "away_shots": rng.poisson(10 * np.exp(corners[a])),
                    "home_corners": ch,
                    "away_corners": ca,
                    "pinnacle_early_over25": 1 / (1.03 * over),
                    "pinnacle_early_under25": 1 / (1.03 * (1 - over)),
                    "pinnacle_close_over25": 1 / (1.02 * over),
                    "pinnacle_close_under25": 1 / (1.02 * (1 - over)),
                    "exp_home": lh,
                    "exp_away": la,
                    "matrix": m,
                }
            )
    df = pd.DataFrame(rows)
    df["season_start"] = 2000 + df["season"].str[:2].astype(int)
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    return df


def test_corner_features_use_earlier_matches_only():
    df = _league(seasons=range(2015, 2016))
    f = totals.corner_features(df)
    later = df.copy()
    cut = later["date"] >= later["date"].iloc[300]
    later.loc[cut, ["home_corners", "away_corners", "home_shots", "away_shots"]] = 40
    g = totals.corner_features(later)
    early = df["date"] < df["date"].iloc[300]
    pd.testing.assert_frame_equal(f[early], g[early])
    assert f.iloc[:3, :8].isna().all().all()  # the first rows: fewer than 3 earlier matches


def test_run_end_to_end_on_a_league_where_the_model_is_the_truth(monkeypatch):
    monkeypatch.setattr(totals, "N_BOOT", 2000)
    df = _league()
    res = totals.run(df, level=0.95)
    # The true matrix is calibrated everywhere. In this league the match total barely
    # varies (strength cancels), but each side's total does: that one beats the baseline.
    # Two checks at 95% per line flag about 1 line in 10 by chance, even for the truth.
    flags = [r["calibrated"] for r in [*res["goals"].values(), *res["team"].values()]]
    assert sum(flags) >= 0.8 * len(flags)
    assert res["team"]["home_1.5"]["gain_range"][0] > 0
    assert set(res["team"]) == {f"{s}_{x}" for s in ("home", "away") for x in totals.TEAM_LINES}
    # Pinnacle's price is the same truth: the model adds nothing beside it.
    p = res["pinnacle_25"]
    assert p["rows"] > 500 and not p["pass"]
    # Corners depend on the teams: the model beats the league-average baseline.
    c = res["corners"]
    assert c["rows"] > 300 and c["seasons"][0] >= totals.FIRST_SCORED
    assert c["count_log_loss"]["independent"] < c["count_log_loss"]["baseline"]
    assert c["9.5"]["gain"] > 0
    text = totals.report("E0", res, {"columns_scanned": 5, "corner_price_columns": []}, 0.95)
    assert "A. Corners" in text and "nan" not in text.lower()


def test_family_size():
    assert totals.MARKET_LINES == 16 and totals.TESTS == 108


def test_a_biased_model_is_flagged_as_miscalibrated():
    rng = np.random.default_rng(5)
    p = rng.uniform(0.2, 0.8, 3000)
    y = (rng.uniform(size=3000) < p).astype(int)
    g = np.arange(3000)
    good = totals.score_line(p, np.full(3000, 0.5), y, g, 0.95)
    bad = totals.score_line(np.clip(p + 0.1, 0, 1), np.full(3000, 0.5), y, g, 0.95)
    assert good["calibrated"] and good["gain_range"][0] > 0
    assert not bad["calibrated"] and bad["obs_minus_pred"] < -0.05
