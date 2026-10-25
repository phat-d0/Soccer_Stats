import json

import pandas as pd

from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import timelines as tl


def raw_event(eid, home, away, goals, day="2024-09-14T14:00Z", score=None):
    return {
        "id": eid,
        "date": day,
        "status": {"type": {"completed": True, "name": "STATUS_FULL_TIME"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": (score or [0, 0])[0],
                        "team": {"id": "1", "displayName": home},
                    },
                    {
                        "homeAway": "away",
                        "score": (score or [0, 0])[1],
                        "team": {"id": "2", "displayName": away},
                    },
                ],
                "details": [
                    {"scoringPlay": True, "clock": {"displayValue": c}, "team": {"id": t}}
                    for c, t in goals
                ],
            }
        ],
    }


def fd(rows):
    """football-data rows (home favourite at about 60%) through phase 1's favourites."""
    base = {
        "league": "E0",
        "season_start": 2024,
        "ph": 1.6,
        "pd": 4.0,
        "pa": 6.0,
        "pov": 1.9,
        "pun": 1.9,
        "kickoff": pd.Timestamp("2024-09-14T14:00Z"),
    }
    df = pd.DataFrame([{**base, **r} for r in rows])
    df["date"] = pd.Timestamp("2024-09-14")
    return cb.favourites(df)


def test_compact_keeps_clock_and_team():
    rec = tl.compact({"events": [raw_event("9", "A", "B", [("12'", "2"), ("45'+2'", "1")])]})
    e = rec["events"][0]
    assert e["home"] == "A" and e["home_id"] == "1" and e["completed"]
    assert [g[:2] for g in e["goals"]] == [["12'", "2"], ["45'+2'", "1"]]


def test_join_checks_full_time_and_half_time():
    fav = fd(
        [
            {"home": "A", "away": "B", "fthg": 2, "ftag": 1, "hthg": 1, "htag": 1},
            {"home": "C", "away": "D", "fthg": 1, "ftag": 0, "hthg": 1, "htag": 0},
        ]
    )
    ev = tl.compact(
        {
            "events": [
                raw_event("1", "A", "B", [("12'", "2"), ("45'+2'", "1"), ("80'", "1")]),
                raw_event("2", "C", "D", [("50'", "1")]),  # half-time 0-0 vs 1-0: dropped
            ]
        }
    )["events"]
    m, counts = tl.join(fav, ev)
    assert counts["joined"] == 1 and counts["mismatch"] == 1
    assert m.iloc[0]["goals"] == [(12, 1, "away"), (45, 1, "home"), (80, 2, "home")]


def test_trail_first_and_half_time_state():
    fav = fd([{"home": "A", "away": "B", "fthg": 2, "ftag": 1, "hthg": 1, "htag": 1}])
    ev = tl.compact(
        {"events": [raw_event("1", "A", "B", [("12'", "2"), ("45'+2'", "1"), ("80'", "1")])]}
    )
    m, _ = tl.join(fav, ev["events"])
    tf = tl.trail_first(m)
    assert len(tf) == 1
    r = tf.iloc[0]
    assert r["minute"] == 12 and r["mband"] == "0-15" and r["ht_state"] == "level"
    assert abs(r["left"] - 78 / 90) < 1e-12 and not r["second_half"]
    assert tl.minute_band(45) == "30-45" and tl.minute_band(76) == "75+"


def test_behind_at_minutes():
    fav = fd([{"home": "A", "away": "B", "fthg": 1, "ftag": 2, "hthg": 0, "htag": 1}])
    ev = tl.compact(
        {"events": [raw_event("1", "A", "B", [("44'", "2"), ("62'", "2"), ("88'", "1")])]}
    )
    m, _ = tl.join(fav, ev["events"])
    assert tl.behind_at(m, 30).empty
    assert tl.behind_at(m, 45).iloc[0]["deficit"] == 1
    assert tl.behind_at(m, 75).iloc[0]["deficit"] == 2


def test_costing_shares_a_bucket_and_adds_the_break():
    tf = pd.DataFrame(
        {
            "league": ["E0", "E0", "E0"],
            "season_start": [2024, 2024, 2024],
            "kickoff": [pd.Timestamp("2024-09-14T14:00Z")] * 3,
            "minute": [12, 13, 50],
            "second_half": [False, False, True],
        }
    )
    c = tl.trail_first_costing(tf, {"E0 2024/25": 41})["E0 2024/25"]
    # 14:17 and 14:18 share the 14:15 bucket; 50' + 5 + 17 = 15:12 is its own
    assert c == {"trail_first": 3, "snapshots": 2, "credits": 20, "half_time_slots": 41}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def test_fetch_caches_resumes_and_skips_4xx(tmp_path):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(params["dates"])
        if params["dates"] == "20240915":
            return Resp(404)
        return Resp(200, {"events": [raw_event("1", "A", "B", [("10'", "1")])]})

    days = {("E0", 2024): ["20240914", "20240915"]}
    res = tl.fetch(["E0"], [2024], tmp_path, get=get, pause=0, days=days)
    row = res["seasons"]["E0 2024/25"]
    assert row["fetched"] == 1 and row["failed"] == 1 and row["with_goals"] == 1
    assert calls == ["20240914", "20240915"]  # a 404 is not retried
    saved = json.loads(tl.day_path(tmp_path, "eng.1", "20240914").read_text())
    assert saved["events"][0]["goals"][0][0] == "10'"
    tl.fetch(["E0"], [2024], tmp_path, get=get, pause=0, days=days)
    assert calls.count("20240914") == 1  # cached days are never asked again


def test_fetch_stops_at_the_time_budget(tmp_path):
    t = iter(range(0, 10_000, 100))
    res = tl.fetch(
        ["E0"],
        [2024],
        tmp_path,
        seconds=150,
        get=lambda *a, **k: Resp(200, {"events": []}),
        pause=0,
        clock=lambda: next(t),
        days={("E0", 2024): [f"202409{d:02d}" for d in range(1, 9)]},
    )
    assert res["stopped"] and res["requests"] < 8


def test_analyse_runs_end_to_end():
    rows, events = [], []
    for i in range(30):
        h, a = f"H{i}", f"A{i}"
        # alternate: favourite concedes first then wins 2-1, or wins 1-0
        if i % 2:
            rows.append({"home": h, "away": a, "fthg": 2, "ftag": 1, "hthg": 0, "htag": 1})
            events.append(raw_event(str(i), h, a, [("20'", "2"), ("60'", "1"), ("70'", "1")]))
        else:
            rows.append({"home": h, "away": a, "fthg": 1, "ftag": 0, "hthg": 1, "htag": 0})
            events.append(raw_event(str(i), h, a, [("30'", "1")]))
    m, counts = tl.join(fd(rows), tl.compact({"events": events})["events"])
    m["fband"] = m["p_fav"].map(tl.band_of)
    res = tl.analyse(m)
    assert counts["joined"] == 30 and res["trail_first"] == 15
    assert res["trail_first_half_time_state"]["behind"]["share"] == 1.0
    assert res["trail_first_by_minute"]["15-30"]["win"] == 1.0
    assert res["behind_at"]["45"]["share_behind"] == 0.5
    assert "15-30" in res["timing_curve"]["E0"]["by_minute"]
