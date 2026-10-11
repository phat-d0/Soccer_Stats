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


# ---------- League One and League Two check (owner, 11 Oct; docs/leagues.md) ----------

CHECK_BOOKS_LISTED = (
    "betway",
    "unibet",
    "williamhill",
    "sport888",
    "betfair",
    "coral",
    "ladbrokes",
    "paddypower",
    "skybet",
    "betvictor",
    "draftkings",
    "fanduel",
)


def _check_odds_body():
    h2h = [
        {"name": "Wigan", "price": 2.5},
        {"name": "Draw", "price": 3.2},
        {"name": "Bolton", "price": 2.9},
    ]
    corners = [
        {"name": "Over", "description": "Wigan", "point": 4.5, "price": 1.9},
        {"name": "Under", "description": "Wigan", "point": 4.5, "price": 1.95},
    ]
    totals = [
        {"name": "Over", "point": 2.5, "price": 2.0},
        {"name": "Under", "point": 2.5, "price": 1.9},
    ]
    return {
        "bookmakers": [
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": h2h}]},
            {
                "key": "pinnacle",
                "markets": [
                    {"key": "h2h", "outcomes": h2h},
                    {"key": "alternate_team_totals_corners", "outcomes": corners},
                    {"key": "totals", "outcomes": totals},
                ],
            },
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": h2h[:2]}]},  # 2 of 3
        ]
    }


def check_api(calls, left=20000, events=True):
    disc = {
        "bookmakers": [{"key": b, "markets": [{"key": "h2h"}]} for b in CHECK_BOOKS_LISTED]
        + [
            {
                "key": "pinnacle",
                "markets": [
                    {"key": "h2h"},
                    {"key": "totals"},
                    {"key": "alternate_team_totals_corners"},
                    {"key": "btts"},
                ],
            }
        ]
    }

    def get(url, params, timeout):
        calls.append((url, dict(params)))
        if url.endswith("/events"):
            ev = {
                "id": "ev1",
                "commence_time": "2026-10-11T14:00:00Z",
                "home_team": "Wigan",
                "away_team": "Bolton",
            }
            return Resp([ev] if events else [], 0, left)
        if url.endswith("/markets"):
            return Resp(disc, 1, left - 1)
        n = len(params["markets"].split(","))
        return Resp(_check_odds_body(), n, left - 1 - n)

    return get


def test_parse_leagues_default_unchanged_and_check_leagues():
    assert mp.parse_leagues("") == mp.LEAGUES == ("E0", "SP1", "D1", "I1", "F1", "E1")
    assert mp.parse_leagues(" e2, E3 ") == ("E2", "E3")
    with pytest.raises(SystemExit):
        mp.parse_leagues("E2,XX")
    assert not any(mp.is_check(lg) for lg in mp.LEAGUES)
    assert mp.is_check("E2") and mp.is_check("E3")


def test_check_books_always_ask_draftkings_pinnacle_fanduel_within_one_region():
    books = {f"b{i:02d}": ["h2h", "totals"] for i in range(12)}
    books |= {"draftkings": ["h2h"], "pinnacle": ["alternate_team_totals_corners"]}
    books["fanduel"] = ["player_shots"]  # listed, none of the keys: still asked
    chosen = mp.check_books(books, ["h2h", "totals"])
    assert chosen[:3] == ["draftkings", "pinnacle", "fanduel"] and len(chosen) == mp.MAX_BOOKS
    assert mp.check_books({"b": ["h2h"]}, ["h2h"]) == ["b"]  # none listed: nothing forced
    assert mp.check_keys({"btts", "totals", "h2h"}) == ["h2h", "totals"]


def test_h2h_margins_need_all_three_outcomes():
    q = {x["book"]: x for x in mp.h2h_quotes(_check_odds_body())}
    assert q["draftkings"]["margin"] == pytest.approx(1 / 2.5 + 1 / 3.2 + 1 / 2.9 - 1)
    assert q["fanduel"]["outcomes"] == 2 and q["fanduel"]["margin"] is None
    s = {x["book"]: x for x in mp.h2h_summary(_check_odds_body())}
    assert s["draftkings"]["two_sided"] == 1 and s["fanduel"]["two_sided"] == 0


def test_check_runs_only_the_named_leagues_within_cap_and_never_leaks_the_key(tmp_path):
    calls = []
    res = mp.run(
        10, leagues=("E2", "E3"), api_key="SECRET", now=NOW, get=check_api(calls), out_dir=tmp_path
    )
    assert res["spent"] == 8  # 1 discovery + 3 markets, in each of two leagues
    sports = {c[0].split("/sports/")[1].split("/")[0] for c in calls}
    assert sports == {"soccer_england_league1", "soccer_england_league2"}
    for lg in ("E2", "E3"):
        r = res["leagues"][lg]
        assert r["events"] == 1 and r["upcoming"] == 1
        assert r["requested"]["markets"] == ["h2h", "alternate_team_totals_corners", "totals"]
        books = r["requested"]["bookmakers"]
        assert books[:3] == ["draftkings", "pinnacle", "fanduel"] and len(books) <= mp.MAX_BOOKS
        v = r["verdict"]
        assert "DraftKings h2h: all three sides, margin" in v
        assert "Pinnacle team corners: two-sided, margin" in v
        assert "FanDuel: h2h one-sided" in v
    odds_calls = [p for u, p in calls if u.endswith("/odds")]
    assert len(odds_calls) == 2 and all("regions" not in p for p in odds_calls)
    text = mp.report(res)
    assert "VERDICT E2" in text and "events listed: 1" in text
    assert "SECRET" not in text and "SECRET" not in str(res)
    assert all("SECRET" not in f.read_text() for f in tmp_path.iterdir())


def test_check_cap_trims_totals_first_and_the_reserve_blocks():
    calls = []
    res = mp.run(6, leagues=("E2", "E3"), api_key="k", now=NOW, get=check_api(calls))
    assert res["spent"] <= 6
    # (6 - 1) // 2 = 2 credits for E2's odds call: the reference totals go first.
    assert res["leagues"]["E2"]["requested"]["markets"] == ["h2h", "alternate_team_totals_corners"]
    calls = []
    # 3,002 left: discovery (1) keeps the 3,000 reserve, the 3-credit odds call wouldn't.
    res = mp.run(10, leagues=("E2",), api_key="k", now=NOW, get=check_api(calls, left=3002))
    assert res["spent"] == 1 and not [u for u, _ in calls if u.endswith("/odds")]
    assert "E2 odds" in " ".join(res["log"]) and "skipped" in " ".join(res["log"])
    assert "discovery not run" not in res["leagues"]["E2"]["verdict"]


def test_check_dry_run_lists_events_only_and_says_so():
    calls = []
    res = mp.run(0, leagues=("E2", "E3"), api_key="k", now=NOW, get=check_api(calls))
    assert res["spent"] == 0 and [c[0].rsplit("/", 1)[-1] for c in calls] == ["events"] * 2
    assert res["leagues"]["E2"]["verdict"].startswith("1 events listed; discovery not run")
    res = mp.run(0, leagues=("E3",), api_key="k", now=NOW, get=check_api([], events=False))
    assert res["leagues"]["E3"]["verdict"].startswith("no events listed")


def test_default_run_has_no_verdict_and_main_defaults_to_the_six(monkeypatch, capsys):
    res = mp.run(0, leagues=("E0",), api_key="k", now=NOW, get=fake_api([]))
    assert "verdict" not in res["leagues"]["E0"]
    seen = {}

    def fake_run(cap, leagues=mp.LEAGUES, **kw):
        seen["leagues"] = leagues
        return {"cap": cap, "leagues": {}, "log": [], "left_before": None, "spent": 0, "left": None}

    monkeypatch.setattr(mp, "run", fake_run)
    mp.main(["--cap", "0"])
    assert seen["leagues"] == mp.LEAGUES and "football-data" not in capsys.readouterr().out
    monkeypatch.setattr(mp, "football_data_coverage", lambda lgs: {"E2 2526": {"error": "X"}})
    mp.main(["--cap", "0", "--leagues", "E2,E3", "--coverage"])
    assert seen["leagues"] == ("E2", "E3")
    assert "football-data E2 2526: unavailable (X)" in capsys.readouterr().out


def test_football_data_coverage_counts_corners_and_pinnacle(tmp_path):
    csv = tmp_path / "E2.csv"
    csv.write_text(
        "Div,Date,HomeTeam,AwayTeam,HC,AC,PSH,PSD,PSA,PSCH,PSCD,PSCA\n"
        "E2,01/08/2025,A,B,5,3,2.0,3.3,3.5,2.1,3.2,3.4\n"
        "E2,02/08/2025,C,D,6,4,,,,2.2,3.1,3.3\n"
        "E2,03/08/2025,E,F,,,,,,,,\n"
        ",,,,,,,,,,,\n"
    )

    def download(lg, year, **kw):
        if year == 2026:
            raise OSError("not posted")
        return csv

    cov = mp.football_data_coverage(["E2"], download=download)
    assert cov["E2 2526"] == {
        "matches": 3,
        "corners": pytest.approx(0.667, abs=1e-3),
        "pinnacle_early": pytest.approx(0.333, abs=1e-3),
        "pinnacle_close": pytest.approx(0.667, abs=1e-3),
    }
    assert cov["E2 2627"] == {"error": "OSError"}
    res = {"cap": 0, "spent": 0, "left_before": None, "left": None, "leagues": {}, "log": []}
    text = mp.report({**res, "coverage": cov})
    assert "football-data E2 2526: 3 matches, corners (HC/AC) 67%" in text
