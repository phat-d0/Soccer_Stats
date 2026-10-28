"""DraftKings' historical coverage probe and backfill estimate (lead brief 2, item 3)."""

import pandas as pd

from soccer_stats import dk_history as dh


class Resp:
    def __init__(self, body, cost=10, left=4_000_000, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def fake_get(calls, dk_from_year=2021):
    """A snapshot with DraftKings and Pinnacle from `dk_from_year` on, Pinnacle alone before."""

    def get(url, params, timeout):
        calls.append((url, params))
        year = int(params["date"][:4])
        books = [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": []}]}]
        if year >= dk_from_year:
            books.append({"key": "draftkings", "markets": [{"key": "h2h", "outcomes": []}]})
        ev = {"id": "e", "home_team": "A", "away_team": "B", "bookmakers": books}
        return Resp({"timestamp": params["date"], "data": [ev, ev]})

    return get


def test_probe_day_is_a_november_saturday_morning():
    for y in (2019, 2020, 2024):
        t = dh.probe_time(y)
        assert t.dayofweek == 5 and t.month == 11 and t.day <= 7 and t.hour == 9


def test_the_probe_walks_back_and_stops_a_league_at_its_first_empty_season(tmp_path):
    calls = []
    res = dh.probe(600, ["E0", "SP1"], api_key="k", raw_dir=tmp_path, get=fake_get(calls))
    # 2024..2021 have DraftKings, 2020 doesn't: five calls per league.
    assert res["covered"] == {"E0": [2024, 2023, 2022, 2021], "SP1": [2024, 2023, 2022, 2021]}
    assert len(calls) == 10 and res["spent"] == 100
    assert calls[0][1]["bookmakers"] == ",".join(dh.BOOKMAKERS) and calls[0][1]["markets"] == "h2h"
    last = res["leagues"]["E0"][-1]
    assert (
        last["season"] == "2020/21" and last["draftkings"] == 0 and last["books"] == {"pinnacle": 2}
    )
    # A rerun buys nothing: every snapshot is cached.
    again = []
    res2 = dh.probe(600, ["E0", "SP1"], api_key="k", raw_dir=tmp_path, get=fake_get(again))
    assert again == [] and res2["covered"] == res["covered"]
    assert all(r["cached"] for r in res2["leagues"]["E0"])


def test_the_probe_stops_at_the_cap_and_never_caches_an_error(tmp_path):
    calls = []
    res = dh.probe(25, ["E0", "SP1"], api_key="k", raw_dir=tmp_path, get=fake_get(calls))
    assert len(calls) == 2 and res["spent"] == 20 and "cap" in res["stopped"]
    assert "SP1" not in res["leagues"] or res["leagues"]["SP1"] == []

    def broken(url, params, timeout):
        return Resp(None, cost=0, status=422)

    res = dh.probe(600, ["D1"], api_key="k", raw_dir=tmp_path, get=broken)
    assert res["covered"]["D1"] == [] and res["leagues"]["D1"][0].get("error")
    assert not list((tmp_path / "odds_history" / "D1").glob("*.json"))  # retried next time


def test_the_estimate_counts_shared_snapshots():
    k = pd.Timestamp("2024-11-02 15:00", tz="UTC")
    ks = [k, k, k + pd.Timedelta(minutes=10), k + pd.Timedelta(hours=2, minutes=30)]
    est = dh.estimate({("E0", 2024): ks})
    # Two kickoff groups (15:00-15:10 share; 17:30 apart), a 48 h look and a close each.
    r = est["rows"][0]
    assert r["kickoffs"] == 3 and r["snapshots"] == 4 and r["credits"] == 40
    assert est["total"] == {"snapshots": 4, "credits": 40}
    assert est["by_league"]["E0"]["seasons"] == 1
