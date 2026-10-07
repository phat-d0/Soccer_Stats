import json

import pandas as pd

from soccer_stats import paper
from soccer_stats import trades as tr

NOW = pd.Timestamp("2026-10-08 12:00", tz="UTC")
KICKOFF = pd.Timestamp("2026-10-10 14:00", tz="UTC")


def card(odds_draw=5.0, kickoff=KICKOFF, **kw):
    c = {
        "home": "Arsenal",
        "away": "Leeds",
        "kickoff": kickoff.isoformat(),
        "p": {"home": 0.61, "draw": 0.24, "away": 0.15, "over25": 0.55, "under25": 0.45},
        "p_base": {"home": 0.63, "draw": 0.23, "away": 0.14, "over25": 0.56},
        "odds": {"home": 1.4, "draw": odds_draw, "away": 7.5, "over25": 1.7, "under25": 2.1},
        "news_applied": True,
        "low_data": False,
    }
    c.update(kw)
    return c


FETCHED = NOW - pd.Timedelta(minutes=30)


def src(fetched=FETCHED):
    return {"name": "DraftKings", "fetched_at": fetched.isoformat()}


def results(home_goals=1, away_goals=1, date="2026-10-10"):
    return pd.DataFrame(
        {
            "season": ["2627"],
            "date": [pd.Timestamp(date)],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_goals": [home_goals],
            "away_goals": [away_goals],
            "close_home": [1.45],
            "close_draw": [4.6],
            "close_away": [7.0],
        }
    )


def build(tmp_path, cards, source, now, res=None):
    """One scheduled build: load, update, append."""
    ledger = paper.load_ledger(tmp_path)
    events, note = paper.update_ledger(ledger, cards, source, res, now)
    paper.append_events(tmp_path, events)
    return paper.load_ledger(tmp_path), events, note


def test_opens_once_and_entry_never_changes(tmp_path):
    led, ev, _ = build(tmp_path, [card()], src(), NOW)
    assert len(ev) == 1 and ev[0]["type"] == "open"
    t = led["E0|2627|Arsenal|Leeds"]
    assert t["market"] == "draw" and t["odds"] == 5.0 and t["edge"] == 0.2
    assert t["model_p_base"] == 0.23 and t["news_applied"] is True
    assert t["hours_to_kickoff"] == 50.0
    entry = {k: t[k] for k in tr.ENTRY_FIELDS}

    # Same build again: nothing new.
    led, ev, _ = build(tmp_path, [card()], src(), NOW)
    assert ev == []
    # The price shortens (edge gone): the entry stays, the close follows the price.
    later = NOW + pd.Timedelta(hours=10)
    led, ev, _ = build(tmp_path, [card(odds_draw=4.2)], src(later), later)
    t = led["E0|2627|Arsenal|Leeds"]
    assert {k: t[k] for k in tr.ENTRY_FIELDS} == entry
    assert t["close_odds"] == 4.2 and t["clv_dk"] > 0
    lines = (tmp_path / "paper_trades" / "E0_2627.jsonl").read_text().splitlines()
    assert sum(json.loads(x)["type"] == "open" for x in lines) == 1


def test_close_freezes_at_kickoff_and_settles(tmp_path):
    build(tmp_path, [card()], src(), NOW)
    after = KICKOFF + pd.Timedelta(hours=3)
    # Odds fetched after kickoff don't move the close; no result yet: still open.
    led, ev, _ = build(tmp_path, [card(odds_draw=9.0)], src(after), after)
    assert ev == [] and led["E0|2627|Arsenal|Leeds"]["close_odds"] == 5.0
    led, ev, _ = build(tmp_path, [], src(after), after, results(1, 1))
    t = led["E0|2627|Arsenal|Leeds"]
    assert (t["status"], t["score"], t["profit"]) == ("won", "1-1", 40.0)
    assert t["clv_pinnacle"] is not None
    led, ev, _ = build(tmp_path, [], src(after), after, results(1, 1))
    assert ev == []  # settled once


def test_loss_and_voids(tmp_path):
    build(tmp_path, [card()], src(), NOW)
    after = KICKOFF + pd.Timedelta(hours=3)
    led, _, _ = build(tmp_path, [], src(after), after, results(2, 0))
    assert led["E0|2627|Arsenal|Leeds"]["profit"] == -10.0

    d2 = tmp_path / "moved"
    build(d2, [card()], src(), NOW)
    moved = card(kickoff=KICKOFF + pd.Timedelta(days=5))
    led, _, _ = build(d2, [moved], src(), NOW + pd.Timedelta(hours=1))
    assert led["E0|2627|Arsenal|Leeds"]["status"] == "void"

    d3 = tmp_path / "noresult"
    build(d3, [card()], src(), NOW)
    led, _, _ = build(d3, [], src(), KICKOFF + pd.Timedelta(days=15), results().iloc[:0])
    assert led["E0|2627|Arsenal|Leeds"]["status"] == "void"


def test_only_fresh_draftkings_odds_open_trades(tmp_path):
    led, ev, note = build(tmp_path, [card()], src(NOW - pd.Timedelta(hours=4)), NOW)
    assert ev == [] and "3 hours" in note
    led, ev, note = build(tmp_path, [card()], {"name": "football-data"}, NOW)
    assert ev == [] and "unavailable" in note
    led, ev, _ = build(tmp_path, [card(low_data=True)], src(), NOW)
    assert ev == []
    led, ev, _ = build(tmp_path, [card(kickoff=NOW - pd.Timedelta(minutes=5))], src(), NOW)
    assert ev == []  # already kicked off


def test_run_fails_safe_and_fills_portfolio(tmp_path):
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    assert paper.run(data, tmp_path / "missing", None, now=NOW) == 0
    assert "unavailable" in data["portfolio"]["live"]["error"]
    (tmp_path / "paper_trades").mkdir()
    (tmp_path / "paper_trades" / "E0_2627.jsonl").write_text("{not json\n")
    assert paper.run(data, tmp_path, None, now=NOW) == 0
    assert "couldn't be read" in data["portfolio"]["live"]["error"]

    good = tmp_path / "good"
    good.mkdir()
    (good / "backtest").mkdir()
    (good / "backtest" / "E0_dk.json").write_text(json.dumps({"summary": {"trades": 3}}))
    assert paper.run(data, good, None, now=NOW) == 1
    live = data["portfolio"]["live"]
    assert live["error"] is None and live["summary"]["open"] == 1
    assert data["portfolio"]["backtest"]["summary"]["trades"] == 3


# ---------- the live threshold: the minimum edge learned from history ----------


def test_paper_threshold_from_the_backtest_file():
    assert tr.paper_threshold(None)["threshold"] == tr.PAPER_EDGE
    assert tr.paper_threshold({"summary": {}})["source"] == "default"  # older file
    assert tr.paper_threshold({"edge_threshold": None})["threshold"] == tr.PAPER_EDGE
    r = tr.paper_threshold({"edge_threshold": {"min_edge": None, "note": "Too few bets."}})
    assert r["threshold"] is None and r["source"] == "history" and "Too few bets." in r["note"]
    r = tr.paper_threshold({"edge_threshold": {"min_edge": 0.07, "note": None}})
    assert r == {"threshold": 0.07, "source": "history", "note": None}
    assert tr.best_pick({"home": 0.9}, {"home": 2.0}, None) is None  # as the app's bestPick


def _ledger_build(tmp_path, cards, now, threshold, res=None):
    ledger = paper.load_ledger(tmp_path)
    events, _ = paper.update_ledger(
        ledger, cards, src(now - pd.Timedelta(minutes=30)), res, now, threshold=threshold
    )
    paper.append_events(tmp_path, events)
    return paper.load_ledger(tmp_path), events


def test_null_level_opens_nothing_but_open_trades_still_settle(tmp_path):
    led, ev = _ledger_build(tmp_path, [card()], NOW, 0.12)  # opened under the old rule
    assert len(ev) == 1 and led["E0|2627|Arsenal|Leeds"]["threshold"] == 0.12
    other = card(home="Chelsea", away="Burnley")
    led, ev = _ledger_build(tmp_path, [card(), other], NOW + pd.Timedelta(hours=1), None)
    assert not any(e["type"] == "open" for e in ev)  # no level learned: no new trades
    after = KICKOFF + pd.Timedelta(hours=3)
    led, ev = _ledger_build(tmp_path, [], after, None, results(1, 1))
    assert led["E0|2627|Arsenal|Leeds"]["status"] == "won"


def test_a_learned_level_trades_only_at_or_above_it(tmp_path):
    # The draw at 5.0 with p 0.24 is a 20% edge.
    _, ev = _ledger_build(tmp_path / "a", [card()], NOW, 0.25)
    assert ev == []
    led, ev = _ledger_build(tmp_path / "b", [card()], NOW, 0.20)
    assert [e["type"] for e in ev] == ["open"]
    assert led["E0|2627|Arsenal|Leeds"]["threshold"] == 0.20


def test_run_uses_the_backtest_level_and_says_why(tmp_path):
    (tmp_path / "backtest").mkdir()
    bt = tmp_path / "backtest" / "E0_dk.json"
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}

    bt.write_text(json.dumps({"summary": {}, "edge_threshold": {"min_edge": None, "note": "x."}}))
    assert paper.run(data, tmp_path, None, now=NOW) == 0
    ml = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "moneyline")
    assert ml["live"]["rule"]["threshold"] is None and "No new match" in ml["live"]["note"]
    assert data["portfolio"]["rule"]["threshold"] is None
    others = [p for p in data["portfolio"]["portfolios"] if p["id"] != "moneyline"]
    assert all("rule" not in p["live"] for p in others)

    bt.write_text(json.dumps({"summary": {}}))  # older file: the fixed 12%
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    assert paper.run(data, tmp_path, None, now=NOW) == 1
    assert data["portfolio"]["rule"]["threshold_source"] == "default"
    led = paper.load_ledger(tmp_path)
    assert led["E0|2627|Arsenal|Leeds"]["threshold"] == tr.PAPER_EDGE
