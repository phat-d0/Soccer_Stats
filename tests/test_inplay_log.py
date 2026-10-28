"""Half-time in-play snapshots (lead brief 2, 28 Oct): what is due, never twice, the
reserve and the cap, and the rows logged."""

import json

import pandas as pd

from soccer_stats import inplay_log as il


def T(s):
    return pd.Timestamp(s, tz="UTC")


KO = "2026-10-31T15:00:00+00:00"


def dk_log(d, league="E0", kickoff=KO, home="Arsenal", away="Chelsea"):
    d.mkdir(parents=True, exist_ok=True)
    row = {"league": league, "home": home, "away": away, "kickoff": kickoff, "market": "h2h"}
    with open(d / f"{league}_2026-10.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


class Resp:
    def __init__(self, code, body, headers):
        self.status_code, self._body, self.headers = code, body, headers

    def json(self):
        return self._body


def event(kickoff=KO, home="Arsenal", away="Chelsea", books=("draftkings", "pinnacle")):
    return {
        "commence_time": kickoff.replace("+00:00", "Z"),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": b,
                "last_update": "2026-10-31T15:57:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-10-31T15:57:00Z",
                        "outcomes": [
                            {"name": home, "price": 1.5},
                            {"name": "Draw", "price": 4.2},
                            {"name": away, "price": 7.0},
                        ],
                    }
                ],
            }
            for b in books
        ],
    }


def fake_get(calls, left=9000, events=None):
    events = [event()] if events is None else events

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/events"):
            return Resp(200, events, {"x-requests-last": "0", "x-requests-remaining": str(left)})
        assert "draftkings,pinnacle" in params["bookmakers"] and params["markets"] == "h2h"
        return Resp(200, events, {"x-requests-last": "1", "x-requests-remaining": str(left - 1)})

    return get


def espn(_league):
    return [{"home": "Arsenal", "away": "Chelsea", "score": "0-1", "clock": "HT", "state": "in",
             "detail": "HT", "goals": [("away", 30)]}]  # fmt: skip


def test_windows_around_55_and_75_minutes():
    assert il.kind_of(44) is None and il.kind_of(45) == "inplay55" and il.kind_of(64) == "inplay55"
    assert il.kind_of(65) == "inplay75" and il.kind_of(89) == "inplay75" and il.kind_of(90) is None
    assert il.nearest_kind(58) == "inplay55" and il.nearest_kind(80) == "inplay75"


def test_due_reads_only_the_draftkings_log_and_skips_what_the_call_record_holds(tmp_path):
    dk_log(tmp_path)
    dk_log(tmp_path, "SP1", "2026-10-31T15:05:00+00:00", "Betis", "Sevilla")
    # Other logs in odds_log carry kickoffs too: never read as a league's own.
    (tmp_path / "E0_corners_2026-10.jsonl").write_text(json.dumps({"kickoff": "2026-10-31T14:30Z"}))
    ks = il.kickoffs(tmp_path)
    assert ks == {"E0": [T(KO)], "SP1": [T("2026-10-31 15:05")]}
    now = T("2026-10-31 15:58")
    d = il.due(ks, [], now)
    assert d == {
        "E0": {"kinds": ["inplay55"], "kickoffs": [T(KO)]},
        "SP1": {"kinds": ["inplay55"], "kickoffs": [T("2026-10-31 15:05")]},
    }
    record = [{"league": "E0", "kinds": ["inplay55"], "kickoffs": [KO]}]
    assert set(il.due(ks, record, now)) == {"SP1"}
    assert il.due(ks, record, T("2026-10-31 16:18"))["E0"]["kinds"] == ["inplay75"]
    assert il.due(ks, [], T("2026-10-31 15:30")) == {}  # first half: nothing due


def test_a_run_logs_every_book_with_espns_score_and_never_calls_twice(tmp_path):
    log, out = tmp_path / "log", tmp_path / "out"
    dk_log(log)
    calls = []
    now = T("2026-10-31 15:58")
    res = il.run(log, "key", now=now, get=fake_get(calls), espn=espn, out_dir=out)
    assert res["calls"] == 1 and res["credits"] == 1 and res["rows"] == 2
    assert res["credits_left"] == 8999
    rows = [json.loads(x) for x in (out / "E0_inplay_2026-10.jsonl").read_text().splitlines()]
    assert {r["book"] for r in rows} == {"draftkings", "pinnacle"}
    r = rows[0]
    assert (r["home"], r["away"], r["kind"], r["minutes_since_kickoff"]) == (
        "Arsenal",
        "Chelsea",
        "inplay55",
        58.0,
    )
    assert r["prices"] == {"home": 1.5, "draw": 4.2, "away": 7.0} and r["age_min"] == 1.0
    assert r["espn_score"] == "0-1" and r["espn_clock"] == "HT" and r["espn_state"] == "in"
    rec = json.loads((out / "inplay_calls_2026-10.jsonl").read_text())
    assert rec["league"] == "E0" and rec["kinds"] == ["inplay55"] and rec["cost"] == 1
    assert rec["kickoffs"] == [KO] and rec["rows"] == 2
    # The record joins data-log: the next run at the same minute buys nothing.
    (log / "inplay_calls_2026-10.jsonl").write_text(
        (out / "inplay_calls_2026-10.jsonl").read_text()
    )
    again = il.run(log, "key", now=now + pd.Timedelta(minutes=2), get=fake_get(calls), espn=espn)
    assert again["calls"] == 0 and again["stop"] == "nothing due"
    assert sum(not u.endswith("/events") for u in calls) == 1


def test_the_reserve_the_cap_and_no_key_stop_the_call(tmp_path):
    dk_log(tmp_path)
    now = T("2026-10-31 15:58")
    calls = []
    res = il.run(tmp_path, "key", now=now, get=fake_get(calls, left=il.RESERVE), espn=espn)
    assert res["calls"] == 0 and "reserve" in res["stop"]
    assert all(u.endswith("/events") for u in calls)
    spent = [{"league": "SP1", "fetched_at": "2026-10-03T12:00:00+00:00", "cost": il.MONTHLY_CAP}]
    (tmp_path / "inplay_calls_2026-10.jsonl").write_text(json.dumps(spent[0]) + "\n")
    res = il.run(tmp_path, "key", now=now, get=fake_get(calls), espn=espn)
    assert res["calls"] == 0 and "monthly cap" in res["stop"]
    assert il.run(tmp_path, "", now=now, get=fake_get([]), espn=espn)["stop"].startswith("no ODDS")


def test_nothing_in_play_in_the_events_list_buys_nothing(tmp_path):
    dk_log(tmp_path)
    calls = []
    later = [event(kickoff="2026-10-31T17:30:00+00:00")]
    res = il.run(tmp_path, "key", now=T("2026-10-31 15:58"), get=fake_get(calls, events=later))
    assert res["calls"] == 0 and calls and all(u.endswith("/events") for u in calls)
    assert not list(tmp_path.glob("*_inplay_*"))
