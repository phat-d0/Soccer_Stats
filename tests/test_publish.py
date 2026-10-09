import json

import numpy as np
import pandas as pd

from soccer_stats.publish import _clean, build_data


def test_clean_makes_json_safe():
    out = _clean({"a": np.float64("nan"), "b": np.int64(3), "c": [np.float32(0.123456)]})
    assert out == {"a": None, "b": 3, "c": [0.1235]}
    json.dumps(out, allow_nan=False)


def test_build_data_shape(league):
    df, _ = league
    # Shift the synthetic league so its last season is the "current" one.
    df = df.copy()
    shift = pd.Timestamp.now().normalize() - pd.Timedelta(days=30) - df["date"].max()
    df["date"] = df["date"] + shift
    df["league"], df["season"] = "E0", "x"
    for col in ["odds_home", "odds_draw", "odds_away", "close_home", "close_draw", "close_away"]:
        df[col] = 3.0
    fixtures = pd.DataFrame(
        {
            "kickoff": [pd.Timestamp.now().normalize() + pd.Timedelta(days=3)],
            "home": ["T00"],
            "away": ["T01"],
            "odds_home": [2.0],
            "odds_draw": [3.4],
            "odds_away": [4.0],
            "odds_over25": [1.9],
            "odds_under25": [1.9],
        }
    )
    data = build_data(df, fixtures, xg_error=None)
    json.dumps(data, allow_nan=False)  # must be strict JSON for the browser

    assert data["xg_weight"] == 0.7
    assert set(data["params"]) == {"intercept", "home_adv", "rho", "attack", "defence"}
    fx = data["fixtures"][0]
    assert abs(fx["p"]["home"] + fx["p"]["draw"] + fx["p"]["away"] - 1) < 1e-3
    assert len(fx["matrix"]) == 6 and len(fx["matrix"][0]) == 6
    assert data["ratings"][0]["goal_diff"] >= data["ratings"][-1]["goal_diff"]
    assert {"model", "goals_only"} <= set(data["record"])


def test_implied_probs_removes_margin():
    from soccer_stats.publish import implied_probs

    out = implied_probs(
        {
            "odds_home": 1.9,
            "odds_draw": 3.6,
            "odds_away": 4.5,
            "odds_over25": 1.95,
            "odds_under25": 1.95,
        }
    )
    assert abs(out["home"] + out["draw"] + out["away"] - 1) < 1e-9
    assert abs(out["over25"] - 0.5) < 1e-9
    assert out["margin"] > 0
    missing = implied_probs({"odds_home": float("nan"), "odds_draw": 3.6, "odds_away": 4.5})
    assert missing["home"] is None and missing["over25"] is None


def test_upcoming_fixtures_merges_schedule_and_odds(tmp_path, monkeypatch):
    import soccer_stats.publish as pub

    dates = [
        # Understat: UTC times, long names; one played match must be ignored.
        {
            "isResult": True,
            "h": {"title": "Arsenal"},
            "a": {"title": "Chelsea"},
            "goals": {"h": "1", "a": "0"},
            "xG": {"h": "1.2", "a": "0.4"},
            "datetime": "2026-10-03 14:00:00",
        },
        {
            "isResult": False,
            "h": {"title": "Manchester City"},
            "a": {"title": "Wolverhampton Wanderers"},
            "goals": {"h": None, "a": None},
            "xG": {"h": None, "a": None},
            "datetime": "2026-10-18 14:00:00",
        },
        {
            "isResult": False,
            "h": {"title": "Liverpool"},
            "a": {"title": "Everton"},
            "goals": {"h": None, "a": None},
            "xG": {"h": None, "a": None},
            "datetime": "2026-10-25 11:30:00",
        },
    ]
    monkeypatch.setattr(pub, "current_season", lambda: 2026)
    (tmp_path / "understat_EPL_2026.json").write_text(json.dumps({"dates": dates}))
    # football-data: UK local times, odds for the first fixture only.
    pd.DataFrame(
        {
            "Div": ["E0"],
            "Date": ["18/10/2026"],
            "Time": ["15:00"],
            "HomeTeam": ["Man City"],
            "AwayTeam": ["Wolves"],
            "PSH": [1.3],
            "PSD": [6.0],
            "PSA": [10.0],
        }
    ).to_csv(tmp_path / "fixtures.csv", index=False)
    import os
    import time

    now_ts = time.time()
    for f in tmp_path.iterdir():
        os.utime(f, (now_ts, now_ts))

    fx = pub.upcoming_fixtures("E0", now=pd.Timestamp("2026-10-05", tz="UTC"), raw_dir=tmp_path)
    assert list(fx["home"]) == ["Man City", "Liverpool"]
    assert fx.loc[0, "kickoff"] == pd.Timestamp("2026-10-18 14:00", tz="UTC")  # 15:00 BST
    assert fx.loc[0, "odds_home"] == 1.3
    assert np.isnan(fx.loc[1, "odds_home"])


def test_build_data_applies_team_news(league):
    from soccer_stats.players import Absence, TeamNews

    df, _ = league
    df = df.copy()
    df["date"] = df["date"] + (
        pd.Timestamp.now().normalize() - pd.Timedelta(days=30) - df["date"].max()
    )
    df["league"], df["season"] = "E0", "x"
    for col in ["odds_home", "odds_draw", "odds_away", "close_home", "close_draw", "close_away"]:
        df[col] = 3.0
    now = pd.Timestamp.now(tz="UTC")
    fixtures = pd.DataFrame(
        {
            "kickoff": [now + pd.Timedelta(days=3), now + pd.Timedelta(days=10)],
            "home": ["T00", "T00"],
            "away": ["T01", "T02"],
        }
    )
    for col in ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]:
        fixtures[col] = np.nan
    news = {
        "T00": TeamNews(
            "T00",
            attack_mult=0.8,
            defence_mult=1.1,
            absences=[Absence("Star", "FWD", "out", 0, "Hamstring", 0.9, 0.2)],
        )
    }
    data = build_data(df, fixtures, xg_error=None, news=news, now=now)
    json.dumps(data, allow_nan=False)
    first, second = data["fixtures"]
    assert first["news_applied"] and not second["news_applied"]  # next match only
    assert first["p"]["home"] < first["p_base"]["home"]
    assert first["xg_mult"] == [0.8, 1.1]
    assert first["news"]["home"]["absences"][0]["name"] == "Star"
    assert data["team_news"]["T00"]["attack_mult"] == 0.8


def _understat_file(tmp_path, league, year, players):
    from soccer_stats.xg import LEAGUES as US

    path = tmp_path / f"understat_{US[league]}_{year}.json"
    path.write_text(json.dumps({"dates": [], "teams": {}, "players": players}))


def test_league_season_stats_reads_understat_players(tmp_path, monkeypatch):
    from soccer_stats import publish
    from soccer_stats.publish import league_season_stats

    real = publish.fetch_season

    def no_network(league, year, raw_dir):  # a season not in the cache fails, as offline
        path = tmp_path / f"understat_{publish.XG_LEAGUES[league]}_{year}.json"
        if not path.exists():
            raise OSError("offline")
        return real(league, year, raw_dir)

    monkeypatch.setattr(publish, "fetch_season", no_network)

    _understat_file(
        tmp_path,
        "SP1",
        2025,
        [
            {
                "id": "1",
                "player_name": "A",
                "games": "30",
                "time": "2500",
                "goals": "12",
                "xG": "10.42",
                "shots": "80",
                "position": "F S",
                "team_title": "Atletico Madrid",
            },
            {
                "id": "2",
                "player_name": "B",
                "games": "10",
                "time": "700",
                "goals": "0",
                "xG": "0.1",
                "shots": "3",
                "position": "GK",
                "team_title": "Sevilla,Real Betis",
            },
        ],
    )
    rows = league_season_stats("SP1", [2025, 2026], raw_dir=tmp_path)  # 2026 missing: skipped
    assert [r["player"] for r in rows] == ["A", "B"]
    a, b = rows
    assert a["season"] == "2526" and a["team"] == "Ath Madrid"  # football-data's name
    assert (a["apps"], a["minutes"], a["shots"], a["goals"], a["xg"]) == (30, 2500, 80, 12, 10.42)
    assert a["position"] == "FWD" and a["sot"] is None and a["starts"] is None
    assert b["position"] == "GK" and b["team"] == "Betis"  # moved: his last club
    assert league_season_stats("E1", [2025], raw_dir=tmp_path) == []  # no Understat


def test_team_block_matches_the_top_level_shape():
    from conftest import simulate_league

    from soccer_stats import dashboard
    from soccer_stats.publish import team_block

    matches, _ = simulate_league(n_teams=8, seasons=2, seed=3)
    matches["home_xg"] = float("nan")
    model = dashboard.fit_model(matches, xg_weight=0.0)
    block = team_block(model, matches, pd.DataFrame(), "SP1")
    assert block["name"] == "La Liga" and block["xg"] is False
    assert set(block) == {"name", "teams", "params", "ratings", "xg"}
    assert {r["team"] for r in block["ratings"]} == set(block["teams"])
    assert {"goals_for", "goals_against", "goal_diff"} <= set(block["ratings"][0])
