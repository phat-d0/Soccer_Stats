"""Champions League historical closes (cup_odds.py) against a fake Odds API and a fake
ESPN. Nothing here may reach the network."""

import json
from pathlib import Path

import pandas as pd
import pytest

from soccer_stats import cup_odds as co
from soccer_stats import cups

KEY = "SECRET-KEY-123"
TODAY = pd.Timestamp("2026-10-13 12:00", tz="UTC")
ROOT = Path(__file__).resolve().parents[1]


def espn(eid, kickoff, home, away):
    return {
        "id": eid,
        "kickoff": pd.Timestamp(kickoff),
        "home": home,
        "away": away,
        "home_id": home,
        "away_id": away,
        "completed": True,
        "stage": "league-phase",
    }


# ESPN's names. Four kickoff slots over two seasons; slot B has Inter and AC Milan on the
# same night (the shared word "Milan" must not cross them).
CAL = {
    2024: [espn("40", "2024-09-17T16:45Z", "Juventus", "PSV Eindhoven")],
    2025: [
        espn("1", "2025-09-16T16:45Z", "Athletic Club", "Arsenal"),
        espn("2", "2025-09-16T19:00Z", "PSV Eindhoven", "Union St.-Gilloise"),
        espn("3", "2025-09-16T19:00Z", "Real Madrid", "Marseille"),
        espn("4", "2025-09-16T19:00Z", "Internazionale", "Ajax Amsterdam"),
        espn("5", "2025-09-16T19:00Z", "AC Milan", "Liverpool"),
        espn("6", "2025-09-17T19:00Z", "F.C. København", "Bayer Leverkusen"),
    ],
}
# The Odds API's names for the same matches (home, away, kickoff), plus one stray event.
ODDS_EVENTS = [
    ("e40", "Juventus", "PSV Eindhoven", "2024-09-17T16:45:00Z"),
    ("e1", "Athletic Bilbao", "Arsenal", "2025-09-16T16:45:00Z"),
    ("e2", "PSV Eindhoven", "Union Saint-Gilloise", "2025-09-16T19:00:00Z"),
    ("e3", "Real Madrid", "Marseille", "2025-09-16T19:00:00Z"),
    ("e4", "Inter Milan", "Ajax", "2025-09-16T19:00:00Z"),
    ("e5", "AC Milan", "Liverpool", "2025-09-16T19:00:00Z"),
    ("e6", "FC Copenhagen", "Bayer Leverkusen", "2025-09-17T19:00:00Z"),
    ("x9", "Mystery FC", "Unknown United", "2025-09-16T19:00:00Z"),
]


class Resp:
    def __init__(self, body, cost, left, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def h2h(book, home, away, ph, pd_, pa):
    return {
        "key": book,
        "last_update": "2025-09-16T18:50:00Z",
        "markets": [
            {
                "key": "h2h",
                "last_update": "2025-09-16T18:51:00Z",
                "outcomes": [
                    {"name": home, "price": ph},
                    {"name": "Draw", "price": pd_},
                    {"name": away, "price": pa},
                ],
            }
        ],
    }


def fake_odds(
    calls,
    left=20000,
    cost=10,
    dk_cost=None,
    status=200,
    message="",
    no_pinnacle_at=None,
    swap=(),
):
    """A fake Odds API: /sports (free, the balance) and historical bulk h2h snapshots that
    list every event kicking off at or after the requested time. `no_pinnacle_at` drops
    Pinnacle's price for events at that requested time; `swap` lists event ids whose home
    and away the API reverses."""
    state = {"left": left}

    def get(url, params=None, timeout=None):
        assert url.startswith("https://api.the-odds-api.com/v4/"), url
        calls.append((url, dict(params or {})))
        if url.endswith("/sports"):
            return Resp([], 0, state["left"])
        assert "/historical/sports/soccer_uefa_champs_league/odds" in url
        if status != 200:
            return Resp({"message": message}, 0, state["left"], status)
        at = pd.Timestamp(params["date"])
        books = params["bookmakers"].split(",")
        c = dk_cost if (dk_cost and "draftkings" in books) else cost
        state["left"] -= c
        data = []
        for eid, h, a, k in ODDS_EVENTS:
            if pd.Timestamp(k) < at:
                continue
            if eid in swap:
                h, a = a, h
            bms = []
            if "pinnacle" in books and not (no_pinnacle_at and at == pd.Timestamp(no_pinnacle_at)):
                bms.append(h2h("pinnacle", h, a, 2.1, 3.4, 3.6))
            if "draftkings" in books:
                bms.append(h2h("draftkings", h, a, 2.05, 3.3, 3.5))
            data.append(
                {"id": eid, "commence_time": k, "home_team": h, "away_team": a, "bookmakers": bms}
            )
        prev = at - pd.Timedelta(minutes=5)
        body = {
            "timestamp": co.iso(at),
            "previous_timestamp": co.iso(prev),
            "next_timestamp": co.iso(at + pd.Timedelta(minutes=5)),
            "data": data,
        }
        return Resp(body, c, state["left"])

    return get


def odds_calls(calls):
    return [c for c in calls if "/historical/" in c[0]]


def run(tmp_path, get, cap=100, state=None, cache="cache", **kw):
    return co.run(
        "UCL",
        (2024, 2025),
        cap,
        api_key=KEY,
        state=state,
        raw_dir=tmp_path,
        cache_dir=tmp_path / cache,
        odds_get=get,
        calendars=CAL,
        **kw,
    )


# ---------- plan ----------


def test_plan_takes_one_snapshot_per_kickoff_slot_five_minutes_before():
    snaps = co.plan(CAL)
    assert [co.iso(s["requested"]) for s in snaps] == [
        "2024-09-17T16:40:00Z",
        "2025-09-16T16:40:00Z",
        "2025-09-16T18:55:00Z",
        "2025-09-17T18:55:00Z",
    ]
    assert [len(s["events"]) for s in snaps] == [1, 1, 4, 1]
    assert co.request_time("2025-09-16T20:02Z") == pd.Timestamp("2025-09-16T19:55Z")


def test_plan_count_matches_the_cost_estimates_kickoff_slots():
    for y, events in CAL.items():
        assert len(co.plan({y: events})) == cups.kickoff_slots([e["kickoff"] for e in events])


# ---------- dry run ----------


def test_dry_run_makes_no_odds_calls_and_prices_the_plan(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls), cap=0)
    assert calls == []  # not even the free balance call
    assert res["dry_run"] and res["spent"] == 0
    assert [st["state"] for st in res["snapshots"]] == ["planned"] * 4
    assert res["estimate_credits"] == 40
    assert res["coverage"]["2025"]["planned"] == 3
    assert res["coverage"]["2025"]["estimate_credits"] == 30
    text = co.report(res)
    assert "2025-09-16T18:55:00Z: 4" in text and "dry run" in text


def test_no_key_is_a_dry_run(tmp_path):
    calls = []
    res = co.run(
        "UCL", (2025,), 500, api_key="", raw_dir=tmp_path, odds_get=fake_odds(calls), calendars=CAL
    )
    assert calls == [] and res["dry_run"]


# ---------- paid run ----------


def test_paid_run_buys_each_slot_once_and_counts_x_requests_last(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls, cost=7))
    oc = odds_calls(calls)
    assert len(oc) == 4
    assert res["spent"] == 28  # the header's cost, not the estimate
    assert res["left"] == 20000 - 28 and res["left_before"] == 20000
    p = oc[0][1]
    assert p["bookmakers"] == "pinnacle,draftkings" and p["markets"] == "h2h"
    assert p["oddsFormat"] == "decimal" and p["date"] == "2024-09-17T16:40:00Z"
    assert "regions" not in p
    # 7 matches x 2 books; the stray event is reported, not stored
    assert len(res["rows"]) == 14
    assert res["coverage"]["2025"]["pinnacle"] == 6 and res["coverage"]["2024"]["draftkings"] == 1
    assert any("Mystery FC v Unknown United" in u for u in res["unmatched"])
    r = next(r for r in res["rows"] if r["espn_id"] == "4" and r["book"] == "pinnacle")
    assert (r["home"], r["away"], r["odds_home"], r["odds_away"]) == (
        "Internazionale",
        "Ajax Amsterdam",
        "Inter Milan",
        "Ajax",
    )
    assert (r["price_home"], r["price_draw"], r["price_away"]) == (2.1, 3.4, 3.6)
    assert r["minutes_before"] == 5.0 and r["snapshot"] == "2025-09-16T18:55:00Z"
    assert r["event_id"] == "e4" and r["last_update"] == "2025-09-16T18:51:00Z"
    milan = next(r for r in res["rows"] if r["espn_id"] == "5")
    assert milan["odds_home"] == "AC Milan" and milan["event_id"] == "e5"
    assert res["name_map"]["FC Copenhagen"]["espn"] == "F.C. København"
    line = co.summary_line(res)
    assert line == (
        "Cup odds: 4 snapshots, 28 credits, 7 matches with Pinnacle, 7 with DraftKings, "
        "credits left 19972"
    )
    assert [c["cost"] for c in res["calls"]] == [7, 7, 7, 7]


def test_draftkings_is_dropped_when_it_doubles_the_cost(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls, cost=10, dk_cost=20), cap=200)
    oc = odds_calls(calls)
    assert oc[0][1]["bookmakers"] == "pinnacle,draftkings"
    assert all(c[1]["bookmakers"] == "pinnacle" for c in oc[1:])
    assert res["dropped_draftkings"] and res["spent"] == 20 + 3 * 10
    assert any("DraftKings dropped" in line for line in res["log"])
    assert res["coverage"]["2024"]["draftkings"] == 1  # the first body still had it
    assert res["coverage"]["2025"]["draftkings"] == 0


def test_cap_skips_a_call_whose_largest_cost_would_pass_it(tmp_path):
    calls = []
    # First call budgeted at 20 (two books, worst case), costs 10; then 10 each: 10, 20,
    # and a third would reach 30 > 25.
    res = run(tmp_path, fake_odds(calls, cost=10), cap=25)
    assert len(odds_calls(calls)) == 2 and res["spent"] == 20
    assert [st["state"] for st in res["snapshots"]] == ["api", "api", "skipped", "skipped"]
    assert any("would pass the 25-credit cap" in line for line in res["log"])


def test_reserve_skips_calls_that_would_leave_under_3000(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls, left=3015), cap=1200)
    assert odds_calls(calls) == [] and res["spent"] == 0
    assert any("3000-credit reserve" in line for line in res["log"])


def test_cached_bodies_are_never_requested_again(tmp_path):
    calls = []
    first = run(tmp_path, fake_odds(calls))
    assert len(odds_calls(calls)) == 4
    calls.clear()
    again = run(tmp_path, fake_odds(calls))
    assert odds_calls(calls) == [] and again["spent"] == 0
    assert [st["state"] for st in again["snapshots"]] == ["cache"] * 4
    assert again["rows"] == first["rows"]
    assert all(c["cost"] == 10 for c in again["calls"])  # the cost when it was bought
    # a dry run reads the cache too (0 credits)
    calls.clear()
    dry = run(tmp_path, fake_odds(calls), cap=0)
    assert calls == [] and len(dry["rows"]) == 14


def test_slots_held_on_data_log_are_skipped_even_without_the_cache(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls))
    co.write_pending(res, tmp_path / "pending")
    log_dir = tmp_path / "data-log"
    assert co.log(tmp_path / "pending", log_dir, "UCL") == (14, 4)
    assert co.log(tmp_path / "pending", log_dir, "UCL") == (0, 0)  # deduplicated
    calls.clear()
    again = run(tmp_path, fake_odds(calls), state=log_dir, cache="evicted")
    assert odds_calls(calls) == [] and again["spent"] == 0
    assert [st["state"] for st in again["snapshots"]] == ["held"] * 4
    assert again["rows"] == [] and again["coverage"]["2025"]["pinnacle"] == 6
    # a flat state copy (as the workflow makes) works the same way
    flat = tmp_path / "flat"
    flat.mkdir()
    for name in ("UCL_closes.jsonl", "UCL_close_calls.jsonl"):
        (flat / name).write_text((log_dir / "odds_history" / name).read_text())
    calls.clear()
    run(tmp_path, fake_odds(calls), state=flat, cache="evicted2")
    assert odds_calls(calls) == []


def test_log_keeps_one_row_per_match_and_book(tmp_path):
    pend = tmp_path / "p"
    pend.mkdir()
    row = {"espn_id": "1", "book": "pinnacle", "price_home": 2.0}
    (pend / "rows.jsonl").write_text(
        json.dumps(row) + "\n" + json.dumps({**row, "price_home": 2.2}) + "\n"
    )
    (pend / "calls.jsonl").write_text("")
    assert co.log(pend, tmp_path / "log", "UCL") == (1, 0)
    lines = (tmp_path / "log" / "odds_history" / "UCL_closes.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["price_home"] == 2.0


def test_a_slot_missing_pinnacle_is_retried_once_at_the_previous_snapshot(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls, no_pinnacle_at="2025-09-17T18:55:00Z"), cap=100)
    oc = odds_calls(calls)
    assert [c[1]["date"] for c in oc][-2:] == ["2025-09-17T18:55:00Z", "2025-09-17T18:50:00Z"]
    assert len(oc) == 5 and res["retried"] == 1
    pin6 = next(r for r in res["rows"] if r["espn_id"] == "6" and r["book"] == "pinnacle")
    dk6 = [r for r in res["rows"] if r["espn_id"] == "6" and r["book"] == "draftkings"]
    assert pin6["kind"] == "retry" and pin6["minutes_before"] == 10.0
    assert len(dk6) == 1 and dk6[0]["kind"] == "close"  # not duplicated by the retry
    assert [c["kind"] for c in res["calls"]].count("retry") == 1
    # once recorded on data-log, neither the close nor the retry is asked again
    co.write_pending(res, tmp_path / "pending")
    co.log(tmp_path / "pending", tmp_path / "log", "UCL")
    calls.clear()
    run(tmp_path, fake_odds(calls), state=tmp_path / "log", cache="evicted")
    assert odds_calls(calls) == []


def test_retries_come_after_every_slot_and_respect_the_cap(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls, no_pinnacle_at="2024-09-17T16:40:00Z"), cap=40)
    dates = [c[1]["date"] for c in odds_calls(calls)]
    assert dates == [
        "2024-09-17T16:40:00Z",
        "2025-09-16T16:40:00Z",
        "2025-09-16T18:55:00Z",
        "2025-09-17T18:55:00Z",
    ]  # the retry would pass the cap, and never jumps ahead of a first snapshot
    assert res["retried"] == 0 and res["spent"] == 40


def test_an_api_error_stops_the_run_and_the_key_never_appears(tmp_path, capsys, monkeypatch):
    calls = []
    get = fake_odds(calls, status=401, message=f"Invalid key {KEY}")
    monkeypatch.setenv("ODDS_API_KEY", KEY)
    res = co.cli(
        "UCL",
        (2024, 2025),
        50,
        pending=str(tmp_path / "pending"),
        json_path=str(tmp_path / "out.json"),
        raw_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        odds_get=get,
        calendars=CAL,
    )
    out = capsys.readouterr().out
    assert len(odds_calls(calls)) == 1  # stopped after the error
    assert res["stopped"] and "Stopped:" in out
    assert "CUP_ODDS_JSON" in out and "Cup odds:" in out
    assert KEY not in out
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(errors="ignore"), p
    assert all(KEY not in line for line in res["log"])


def test_cached_files_and_rows_never_hold_the_key(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls))
    co.write_pending(res, tmp_path / "pending")
    for p in tmp_path.rglob("*.json*"):
        assert KEY not in p.read_text()
    assert KEY not in json.dumps(co.compact(res), default=str)


# ---------- names ----------


def test_team_names_match_through_aliases_accents_and_words():
    assert co.same_team("Inter Milan", "Internazionale") == "alias"
    assert co.same_team("Inter Milan", "AC Milan") is None  # an alias is authoritative
    assert co.same_team("Atletico Madrid", "Atlético Madrid") == "name"
    assert co.same_team("Bayern Munich", "FC Bayern Munich") == "name"
    assert co.same_team("Red Bull Salzburg", "FC Salzburg") is None
    assert co.same_team("Manchester City", "Manchester United") is None
    assert co.same_team("Shakhtar Donetsk", "Shakhtar") == "words"
    assert co.same_team("Galatasaray SK", "Galatasaray") == "name"


def test_a_match_pairs_on_one_strong_team_and_records_the_partner():
    e = [espn("7", "2025-10-01T19:00Z", "Club Brugge", "Sturm Graz")]
    o = [
        {
            "id": "o",
            "commence_time": "2025-10-01T19:00:00Z",
            "home_team": "Club Brugge KV",
            "away_team": "SK Sturm",
        }
    ]
    p = co.pair_events(e, o)
    assert len(p) == 1 and p[0]["how"] in ("both", "one")
    far = [{**o[0], "commence_time": "2025-10-02T19:00:00Z"}]
    assert co.pair_events(e, far) == []  # more than 3 hours apart


def test_a_home_away_swap_keeps_each_price_with_its_team(tmp_path):
    calls = []
    res = run(tmp_path, fake_odds(calls, swap=("e3",)))
    r = next(r for r in res["rows"] if r["espn_id"] == "3" and r["book"] == "pinnacle")
    # the API listed Marseille at home with 2.1; ESPN's home (Real Madrid) gets 3.6
    assert r["swapped"] and r["odds_home"] == "Real Madrid"
    assert (r["price_home"], r["price_away"]) == (3.6, 2.1)


# ---------- ESPN calendar through the run ----------


def test_run_reads_espns_main_slug_only(tmp_path):
    asked = []

    def fake_espn(url, params=None, timeout=None):
        assert url.startswith("https://site.api.espn.com/"), url
        asked.append(url)
        day = params["dates"]
        events = []
        if day == "20250916":
            events = [
                {
                    "id": "1",
                    "date": "2025-09-16T16:45Z",
                    "season": {"slug": "league-phase"},
                    "status": {"type": {"completed": True}},
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "team": {"id": "a", "displayName": "Athletic Club"},
                                },
                                {"homeAway": "away", "team": {"id": "b", "displayName": "Arsenal"}},
                            ]
                        }
                    ],
                }
            ]

        class R:
            status_code = 200

            def json(self):
                return {"events": events, "leagues": [{"calendar": ["2025-09-16T07:00Z"]}]}

        return R()

    calls = []
    res = co.run(
        "UCL",
        (2025,),
        0,
        api_key=KEY,
        raw_dir=tmp_path,
        odds_get=fake_odds(calls),
        espn_get=fake_espn,
        today=TODAY,
    )
    assert calls == []
    assert all("/uefa.champions/" in u for u in asked)  # never the _qual slug
    assert res["coverage"]["2025"]["matches"] == 1 and res["estimate_credits"] == 10


# ---------- workflow ----------


def test_backfill_workflow_routes_the_key_to_the_cup_step_only():
    text = (ROOT / ".github/workflows/backfill.yml").read_text()
    assert "cup_odds:" in text
    assert "soccer-stats backfill-cup-odds" in text and "soccer-stats log-cup-odds" in text
    assert "cup-odds-history-" in text
    # the DraftKings download and backtest never run on a cup-odds dispatch
    for name in ("- name: Download historical odds", "- name: Run the DraftKings backtest"):
        block = text.split(name, 1)[1].split("\n", 2)[1]
        assert "!inputs.cup_odds" in block, name
    # a dry run passes no key to the cup step
    cup = text.split("- name: Download the Champions League closes", 1)[1].split("- uses:", 1)[0]
    assert "!inputs.dry_run" in cup and "secrets.ODDS_API_KEY" in cup


@pytest.mark.parametrize("cup", ["UCL", "UEL", "UECL"])
def test_cups_have_a_sport_key(cup):
    from soccer_stats.leagues import LEAGUES

    assert LEAGUES[cup].odds_sport.startswith("soccer_uefa_")


def test_a_network_failure_stops_further_calls_and_keeps_only_the_error_type(tmp_path):
    import requests

    calls = []
    inner = fake_odds(calls)

    def get(url, params=None, timeout=None):
        if "/historical/" in url:
            calls.append((url, dict(params or {})))
            raise requests.ConnectionError(f"boom {url}?apiKey={KEY}")
        return inner(url, params, timeout)

    res = run(tmp_path, get)
    assert len(odds_calls(calls)) == 1 and res["spent"] == 0
    assert "ConnectionError" in res["stopped"] and KEY not in co.report(res)
    assert [st["state"] for st in res["snapshots"]] == ["api", "skipped", "skipped", "skipped"]
