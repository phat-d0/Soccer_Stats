import json
from dataclasses import replace

import pandas as pd
import pytest
import requests
from test_paper import KICKOFF, NOW, card, src

from soccer_stats import data as fd
from soccer_stats import leagues as lgs
from soccer_stats import odds_feed as feed
from soccer_stats import odds_log as ol
from soccer_stats import paper, xg
from soccer_stats import trades as tr

T = pd.Timestamp("2026-11-06 12:00", tz="UTC")  # a Friday


# ---------- the registry ----------


def test_registry_matches_the_loaders_and_only_e0_is_live():
    assert set(lgs.LEAGUES) == {"E0", "SP1", "D1", "I1", "F1"}
    assert lgs.live_codes() == ["E0"]
    for code, lg in lgs.LEAGUES.items():
        assert xg.LEAGUES[code] == lg.understat  # Understat names stay in step
        assert code in fd.LEAGUES  # football-data knows the division
        assert feed.SPORTS[code] == lg.odds_sport
    assert lgs.get("E0").odds_policy == "always"
    assert {lgs.get(c).odds_policy for c in ("SP1", "D1", "I1", "F1")} == {"matchday"}
    assert feed.SPORTS["E0"] == "soccer_epl"  # the live path is unchanged
    assert lgs.name("XX") == "XX"


# ---------- refresh policy ----------


def test_policy_floor():
    in_h = lambda h: [T + pd.Timedelta(hours=h)]  # noqa: E731
    # Premier League: as before, hourly, every 30 minutes within 2 hours of a kickoff.
    assert feed.policy_floor("E0", [], T) == 1.0
    assert feed.policy_floor("E0", in_h(1.5), T) == 0.5
    # Matchday leagues: nothing without a match within 48 hours, then 3 h / 1 h / 30 min.
    assert feed.policy_floor("SP1", [], T) is None
    assert feed.policy_floor("SP1", in_h(60), T) is None
    assert feed.policy_floor("SP1", in_h(-1), T) is None  # already kicked off
    assert feed.policy_floor("SP1", in_h(30), T) == 3.0
    assert feed.policy_floor("SP1", in_h(5), T) == 1.0
    assert feed.policy_floor("SP1", in_h(1), T) == 0.5


def test_refresh_interval_splits_the_budget_between_live_leagues():
    one = feed.refresh_interval_hours(5000, 2, T, min_hours=0.0)
    two = feed.refresh_interval_hours(5000, 2, T, min_hours=0.0, share=2)
    assert two == pytest.approx(2 * one, rel=0.01)


def test_a_league_that_is_not_live_is_never_fetched(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should not call"))
    events, status = feed.fetch_odds("SP1", raw_dir=tmp_path, api_key="k", now=T)
    assert events is None and "not live" in status.error


def test_a_live_matchday_league_fetches_only_near_a_match(tmp_path, monkeypatch):
    on = dict(lgs.LEAGUES, SP1=replace(lgs.LEAGUES["SP1"], live=True))
    monkeypatch.setattr(feed, "LEAGUES", on)
    calls = []

    class Resp:
        ok, status_code, text = True, 200, "[]"
        headers = {"x-requests-remaining": "20000", "x-requests-last": "2"}

    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(url) or Resp())
    far = [T + pd.Timedelta(days=4)]
    events, status = feed.fetch_odds("SP1", raw_dir=tmp_path, api_key="k", now=T, kickoffs=far)
    assert calls == [] and events is None and "48 hours" in status.error
    near = [T + pd.Timedelta(hours=30)]
    events, status = feed.fetch_odds("SP1", raw_dir=tmp_path, api_key="k", now=T, kickoffs=near)
    assert len(calls) == 1 and "soccer_spain_la_liga" in calls[0] and events == []
    assert (tmp_path / "odds_api_SP1_draftkings.json").exists()  # its own cache file


# ---------- credit estimate ----------


def test_publish_runs_follow_the_workflow_schedule():
    runs = feed.publish_runs(T.normalize(), T.normalize() + pd.Timedelta(days=1))
    assert len(runs) == 24 + 12 * 3
    assert runs[0] == T.normalize() + pd.Timedelta(minutes=7)


def test_estimate_credits():
    start, end = T.normalize(), T.normalize() + pd.Timedelta(days=7)
    # No matches: a matchday league costs nothing; the Premier League refreshes hourly.
    assert feed.estimate_credits("SP1", [], start, end)["credits"] == 0
    e0 = feed.estimate_credits("E0", [], start, end)
    assert e0["calls"] == 24 * 7 and e0["credits"] == 2 * 24 * 7
    # One kickoff: calls start 48 hours out and thicken toward kickoff.
    k = [start + pd.Timedelta(days=3, hours=15)]
    one = feed.estimate_credits("SP1", k, start, end)
    assert one["matches"] == 1 and 16 + 4 < one["calls"] < 48
    two = feed.estimate_credits("SP1", k + [k[0] + pd.Timedelta(days=1)], start, end)
    assert one["calls"] < two["calls"] < 2 * one["calls"] + 1
    # A league priced while its live flag is off (the estimate ignores the flag).
    assert not lgs.get("D1").live and feed.estimate_credits("D1", k, start, end)["calls"] > 0


# ---------- a second league through publish, the odds log and paper trades ----------


def _two_league_data(sp1_source=None):
    e0 = card()
    e0["league"] = "E0"
    sp1 = card(home="Real Madrid", away="Getafe")
    sp1["league"] = "SP1"
    d = {
        "fixtures": [e0, sp1],
        "odds_source": src(),
        "odds_sources": {"E0": src(), "SP1": sp1_source or {"league": "SP1", "name": None}},
        "leagues": [
            {"code": "E0", "fixtures": 1},
            {"code": "SP1", "fixtures": 1},
            {"code": "D1", "fixtures": 0},
        ],
        "portfolio": {},
    }
    return d


def test_league_list_and_cards(league):
    from soccer_stats.publish import build_data, league_list

    df, _ = league
    df = df.copy()
    df["date"] = df["date"] + (pd.Timestamp.now().normalize() - df["date"].max())
    df["league"], df["season"] = "SP1", "x"
    for col in ["odds_home", "odds_draw", "odds_away", "close_home", "close_draw", "close_away"]:
        df[col] = 3.0
    fixtures = pd.DataFrame(
        {
            "kickoff": [pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=3)],
            "home": ["T00"],
            "away": ["T01"],
            **{c: [2.5] for c in ("odds_home", "odds_draw", "odds_away")},
            **{c: [1.9] for c in ("odds_over25", "odds_under25")},
        }
    )
    d = build_data(df, fixtures, xg_error=None, league="SP1")
    assert d["league"] == "La Liga" and d["league_code"] == "SP1"
    assert [f["league"] for f in d["fixtures"]] == ["SP1"]
    rows = league_list(d["fixtures"], {"SP1": {"name": "football-data"}})
    sp1 = next(r for r in rows if r["code"] == "SP1")
    assert sp1 == {
        "code": "SP1",
        "name": "La Liga",
        "live": False,
        "fixtures": 1,
        "odds": "football-data",
    }
    json.dumps(rows)


def test_odds_log_writes_each_league_to_its_own_file(tmp_path):
    # SP1 without DraftKings odds (live off) logs nothing; E0 logs as before.
    rows = ol.rows_from_data(_two_league_data(), NOW)
    assert {r["league"] for r in rows} == {"E0"}
    rows = ol.rows_from_data(_two_league_data({"league": "SP1", **src()}), NOW)
    assert {r["league"] for r in rows} == {"E0", "SP1"}
    assert ol.append(tmp_path, rows) == 4
    names = sorted(p.name for p in (tmp_path / "odds_log").iterdir())
    assert names == ["E0_2026-10.jsonl", "SP1_2026-10.jsonl"]
    assert ol.append(tmp_path, rows) == 0
    assert set(ol.load(tmp_path, "SP1")["home"]) == {"Real Madrid"}


def test_paper_threshold_per_league():
    assert tr.paper_threshold(None)["threshold"] == tr.PAPER_EDGE  # E0 fallback unchanged
    r = tr.paper_threshold(None, "SP1")
    assert r["threshold"] is None and r["source"] == "none" and "this league" in r["note"]
    level = {"edge_threshold": {"min_edge": 0.05, "note": None}}
    assert tr.paper_threshold(level, "SP1")["threshold"] == 0.05


def test_second_league_flows_through_paper_without_trading(tmp_path):
    d = _two_league_data({"league": "SP1", **src()})
    assert paper.leagues_in_play(d) == ["E0", "SP1"]
    assert paper.run(d, tmp_path, None, now=NOW) == 1  # the E0 trade only (12% fallback)
    led = {}
    for lg in ("E0", "SP1"):
        led.update(paper.load_ledger(tmp_path, lg))
    assert list(led) == ["E0|2627|Arsenal|Leeds"]
    rules = d["portfolio"]["rules"]
    assert rules["E0"]["threshold"] == tr.PAPER_EDGE and rules["SP1"]["threshold"] is None
    assert d["portfolio"]["rule"]["threshold"] == tr.PAPER_EDGE  # the primary league's

    # SP1 learns a level: its trade opens into its own ledger file, id prefixed SP1.
    (tmp_path / "backtest").mkdir()
    level = {"summary": {}, "edge_threshold": {"min_edge": 0.05, "note": None}}
    (tmp_path / "backtest" / "SP1_dk.json").write_text(json.dumps(level))
    d = _two_league_data({"league": "SP1", **src()})
    assert paper.run(d, tmp_path, None, now=NOW) == 1
    assert (tmp_path / "paper_trades" / "SP1_2627.jsonl").exists()
    t = paper.load_ledger(tmp_path, "SP1")["SP1|2627|Real Madrid|Getafe"]
    assert t["league"] == "SP1" and t["threshold"] == 0.05
    ml = next(p for p in d["portfolio"]["portfolios"] if p["id"] == "moneyline")
    assert {x["league"] for x in ml["live"]["trades"]} == {"E0", "SP1"}

    # Settling uses each league's own results.
    after = KICKOFF + pd.Timedelta(hours=3)
    res = pd.DataFrame(
        {
            "league": ["E0", "SP1"],
            "season": ["2627", "2627"],
            "date": [pd.Timestamp("2026-10-10")] * 2,
            "home": ["Arsenal", "Real Madrid"],
            "away": ["Leeds", "Getafe"],
            "home_goals": [1, 2],
            "away_goals": [1, 0],
        }
    )
    d = _two_league_data({"league": "SP1", **src()})
    d["fixtures"] = []
    paper.run(d, tmp_path, res, now=after)
    led = {**paper.load_ledger(tmp_path, "E0"), **paper.load_ledger(tmp_path, "SP1")}
    assert all(t["status"] in ("won", "lost") for t in led.values())
