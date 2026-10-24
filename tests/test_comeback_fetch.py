import json

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


def fake(balance=20000, cost=10, status=200):
    state = {"left": balance, "calls": 0}

    def get(url, params=None, timeout=None):
        if url.endswith("/sports"):
            return Resp([], headers={"x-requests-remaining": str(state["left"])})
        state["calls"] += 1
        state["left"] -= cost
        body = (
            {"timestamp": params["date"], "data": []}
            if status == 200
            else {"message": f"bad {KEY}"}
        )
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
