"""§17: the shots17 backfill design (no network) and gate (a)'s reader."""

import json

import numpy as np
import pandas as pd

from soccer_stats import player_goal_backfill as gb
from soccer_stats import player_shots_priced as sp


class Resp:
    def __init__(self, body, cost, status=200):
        self.body, self.status_code, self.ok = body, status, status < 400
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": "4000000"}

    def json(self):
        return self.body


def kickoffs(league, season):
    assert (league, season) == ("E0", 2025)
    return pd.Series(pd.to_datetime(["2025-11-01T15:00Z", "2025-11-01T17:30Z"]))


def test_shots17_asks_two_markets_at_seven_books_and_reuses_held_lists(tmp_path):
    held = tmp_path / "player_odds_history" / "goalscorer_test" / "E0"
    held.mkdir(parents=True)
    evs = [
        {"id": "a", "home_team": "A", "away_team": "B", "commence_time": "2025-11-01T15:00:00Z"},
        {"id": "b", "home_team": "C", "away_team": "D", "commence_time": "2025-11-01T17:30:00Z"},
    ]
    (held / "events_20251101.json").write_text(json.dumps({"data": evs}))
    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        if url.endswith("/sports"):
            return Resp([], 0)
        assert not url.endswith("/events"), "a held events list is never bought again"
        assert params["bookmakers"] == ",".join(gb.SHOTS_BOOKS)
        assert params["markets"] == ",".join(gb.SHOTS_MARKETS)
        return Resp({"data": {"id": url.split("/")[-2], "bookmakers": []}}, 20)

    res = gb.run("shots17", 1000, api_key="k", raw_dir=tmp_path, get=get, kickoffs=kickoffs)
    assert res["stopped"] is None
    assert res["bought"] == {"lists": 0, "snapshots": 4}
    assert res["spent"] == 80
    assert (tmp_path / "player_odds_history" / "shots_test" / "E0" / "a_entry.json").exists()
    assert len(gb.SHOTS_BOOKS) == 7 and "fanduel" not in gb.SHOTS_BOOKS


def test_shots17_stops_on_a_call_over_twenty(tmp_path):
    def get(url, params=None, timeout=None):
        if url.endswith("/sports"):
            return Resp([], 0)
        if url.endswith("/events"):
            evs = [{"id": "a", "home_team": "A", "away_team": "B",
                    "commence_time": "2025-11-01T15:00:00Z"}]  # fmt: skip
            return Resp({"data": evs}, 1)
        return Resp({"data": {"id": "a", "bookmakers": []}}, 30)

    res = gb.run("shots17", 1000, api_key="k", raw_dir=tmp_path, get=get, kickoffs=kickoffs)
    assert "> 20" in res["stopped"]


def test_shots17_dry_run_estimates_twenty_a_snapshot(tmp_path):
    res = gb.run("shots17", 0, api_key="k", raw_dir=tmp_path, kickoffs=kickoffs)
    assert res["dry_run"] and res["estimate"] == 1 + 2 * 20 * 2


def _name(team, i):
    return f"{('Alan', 'Brian', 'Carl')[i]} {team}{('ford', 'ley', 'ton')[i]}"


def _apps():
    rows = []
    for mid, home, away in (("m1", "Arsenal", "Chelsea"), ("m2", "Leeds", "Wolves")):
        for team, is_home in ((home, True), (away, False)):
            for i in range(3):
                rows.append({"match_id": mid, "kickoff": pd.Timestamp("2025-11-01T15:00Z"),
                             "team": team, "home": is_home, "player_id": f"{team}{i}",
                             "player": _name(team, i), "started": i < 2,
                             "shots": i + 1, "sot": i})  # fmt: skip
    return pd.DataFrame(rows)


def _prices():
    out = []
    for eid, home, away in (("e1", "Arsenal", "Chelsea"), ("e2", "Leeds", "Wolves")):
        for team in (home, away):
            for i in range(3):
                for book, odds in (("draftkings", 1.8), ("betmgm", 1.9)):
                    for kind in ("entry", "close"):
                        out.append({"event_id": eid, "kind": kind, "home": home, "away": away,
                                    "kickoff": pd.Timestamp("2025-11-01T15:00Z"), "book": book,
                                    "market": "shots", "player": _name(team, i),
                                    "point": 1.5, "odds": odds})  # fmt: skip
    out.append({**out[0], "player": "Nobody Here"})
    return pd.DataFrame(out)


def test_join_settles_by_ceil_of_the_line_and_drops_unmatched():
    j, counts = sp.join(_apps(), _prices())
    assert counts["events_linked"] == 2 and counts["names_unmatched"] == 1
    # point 1.5 needs 2 shots: player 0 (1 shot) loses, players 1 and 2 win
    p0 = j[j["player_id"] == "Arsenal0"]
    assert (p0["won"] == 0).all() and (j[j["player_id"] == "Arsenal1"]["won"] == 1).all()
    assert set(j["need"]) == {2}


def test_whole_line_means_at_least_k():
    x = pd.DataFrame({"point": [1.0, 1.5, 2.0], "count": [1, 1, 1]})
    assert list((x["count"] >= np.ceil(x["point"])).astype(int)) == [1, 0, 0]


def test_gate_a_uses_best_price_starters_at_entry():
    j, _ = sp.join(_apps(), _prices())
    g = sp.gate_a(j)
    m = g["markets"]["shots"]
    # starters only: players 0 and 1 of four teams = 8 lines, best 1.9; 4 won
    assert m["gate"]["lines"] == 8 and m["gate"]["won"] == 4
    assert abs(m["gate"]["markup"] - (8 / 1.9 / 4 - 1)) < 1e-4
    assert abs(m["average_price"]["markup"] - (8 / 1.85 / 4 - 1)) < 1e-4
    assert m["best_book_share"] == {"betmgm": 1.0}
    assert g["markets"]["sot"]["gate"] == {"lines": 0} and not g["markets"]["sot"]["stop"]
    assert m["stop"] == (m["gate"]["ci95"][0] > 0.08)


def test_markup_range_resamples_matches_reproducibly():
    df = pd.DataFrame({"match_id": list("aabbcc"), "won": [1, 0, 1, 1, 0, 0],
                       "best_odds": [1.5] * 6})  # fmt: skip
    a, b = sp.markup(df, "best_odds"), sp.markup(df, "best_odds")
    assert a == b and a["matches"] == 3 and a["ci95"][0] <= a["markup"] <= a["ci95"][1]
