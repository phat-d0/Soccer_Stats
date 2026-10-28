import json
from datetime import UTC, datetime

from soccer_stats.lab.inplay_probe import (
    _minute,
    match_espn,
    match_minute,
    run_historical,
    score_at,
    summarise,
)


class _Resp:
    def __init__(self, code, body, headers):
        self.status_code, self._body, self.headers = code, body, headers

    def json(self):
        return self._body


SNAP = {
    "timestamp": "2026-10-10T14:50:36Z",
    "data": [
        {
            "commence_time": "2026-10-10T14:00:00Z",
            "home_team": "Aston Villa",
            "away_team": "Brentford",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-10-10T14:50:20Z",
                            "outcomes": [
                                {"name": "Aston Villa", "price": 3.1},
                                {"name": "Draw", "price": 2.79},
                                {"name": "Brentford", "price": 2.7},
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}
ESPN = {
    "events": [
        {
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "2",
                            "team": {"id": "1", "displayName": "Aston Villa"},
                        },
                        {
                            "homeAway": "away",
                            "score": "2",
                            "team": {"id": "2", "displayName": "Brentford"},
                        },
                    ],
                    "details": [
                        {
                            "scoringPlay": True,
                            "team": {"id": "2"},
                            "clock": {"displayValue": "10'"},
                        },
                        {
                            "scoringPlay": True,
                            "team": {"id": "1"},
                            "clock": {"displayValue": "45'+2'"},
                        },
                        {
                            "scoringPlay": True,
                            "team": {"id": "1"},
                            "clock": {"displayValue": "77'"},
                        },
                    ],
                }
            ],
            "status": {"displayClock": "90'", "type": {"state": "post", "shortDetail": "FT"}},
        }
    ]
}


def _get(calls):
    def get(url, params=None, timeout=None):
        calls.append(url)
        if "historical" in url:
            return _Resp(200, SNAP, {"x-requests-last": "10", "x-requests-remaining": "19114"})
        if "espn" in url:
            return _Resp(200, ESPN, {})
        return _Resp(200, [], {"x-requests-last": "0", "x-requests-remaining": "19124"})

    return get


def test_minutes_and_score_at_half_time():
    assert _minute("45'+2'") == 45 and _minute("10'") == 10 and _minute("") is None
    assert match_minute(30) == 30 and match_minute(51) == 45 and match_minute(80) == 63
    goals = [("away", 10), ("home", 45), ("home", 77)]
    assert score_at(goals, 45) == "1-1" and score_at(goals, None) is None


def test_historical_snapshot_reports_in_play_prices_and_score_then(tmp_path):
    calls = []
    res = run_historical("E0", "2026-10-10T14:55:00Z", 10, "key", _get(calls), raw_dir=tmp_path)
    assert res["spent"] == 10 and res["credits_after"] == 19114
    m = res["matches"][0]
    assert m["started"] and m["minutes_since_kickoff"] == 51
    assert m["score_then"] == "1-1" and m["final"] == "2-2"
    assert m["books"][0]["book"] == "pinnacle" and m["books"][0]["age_min"] == 0.3
    assert "1 of 1 started matches came back with h2h prices" in res["verdict"]


def test_cap_and_reserve_stop_the_paid_call(tmp_path):
    calls = []
    res = run_historical("E0", "2026-10-10T14:55:00Z", 9, "key", _get(calls), raw_dir=tmp_path)
    assert res["spent"] == 0 and "stop" in res
    assert not any("historical" in u for u in calls)
    no_key = run_historical("E0", "2026-10-10T14:55:00Z", 10, "", _get([]), raw_dir=tmp_path)
    assert no_key["stop"] == "no ODDS_API_KEY"
    assert not list(tmp_path.rglob("*.json"))  # nothing bought, nothing kept


def test_a_bought_snapshot_is_kept_and_never_bought_again(tmp_path):
    calls = []
    first = run_historical("E0", "2026-10-10T14:55:00Z", 10, "key", _get(calls), raw_dir=tmp_path)
    kept = tmp_path / "inplay_probe" / "E0" / "hist_20261010T145500Z.json"
    assert first["kept"] == kept.as_posix() and json.loads(kept.read_text()) == SNAP
    assert sum("historical" in u for u in calls) == 1
    again = run_historical("E0", "2026-10-10T14:55:00Z", 10, "key", _get(calls), raw_dir=tmp_path)
    assert sum("historical" in u for u in calls) == 1 and again["spent"] == 0
    assert again["matches"] == first["matches"]


def test_a_live_answer_is_kept(tmp_path, monkeypatch):
    import soccer_stats.lab.inplay_probe as ip

    monkeypatch.setattr(ip, "_now", lambda: datetime(2026, 10, 10, 14, 50, tzinfo=UTC))
    live = SNAP["data"]

    def get(url, params=None, timeout=None):
        if "espn" in url:
            return _Resp(200, ESPN, {})
        if url.endswith("/events"):
            return _Resp(200, live, {"x-requests-last": "0", "x-requests-remaining": "9000"})
        return _Resp(200, live, {"x-requests-last": "1", "x-requests-remaining": "8999"})

    res = ip.run("E0", 1, "key", get, raw_dir=tmp_path)
    kept = tmp_path / "inplay_probe" / "E0" / "live_20261010T145000Z.json"
    assert res["spent"] == 1 and res["kept"] == kept.as_posix()
    assert json.loads(kept.read_text()) == live


def test_summarise_and_name_match():
    now = datetime(2026, 10, 10, 14, 50, tzinfo=UTC)
    rows = summarise(SNAP["data"], now, [{"home": "Aston Villa", "away": "Brentford"}])
    assert rows[0]["espn"] and rows[0]["books"][0]["margin"] == round(
        1 / 3.1 + 1 / 2.79 + 1 / 2.7 - 1, 4
    )
    assert match_espn(
        "Brighton and Hove Albion", "Fulham", [{"home": "Brighton & Hove Albion", "away": "Fulham"}]
    )
