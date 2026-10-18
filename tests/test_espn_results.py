"""Fast settlement from ESPN (18 Oct): match trades on ESPN's final 90-minute score,
corner trades on its summary's corner counts, football-data as fallback and later check."""

import json

import pandas as pd
import pytest
import requests

from soccer_stats import cli, paper
from soccer_stats import espn_results as er
from soccer_stats import trades as tr

K = pd.Timestamp("2026-10-17 14:00", tz="UTC")
AFTER = K + pd.Timedelta(hours=2, minutes=10)
DAY = "20261017"


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def event(eid, home, away, score=None, kickoff=K, completed=True, status="STATUS_FULL_TIME"):
    comp = {
        "competitors": [
            {"homeAway": "home", "team": {"id": f"id-{home}", "displayName": home}},
            {"homeAway": "away", "team": {"id": f"id-{away}", "displayName": away}},
        ]
    }
    if score is not None:
        comp["competitors"][0]["score"] = str(score[0])
        comp["competitors"][1]["score"] = str(score[1])
    return {
        "id": str(eid),
        "date": kickoff.isoformat(),
        "status": {"type": {"completed": completed, "name": status}},
        "competitions": [comp],
    }


def summary_body(home, away, hc, ac):
    def team(name, n, side):
        stats = [{"name": "foulsCommitted", "displayValue": "11"}]
        if n is not None:
            stats.append({"name": "wonCorners", "displayValue": str(n)})
        return {
            "homeAway": side,
            "team": {"id": f"id-{name}", "displayName": name},
            "statistics": stats,
        }

    return {"boxscore": {"teams": [team(away, ac, "away"), team(home, hc, "home")]}}


def fake_get(days, summaries=None, log=None, fail=False):
    """ESPN by slug: days {slug: {YYYYMMDD: [events]}}, summaries {event id: body}."""

    def get(url, params=None, timeout=None):
        assert url.startswith("https://site.api.espn.com/"), url  # ESPN only, no key
        if log is not None:
            log.append((url, dict(params or {})))
        if fail:
            raise requests.ConnectionError("down")
        slug = url.split("/soccer/")[1].split("/")[0]
        if url.endswith("/summary"):
            return Resp((summaries or {}).get(params["event"], {}))
        return Resp({"events": days.get(slug, {}).get(params["dates"], [])})

    return get


def todo(home="Arsenal", away="Leeds", league="E0", kind="match", kickoff=K):
    return {
        "kind": kind,
        "league": league,
        "season": "2627",
        "home": home,
        "away": away,
        "kickoff": kickoff.isoformat(),
    }


def fetch(tmp_path, items, days, summaries=None, now=AFTER, known=None, log=None, **kw):
    return er.fetch(
        items,
        known or {},
        now,
        tmp_path,
        fake_get(days, summaries, log, kw.pop("fail", False)),
        er.Budget(**kw) if kw else None,
    )


# ---------- ESPN parsing and matching ----------


def test_only_a_final_match_gives_a_row(tmp_path):
    days = {
        "eng.1": {
            DAY: [
                event(1, "Arsenal", "Leeds United", (2, 1)),
                event(
                    2, "Chelsea", "Burnley", (0, 0), completed=False, status="STATUS_SECOND_HALF"
                ),
            ]
        }
    }
    known = {"E0": {"Arsenal", "Leeds", "Chelsea", "Burnley"}}
    rows, rep = fetch(tmp_path, [todo(), todo("Chelsea", "Burnley")], days, known=known)
    assert rows[["home", "away", "home_goals", "away_goals", "result_source"]].values.tolist() == [
        ["Arsenal", "Leeds", 2, 1, "espn"]
    ]
    assert rows["season"].tolist() == ["2627"] and rows["event_id"].tolist() == ["1"]
    assert rep["settled"] == 1 and rep["pending"] == [
        "E0 Chelsea v Burnley (17 Oct 14:00): STATUS_SECOND_HALF"
    ]


def test_espn_names_map_to_the_trades_football_data_names(tmp_path):
    days = {"eng.1": {DAY: [event(1, "Brighton & Hove Albion", "Manchester City", (1, 3))]}}
    known = {"E0": {"Brighton", "Man City", "Man United", "Arsenal"}}
    rows, _ = fetch(tmp_path, [todo("Brighton", "Man City")], days, known=known)
    assert rows[["home", "away", "home_goals", "away_goals"]].values.tolist() == [
        ["Brighton", "Man City", 1, 3]
    ]


@pytest.mark.parametrize(
    "events, why",
    [
        (
            [event(1, "Arsenal", "Leeds United", (1, 0)), event(2, "Arsenal", "Leeds", (2, 0))],
            "two events fit the match",
        ),
        (
            [
                event(1, "Arsenal", "Leeds United", (1, 0)),
                event(2, "Arsenal FC", "Spurs", (2, 0), kickoff=K + pd.Timedelta(hours=1)),
            ],
            "two ESPN clubs land on Arsenal",
        ),
    ],
)
def test_an_ambiguous_match_stays_open_and_is_logged(tmp_path, events, why):
    rows, rep = fetch(tmp_path, [todo()], {"eng.1": {DAY: events}}, known={"E0": {"Arsenal"}})
    assert rows.empty, why
    assert rep["ambiguous"] == ["E0 Arsenal v Leeds (17 Oct 14:00)"]
    assert any("ambiguous" in line for line in er.describe(rep))


def test_unmatched_moved_swapped_or_postponed_matches_never_settle(tmp_path):
    days = {
        "eng.1": {
            DAY: [
                event(1, "Leeds United", "Arsenal", (1, 0)),  # home and away swapped
                event(2, "Chelsea", "Burnley", (1, 0), kickoff=K + pd.Timedelta(hours=4)),
                event(3, "Everton", "Fulham", (0, 0), status="STATUS_POSTPONED"),
            ]
        }
    }
    items = [todo(), todo("Chelsea", "Burnley"), todo("Everton", "Fulham")]
    rows, rep = fetch(tmp_path, items, days)
    assert rows.empty
    assert len(rep["unmatched"]) == 2 and "STATUS_POSTPONED" in rep["pending"][0]


def test_only_open_leagues_and_days_are_asked_and_final_days_cached(tmp_path):
    log = []
    days = {"esp.1": {DAY: [event(1, "Real Betis", "Barcelona", (1, 2))]}}
    items = [todo("Betis", "Barcelona", "SP1"), todo("Betis", "Barcelona", "SP1", kind="corners")]
    rows, _ = fetch(tmp_path, items, days, log=log)
    asked = [(u.split("/soccer/")[1], p) for u, p in log]
    assert asked[0] == ("esp.1/scoreboard", {"dates": DAY}) and len(rows) == 1
    assert all("eng." not in u for u, _ in asked)  # no other league
    log.clear()
    fetch(tmp_path, [todo("Betis", "Barcelona", "SP1")], days, log=log)
    assert log == []  # every match that day was final: cached for good
    # Before full time nothing is asked; the Champions League is never asked here.
    assert (
        fetch(tmp_path, [todo(kickoff=AFTER - pd.Timedelta(minutes=30))], days, log=log)[1][
            "matches"
        ]
        == 0
    )
    assert fetch(tmp_path, [todo("A", "B", "UCL")], days, log=log)[1]["matches"] == 0
    assert log == []


def test_a_day_with_a_match_not_final_is_asked_again(tmp_path):
    log = []
    live = {"eng.1": {DAY: [event(1, "Arsenal", "Leeds", (1, 0), completed=False)]}}
    fetch(tmp_path, [todo()], live, log=log)
    fetch(tmp_path, [todo()], live, log=log)
    assert len(log) == 2


def test_the_run_budget_stops_espn_without_raising(tmp_path):
    log = []
    items = [todo(), todo("Pisa", "Lecce", "I1"), todo("Lens", "Lyon", "F1")]
    rows, rep = fetch(tmp_path, items, {}, log=log, fail=True, failures=2)
    assert rows.empty and rep["failures"] == 2 and "2 failed" in rep["stopped"]
    assert len(log) == 4  # two leagues' calls with one retry each, then nothing
    assert len(rep["no_answer"]) == 3
    clock = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    b = er.Budget(seconds=60, clock=lambda: next(clock))
    _, rep2 = er.fetch(items, {}, AFTER, tmp_path / "t", fake_get({}), b)
    assert "time budget" in rep2["stopped"] and rep2["requests"] == 1


# ---------- corner counts ----------


def test_corner_counts_come_from_the_final_summary(tmp_path):
    days = {"eng.1": {DAY: [event(9, "Arsenal", "Leeds United", (2, 1))]}}
    summ = {"9": summary_body("Arsenal", "Leeds United", 5, 3)}
    rows, rep = fetch(tmp_path, [todo(kind="corners")], days, summ)
    assert rows[["home_corners", "away_corners"]].values.tolist() == [[5, 3]]
    assert rep["corners"] == 1
    # No corner trade on a match: no summary call.
    log = []
    fetch(tmp_path / "b", [todo()], days, summ, log=log)
    assert not any(u.endswith("/summary") for u, _ in log)
    # A summary without both counts: the match row has none (football-data settles corners).
    rows, rep = fetch(
        tmp_path / "c",
        [todo(kind="corners")],
        days,
        {"9": summary_body("Arsenal", "Leeds United", 5, None)},
    )
    assert rows["home_corners"].isna().all() and rep["no_corners"]


# ---------- paper settlement ----------

PROBS = {"home": 0.61, "draw": 0.24, "away": 0.15, "over25": 0.55, "under25": 0.45}


def match_card():
    return {
        "home": "Arsenal",
        "away": "Leeds",
        "kickoff": K.isoformat(),
        "p": PROBS,
        "odds": {"home": 1.4, "draw": 5.0, "away": 7.5},
        "low_data": False,
    }


def open_match_trade(tmp_path):
    now = K - pd.Timedelta(hours=4)
    src = {"name": "DraftKings", "fetched_at": (now - pd.Timedelta(minutes=10)).isoformat()}
    ledger = {}
    ev, _ = paper.update_ledger(ledger, [match_card()], src, None, now)
    paper.append_events(tmp_path, ev)
    return "E0|2627|Arsenal|Leeds"


def espn_rows(hg=1, ag=1, hc=None, ac=None, kickoff=K):
    return pd.DataFrame(
        [
            {
                "league": "E0",
                "season": "2627",
                "date": pd.Timestamp(kickoff.date()),
                "kickoff": kickoff,
                "home": "Arsenal",
                "away": "Leeds",
                "home_goals": hg,
                "away_goals": ag,
                "home_corners": hc,
                "away_corners": ac,
                "event_id": "1",
                "result_source": "espn",
                "close_home": float("nan"),
                "close_draw": float("nan"),
                "close_away": float("nan"),
            }
        ]
    )


def fd_results(hg=1, ag=1):
    return pd.DataFrame(
        {
            "league": ["E0"],
            "season": ["2627"],
            "date": [pd.Timestamp("2026-10-17")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_goals": [hg],
            "away_goals": [ag],
            "close_home": [1.45],
            "close_draw": [4.6],
            "close_away": [7.0],
            "result_source": ["football-data"],
        }
    )


def settle(tmp_path, now, results=None, espn=None):
    ledger = paper.load_ledger(tmp_path)
    ev, _ = paper.update_ledger(ledger, [], {}, results, now, espn=espn)
    paper.append_events(tmp_path, ev)
    return paper.load_ledger(tmp_path), ev


def test_a_match_trade_settles_from_espn_then_football_data_checks_it(tmp_path, capsys):
    tid = open_match_trade(tmp_path)
    assert paper.load_ledger(tmp_path)[tid]["market"] == "draw"
    led, ev = settle(tmp_path, AFTER, None, espn_rows(1, 1))
    t = led[tid]
    assert (t["status"], t["score"], t["profit"], t["result_source"]) == (
        "won",
        "1-1",
        40.0,
        "espn",
    )
    assert t["clv_pinnacle"] is None  # no Pinnacle close on ESPN
    assert settle(tmp_path, AFTER, None, espn_rows(1, 1))[1] == []  # settled once
    # football-data's result arrives: one check event with Pinnacle's CLV, no re-settlement.
    later = AFTER + pd.Timedelta(hours=30)
    led, ev = settle(tmp_path, later, fd_results(1, 1))
    assert len(ev) == 1 and ev[0]["result_check"] == "ok" and ev[0]["check_score"] == "1-1"
    assert "status" not in ev[0] and led[tid]["clv_pinnacle"] is not None
    assert settle(tmp_path, later, fd_results(1, 1))[1] == []  # checked once
    assert "Result check" not in capsys.readouterr().out


def test_a_score_mismatch_is_flagged_and_espn_stands(tmp_path, capsys):
    tid = open_match_trade(tmp_path)
    settle(tmp_path, AFTER, None, espn_rows(1, 1))
    led, ev = settle(tmp_path, AFTER + pd.Timedelta(days=1), fd_results(2, 1))
    t = led[tid]
    assert (t["status"], t["score"], t["result_check"], t["check_score"]) == (
        "won",
        "1-1",
        "mismatch",
        "2-1",
    )
    assert "Result check: E0|2627|Arsenal|Leeds settled on ESPN's 1-1" in capsys.readouterr().out


def test_football_data_first_when_it_has_the_result_and_espn_as_fallback(tmp_path):
    tid = open_match_trade(tmp_path)
    led, _ = settle(tmp_path, AFTER, fd_results(0, 2), espn_rows(1, 1))
    t = led[tid]
    assert (t["score"], t["result_source"]) == ("0-2", "football-data")
    assert t["clv_pinnacle"] is not None and "result_check" not in t
    # An ESPN row at another kickoff (a different match) never settles the trade.
    d2 = tmp_path / "moved"
    open_match_trade(d2)
    led, ev = settle(d2, AFTER, None, espn_rows(1, 1, kickoff=K + pd.Timedelta(hours=4)))
    assert ev == [] and led[tid]["status"] == "open"


def corner_trade(tmp_path, line=5.0, side="over"):
    pick = {
        "market": "team_corners",
        "side": side,
        "odds": 2.0,
        "model_p": 0.55,
        "p_push": 0.1,
        "edge": 0.2,
        "threshold": 0.12,
    }
    t = tr.new_corner_trade(
        pick,
        league="E0",
        home="Arsenal",
        away="Leeds",
        team_side="home",
        line=line,
        kickoff=K,
        opened_at=K - pd.Timedelta(hours=20),
        odds_fetched_at=(K - pd.Timedelta(hours=20)).isoformat(),
        snapshot="look",
    )
    paper.append_corner_events(tmp_path, [{"type": "open", **t}])
    return t["id"]


def corner_book(tmp_path, now, results=None, espn=None):
    ledger = paper.load_corner_ledger(tmp_path, "E0")
    ev = paper.update_corner_ledger(ledger, [], [], results, now, "E0", espn=espn)
    paper.append_corner_events(tmp_path, ev)
    return paper.load_corner_ledger(tmp_path, "E0"), ev


def fd_corners(hc, ac):
    return pd.DataFrame(
        {
            "league": ["E0"],
            "season": ["2627"],
            "date": [pd.Timestamp("2026-10-17")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_corners": [hc],
            "away_corners": [ac],
        }
    )


def test_a_corner_push_settles_from_espn_and_football_data_checks_it(tmp_path, capsys):
    tid = corner_trade(tmp_path, line=5.0)
    led, _ = corner_book(tmp_path, AFTER, None, espn_rows(hc=None, ac=None))
    assert led[tid]["status"] == "open"  # no ESPN corner counts: waits for football-data
    led, _ = corner_book(tmp_path, AFTER, None, espn_rows(hc=5, ac=2))
    t = led[tid]
    assert (t["status"], t["push"], t["profit"], t["actual"]) == ("void", True, 0.0, 5)
    assert (t["score"], t["result_source"]) == ("5-2 corners", "espn")
    # football-data agrees: "ok"; once only.
    led, ev = corner_book(tmp_path, AFTER + pd.Timedelta(days=1), fd_corners(5, 2))
    assert [e["result_check"] for e in ev] == ["ok"] and led[tid]["status"] == "void"
    assert corner_book(tmp_path, AFTER + pd.Timedelta(days=1), fd_corners(5, 2))[1] == []


def test_a_corner_count_mismatch_is_flagged_not_resettled(tmp_path, capsys):
    tid = corner_trade(tmp_path, line=4.5)
    corner_book(tmp_path, AFTER, None, espn_rows(hc=5, ac=2))
    assert paper.load_corner_ledger(tmp_path, "E0")[tid]["status"] == "won"
    led, ev = corner_book(tmp_path, AFTER + pd.Timedelta(days=1), fd_corners(4, 2))
    t = led[tid]
    assert (t["status"], t["actual"], t["result_check"], t["check_score"]) == (
        "won",
        5,
        "mismatch",
        "4-2 corners",
    )
    assert "Corner check" in capsys.readouterr().out
    # football-data first when it already has the counts.
    d2 = tmp_path / "fd"
    tid2 = corner_trade(d2, line=4.5)
    led, _ = corner_book(d2, AFTER, fd_corners(3, 2), espn_rows(hc=5, ac=2))
    assert (led[tid2]["status"], led[tid2]["result_source"]) == ("lost", "football-data")


# ---------- the paper step ----------


def test_awaiting_results_lists_only_trades_football_data_cant_settle(tmp_path):
    open_match_trade(tmp_path)
    corner_trade(tmp_path)
    before = K + pd.Timedelta(minutes=30)
    got = paper.awaiting_results(tmp_path, ["E0"], before)
    assert {t["kind"] for t in got} == {"match", "corners"}
    assert paper.awaiting_results(tmp_path, ["E0"], K - pd.Timedelta(minutes=1)) == []
    got = paper.awaiting_results(tmp_path, ["E0"], AFTER, fd_results(), fd_corners(5, 2))
    assert got == []
    assert paper.awaiting_results(tmp_path, ["E0"], K + pd.Timedelta(days=15)) == []


def test_espn_settlement_in_the_paper_step_never_costs_the_ledger(tmp_path, capsys):
    open_match_trade(tmp_path)
    calls = []

    def boom(todo, known, now):
        calls.append((todo, known))
        raise ValueError("ESPN changed shape")

    assert cli.espn_settlement(tmp_path, ["E0"], fd_results().iloc[:0], None, AFTER, boom) is None
    assert "ESPN results unavailable (ValueError)" in capsys.readouterr().out
    assert calls[0][0][0]["home"] == "Arsenal"
    assert cli.espn_settlement(None, ["E0"], None, None, AFTER, boom) is None  # no ledger

    def good(todo, known, now):
        return espn_rows(1, 1), {
            "matches": 1,
            "settled": 1,
            "corners": 0,
            "requests": 1,
            "failures": 0,
            "stopped": None,
        }

    rows = cli.espn_settlement(tmp_path, ["E0"], fd_results().iloc[:0], None, AFTER, good)
    assert "ESPN results: 1 of 1 matches final" in capsys.readouterr().out
    data = {"fixtures": [], "portfolio": {}}
    paper.run(data, tmp_path, fd_results().iloc[:0], now=AFTER, espn_results=rows)
    line = (tmp_path / "paper_trades" / "E0_2627.jsonl").read_text().splitlines()[-1]
    assert json.loads(line)["result_source"] == "espn"
