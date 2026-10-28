import json

import pandas as pd

from soccer_stats.lab import comeback_fetch as cf

KEY = "secret-key-xyz"


class Resp:
    def __init__(self, body, status=200, headers=None):
        self._body, self.status_code, self.headers = body, status, headers or {}
        self.ok = status == 200

    def json(self):
        return self._body


def slots(n, sport="soccer_epl"):
    return [
        {
            "league": "E0",
            "sport": sport,
            "kickoff": f"2024-09-{1 + i // 24:02d}T{i % 24:02d}:00Z",
            "request_at": f"2024-09-{1 + i // 24:02d}T{i % 24:02d}:55Z",
            "matches": [
                {"home": "A", "away": "B", "favourite": "home", "p_fav": 0.6, "ht": [0, 1]}
            ],
        }
        for i in range(n)
    ]


def priced_body(date):
    """A snapshot with one started event freshly priced by DraftKings (passes the guard)."""
    snap = pd.Timestamp(date)
    upd = (snap - pd.Timedelta(minutes=2)).isoformat()
    ev = {
        "commence_time": (snap - pd.Timedelta(minutes=55)).isoformat(),
        "bookmakers": [{"key": "draftkings", "markets": [{"key": "h2h", "last_update": upd}]}],
    }
    return {"timestamp": date, "data": [ev]}


def fake(balance=20000, cost=10, status=200):
    state = {"left": balance, "calls": 0}

    def get(url, params=None, timeout=None):
        if url.endswith("/sports"):
            return Resp([], headers={"x-requests-remaining": str(state["left"])})
        state["calls"] += 1
        state["left"] -= cost
        body = priced_body(params["date"]) if status == 200 else {"message": f"bad {KEY}"}
        return Resp(
            body,
            status,
            {"x-requests-remaining": str(state["left"]), "x-requests-last": str(cost)},
        )

    return get, state


def test_frozen_sample_is_design_b():
    s = cf.load_slots()
    assert len(s) == 192 and sum(len(x["matches"]) for x in s) == 210
    assert {x["league"] for x in s} == {"E0", "SP1", "D1", "I1", "F1"}


def test_dry_run_makes_no_call(tmp_path):
    get, state = fake()
    res = cf.run(cap=0, api_key=KEY, cache_dir=tmp_path, slots=slots(5), get=get)
    assert res["dry_run"] and state["calls"] == 0 and res["estimate"] == 50
    res = cf.run(cap=1920, api_key="", cache_dir=tmp_path, slots=slots(5), get=get)
    assert res["dry_run"] and state["calls"] == 0


def test_cap_stops_at_1920(tmp_path):
    get, state = fake()
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(200), get=get)
    assert res["spent"] == 1920 and res["bought"] == 192 and state["calls"] == 192
    assert "cap" in res["stopped"]


def test_reserve_holds(tmp_path):
    get, state = fake(balance=3015)
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(5), get=get)
    assert state["calls"] == 0 and res["bought"] == 0 and "reserve" in res["stopped"]
    get, state = fake(balance=3030)
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(5), get=get)
    assert res["left_after"] >= 3000 and state["calls"] == 3


def test_no_slot_is_bought_twice(tmp_path):
    get, state = fake()
    cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(4), get=get)
    assert state["calls"] == 4
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(4), get=get)
    assert state["calls"] == 4 and res["pending"] == 0 and res["held"] == 4
    # an answered call in the record also counts, even if its body went missing
    (tmp_path / cf.body_path(tmp_path, slots(1)[0]).name).unlink()
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(4), get=get)
    assert state["calls"] == 4 and res["pending"] == 0


def test_error_stops_every_call_and_key_never_shows(tmp_path, capsys):
    get, state = fake(status=401)
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(5), get=get)
    assert state["calls"] == 1 and res["bought"] == 0 and res["stopped"]
    text = cf.report(res) + json.dumps(res) + (tmp_path / cf.CALLS).read_text()
    assert KEY not in text


def test_a_dear_call_stops_the_run(tmp_path):
    get, state = fake(cost=30)
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(5), get=get)
    assert state["calls"] == 1 and "cost 30" in res["stopped"]


def lower_slots(n, league="E2", sport="soccer_england_league1"):
    return [
        {
            **s,
            "league": league,
            "sport": sport,
            "kickoff": s["kickoff"],
            "request_at": s["request_at"],
        }
        for s in slots(n, sport=sport)
    ]


def priced_get(priced):
    """Snapshots whose one started event has a DraftKings quote that is fresh or stale;
    `priced` is a bool or a function of the snapshot time."""
    state = {"calls": 0, "left": 20000}

    def get(url, params=None, timeout=None):
        if url.endswith("/sports"):
            return Resp([], headers={"x-requests-remaining": str(state["left"])})
        state["calls"] += 1
        state["left"] -= 10
        snap = pd.Timestamp(params["date"])
        fresh = priced(snap) if callable(priced) else priced
        upd = snap - pd.Timedelta(minutes=2 if fresh else 50)
        ev = {
            "commence_time": (snap - pd.Timedelta(minutes=55)).isoformat(),
            "bookmakers": [
                {"key": "draftkings", "markets": [{"key": "h2h", "last_update": upd.isoformat()}]}
            ],
        }
        return Resp(
            {"timestamp": params["date"], "data": [ev]},
            200,
            {"x-requests-remaining": str(state["left"]), "x-requests-last": "10"},
        )

    return get, state


def test_guard_stops_an_unpriced_lower_league_after_three(tmp_path):
    get, state = priced_get(priced=False)
    s = lower_slots(6) + slots(2)  # E2 slots plus two top-five ones, interleaved in time
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=s, get=get)
    assert res["not_priced"] == {"E2 2024/25": 3}
    assert state["calls"] == 3 + 2  # three E2 snapshots, then only the top-five slots
    assert cf.read_guard(tmp_path)["E2 2024/25"]["blocked"]
    # a restart reaches the same decision from the held bodies and buys nothing new
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=s, get=get)
    assert state["calls"] == 5 and res["not_priced"] == {"E2 2024/25": 3}


def test_guard_lets_a_priced_lower_league_through(tmp_path):
    get, state = priced_get(priced=True)
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=lower_slots(6), get=get)
    assert state["calls"] == 6 and res["not_priced"] == {} and cf.read_guard(tmp_path) == {}


def season_slots(year, n):
    return [
        {
            **s,
            "kickoff": s["kickoff"].replace("2024-09", f"{year}-09"),
            "request_at": s["request_at"].replace("2024-09", f"{year}-09"),
        }
        for s in slots(n)
    ]


def test_guard_is_per_league_season_in_the_top_five_too(tmp_path):
    # E0 2022/23 carries no in-play prices, E0 2023/24 does: only the first season stops.
    get, state = priced_get(priced=lambda snap: snap.year >= 2023)
    s = season_slots(2022, 6) + season_slots(2023, 4)
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=s, get=get)
    assert res["not_priced"] == {"E0 2022/23": 3}
    assert state["calls"] == 3 + 4
    assert set(cf.read_guard(tmp_path)) == {"E0 2022/23"}
    assert cf.guard_key(s[0]) == "E0 2022/23" and cf.guard_key(s[-1]) == "E0 2023/24"


def test_design_c_is_c_minus_b():
    c = cf.load_slots(cf.slots_path("C"))
    b = cf.load_slots(cf.slots_path("B"))
    assert len(c) == 709 and sum(len(s["matches"]) for s in c) == 902
    assert not {(s["league"], s["kickoff"]) for s in c} & {(s["league"], s["kickoff"]) for s in b}
    assert [s["request_at"] for s in c] == sorted(s["request_at"] for s in c)
    assert len(c) * cf.HIST_COST == 7090


def test_a_time_budget_stops_cleanly_and_the_next_run_resumes(tmp_path, monkeypatch):
    get, state = fake()
    clock = iter(range(0, 10_000, 30))  # each check of the clock moves 30 seconds on
    monkeypatch.setattr(cf.time, "monotonic", lambda: next(clock))
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(10), get=get, minutes=2)
    assert res["out_of_time"] and 0 < res["bought"] < 10 and "time budget" in res["stopped"]
    first = res["bought"]
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(10), get=get)
    assert res["held"] == first and res["bought"] == 10 - first and state["calls"] == 10


def test_a_pre_match_plan_turns_the_guard_off(tmp_path):
    get, state = fake()

    def empty_get(url, params=None, timeout=None):
        r = get(url, params, timeout)
        if not url.endswith("/sports"):
            r._body = {"timestamp": params["date"], "data": []}  # nothing started
        return r

    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path, slots=slots(6), get=empty_get,
                 guard_on=False)  # fmt: skip
    assert res["bought"] == 6 and not res["not_priced"]
    res = cf.run(cap=1920, api_key=KEY, cache_dir=tmp_path / "g", slots=slots(6), get=empty_get)
    assert res["bought"] == 3 and res["not_priced"]


def test_an_unknown_sport_key_counts_as_not_priced_without_a_call(tmp_path):
    get, state = fake()

    def with_list(url, params=None, timeout=None):
        if url.endswith("/sports") and (params or {}).get("all") == "true":
            return Resp([{"key": "soccer_epl"}])
        return get(url, params, timeout)

    s = slots(2) + slots(1, sport="soccer_romania_liga_1")
    s[-1] = {**s[-1], "league": "ROU", "request_at": "2024-09-01T05:55Z"}
    res = cf.run(cap=100, api_key=KEY, cache_dir=tmp_path, slots=s, get=with_list)
    assert res["bought"] == 2 and state["calls"] == 2 and res["stopped"] is None
    assert res["not_priced"] == {"ROU 2024/25": 1}
    assert cf.read_guard(tmp_path)["ROU 2024/25"]["blocked"] is True
