import pandas as pd

from soccer_stats.lab import inplay_probe as ip

KEY = "secret-key-123"


class Resp:
    def __init__(self, body, status=200, headers=None):
        self._body, self.status_code = body, status
        self.ok = status == 200
        self.headers = headers or {}

    def json(self):
        return self._body


def body(update):
    ev = lambda home, ko, upd: {  # noqa: E731
        "home_team": home,
        "away_team": "B",
        "commence_time": ko,
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": upd,
                        "outcomes": [{"name": home, "price": 3.1}, {"name": "B", "price": 1.6}],
                    }
                ],
            }
        ],
    }
    return {
        "timestamp": "2025-05-25T15:55:00Z",
        "data": [
            ev("A", "2025-05-25T15:00:00Z", update),
            ev("C", "2025-05-26T19:00:00Z", "2025-05-25T15:50:00Z"),
        ],
    }


def fake_get(b):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/sports"):
            return Resp([], headers={"x-requests-remaining": "20000"})
        return Resp(b, headers={"x-requests-remaining": "19990", "x-requests-last": "10"})

    return get, calls


def test_fresh_quote_after_kickoff_is_in_play():
    get, calls = fake_get(body("2025-05-25T15:53:00Z"))
    res = ip.run(cap=10, api_key=KEY, get=get)
    assert res["started"] == 1 and res["started_with_fresh_quotes"] == 1
    assert res["books_quoting_in_play"] == ["pinnacle"]
    assert res["spent"] == 10
    assert KEY not in ip.report(res) and KEY not in str(res)


def test_quote_frozen_at_kickoff_is_not_in_play():
    get, _ = fake_get(body("2025-05-25T14:58:00Z"))
    res = ip.run(cap=10, api_key=KEY, get=get)
    assert res["started"] == 1 and res["started_with_fresh_quotes"] == 0
    assert "no in-play prices" in res["verdict"]


def test_cap_zero_makes_no_call():
    get, calls = fake_get(body("2025-05-25T15:53:00Z"))
    res = ip.run(cap=0, api_key=KEY, get=get)
    assert res["dry_run"] and calls == []


def test_cap_below_cost_skips_the_snapshot():
    get, calls = fake_get(body("2025-05-25T15:53:00Z"))
    res = ip.run(cap=5, api_key=KEY, get=get)
    assert res["spent"] == 0 and "verdict" not in res
    assert all(c.endswith("/sports") for c in calls)


def test_default_moment_is_the_final_day_half_time():
    assert pd.Timestamp(ip.AT) == pd.Timestamp("2025-05-25T15:55:00Z")
