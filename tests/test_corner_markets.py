"""Corner-market discovery (edge/corner_markets.py) against a fake Odds API."""

import pandas as pd
import pytest

from soccer_stats.edge import corner_markets as cm

NOW = pd.Timestamp("2026-11-08T09:00:00Z")
KICK = "2026-11-08T14:00:00Z"


class Resp:
    def __init__(self, body, cost, left=4_000_000):
        self.body, self.status_code, self.ok = body, 200, True
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def ou(over, under, line, team=None):
    d = {"description": team} if team else {}
    return [
        {"name": "Over", "point": line, "price": over, **d},
        {"name": "Under", "point": line, "price": under, **d},
    ]


def odds_body():
    def book(key, team_o, team_u, tot_o, tot_u, h_home, h_away):
        return {
            "key": key,
            "markets": [
                {
                    "key": "alternate_team_totals_corners",
                    "last_update": "2026-11-08T08:50:00Z",
                    "outcomes": ou(team_o, team_u, 4.5, "Arsenal"),
                },
                {"key": "alternate_totals_corners", "outcomes": ou(tot_o, tot_u, 9.5)},
                {
                    "key": "alternate_spreads_corners",
                    "outcomes": [
                        {"name": "Arsenal", "point": -1.5, "price": h_home},
                        {"name": "Leeds", "point": 1.5, "price": h_away},
                    ],
                },
                {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 1.5}]},
            ],
        }

    return {
        "home_team": "Arsenal",
        "away_team": "Leeds",
        "bookmakers": [
            book("pinnacle", 1.90, 1.90, 1.95, 1.85, 1.9, 1.9),
            book("betfair_ex_uk", 2.10, 1.95, 1.90, 1.80, 1.8, 1.8),
        ],
    }


def fake(disc_cost=1, odds_cost=None, leagues=("E0", "SP1")):
    calls = []

    def get(url, params, timeout):
        calls.append((url, dict(params)))
        if url.endswith("/events"):
            ev = {"id": "ev1", "home_team": "Arsenal", "away_team": "Leeds"}
            return Resp([{**ev, "commence_time": KICK}], 0)
        if url.endswith("/markets"):
            mk = [{"key": k} for k in ("alternate_team_totals_corners", "h2h")]
            mk += [{"key": "alternate_totals_corners"}, {"key": "alternate_spreads_corners"}]
            return Resp(
                {
                    "bookmakers": [
                        {"key": "pinnacle", "markets": mk},
                        {"key": "betfair_ex_uk", "markets": mk},
                        {"key": "fanduel", "markets": [{"key": "h2h"}]},
                    ]
                },
                disc_cost,
            )
        n = len(params["markets"].split(","))
        return Resp(odds_body(), n if odds_cost is None else odds_cost)

    return get, calls


def test_keys_are_ordered_team_total_handicap_then_first_half():
    found = {
        "h2h",
        "totals_corners",
        "alternate_totals_corners",
        "alternate_team_totals_corners",
        "alternate_spreads_corners",
        "alternate_totals_corners_h1",
    }
    assert cm.corner_keys(found) == [
        "alternate_team_totals_corners",
        "alternate_totals_corners",
        "totals_corners",
        "alternate_spreads_corners",
        "alternate_totals_corners_h1",
    ]
    assert cm.kind_of("alternate_totals_corners_h1") == "total_h1"


def test_pinnacle_and_exchanges_lead_the_books():
    books = {"fanduel": ["a", "b"], "betfair_ex_uk": ["a"], "pinnacle": ["a"], "x": ["h2h"]}
    assert cm.choose_books(books, ["a", "b"]) == ["pinnacle", "betfair_ex_uk", "fanduel"]


def test_values_against_pinnacle_fair_price_and_handicap_pairing():
    p = cm.pairs(cm.rows(odds_body(), NOW))
    hc = p[(p["kind"] == "handicap") & (p["book"] == "pinnacle")].iloc[0]
    assert hc["line"] == -1.5 and hc["over"] == 1.9 and hc["under"] == 1.9
    team = p[(p["kind"] == "team") & (p["book"] == "pinnacle")].iloc[0]
    assert team["age_min"] == pytest.approx(10)
    c = cm.against_sharp(p)
    t = c[c["kind"] == "team"].iloc[0]
    # Pinnacle 1.90/1.90 → fair 0.5 each; the exchange's 2.10 over is +5%.
    assert t["value_over"] == pytest.approx(0.05)
    assert t["value_best"] == pytest.approx(0.05)
    s = {(x["kind"], x["book"]): x for x in cm.summarize(p, c)}
    assert s[("team", "betfair_ex_uk")]["share_above_0.02"] == 1.0
    assert s[("handicap", "betfair_ex_uk")]["share_above_0"] == 0.0
    assert s[("team", "pinnacle")]["vs_pinnacle"] == 0


def test_run_within_cap_and_dry_run_spends_nothing():
    get, calls = fake()
    res = cm.run(0, leagues=("E0", "SP1"), api_key="k", now=NOW, get=get)
    assert res["spent"] == 0 and all(u.endswith("/events") for u, _ in calls)
    get, calls = fake()
    res = cm.run(20, leagues=("E0", "SP1"), odds_leagues=1, api_key="k", now=NOW, get=get)
    assert res["spent"] == 2 + 3 and res["stopped"] is None
    odds = [p for u, p in calls if u.endswith("/odds")]
    assert len(odds) == 1 and odds[0]["bookmakers"] == "pinnacle,betfair_ex_uk"
    assert res["leagues"]["E0"]["compared_lines"] == 3
    assert "_books" not in res["leagues"]["E0"]
    assert "k" not in cm.report(res).split()


def test_stops_when_a_call_bills_more_than_planned():
    get, calls = fake(disc_cost=4)
    res = cm.run(20, leagues=("E0", "SP1"), api_key="k", now=NOW, get=get)
    assert "discovery billed 4" in res["stopped"]
    assert sum(u.endswith("/markets") for u, _ in calls) == 1
    assert not any(u.endswith("/odds") for u, _ in calls)
    get, calls = fake(odds_cost=9)
    res = cm.run(20, leagues=("E0", "SP1"), odds_leagues=2, api_key="k", now=NOW, get=get)
    assert "odds billed 9" in res["stopped"]
    assert sum(u.endswith("/odds") for u, _ in calls) == 1
