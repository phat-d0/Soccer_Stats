"""Priced goalscorer test, steps 1 and 3: the capped historical backfill (no network)."""

import pandas as pd

from soccer_stats import player_goal_backfill as gb

NOW = pd.Timestamp("2026-11-01T12:00:00Z")


class Resp:
    def __init__(self, body, cost, status=200):
        self.body, self.status_code, self.ok = body, status, status < 400
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": "4000000"}

    def json(self):
        return self.body


def kickoffs(league, season):
    if season == 2025:
        return pd.Series(
            pd.to_datetime(["2025-11-01T15:00Z", "2025-11-01T17:30Z", "2025-11-08T15:00Z"])
        )
    return pd.Series(
        pd.to_datetime(["2026-10-04T15:00Z", "2026-10-31T15:00Z", "2026-11-01T11:00Z"])
    )


def fake_api(calls, status_for=None, cost=10):
    def get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        assert "apiKey" in params
        if url.endswith("/sports"):
            return Resp([], 0)
        if url.endswith("/events"):
            day = params["date"][:10]
            evs = [
                {
                    "id": f"{day}a",
                    "home_team": "A",
                    "away_team": "B",
                    "commence_time": f"{day}T15:00:00Z",
                },
                {
                    "id": f"{day}b",
                    "home_team": "C",
                    "away_team": "D",
                    "commence_time": f"{day}T17:30:00Z",
                },
                {
                    "id": "next",
                    "home_team": "E",
                    "away_team": "F",
                    "commence_time": "2030-01-01T15:00:00Z",
                },
            ]
            return Resp({"timestamp": params["date"], "data": evs}, 1)
        eid = url.split("/")[-2]
        if status_for and eid in status_for:
            return Resp({"message": "nope"}, 0, status_for[eid])
        assert params["bookmakers"] == ",".join(gb.BOOKS) and params["markets"] == gb.MARKET
        book = {
            "key": "draftkings",
            "markets": [{"key": gb.MARKET, "outcomes": [{"name": "X", "price": 5.0}]}],
        }
        return Resp({"data": {"id": eid, "bookmakers": [book]}}, cost)

    return get


def test_dry_run_calls_nothing(tmp_path):
    calls = []
    res = gb.run(
        "dev",
        0,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api(calls),
        kickoffs=kickoffs,
    )
    assert calls == [] and res["dry_run"] and res["spent"] == 0
    assert res["matches_football_data"] == 3 and res["days"] == 2
    assert res["estimate"] == 2 * 1 + 3 * 2 * 10


def test_paid_run_buys_each_snapshot_once_and_resumes(tmp_path):
    calls = []
    res = gb.run(
        "dev",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0", "SP1"),
        get=fake_api(calls),
        kickoffs=kickoffs,
    )
    # Two leagues x two days: 4 lists (1 each) and 2 events a day x 2 snapshots x 10.
    assert res["spent"] == 4 * 1 + 4 * 2 * 2 * 10 and res["stopped"] is None
    assert res["bought"] == {"lists": 4, "snapshots": 16}
    e0 = res["leagues"]["E0"]
    assert e0["events"] == 4 and e0["entry"] == e0["close"] == e0["close_priced"] == 4
    entry = [p for u, p in calls if u.endswith("2025-11-01a/odds") and "/soccer_epl/" in u]
    assert sorted(p["date"] for p in entry) == ["2025-11-01T14:10:00Z", "2025-11-01T14:58:00Z"]
    lists = [p for u, p in calls if u.endswith("/events")]
    assert lists[0]["date"] == "2025-11-01T12:00:00Z"  # 3 h before the day's first kickoff
    assert not any("2030" in u for u, _ in calls)  # only the day's events
    again = []
    res2 = gb.run(
        "dev",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0", "SP1"),
        get=fake_api(again),
        kickoffs=kickoffs,
    )
    assert [u for u, _ in again if not u.endswith("/sports")] == [] and res2["spent"] == 0
    assert res2["leagues"]["E0"]["close"] == 4


def test_not_listed_event_is_recorded_and_the_run_goes_on(tmp_path):
    calls = []
    res = gb.run(
        "dev",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api(calls, {"2025-11-01a": 404}),
        kickoffs=kickoffs,
    )
    assert res["stopped"] is None and res["leagues"]["E0"]["entry"] == 3
    again = []
    gb.run(
        "dev",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api(again),
        kickoffs=kickoffs,
    )
    assert not any("2025-11-01a" in u for u, _ in again)  # a recorded 404 is never asked again


def test_rate_limit_or_dear_call_stops_the_run(tmp_path):
    res = gb.run(
        "dev",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api([], {"2025-11-01a": 429}),
        kickoffs=kickoffs,
    )
    assert res["stopped"] and res["leagues"]["E0"]["entry"] == 0
    res = gb.run(
        "dev",
        500,
        api_key="k",
        raw_dir=tmp_path / "b",
        leagues=("E0",),
        get=fake_api([], cost=20),
        kickoffs=kickoffs,
    )
    assert "cost 20" in res["stopped"] and res["bought"]["snapshots"] == 1


def test_cap_is_never_passed(tmp_path):
    res = gb.run(
        "dev",
        25,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api([]),
        kickoffs=kickoffs,
    )
    assert res["spent"] <= 25 and "cap" in res["stopped"]


def test_forward_takes_only_matches_from_10_oct_that_have_kicked_off(tmp_path):
    calls = []
    res = gb.run(
        "forward",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api(calls),
        now=NOW,
        kickoffs=kickoffs,
    )
    days = sorted({p["date"][:10] for u, p in calls if u.endswith("/events")})
    assert days == ["2026-10-31"]  # 4 Oct is before the window; 1 Nov 11:00 not 3 h ago
    assert res["leagues"]["E0"]["events"] == 2
    assert gb.summary_lines(res)[0].startswith("Goalscorer backfill (forward")


def gap_kickoffs(league, season):
    return pd.Series(
        pd.to_datetime(
            [
                "2026-10-31T15:00Z",
                "2026-11-01T15:00Z",
                "2026-11-01T17:30Z",
                "2026-11-02T15:00Z",
                "2026-11-02T17:30Z",
            ]  # fmt: skip
        )
    )


def live(eid, kind, status=200, day="2026-11-02", at="15:00"):
    return {"league": "E0", "event_id": eid, "home": "A", "away": "B", "snapshot": kind,
            "kickoff": f"{day}T{at}:00+00:00", "status": status}  # fmt: skip


def test_gaps_buys_only_the_snapshots_the_live_log_lacks(tmp_path):
    calls = []
    record = [
        live("2026-11-02a", "entry"),
        live("2026-11-02a", "close"),
        live("2026-11-02b", "entry", 502, at="17:30"),  # failed: still a gap
        live("2026-11-02b", "close", at="17:30"),
    ]
    res = gb.run(
        "gaps",
        500,
        api_key="k",
        raw_dir=tmp_path,
        leagues=("E0",),
        get=fake_api(calls),
        now=pd.Timestamp("2026-11-03T12:00:00Z"),
        kickoffs=gap_kickoffs,
        live_calls=record,
    )
    paid = [u for u, _ in calls if not u.endswith("/sports")]
    # 31 Oct is before the live log (step 1 holds it); 1 Nov has no live call: its list and
    # both snapshots of both matches; 2 Nov: no list (the live record has both matches),
    # only 2 Nov b's entry.
    assert sum(u.endswith("/events") for u in paid) == 1
    odds = sorted(u.split("/")[-2] for u in paid if u.endswith("/odds"))
    assert odds == ["2026-11-01a", "2026-11-01a", "2026-11-01b", "2026-11-01b", "2026-11-02b"]
    assert res["spent"] == 1 + 5 * 10 and res["stopped"] is None
    e0 = res["leagues"]["E0"]
    assert e0["live_held"] == 3 and e0["gaps"] == 5 and e0["lists_from_live"] == 1
    assert any("2026-11-02T17:30 A v B: entry" in g for g in res["gap_list"])
    lines = gb.summary_lines(res)
    assert any("history asked for 5" in x for x in lines)


def test_gaps_dry_run_estimates_and_needs_the_live_record(tmp_path):
    record = [live("2026-11-02a", "entry"), live("2026-11-02a", "close"),
              live("2026-11-02b", "close", at="17:30")]  # fmt: skip
    res = gb.run("gaps", 0, api_key="k", raw_dir=tmp_path, leagues=("E0",),
                 get=fake_api([]), now=pd.Timestamp("2026-11-03T12:00:00Z"),
                 kickoffs=gap_kickoffs, live_calls=record)  # fmt: skip
    assert res["dry_run"] and res["estimate"] == 1 + 2 * 2 * 10 + 1 * 10
    import pytest

    with pytest.raises(ValueError):
        gb.run("gaps", 0, raw_dir=tmp_path, leagues=("E0",), kickoffs=gap_kickoffs)
