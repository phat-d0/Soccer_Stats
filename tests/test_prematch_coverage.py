import json

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
    import pandas as pd

    assert pc.find([e], "Chelsea", "Liverpool", pd.Timestamp("2024-02-25T15:00Z")) is e
    assert pc.find([e], "Chelsea", "Liverpool", pd.Timestamp("2024-02-25T17:00Z")) is None
