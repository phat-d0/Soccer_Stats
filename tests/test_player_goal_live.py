"""Priced goalscorer test, step 2: the live anytime-goalscorer log (no network)."""

import json

import pandas as pd

from soccer_stats import player_goal_live as gl

NOW = pd.Timestamp("2026-11-07T14:20:00Z")


def card(minutes, confirmed=False, home="Arsenal", away="Chelsea", league="E0"):
    k = NOW + pd.Timedelta(minutes=minutes)
    return {
        "league": league,
        "home": home,
        "away": away,
        "kickoff": k.isoformat(),
        "team_news": {
            "lineup": {"confirmed": confirmed, "first_confirmed_at": "2026-11-07T13:50:00Z"}
        },
    }


def test_due_snapshots_follow_lineups_and_the_close_window():
    assert gl.due_snapshot(card(60, confirmed=True), NOW) == "entry"
    assert gl.due_snapshot(card(60, confirmed=False), NOW) is None
    assert gl.due_snapshot(card(28, confirmed=False), NOW) == "entry"  # by 30 min regardless
    assert gl.due_snapshot(card(80, confirmed=True), NOW) is None
    assert gl.due_snapshot(card(15), NOW) == "close"
    assert gl.due_snapshot(card(-1), NOW) is None


class Resp:
    def __init__(self, body, cost=1, left=4_000_000, status=200):
        self.body, self.status_code, self.ok = body, status, status < 400
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def raw_with_events(tmp_path, cards):
    raw = tmp_path / "raw"
    raw.mkdir()
    evs = [
        {
            "id": f"ev{i}",
            "home_team": c["home"],
            "away_team": c["away"],
            "commence_time": c["kickoff"],
        }
        for i, c in enumerate(cards)
    ]
    (raw / "odds_api_E0_draftkings.json").write_text(json.dumps(evs))
    return raw


def body(eid):
    outs = [{"name": "Yes", "description": "Bukayo Saka", "price": 3.0}]
    return {
        "id": eid,
        "bookmakers": [{"key": "draftkings", "markets": [{"key": gl.MARKET, "outcomes": outs}]}],
    }


def test_run_logs_each_snapshot_once_within_the_cap(tmp_path):
    cards = [card(60, confirmed=True), card(15, home="Leeds", away="Fulham")]
    raw = raw_with_events(tmp_path, cards)
    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, params))
        assert params["bookmakers"] == ",".join(gl.BOOKS) and params["markets"] == gl.MARKET
        return Resp(body(url.split("/")[-2]))

    out = tmp_path / "out"
    s = gl.run(cards, out, raw_dir=raw, api_key="k", now=NOW, get=get)
    assert s["calls"] == 2 and s["credits"] == 2 and s["rows"] == 2 and s["stop"] is None
    rows = [json.loads(x) for x in (out / "rows.jsonl").read_text().splitlines()]
    assert {r["snapshot"] for r in rows} == {"entry", "close"}
    assert rows[0]["player"] == "Bukayo Saka" and rows[0]["lineup_confirmed"] is True
    # The local record stops a second fetch of the same snapshots.
    s2 = gl.run(cards, tmp_path / "out2", raw_dir=raw, api_key="k", now=NOW, get=get)
    assert s2["calls"] == 0 and len(calls) == 2
    # data-log: rows by month and the call record, deduplicated.
    log = tmp_path / "log"
    assert gl.log(out, log) == (2, 2)
    assert gl.log(out, log) == (0, 0)
    assert (log / "odds_log" / "E0_goalscorer_2026-11.jsonl").exists()
    assert len(gl.load_rows(log)) == 2


def test_reserve_cap_and_end_date_stop_calls(tmp_path):
    cards = [card(15)]
    raw = raw_with_events(tmp_path, cards)
    seen = []

    def get(url, params=None, timeout=None):
        seen.append(url)
        return Resp(body("ev0"))

    state = tmp_path / "state"
    state.mkdir()
    full = [{"event_id": f"x{i}", "snapshot": "close", "fetched_at": NOW.isoformat(), "cost": 1}
            for i in range(gl.MONTHLY_CAP)]  # fmt: skip
    (state / "goalscorer_calls_2026-11.jsonl").write_text("\n".join(json.dumps(c) for c in full))
    s = gl.run(cards, tmp_path / "o", [state], raw_dir=raw, api_key="k", now=NOW, get=get)
    assert "monthly cap" in s["stop"] and not seen
    low = [{"event_id": "y", "snapshot": "close", "fetched_at": NOW.isoformat(), "cost": 0,
            "credits_left": 3000}]  # fmt: skip
    state2 = tmp_path / "state2"
    state2.mkdir()
    (state2 / "goalscorer_calls_2026-11.jsonl").write_text(json.dumps(low[0]))
    s = gl.run(cards, tmp_path / "o2", [state2], raw_dir=raw, api_key="k", now=NOW, get=get)
    assert "reserve" in s["stop"] and not seen
    late = gl.LIVE_UNTIL + pd.Timedelta(minutes=1)
    s = gl.run([card(15)], tmp_path / "o3", raw_dir=raw, api_key="k", now=late, get=get)
    assert "judged window ended" in s["stop"] and not seen
