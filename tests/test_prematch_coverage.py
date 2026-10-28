import json

import pandas as pd

from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import prematch_coverage as pc


def ev(home, away, t, books):
    return {
        "home_team": home,
        "away_team": away,
        "commence_time": t,
        "bookmakers": [
            {
                "key": b,
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": 2.0},
                            {"name": away, "price": 3.5},
                            {"name": "Draw", "price": 3.2},
                        ],
                    }
                ],
            }  # fmt: skip
            for b in books
        ],
    }


def test_counts_found_matches_and_full_prices(tmp_path):
    slot = {"league": "FAC", "sport": "soccer_fa_cup", "kickoff": "2025-01-11T15:00Z",
            "request_at": "2025-01-11T14:55Z",
            "matches": [{"home": "Manchester City", "away": "Salford City"},
                        {"home": "Leyton Orient", "away": "Derby County"}]}  # fmt: skip
    empty = {**slot, "kickoff": "2025-01-12T15:00Z", "request_at": "2025-01-12T14:55Z",
             "matches": [{"home": "A", "away": "B"}]}  # fmt: skip
    plan = {"design": "H2P", "slots": [slot, empty], "kickoff_slots": [slot, empty]}
    t = "2025-01-11T15:00:00Z"
    body = {"data": [ev("Man City", "Salford City FC", t, ["pinnacle", "draftkings"]),
                     ev("Leyton Orient", "Derby", t, ["draftkings"])]}  # fmt: skip
    cf.body_path(tmp_path, slot).write_text(json.dumps(body))
    cf.body_path(tmp_path, empty).write_text(json.dumps({"data": []}))
    (tmp_path / cf.CALLS).write_text(
        json.dumps({"sport": slot["sport"], "request_at": slot["request_at"], "cost": 10})
        + "\n"
        + json.dumps({"sport": slot["sport"], "request_at": empty["request_at"], "cost": 0})
    )
    res = pc.coverage(plan, tmp_path)
    assert res["snapshots"] == {"bought": 2, "any_events": 1, "events_at_kickoff": 1}
    assert res["cost_per_call"] == {"10": 1, "0": 1}
    assert res["matches"] == {"matches": 3, "events_at_kickoff": 2, "found": 2, "pinnacle": 1,
                              "draftkings": 2, "either": 2}  # fmt: skip
    assert res["by_season"]["2024/25"]["matches"] == 3


def test_a_neutral_ground_final_matches_either_way_round():
    e = ev("Liverpool", "Chelsea", "2024-02-25T15:00:00Z", ["pinnacle"])
    assert pc.find([e], "Chelsea", "Liverpool", pd.Timestamp("2024-02-25T15:00Z")) is e
    assert pc.find([e], "Chelsea", "Liverpool", pd.Timestamp("2024-02-25T17:00Z")) is None


def espn_event(eid, home, away, date, ht):
    return {
        "id": eid,
        "date": date,
        "status": {"type": {"completed": True}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"id": "1", "displayName": home},
                        "linescores": [{"value": ht[0]}],
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": "2", "displayName": away},
                        "linescores": [{"value": ht[1]}],
                    },
                ]  # fmt: skip
            }
        ],
    }


def priced_ev(home, away, t, prices):
    e = ev(home, away, t, ["pinnacle"])
    outs = e["bookmakers"][0]["markets"][0]["outcomes"]
    for o, p in zip(outs, prices, strict=True):
        o["price"] = p
    return e


def test_trailing_counts_heavy_favourites_behind_at_half_time(tmp_path):
    from soccer_stats import cups

    t = "2025-01-11T15:00:00Z"
    slot = {"league": "FAC", "sport": "soccer_fa_cup", "kickoff": "2025-01-11T15:00Z",
            "request_at": "2025-01-11T14:55Z",
            "matches": [{"home": "Manchester City", "away": "Salford City"},
                        {"home": "Tamworth", "away": "Tottenham Hotspur"},
                        {"home": "Leyton Orient", "away": "Derby County"}]}  # fmt: skip
    plan = {"design": "H2P", "slots": [slot], "kickoff_slots": [slot]}
    # prices are (home, away, draw) in the feed's order; Tottenham listed as home (swapped)
    body = {"data": [priced_ev("Man City", "Salford City", t, [1.15, 17.0, 8.0]),
                     priced_ev("Tottenham", "Tamworth", t, [1.2, 13.0, 7.0]),
                     priced_ev("Leyton Orient", "Derby", t, [2.4, 3.0, 3.3])]}  # fmt: skip
    cf.body_path(tmp_path, slot).write_text(json.dumps(body))
    days = tmp_path / "espn" / cups.CACHE / "espn"
    days.mkdir(parents=True)
    (days / "eng.fa_20250111.json").write_text(json.dumps({"events": [
        espn_event("1", "Manchester City", "Salford City", t, [0, 1]),
        espn_event("2", "Tamworth", "Tottenham Hotspur", t, [1, 0]),
        espn_event("3", "Leyton Orient", "Derby County", t, [1, 0]),
    ]}))  # fmt: skip
    res = pc.trailing(plan, tmp_path, tmp_path / "espn")
    # City (home, ~85%) trails 0-1; Spurs (ESPN's away, ~80%) trail 1-0; Orient isn't heavy
    assert res["all"] == {"matches": 3, "priced": 3, "pinnacle": 3, "no_half_time": 0,
                          "heavy": 2, "heavy_trailing": 2}  # fmt: skip


def test_trailing_reads_the_held_champions_league_closes(tmp_path):
    from soccer_stats import cups

    plan = {"design": "H2U", "slots": [], "kickoff_slots": []}
    days = tmp_path / "espn" / cups.CACHE / "espn"
    days.mkdir(parents=True)
    t = "2024-10-01T19:00:00Z"
    (days / "uefa.champions_20241001.json").write_text(json.dumps({"events": [
        espn_event("9", "Real Madrid", "Lille", t, [0, 1]),
        espn_event("8", "Arsenal", "PSG", t, [0, 0]),
    ]}))  # fmt: skip
    closes = tmp_path / "UCL_closes.jsonl"
    closes.write_text(
        json.dumps(
            {
                "espn_id": "9",
                "book": "pinnacle",
                "price_home": 1.2,
                "price_draw": 7.0,
                "price_away": 14.0,
            }
        )
        + "\n"  # fmt: skip
    )
    res = pc.trailing(
        plan, tmp_path, tmp_path / "espn", closes, now=pd.Timestamp("2026-10-28", tz="UTC")
    )
    # the level match is not counted; Madrid (~81%) trails at half-time
    assert res["by_cup"]["UCL"] == {"matches": 1, "priced": 1, "pinnacle": 1, "no_half_time": 0,
                                    "heavy": 1, "heavy_trailing": 1, "held_close": 1}  # fmt: skip
