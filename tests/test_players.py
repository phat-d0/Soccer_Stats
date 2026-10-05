import json

import pandas as pd
import pytest

from soccer_stats.players import (
    chance_of_playing,
    fixture_multipliers,
    news_snapshot,
    parse_players,
    team_news,
)


def _el(i, name, team, pos, minutes, xg, xa, status="a", chance=None, news=""):
    return {
        "id": i,
        "web_name": name,
        "team": team,
        "element_type": pos,
        "status": status,
        "chance_of_playing_next_round": chance,
        "news": news,
        "minutes": minutes,
        "expected_goals": str(xg),
        "expected_assists": str(xa),
    }


def fpl_feed(star_status="a", star_chance=None, keeper_status="a"):
    """Two teams of 11 regulars over 10 games; team 1's striker is a star."""
    els, i = [], 0
    for team in (1, 2):
        for pos, n in ((1, 1), (2, 4), (3, 4), (4, 2)):
            for k in range(n):
                i += 1
                star = team == 1 and pos == 4 and k == 0
                status = star_status if star else (keeper_status if team == 1 and pos == 1 else "a")
                els.append(
                    _el(
                        i,
                        f"P{i}",
                        team,
                        pos,
                        900,
                        6.0 if star else 1.0,
                        1.0 if star else 0.5,
                        status=status,
                        chance=star_chance if star else None,
                        news="Hamstring injury" if star and star_status != "a" else "",
                    )
                )
        i += 1
        els.append(_el(i, f"Sub{i}", team, 3, 0, 0, 0))  # unused squad player
    return {"teams": [{"id": 1, "name": "Man Utd"}, {"id": 2, "name": "Spurs"}], "elements": els}


def test_chance_of_playing():
    assert chance_of_playing("a", None) == 1.0
    assert chance_of_playing("d", None) == 0.5
    assert chance_of_playing("d", 75) == 0.75
    assert chance_of_playing("i", None) == 0.0
    assert chance_of_playing("s", None) == 0.0


def test_parse_maps_team_names_and_positions():
    df = parse_players(fpl_feed())
    assert set(df["team"]) == {"Man United", "Tottenham"}
    assert set(df["position"]) == {"GK", "DEF", "MID", "FWD"}


def test_full_squad_means_no_adjustment():
    news = team_news(parse_players(fpl_feed()), {"Man United": 10, "Tottenham": 10})
    assert news["Man United"].attack_mult == pytest.approx(1.0)
    assert news["Man United"].defence_mult == pytest.approx(1.0)
    assert news["Man United"].absences == []


def test_injured_star_cuts_attack_and_is_listed():
    news = team_news(parse_players(fpl_feed("i")), {"Man United": 10, "Tottenham": 10})
    mu = news["Man United"]
    assert 0.7 <= mu.attack_mult < 0.9
    assert mu.defence_mult == pytest.approx(1.0)
    assert mu.absences[0].status == "out" and mu.absences[0].news == "Hamstring injury"
    assert news["Tottenham"].attack_mult == pytest.approx(1.0)


def test_doubtful_star_costs_less_than_injured():
    games = {"Man United": 10, "Tottenham": 10}
    out = team_news(parse_players(fpl_feed("i")), games)["Man United"].attack_mult
    doubt = team_news(parse_players(fpl_feed("d", 75)), games)["Man United"].attack_mult
    assert out < doubt < 1.0


def test_missing_keeper_raises_goals_conceded():
    news = team_news(parse_players(fpl_feed(keeper_status="s")), {"Man United": 10})
    assert news["Man United"].defence_mult == pytest.approx(1.08)


def test_multipliers_apply_to_next_fixture_only():
    news = team_news(parse_players(fpl_feed("i")), {"Man United": 10, "Tottenham": 10})
    now = pd.Timestamp("2026-10-05", tz="UTC")
    fixtures = pd.DataFrame(
        {
            "kickoff": pd.to_datetime(["2026-10-18 14:00", "2026-10-25 14:00"], utc=True),
            "home": ["Man United", "Tottenham"],
            "away": ["Tottenham", "Man United"],
        }
    )
    m = fixture_multipliers(fixtures, news, now, days=30)
    assert list(m) == [("Man United", "Tottenham")]
    home_mult, away_mult = m[("Man United", "Tottenham")]
    assert home_mult == pytest.approx(news["Man United"].attack_mult)
    assert away_mult == pytest.approx(1.0)


def test_snapshot_lists_flagged_players():
    snap = news_snapshot(parse_players(fpl_feed("d", 50)), "2026-10-05T07:00")
    assert len(snap) == 1 and snap[0]["chance"] == 50 and snap[0]["team"] == "Man United"


def test_news_log_records_only_changes(tmp_path):
    from soccer_stats.players import append_news_log

    snap1 = [
        {
            "at": "2026-10-05T07:00",
            "team": "A",
            "name": "X",
            "status": "d",
            "chance": 50,
            "news": "Knock",
        }
    ]
    assert append_news_log(snap1, tmp_path) == 1
    snap2 = [{**snap1[0], "at": "2026-10-05T08:00"}]
    assert append_news_log(snap2, tmp_path) == 0  # unchanged
    snap3 = [{**snap1[0], "at": "2026-10-05T09:00", "chance": 75}]
    assert append_news_log(snap3, tmp_path) == 1
    assert append_news_log([], tmp_path, at="2026-10-05T10:00") == 1  # recovered -> available
    lines = (tmp_path / "2026-10.jsonl").read_text().splitlines()
    assert [json.loads(x)["chance"] for x in lines] == [50, 75, 100]
