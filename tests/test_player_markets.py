"""Player market probe (parsing, budget order) and the assists lab's features."""

import numpy as np
import pandas as pd

from soccer_stats import player_assist_lab as al
from soccer_stats import player_market_probe as mp


def _body(markets):
    """An event odds body: {market: {book: [(name, desc, point, price)]}}."""
    books = {}
    for mk, by_book in markets.items():
        for book, outs in by_book.items():
            books.setdefault(book, []).append(
                {
                    "key": mk,
                    "outcomes": [
                        {"name": n, "description": d, "point": p, "price": o} for n, d, p, o in outs
                    ],
                }
            )
    return {"id": "ev1", "bookmakers": [{"key": b, "markets": m} for b, m in books.items()]}


def test_rows_and_summary_two_sided_and_yes_only():
    body = _body(
        {
            "player_shots": {
                "pinnacle": [
                    ("Over", "Bukayo Saka", 2.5, 2.0),
                    ("Under", "Bukayo Saka", 2.5, 1.8),
                ],
                "fanduel": [("Over", "Bukayo Saka", 2.0, 1.5)],
            },
            "player_assists": {
                "draftkings": [
                    ("Yes", "Bukayo Saka", None, 4.0),
                    ("Yes", "Martin Ødegaard", None, 5.0),
                ]
            },
        }
    )
    r = mp.rows(body)
    assert len(r) == 5 and set(r["side"]) == {"over", "under"}
    s = {(x["market"], x["book"]): x for x in mp.summary(r)}
    pin = s[("player_shots", "pinnacle")]
    assert pin["two_sided"] == 1 and abs(pin["margin_two_sided"] - (1 / 2 + 1 / 1.8 - 1)) < 1e-4
    assert pin["lines"] == [2.5]
    fd = s[("player_shots", "fanduel")]
    assert fd["two_sided"] == 0 and fd["margin_two_sided"] is None
    dk = s[("player_assists", "draftkings")]
    assert dk["two_sided"] == 0 and abs(dk["yes_sum_per_event"] - (0.25 + 0.2)) < 1e-6
    assert dk["lines"] == []


def test_market_keys_wanted_order_and_goal_or_assist_found():
    body = {
        "id": "e",
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "player_shots"}, {"key": "h2h"}]},
            {
                "key": "draftkings",
                "markets": [
                    {"key": "player_assists"},
                    {"key": "player_goal_or_assist"},
                    {"key": "player_goal_scorer_anytime"},
                    {"key": "player_shots"},
                ],
            },
        ],
    }
    keys = mp.market_keys(body)
    assert keys["player_shots"] == ["fanduel", "draftkings"]
    w = mp.wanted_markets(keys)
    assert w == ["player_shots", "player_assists", "player_goal_or_assist"]
    assert mp.books_listing(keys, w) == ["draftkings", "fanduel"]


class _Resp:
    def __init__(self, body, cost):
        self._b, self.status_code, self.ok = body, 200, True
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": "500000"}

    def json(self):
        return self._b


def test_run_stays_under_the_cap_and_buys_history_by_priority(tmp_path):
    ko = "2026-11-28T15:00:00Z"
    hist_ko = "2025-11-01T15:00:00Z"
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/historical/sports/soccer_epl/events"):
            return _Resp({"data": [{"id": "h1", "commence_time": hist_ko, "home_team": "A",
                                    "away_team": "B"}]}, 1)  # fmt: skip
        if "/historical/" in url:
            mk = params["markets"].split(",")
            body = _body({m: {"fanduel": [("Over", "X", 1.0, 1.4)]} for m in mk})
            return _Resp({"data": body}, 10 * len(mk))
        if url.endswith("/events"):
            return _Resp([{"id": "e1", "commence_time": ko, "home_team": "A", "away_team": "B"}], 0)
        if url.endswith("/markets"):
            return _Resp(
                {
                    "id": "e1",
                    "bookmakers": [
                        {
                            "key": "fanduel",
                            "markets": [{"key": "player_shots"}, {"key": "player_assists"}],
                        }
                    ],
                },  # fmt: skip
                1,
            )
        mk = params["markets"].split(",")
        return _Resp(_body({m: {"fanduel": [("Over", "X", 1.0, 1.4)]} for m in mk}), len(mk))

    res = mp.run(
        25, leagues=("E0",), api_key="k", now=pd.Timestamp("2026-11-26", tz="UTC"),
        get=get, raw_dir=tmp_path,
    )  # fmt: skip
    # markets 1 + live 2 + history events 1 = 4; 21 left -> two markets at 10 each = 20.
    assert res["history"]["markets"] == ["player_shots", "player_assists"]
    assert res["spent"] <= 25
    # No US book two-sided -> other regions asked only if the budget allows (it doesn't).
    assert res["other_regions"] is None
    assert any("skipped" in x for x in res["log"])


def _apps():
    rows = []
    k0 = pd.Timestamp("2024-08-01", tz="UTC")
    for i in range(12):
        for pid, pos, a, xa in (("1", "MID", i % 3 == 0, 0.3), ("2", "DEF", False, 0.05)):
            rows.append(
                {
                    "match_id": str(i),
                    "season": "2425",
                    "kickoff": k0 + pd.Timedelta(days=7 * i),
                    "team": "T",
                    "player_id": pid,
                    "position": pos,
                    "minutes": 90,
                    "started": True,
                    "goals": 1 if (pid == "1" and i == 5) else 0,
                    "assists": int(a),
                    "xa": xa,
                    "key_passes": 2 if pid == "1" else 0,
                    "yellow": int(pid == "2" and i % 4 == 0),
                    "red": 0,
                }
            )
    return pd.DataFrame(rows)


def test_assist_features_use_only_earlier_matches():
    apps = _apps()
    a = al.assist_features(apps)
    later = apps.copy()
    last = later["kickoff"] == later["kickoff"].max()
    later.loc[last, ["assists", "xa", "key_passes", "goals"]] = [9, 5.0, 20, 4]
    b = al.assist_features(later)
    cols = ["log_xa_rate", "log_kp_rate", "log_assist_conv", "season_avg_assists", "season_avg_ga"]
    pd.testing.assert_frame_equal(a[cols], b[cols])  # the last match's own result is never used
    mid = a[a["player_id"] == "1"]
    assert mid["log_xa_rate"].iloc[-1] > a[a["player_id"] == "2"]["log_xa_rate"].iloc[-1]
    assert np.isfinite(a[cols].to_numpy()).all()


def test_base_rates_count_distinct_players_per_match():
    br = al.base_rates(_apps(), seasons=("2425",))
    assert br["matches"] == 12
    assert br["assisters_per_match"] == round(4 / 12, 3)  # player 1 assists in 4 of 12
    assert br["booked_per_match"] == round(3 / 12, 3)
    assert br["scorers_or_assisters_per_match"] == round(5 / 12, 3)


def test_espn_appearances_minutes_and_team_shots():
    from soccer_stats import espn_players as ep

    def pl(aid, name, starter, sub_in=False, sub_out=False, shots=0, goals=0, pos="M"):
        return {
            "athlete": {"id": aid, "displayName": name},
            "starter": starter,
            "subbedIn": sub_in,
            "subbedOut": sub_out,
            "position": {"abbreviation": pos},
            "stats": [
                {"name": "totalShots", "value": shots},
                {"name": "totalGoals", "value": goals},
                {"name": "shotsOnTarget", "value": min(shots, 1)},
            ],
        }

    body = {
        "rosters": [
            {
                "homeAway": "home",
                "team": {"displayName": "Arsenal"},
                "roster": [
                    pl("1", "A One", True, sub_out=True, shots=3, goals=1, pos="F"),
                    pl("2", "B Two", False, sub_in=True, shots=1),
                    pl("3", "C Three", False),  # unused substitute
                    pl("4", "D Four", True, shots=2, pos="D"),
                ],
            },
            {
                "homeAway": "away",
                "team": {"displayName": "Chelsea"},
                "roster": [pl("5", "E Five", True, sub_out=True, shots=4)],
            },
        ],
        "keyEvents": [
            {
                "type": {"text": "Substitution"},
                "clock": {"value": 3600, "displayValue": "60'"},
                "participants": [{"athlete": {"id": "2"}}, {"athlete": {"id": "1"}}],
            }
        ],
    }
    meta = {"match_id": "9", "season": "2425", "kickoff": pd.Timestamp("2024-09-01", tz="UTC"),
            "home": "Arsenal", "away": "Chelsea", "espn_home": "Arsenal"}  # fmt: skip
    rows = {r["player"]: r for r in ep.appearances(body, meta)}
    assert "C Three" not in rows
    assert rows["A One"]["minutes"] == 60 and rows["A One"]["position"] == "FWD"
    assert rows["B Two"]["minutes"] == 30 and not rows["B Two"]["started"]
    assert rows["D Four"]["minutes"] == 90 and rows["D Four"]["position"] == "DEF"
    assert rows["E Five"]["minutes"] == ep.DEFAULT_START_OFF and not rows["E Five"]["minutes_known"]
    assert rows["A One"]["team_shots"] == 6 and rows["A One"]["opp_shots"] == 4
    assert rows["E Five"]["team"] == "Chelsea" and rows["E Five"]["home"] is False
