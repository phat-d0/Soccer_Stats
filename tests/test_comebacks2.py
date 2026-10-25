import json

import pandas as pd

from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import comebacks2 as c2

KO = "2024-09-14T14:00Z"
SLOT = {
    "league": "E0",
    "sport": "soccer_epl",
    "kickoff": KO,
    "request_at": "2024-09-14T14:55Z",
    "matches": [
        {"home": "Arsenal", "away": "Leicester", "favourite": "home", "p_fav": 0.7, "ht": [0, 1]},
        {"home": "Chelsea", "away": "Everton", "favourite": "home", "p_fav": 0.6, "ht": [0, 1]},
    ],
}


def fd():
    raw = pd.DataFrame(
        {
            "league": "E0",
            "season_start": 2024,
            "date": pd.Timestamp("2024-09-14"),
            "home": ["Arsenal", "Chelsea"],
            "away": ["Leicester", "Everton"],
            "fthg": [2, 0],
            "ftag": [1, 1],
            "hthg": [0, 0],
            "htag": [1, 1],
            "ph": [1.40, 1.62],
            "pd": [5.0, 4.0],
            "pa": [8.0, 6.0],
            "pov": [1.7, 1.9],
            "pun": [2.2, 1.95],
        }
    )
    return cb.favourites(raw)


def book(key, home, draw, away, upd="2024-09-14T14:53:00Z", names=("Arsenal", "Leicester City")):
    return {
        "key": key,
        "markets": [
            {
                "key": "h2h",
                "last_update": upd,
                "outcomes": [
                    {"name": names[0], "price": home},
                    {"name": "Draw", "price": draw},
                    {"name": names[1], "price": away},
                ],
            }
        ],
    }


def body(pin_home=2.6):
    ev1 = {
        "home_team": "Arsenal",
        "away_team": "Leicester City",
        "commence_time": "2024-09-14T14:00:00Z",
        "bookmakers": [
            book("draftkings", 2.5, 3.4, 3.0),
            book("pinnacle", pin_home, 3.5, 3.1),
            book("betfair_ex_eu", 2.55, 3.6, 3.2),
            book("fanduel", 2.45, 3.3, 3.0),
            book("bovada", 2.5, 3.3, 2.9, upd="2024-09-14T14:40:00Z"),  # stale
        ],
    }
    names = ("Chelsea", "Everton")
    ev2 = {
        "home_team": "Chelsea",
        "away_team": "Everton",
        "commence_time": "2024-09-14T14:00:00Z",
        "bookmakers": [
            book("draftkings", 1.5, 4.0, 7.0, names=names),  # not moved against Chelsea
            book("pinnacle", 3.3, 3.2, 2.4, names=names),
        ],
    }
    return {"timestamp": "2024-09-14T14:50:30Z", "data": [ev1, ev2]}


def write(tmp_path, b):
    cf.body_path(tmp_path, SLOT).write_text(json.dumps(b))


def test_quote_checks_and_primary_bet(tmp_path):
    write(tmp_path, body())
    rows, counts = c2.match_rows([SLOT], tmp_path, fd())
    assert counts["unmatched"] == 0 and len(rows) == 2
    ars, che = rows
    assert ars["dk_odds"] == 2.5 and ars["won"] == 1 and ars["fair_from"] == "pinnacle"
    assert che["dk_odds"] is None  # DraftKings' price still favoured Chelsea: not moved
    assert counts["fails_by_book"]["bovada"]["stale"] == 1
    assert counts["fails_by_book"]["draftkings"]["not_moved"] == 1
    res = c2.analyse(rows, counts)
    assert res["primary_draftkings"]["matches"] == 1
    assert res["primary_draftkings"]["roi"] == 1.5  # won at 2.5
    assert res["verdict"].startswith("inconclusive")
    bf = res["second_line_betfair"]
    assert abs(bf["roi"] - 1.55 * 0.98) < 1e-9


def test_pinnacle_off_the_median_falls_back_to_betfair(tmp_path):
    write(tmp_path, body(pin_home=1.6))  # Pinnacle still near its pre-match price: stale
    rows, counts = c2.match_rows([SLOT], tmp_path, fd())
    assert rows[0]["fair_from"] in ("betfair", None)
    fails = counts["fails_by_book"]["pinnacle"]
    assert fails["pinnacle_off"] + fails["not_moved"] >= 1


def test_snapshot_outside_half_time_drops_the_slot(tmp_path):
    b = body()
    b["timestamp"] = "2024-09-14T14:30:00Z"
    write(tmp_path, b)
    rows, counts = c2.match_rows([SLOT], tmp_path, fd())
    assert rows == [] and counts["slots_off_half_time"] == 1


def test_missing_body_is_counted(tmp_path):
    rows, counts = c2.match_rows([SLOT], tmp_path, fd())
    assert rows == [] and counts["slots_missing"] == 1


def test_verdict_rules():
    assert c2.verdict({"matches": 99, "roi_ci95": [0.1, 0.2]}).startswith("inconclusive")
    assert c2.verdict({"matches": 150, "roi_ci95": [0.01, 0.2]}).startswith("pass")
    assert c2.verdict({"matches": 150, "roi_ci95": [-0.2, -0.01]}).startswith("fail: the in-play")
    assert c2.verdict({"matches": 150, "roi_ci95": [-0.1, 0.1]}) == "fail: ROI range includes 0"


def test_season_join_picks_the_right_season(tmp_path):
    # The same pairing a season earlier, with a different result: the slot's season must win.
    old = fd().copy()
    old["season_start"] = 2023
    old["fthg"], old["ftag"] = [0, 0], [3, 3]
    write(tmp_path, body())
    rows, _ = c2.match_rows([SLOT], tmp_path, pd.concat([old, fd()], ignore_index=True))
    assert rows[0]["season"] == 2024 and rows[0]["won"] == 1


def test_guarded_league_counts_as_not_priced(tmp_path):
    slot = {**SLOT, "league": "E2", "sport": "soccer_england_league1"}
    (tmp_path / cf.GUARD_FILE).write_text(json.dumps({"E2": {"blocked": True}}))
    rows, counts = c2.match_rows([slot], tmp_path, fd())
    assert rows == [] and counts["slots_not_priced"] == 1 and counts["matches_not_priced"] == 2
    assert counts["slots_missing"] == 0


def test_by_league_and_season_tables(tmp_path):
    write(tmp_path, body())
    rows, counts = c2.match_rows([SLOT], tmp_path, fd())
    res = c2.analyse(rows, counts)
    assert res["primary_by_league"]["E0"]["matches"] == 1
    assert res["primary_by_season"]["2024/25"]["matches"] == 1
