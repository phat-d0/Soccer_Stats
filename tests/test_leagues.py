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


def test_registry_matches_the_loaders_and_every_league_is_live():
    assert set(lgs.LEAGUES) == {"E0", "SP1", "D1", "I1", "F1", "E1"}
    assert lgs.live_codes() == ["E0", "SP1", "D1", "I1", "F1", "E1"]  # primary first
    for code, lg in lgs.LEAGUES.items():
        assert xg.LEAGUES.get(code) == lg.understat  # Understat names stay in step
        assert code in fd.LEAGUES  # football-data knows the division
        assert feed.SPORTS[code] == lg.odds_sport
    assert lgs.get("E0").odds_policy == "always"
    assert {lgs.get(c).odds_policy for c in ("SP1", "D1", "I1", "F1", "E1")} == {"matchday"}
    assert lgs.get("E1").understat is None and feed.SPORTS["E1"] == "soccer_efl_champ"
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
    off = dict(lgs.LEAGUES, SP1=replace(lgs.LEAGUES["SP1"], live=False))
    monkeypatch.setattr(feed, "LEAGUES", off)
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should not call"))
    events, status = feed.fetch_odds("SP1", raw_dir=tmp_path, api_key="k", now=T)
    assert events is None and "not live" in status.error


def test_a_live_matchday_league_fetches_only_near_a_match(tmp_path, monkeypatch):
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


def _meta(tmp_path, code, fetched_at, credits):
    path = tmp_path / f"odds_api_{code}_draftkings.json"
    path.write_text("[]")
    meta = {"fetched_at": fetched_at.isoformat(), "credits_left": credits, "last_cost": 2}
    path.with_suffix(".meta.json").write_text(json.dumps(meta))


def test_the_premier_league_keeps_its_cadence_with_six_leagues_live():
    # Healthy balance: the even split doesn't bind; every league sits at its floor.
    assert feed.refresh_interval_hours(22_000, 2, T, share=6) == 1.0
    # Low balance: an even split would slow E0, so it budgets alone (publish passes 1).
    assert feed.refresh_interval_hours(3_000, 2, T, share=6) > 1.0
    assert feed.refresh_interval_hours(3_000, 2, T) == 1.0
    # Matchday leagues keep a 3,000-credit reserve: they stop first.
    reserve = feed.MATCHDAY_RESERVE_CREDITS
    assert feed.refresh_interval_hours(2_900, 2, T, share=6, reserve=reserve) == float("inf")


def test_a_matchday_league_pauses_below_its_reserve_on_the_freshest_balance(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should not call"))
    near = [T + pd.Timedelta(hours=30)]
    # SP1 last saw 20,000 credits a day ago; E0 saw 2,500 an hour ago (the shared key).
    _meta(tmp_path, "SP1", T - pd.Timedelta(days=1), 20_000)
    _meta(tmp_path, "E0", T - pd.Timedelta(hours=1), 2_500)
    _, status = feed.fetch_odds("SP1", raw_dir=tmp_path, api_key="k", now=T, kickoffs=near)
    assert status.refresh_hours is None and "2500 credits left" in status.error
    assert "keeping 3000" in status.error
    # E0 itself keeps fetching on the same balance (only the 20-credit floor applies).
    calls = []

    class Resp:
        ok, status_code, text = True, 200, "[]"
        headers = {"x-requests-remaining": "2498", "x-requests-last": "2"}

    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(url) or Resp())
    _, status = feed.fetch_odds("E0", raw_dir=tmp_path, api_key="k", now=T)
    assert len(calls) == 1 and status.refresh_hours == 1.0


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
    assert feed.estimate_credits("D1", k, start, end)["calls"] > 0


def test_team_total_snapshots():
    k = T.normalize() + pd.Timedelta(hours=15)  # a 15:00 UTC kickoff
    runs = feed.publish_runs(k - pd.Timedelta(days=2), k + pd.Timedelta(hours=1))
    taken = feed.snapshot_runs(k, (24.0, 6.0), runs)
    assert taken["24h"] == k - pd.Timedelta(hours=24) + pd.Timedelta(minutes=7)
    assert taken["6h"] == k - pd.Timedelta(hours=6) + pd.Timedelta(minutes=7)
    assert taken["close"] == k - pd.Timedelta(minutes=8)  # the 14:52 run
    # Never a run at or after kickoff.
    assert all(t < k for t in taken.values())
    start, end = T.normalize(), T.normalize() + pd.Timedelta(days=7)
    early = T.normalize() + pd.Timedelta(days=1, hours=5)  # 05:00 UTC: hourly runs only
    ks = [k, early]
    lean = feed.estimate_snapshot_credits("E0", ks, start, end, "lean")
    base = feed.estimate_snapshot_credits("E0", ks, start, end, "base")
    rich = feed.estimate_snapshot_credits("E0", ks, start, end, "rich", markets=2)
    assert (lean["calls"], base["calls"], rich["calls"]) == (2, 4, 6)
    assert lean["credits"] == 2 and rich["credits"] == 12  # 1 credit per market per call
    assert base["close_in_window"] == 1  # 05:00's close is the 04:07 run, 53 min out
    # A match outside the month costs nothing in it; matches_by counts kickoffs.
    assert feed.estimate_snapshot_credits("E0", ks, end, end + pd.Timedelta(days=7))["calls"] == 0
    assert feed.matches_by(ks, start, 1) == 2 and feed.matches_by(ks, k, 12 / 168) == 1
    assert "E1" not in feed.TEAM_TOTAL_LEAGUES  # no team totals on the API


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
        "live": True,
        "fixtures": 1,
        "odds": "football-data",
    }
    json.dumps(rows)


def test_odds_log_writes_each_league_to_its_own_file(tmp_path):
    # SP1 without DraftKings odds (none fetched yet) logs nothing; E0 logs as before.
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


def test_moneyline_backtest_carries_each_leagues_level(tmp_path):
    (tmp_path / "backtest").mkdir()
    e0 = {"summary": {"trades": 0}, "edge_threshold": {"min_edge": 0.08, "note": None}}
    (tmp_path / "backtest" / "E0_dk.json").write_text(json.dumps(e0))
    d = _two_league_data({"league": "SP1", **src()})
    paper.run(d, tmp_path, None, now=NOW)
    ml = next(p for p in d["portfolio"]["portfolios"] if p["id"] == "moneyline")
    by = ml["backtest"]["edge_threshold"]["by_league"]
    assert by["E0"]["min_edge"] == 0.08
    # SP1 has no SP1_dk.json, so its level comes from the lab's committed file (null
    # since bake-off 3), or the rule's own note if that file has no entry.
    lab = paper.league_backtest(tmp_path, "SP1")
    assert by["SP1"]["min_edge"] is None and by["SP1"]["note"]
    if lab is not None:
        assert by["SP1"] == lab["edge_threshold"]
    else:
        assert "this league" in by["SP1"]["note"]
    assert ml["backtest"]["edge_threshold"]["min_edge"] == 0.08  # top level unchanged

    # One league only: no by_league (the app falls back to the top level).
    d = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    paper.run(d, tmp_path, None, now=NOW)
    ml = next(p for p in d["portfolio"]["portfolios"] if p["id"] == "moneyline")
    assert "by_league" not in ml["backtest"]["edge_threshold"]


def test_championship_runs_goals_only(league, tmp_path):
    """E1 has no Understat xG: the goals-only fit, its own cards, live odds, no trades."""
    from soccer_stats.publish import build_data

    df, _ = league
    df = df.copy()
    df["date"] = df["date"] + (pd.Timestamp.now().normalize() - df["date"].max())
    df["league"], df["season"] = "E1", "x"
    df[["home_xg", "away_xg"]] = float("nan")  # what with_xg gives a league it can't cover
    for col in ["odds_home", "odds_draw", "odds_away", "close_home", "close_draw", "close_away"]:
        df[col] = 3.0
    fixtures = pd.DataFrame(
        {
            "kickoff": [pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=2)],
            "home": ["T02"],
            "away": ["T03"],
            **{c: [2.5] for c in ("odds_home", "odds_draw", "odds_away")},
            **{c: [1.9] for c in ("odds_over25", "odds_under25")},
        }
    )
    d = build_data(df, fixtures, xg_error=None, league="E1")
    assert d["xg_weight"] == 0.0 and d["league"] == "Championship"
    assert [f["league"] for f in d["fixtures"]] == ["E1"]
    assert lgs.get("E1").live and lgs.get("E1").odds_policy == "matchday"
    assert tr.paper_threshold(None, "E1")["threshold"] is None


def test_season_kickoffs_from_football_data(tmp_path, monkeypatch):
    path = tmp_path / "E1_2526.csv"
    path.write_text(
        "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG\n"
        "E1,04/10/2025,15:00,Leeds,Hull,1,0\n"
        "E1,07/10/2025,19:45,Hull,Leeds,0,0\n"
        "E1,01/02/2026,,Hull,Leeds,0,0\n"
    )
    monkeypatch.setattr(fd, "download", lambda *a, **k: path)
    ks = fd.season_kickoffs("E1", 2025)
    assert list(ks) == [
        pd.Timestamp("2025-10-04 14:00", tz="UTC"),  # BST
        pd.Timestamp("2025-10-07 18:45", tz="UTC"),
        pd.Timestamp("2026-02-01 15:00", tz="UTC"),  # GMT; no time = 15:00
    ]


def test_lab_levels_fill_in_for_leagues_without_a_backtest(tmp_path, monkeypatch):
    lab = tmp_path / "min_edge.json"
    lab.write_text(
        json.dumps(
            {
                "method": "model at Pinnacle early",
                "leagues": {
                    "SP1": {"min_edge": 0.07, "note": None},
                    "E0": {"min_edge": 0.01, "note": None},  # never used for E0
                },
            }
        )
    )
    monkeypatch.setattr(paper, "LAB_LEVELS", lab)
    log = tmp_path / "log"
    (log / "backtest").mkdir(parents=True)
    assert paper.league_backtest(log, "SP1") == {"edge_threshold": {"min_edge": 0.07, "note": None}}
    assert paper.league_backtest(log, "E0") is None  # E0 reads E0_dk.json only
    assert paper.league_backtest(log, "D1") is None
    # A league's own DraftKings backtest wins over the lab file.
    own = {"edge_threshold": {"min_edge": 0.03, "note": None}}
    (log / "backtest" / "SP1_dk.json").write_text(json.dumps(own))
    assert paper.league_backtest(log, "SP1") == own

    (log / "backtest" / "SP1_dk.json").unlink()
    d = _two_league_data({"league": "SP1", **src()})
    paper.run(d, log, None, now=NOW)
    assert d["portfolio"]["rules"]["SP1"]["threshold"] == 0.07
    assert paper.load_ledger(log, "SP1")  # the SP1 trade (20% edge) opened at 7%


def test_draftkings_names_map_to_football_data_in_new_leagues():
    """DraftKings spellings from the first six-league publish land on football-data names,
    so a priced match joins its scheduled fixture instead of being added a second time."""
    seen = {
        "Borussia Dortmund": "Dortmund",
        "Borussia Monchengladbach": "M'gladbach",
        "1. FC Köln": "FC Koln",
        "Atlético Madrid": "Ath Madrid",
        "Deportivo La Coruña": "La Coruna",
        "AS Roma": "Roma",
        "AC Milan": "Milan",
        "Paris Saint Germain": "Paris SG",
        "RC Lens": "Lens",
    }
    for dk, name in seen.items():
        assert feed._team(dk, {name, "Barcelona"}) == name
    events = [
        {
            "home_team": "Borussia Dortmund",
            "away_team": "Werder Bremen",
            "commence_time": "2026-10-09T18:30:00Z",
            "bookmakers": [
                {
                    "key": feed.BOOKMAKER,
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Borussia Dortmund", "price": 1.6},
                                {"name": "Draw", "price": 4.2},
                                {"name": "Werder Bremen", "price": 5.0},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    row = feed.parse_odds(events, {"Dortmund", "Werder Bremen"}).iloc[0]
    assert (row["home"], row["away"]) == ("Dortmund", "Werder Bremen")
    assert row["odds_home"] == 1.6 and row["odds_away"] == 5.0
