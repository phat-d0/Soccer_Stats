import json

import pandas as pd

from soccer_stats.lab import archive_probe as ap
from soccer_stats.lab import comeback_plan as cp


def season_csv(tmp_path, name="N1.csv", year=2025):
    rows = []
    sat, sun, wed = (f"0{d}/11/{year}" for d in (1, 2, 5))
    if year != 2025:  # another season: another Saturday
        sat, sun, wed = (f"0{d}/11/{year}" for d in (6, 7, 9))
    for home, away, date, time in [
        ("A", "B", sat, "15:00"),  # Saturday: three at 15:00
        ("C", "D", sat, "15:00"),
        ("E", "F", sat, "15:00"),
        ("G", "H", sun, "13:30"),
        ("I", "J", wed, "20:00"),  # midweek, ignored
    ]:
        rows.append({"Date": date, "Time": time, "HomeTeam": home, "AwayTeam": away,
                     "FTHG": 1, "FTAG": 0, "HTHG": 0, "HTAG": 0})  # fmt: skip
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_pick_slot_takes_the_busiest_weekend_kickoff(tmp_path, monkeypatch):
    path = season_csv(tmp_path)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    s = ap.pick_slot("N1", 2025, tmp_path)
    assert s["kickoff"] == "2025-11-01T15:00Z" and s["request_at"] == "2025-11-01T15:55Z"
    assert s["matches_in_slot"] == 3 and s["sport"] == "soccer_netherlands_eredivisie"
    assert s["season"] == "2025/26"


def body(priced: bool, ts="2025-11-01T15:55:00Z"):
    upd = "2025-11-01T15:53:00Z" if priced else "2025-11-01T14:50:00Z"
    return {
        "timestamp": ts,
        "data": [
            {
                "commence_time": "2025-11-01T15:00:00Z",
                "bookmakers": [
                    {"key": "draftkings", "markets": [{"key": "h2h", "last_update": upd}]},
                    {"key": "pinnacle", "markets": [{"key": "h2h", "last_update": upd}]},
                ],
            },
            {"commence_time": "2025-11-01T18:00:00Z", "bookmakers": []},
        ],
    }


def test_books_seen_counts_fresh_in_play_quotes():
    assert ap.books_seen(body(True)) == {"started": 1, "books": {"draftkings": 1, "pinnacle": 1}}
    assert ap.books_seen(body(False)) == {"started": 1, "books": {}}


class Resp:
    def __init__(self, payload, headers=None, ok=True):
        self._p, self.headers, self.ok, self.status_code = payload, headers or {}, ok, 200

    def json(self):
        return self._p


def fake_get(bodies, calls):
    left = {"n": 5000}

    def get(url, params=None, timeout=None):
        if url.endswith("/sports"):
            return Resp([{"key": "soccer_netherlands_eredivisie"}],
                        {"x-requests-remaining": str(left["n"])})  # fmt: skip
        calls.append(params["date"])
        left["n"] -= 10
        b = bodies.pop(0)
        return Resp(b, {"x-requests-last": "10", "x-requests-remaining": str(left["n"])})

    return get


def test_run_walks_back_and_stops_at_the_first_unpriced_season(tmp_path, monkeypatch):
    paths = {y: season_csv(tmp_path, f"N1_{y}.csv", y) for y in (2020, 2021, 2022)}
    monkeypatch.setattr(cp, "download", lambda lg, y, *a, **k: paths[y])
    calls = []
    get = fake_get([body(True), body(False), body(True)], calls)
    res = ap.run(cap=100, api_key="k", cache_dir=tmp_path / "c", chains={"N1": (2022, 2021, 2020)},
                 get=get, raw_dir=tmp_path)  # fmt: skip
    assert [p["priced"] for p in res["probes"]] == [True, False]
    assert len(calls) == 2 and res["spent"] == 20
    assert res["left_before"] == 5000 and res["left_after"] == 4980
    # bodies are cached: a second run buys nothing for the seasons already held
    calls2 = []
    res2 = ap.run(cap=100, api_key="k", cache_dir=tmp_path / "c", chains={"N1": (2022, 2021)},
                  get=fake_get([], calls2), raw_dir=tmp_path)  # fmt: skip
    assert calls2 == [] and [p["cached"] for p in res2["probes"]] == [True, True]


def test_dry_run_and_unknown_sport_buy_nothing(tmp_path, monkeypatch):
    path = season_csv(tmp_path)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    res = ap.run(cap=0, api_key="", cache_dir=tmp_path / "c", chains={"N1": (2022, 2021)},
                 raw_dir=tmp_path)  # fmt: skip
    assert res["dry_run"] and len(res["probes"]) == 1 and res["probes"][0]["dry_run"]
    calls = []
    res = ap.run(cap=100, api_key="k", cache_dir=tmp_path / "c", chains={"P1": (2022,)},
                 get=fake_get([], calls), raw_dir=tmp_path)  # fmt: skip
    assert calls == [] and "not listed" in res["probes"][0]["error"]
    assert "k" not in json.dumps(res["log"])


def test_slots_from_more_build_newest_first_chains(tmp_path):
    more = {
        "cups": [
            {"cup": "FAC", "season": "2022/23", "sport": "soccer_fa_cup",
             "probe_kickoff": "2023-01-07T15:00Z"},
            {"cup": "FAC", "season": "2023/24", "sport": "soccer_fa_cup",
             "probe_kickoff": "2024-01-06T15:00Z"},
            {"cup": "CDR", "season": "2022/23", "error": "time"},
        ],
        "extra": [{"league": "AUT", "season": "2023/2024", "sport": "soccer_austria_bundesliga",
                   "probe_kickoff": "2023-10-01T13:00Z"}],
    }  # fmt: skip
    path = tmp_path / "more.json"
    path.write_text(json.dumps(more))
    chains = ap.slots_from_more(path)
    assert list(chains) == ["FAC", "AUT"]
    assert [s["season"] for s in chains["FAC"]] == ["2023/24", "2022/23"]
    assert chains["FAC"][0]["request_at"] == "2024-01-06T15:55Z"
