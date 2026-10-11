"""Historical corner prices probe (edge/corner_history.py) against a fake Odds API."""

import json

import pandas as pd
import pytest

from soccer_stats.edge import corner_history as ch
from soccer_stats.player_goal_odds import CappedBudget

KEY = "SECRET-KEY-123"


class Resp:
    def __init__(self, body, cost, left, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def corners(home="Arsenal", away="Chelsea", h=(4.5, 1.95, 1.87), a=(3.5, 1.80, 2.02)):
    def tt(team, line, over, under):
        return [
            {"name": "Over", "description": team, "point": line, "price": over},
            {"name": "Under", "description": team, "point": line, "price": under},
        ]

    return [
        {
            "key": "pinnacle",
            "markets": [{"key": ch.MARKET, "outcomes": tt(home, *h) + tt(away, *a)}],
        }
    ]


def fake_api(calls, left=20000, odds_cost=10, quoted=True, odds_status=200, message=""):
    state = {"left": left}

    def get(url, params, timeout):
        calls.append((url, dict(params)))
        at = pd.Timestamp(params["date"])
        if url.endswith("/events"):
            state["left"] -= 1
            k = at + pd.Timedelta(hours=24)
            return Resp(
                {
                    "timestamp": params["date"],
                    "data": [
                        {
                            "id": "far",
                            "commence_time": ch._iso(k + pd.Timedelta(days=3)),
                            "home_team": "X",
                            "away_team": "Y",
                        },
                        {
                            "id": f"ev{len(calls)}",
                            "commence_time": ch._iso(k),
                            "home_team": "Arsenal",
                            "away_team": "Chelsea",
                        },
                    ],
                },
                1,
                state["left"],
            )
        if odds_status != 200:
            return Resp({"message": message}, 0, state["left"], odds_status)
        cost = odds_cost if quoted else 0
        state["left"] -= cost
        data = {"id": url.split("/")[-2], "bookmakers": corners() if quoted else []}
        return Resp({"timestamp": params["date"], "data": data}, cost, state["left"])

    return get


def test_capped_budget_refuses_past_cap_and_reserve():
    b = CappedBudget(10)
    assert b.allows(10) and not b.allows(11)
    b.left = 3005
    assert not b.allows(10)  # would leave under 3,000 on the shared key
    assert b.allows(5)


def test_dry_run_makes_no_calls(tmp_path):
    calls = []
    res = ch.run(0, api_key=KEY, get=fake_api(calls), raw_dir=tmp_path)
    assert calls == []
    assert res["spent"] == 0 and res["dry_run"]
    assert res["verdict"]["verdict"] == "not tested"
    assert res["projection"]["cost_source"].startswith("assumed")
    assert not (tmp_path / ch.CACHE).exists()


def test_no_key_makes_no_calls(tmp_path):
    calls = []
    res = ch.run(50, api_key="", get=fake_api(calls), raw_dir=tmp_path)
    assert calls == [] and res["dry_run"]


def test_full_probe_counts_costs_and_parses(tmp_path):
    calls = []
    res = ch.run(60, api_key=KEY, get=fake_api(calls), raw_dir=tmp_path)
    # 4 events + 4 looks + the close (60 leaves room) = 9 calls, 4 + 50 credits.
    assert len(calls) == 9
    assert res["spent"] == 54
    assert res["left_before"] == 20000 and res["left"] == 20000 - 54
    odds = [p for u, p in calls if u.endswith("/odds")]
    assert all(
        p["markets"] == ch.MARKET and p["bookmakers"] == "pinnacle" and p["regions"] == "eu"
        for p in odds
    )
    p0 = res["points"][0]
    assert p0["event"]["home"] == "Arsenal"  # the event nearest the target, not "far"
    look = p0["look"]
    k = pd.Timestamp(p0["event"]["commence_time"])
    assert look["requested"] == ch._iso(k - pd.Timedelta(hours=24))
    assert look["quoted"] and look["both_sides"] and look["cost"] == 10
    assert look["teams"]["Arsenal"]["lines"] == [4.5]
    assert look["margin_median"] == pytest.approx(
        ((1 / 1.95 + 1 / 1.87 - 1) + (1 / 1.80 + 1 / 2.02 - 1)) / 2, abs=1e-4
    )
    assert p0["close"]["requested"] == ch._iso(k - pd.Timedelta(minutes=15))
    assert res["verdict"]["verdict"] == "available"
    assert res["projection"]["cost_source"] == "measured"
    # Bodies are cached; a rerun costs nothing.
    assert len(list((tmp_path / ch.CACHE).glob("*.json"))) == 9
    calls2 = []
    res2 = ch.run(60, api_key=KEY, get=fake_api(calls2), raw_dir=tmp_path)
    assert calls2 == [] and res2["spent"] == 0
    assert res2["points"][0]["look"]["quoted"] and res2["points"][0]["look"]["cached"]
    assert res2["projection"]["cost_per_odds_call"] == 10  # from the cached cost


def test_cap_50_skips_the_close_when_every_point_is_priced(tmp_path):
    calls = []
    res = ch.run(50, api_key=KEY, get=fake_api(calls), raw_dir=tmp_path)
    assert res["spent"] == 44 and len(calls) == 8
    assert res["points"][0]["close"]["skipped"]
    assert "close call was skipped" in res["verdict"]["text"]


def test_cap_counts_x_requests_last_not_the_estimate(tmp_path):
    calls = []
    # Odds calls cost 3 by header: 4 + 12, then the close (estimate 10) fits under 30.
    res = ch.run(30, api_key=KEY, get=fake_api(calls, odds_cost=3), raw_dir=tmp_path)
    assert res["spent"] == 4 + 3 * 5
    assert res["projection"]["cost_per_odds_call"] == 3


def test_tight_cap_skips_and_never_passes_it(tmp_path):
    calls = []
    res = ch.run(12, api_key=KEY, get=fake_api(calls), raw_dir=tmp_path)
    assert res["spent"] <= 12
    assert res["points"][0]["look"]["quoted"]
    skipped = [p for p in res["points"] if p.get("look", {}).get("skipped")]
    assert skipped and res["verdict"]["verdict"] == "partly available"


def test_reserve_stops_calls(tmp_path):
    calls = []
    res = ch.run(50, api_key=KEY, get=fake_api(calls, left=3008), raw_dir=tmp_path)
    # The first events call goes (balance unknown); then 3,007 - 10 < 3,000 stops odds.
    assert all(not u.endswith("/odds") for u, _ in calls)
    assert res["verdict"]["verdict"] == "not tested"


def test_empty_snapshots_read_not_available(tmp_path):
    calls = []
    res = ch.run(50, api_key=KEY, get=fake_api(calls, quoted=False), raw_dir=tmp_path)
    assert res["spent"] == 4  # empty snapshots cost 0 here
    assert res["verdict"]["verdict"] == "not available"
    assert res["projection"]["cost_source"].startswith("assumed")
    assert "close" in res["points"][0]  # room left, so the close was tried


def test_key_never_reaches_output(tmp_path, capsys, monkeypatch):
    calls = []
    get = fake_api(calls, odds_status=422, message=f"bad request apiKey={KEY}")
    res = ch.run(50, api_key=KEY, get=get, raw_dir=tmp_path)
    text = json.dumps(res, default=str) + ch.report(res)
    assert KEY not in text and "***" in text
    for f in (tmp_path / ch.CACHE).glob("*.json"):
        assert KEY not in f.read_text()
    monkeypatch.setenv("ODDS_API_KEY", KEY)
    out_json = tmp_path / "o.json"
    ch.cli(50, str(out_json), get=get, raw_dir=tmp_path / "again")
    printed = capsys.readouterr().out
    assert KEY not in printed and KEY not in out_json.read_text()
    assert "CORNER_HISTORY_JSON " in printed


def test_network_error_logs_type_only(tmp_path):
    import requests

    def boom(url, params, timeout):
        raise requests.ConnectionError(f"https://x/?apiKey={params['apiKey']}")

    res = ch.run(50, api_key=KEY, get=boom, raw_dir=tmp_path)
    assert KEY not in json.dumps(res, default=str)
    assert any("ConnectionError" in x for x in res["log"])


def _write_season(tmp_path, league, start, kickoffs):
    rows = [
        {"Date": k.strftime("%d/%m/%Y"), "Time": k.strftime("%H:%M"), "HomeTeam": "A"}
        for k in kickoffs
    ]
    pd.DataFrame(rows).to_csv(ch.season_file(league, start, tmp_path), index=False)


def test_projection_arithmetic(tmp_path):
    days = pd.date_range("2024-08-17 15:00", periods=10, freq="D")
    ks = [d for d in days for _ in range(3)]  # 30 matches on 10 dates
    _write_season(tmp_path, "E0", 2024, ks)
    pr = ch.projection(10, tmp_path)
    e0 = pr["leagues"]["E0"]["seasons"]
    assert e0["2024/25"] == {
        "matches": 30,
        "matches_source": "football-data",
        "kickoff_dates": 10,
        "look": 30 * 10 + 10,
        "look_close": 2 * 30 * 10 + 2 * 10,
    }
    assert e0["2023/24"]["matches"] == 380 and e0["2023/24"]["matches_source"] == "standard"
    assert pr["leagues"]["F1"]["seasons"]["2025/26"]["matches"] == 306
    std = 380 * 10 + 114
    assert pr["leagues"]["E0"]["total"]["look"] == 2 * std + 310
    assert pr["all"]["look"] == sum(x["total"]["look"] for x in pr["leagues"].values())
    assert pr["by_season"]["2024/25"]["matches"] == 30 + 380 + 306
    assert pr["all"]["look_close"] == sum(v["look_close"] for v in pr["by_season"].values())


def test_target_kickoff_from_cached_file(tmp_path):
    ks = [pd.Timestamp("2026-02-06 20:00"), pd.Timestamp("2026-02-07 12:30")]
    _write_season(tmp_path, "E0", 2025, ks)
    k, src = ch.target_kickoff(ch.POINTS[0], tmp_path)
    assert src == "football-data" and k == pd.Timestamp("2026-02-07T12:30:00Z")  # GMT
    k, src = ch.target_kickoff(ch.POINTS[2], tmp_path)  # I1 not cached
    assert src == "fallback"


def test_corner_quotes_one_sided_and_other_books():
    body = {
        "data": {
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "markets": [
                        {
                            "key": ch.MARKET,
                            "outcomes": [
                                {"name": "Over", "description": "A", "point": 4.5, "price": 1.9},
                                {"name": "Over", "description": "B", "point": 3.5, "price": 1.9},
                                {"name": "Under", "description": "B", "point": 3.5, "price": 1.9},
                            ],
                        },
                        {"key": "h2h", "outcomes": [{"name": "A", "price": 2.0}]},
                    ],
                },
                {"key": "fanduel", "markets": corners()[0]["markets"]},
            ]
        }
    }
    q = ch.corner_quotes(body)
    assert q["quoted"] and not q["both_sides"]
    assert q["teams"]["A"] == {"lines": [4.5], "two_sided": 0, "one_sided": 1, "margins": []}
    assert q["teams"]["B"]["two_sided"] == 1
    assert set(q["teams"]) == {"A", "B"}  # fanduel's quotes are ignored
    assert ch.corner_quotes({"data": {}})["quoted"] is False
    assert ch.corner_quotes(None)["quoted"] is False


def test_pick_event_falls_back_to_window():
    snap = pd.Timestamp("2026-02-06T15:00:00Z")
    evs = [
        {"id": "a", "commence_time": "2026-02-07T20:00:00Z"},
        {"id": "b", "commence_time": "2026-02-09T20:00:00Z"},
    ]
    assert ch.pick_event(evs, pd.Timestamp("2026-02-07T15:00:00Z"), snap)["id"] == "a"
    assert ch.pick_event(evs[1:], pd.Timestamp("2026-02-07T15:00:00Z"), snap) is None
    assert ch.pick_event([], snap, snap) is None
