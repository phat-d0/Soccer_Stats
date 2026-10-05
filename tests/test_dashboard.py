import numpy as np
import pandas as pd
import pytest

from soccer_stats import dashboard
from soccer_stats.data import current_season, load_fixtures


@pytest.fixture(scope="module")
def model(league):
    return dashboard.fit_model(league[0])


def test_predict_fixtures_flags_best_value(model, league):
    fixtures = pd.DataFrame(
        {
            "kickoff": pd.to_datetime(["2030-01-01 15:00", "2030-01-01 17:30"]),
            "home": ["T00", "T02"],
            "away": ["T01", "Newcomer"],
            "odds_home": [10.0, np.nan],  # absurdly generous price -> must be the pick
            "odds_draw": [3.5, np.nan],
            "odds_away": [1.2, np.nan],
            "odds_over25": [np.nan, np.nan],
            "odds_under25": [np.nan, np.nan],
        }
    )
    counts = dashboard.match_counts(league[0], ["T00", "T01", "T02", "Newcomer"])
    preds = dashboard.predict_fixtures(model, fixtures, counts)
    assert preds.loc[0, "pick"] == "home" and preds.loc[0, "pick_edge"] > 0
    assert preds.loc[1, "pick"] == ""
    assert preds.loc[1, "low_data"] and not preds.loc[0, "low_data"]
    np.testing.assert_allclose(preds[["p_home", "p_draw", "p_away"]].sum(axis=1), 1.0)


def test_team_ratings_sorted_by_net(model, league):
    teams = sorted(model.teams)
    ratings = dashboard.team_ratings(model, dashboard.match_counts(league[0], teams), teams)
    assert list(ratings.index) == list(range(1, len(teams) + 1))
    assert ratings["goal_diff"].is_monotonic_decreasing


def test_top_scorelines():
    m = np.zeros((3, 3))
    m[1, 0], m[2, 2], m[0, 0] = 0.5, 0.3, 0.2
    assert dashboard.top_scorelines(m, 2) == [("1-0", 0.5), ("2-2", 0.3)]


def test_current_season_rolls_over_in_july():
    from datetime import date

    assert current_season(date(2026, 6, 30)) == 2025
    assert current_season(date(2026, 7, 1)) == 2026


def test_load_fixtures_from_cache(tmp_path):
    pd.DataFrame(
        {
            "Div": ["E0", "SP1"],
            "Date": ["18/10/2026", "18/10/2026"],
            "Time": ["12:30", "20:00"],
            "HomeTeam": ["Arsenal", "Barcelona"],
            "AwayTeam": ["Chelsea", "Sevilla"],
            "PSH": [1.9, 1.4],
        }
    ).to_csv(tmp_path / "fixtures.csv", index=False)
    fx = load_fixtures(["E0"], raw_dir=tmp_path)
    assert len(fx) == 1
    assert fx.loc[0, "kickoff"] == pd.Timestamp("2026-10-18 12:30")
    assert fx.loc[0, "odds_home"] == 1.9 and np.isnan(fx.loc[0, "odds_draw"])
