"""Live market probe (edge/market_probe.py) against a fake Odds API (no network)."""

import pandas as pd
import pytest

from soccer_stats.edge import market_probe as mp

NOW = pd.Timestamp("2026-10-09T06:00:00Z")


class Resp:
    def __init__(self, body, cost, left, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def _odds_body():
    def tt(team, line, over, under):
        return [
            {"name": "Over", "description": team, "point": line, "price": over},
            {"name": "Under", "description": team, "point": line, "price": under},
        ]

    return {
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {"key": "team_totals", "outcomes": tt("Arsenal", 1.5, 1.95, 1.95)},
                    {
                        "key": "alternate_totals_corners",
                        "outcomes": [
                            {"name": "Over", "point": 9.5, "price": 1.9},
                            {"name": "Under", "point": 9.5, "price": 1.9},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "markets": [
                    {"key": "team_totals", "outcomes": tt("Arsenal", 1.5, 1.8, 1.8)},
                    {
                        "key": "btts",
                        "outcomes": [{"name": "Yes", "price": 1.7}],  # one side only
                    },
                ],
            },
        ]
    }


def fake_api(calls, left=20000, discovery_cost=1):
    disc = {
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {"key": "team_totals"},
                    {"key": "alternate_totals_corners"},
                    {"key": "h2h"},
                ],
            },
            {"key": "fanduel", "markets": [{"key": "team_totals"}, {"key": "btts"}]},
        ]
    }

    def get(url, params, timeout):
        calls.append((url, dict(params)))
        if url.endswith("/events"):
            return Resp(
                [
                    {
                        "id": "old",
                        "commence_time": "2026-10-08T12:00:00Z",
                        "home_team": "A",
                        "away_team": "B",
                    },
                    {
                        "id": "e1",
                        "commence_time": "2026-10-10T14:00:00Z",
                        "home_team": "Arsenal",
                        "away_team": "Chelsea",
                    },
                ],
                0,
                left,
            )
        if url.endswith("/markets"):
            return Resp(disc, discovery_cost, left - 1)
        n = len(params["markets"].split(","))
        return Resp(_odds_body(), n, left - 1 - n)

    return get


def test_pairs_and_margins():
    p = mp.pairs(mp.quotes(_odds_body()))
    pin = p[(p["book"] == "pinnacle") & (p["market"] == "team_totals")].iloc[0]
    assert pin["subject"] == "Arsenal" and pin["line"] == 1.5
    assert pin["margin"] == pytest.approx(2 / 1.95 - 1)
    btts = p[p["market"] == "btts"].iloc[0]
    assert pd.isna(btts["margin"]) and pd.isna(btts["line"])
    s = {(x["market"], x["book"]): x for x in mp.summarize(p)}
    assert s[("btts", "fanduel")]["two_sided"] == 0
    assert mp.reading(s[("team_totals", "pinnacle")]["margin_median"]) == "close to fair"
    assert mp.reading(s[("team_totals", "fanduel")]["margin_median"]) == "not bettable"
    assert mp.reading(None) == "one-sided"


def test_target_keys_priority_and_league_extras():
    found = {
        "h2h",
        "btts",
        "alternate_totals",
        "team_totals",
        "alternate_totals_corners",
        "player_shots_on_target",
    }
    assert mp.target_keys(found, "E0") == [
        "team_totals",
        "alternate_totals",
        "alternate_totals_corners",
        "btts",
        "player_shots_on_target",
    ]
    assert "player_shots_on_target" not in mp.target_keys(found, "SP1")


def test_run_stays_within_the_cap_and_never_leaks_the_key(tmp_path, capsys):
    calls = []
    res = mp.run(
        12, leagues=("E0", "SP1"), api_key="SECRET", now=NOW, get=fake_api(calls), out_dir=tmp_path
    )
    assert res["spent"] <= 12 and res["left_before"] == 20000
    e0 = res["leagues"]["E0"]
    assert e0["event"] == "Arsenal v Chelsea"  # the soonest upcoming, not the past one
    assert e0["requested"]["bookmakers"][0] in {"pinnacle", "fanduel"}
    assert {s["market"] for s in e0["summary"]} >= {"team_totals", "alternate_totals_corners"}
    odds_calls = [c for c in calls if c[0].endswith("/odds")]
    assert all(
        "regions" not in p and len(p["bookmakers"].split(",")) <= mp.MAX_BOOKS
        for _, p in odds_calls
    )
    text = mp.report(res)
    assert "SECRET" not in text and "SECRET" not in str(res)
    assert any(f.name.endswith("_odds.json") for f in tmp_path.iterdir())


def test_cap_zero_is_a_dry_run():
    calls = []
    res = mp.run(0, leagues=("E0",), api_key="k", now=NOW, get=fake_api(calls))
    assert res["spent"] == 0
    assert [c[0].rsplit("/", 1)[-1] for c in calls] == ["events"]  # only the free list


def test_reserve_floor_blocks_paid_calls():
    calls = []
    res = mp.run(40, leagues=("E0",), api_key="k", now=NOW, get=fake_api(calls, left=3000))
    assert res["spent"] == 0 and "skipped" in " ".join(res["log"])


def test_share_drops_lower_priority_markets_when_credits_are_short():
    calls = []
    res = mp.run(3, leagues=("E0", "SP1"), api_key="k", now=NOW, get=fake_api(calls))
    assert res["spent"] <= 3
    e0 = res["leagues"]["E0"]["requested"]["markets"]
    assert e0 == ["team_totals"]  # (3 - 1) // 2 = 1 credit for E0's odds call


def test_no_key_fetches_nothing():
    calls = []
    res = mp.run(40, api_key="", get=fake_api(calls))
    assert calls == [] and res["spent"] == 0
