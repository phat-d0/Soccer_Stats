"""The anytime-goalscorer paper track (player_goal_paper; owner, 9 Nov 2026)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats import paper, sizing
from soccer_stats import player_goal_paper as pgp

TEAMS = ["Arsenal", "Chelsea", "Everton", "Fulham", "Leeds", "Wolves"]
POS = ["GK", "DEF", "DEF", "DEF", "DEF", "MID", "MID", "MID", "FWD", "FWD", "FWD"]


def _pid(team: str, i: int) -> str:
    return f"{TEAMS.index(team)}{i:02d}"


def _name(team: str, i: int) -> str:
    return f"{team} Player{chr(65 + i)}"


def synth_apps(start="2025-08-09", weeks=64, seed=0) -> pd.DataFrame:
    """Six clubs, three matches a week, eleven starters each, goals ~ Poisson."""
    rng = np.random.default_rng(seed)
    rows = []
    t0 = pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=15)
    mid = 0
    for w in range(weeks):
        order = rng.permutation(TEAMS)
        for j in range(3):
            h, a = order[2 * j], order[2 * j + 1]
            k = t0 + pd.Timedelta(days=7 * w, hours=2 * j)
            season = "2526" if k < pd.Timestamp("2026-07-01", tz="UTC") else "2627"
            mid += 1
            for team, opp, home in ((h, a, True), (a, h, False)):
                for i, pos in enumerate(POS):
                    rate = {"GK": 0.002, "DEF": 0.05, "MID": 0.12, "FWD": 0.4}[pos] * (1 + i / 20)
                    goals = int(rng.poisson(rate))
                    xg = round(float(rate * rng.uniform(0.7, 1.3)), 3)
                    rows.append(
                        {
                            "match_id": str(mid),
                            "season": season,
                            "kickoff": k,
                            "team": team,
                            "opponent": opp,
                            "home": home,
                            "player_id": _pid(team, i),
                            "player": _name(team, i),
                            "position": pos,
                            "minutes": 90,
                            "started": True,
                            "shots": float(rng.poisson(rate * 6)),
                            "sot": 0,
                            "goals": goals,
                            "xg": xg,
                            "penalties": 0,
                            "pen_xg": 0.0,
                            "sp_xg": 0.0,
                            "team_shots": 12,
                            "opp_shots": 10,
                        }
                    )
    return pd.DataFrame(rows).sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True)


APPS = synth_apps()
KICKOFF = pd.Timestamp("2026-11-08T15:00:00Z")
INFO = pd.DataFrame(columns=["match_id", "team", "team_xg", "game_state"])


def _target(home="Arsenal", away="Chelsea"):
    return {
        "home": home,
        "away": away,
        "kickoff": KICKOFF,
        "season": "2627",
        "match_model": {home: (1.6, 0.2), away: (1.1, -0.2)},
    }


def _starters(home="Arsenal", away="Chelsea"):
    return {t: [_pid(t, i) for i in range(11)] for t in (home, away)}


def test_starter_chances_use_only_earlier_matches():
    before = APPS[APPS["kickoff"] < KICKOFF]
    p1, d1 = pgp.starter_chances(before, INFO, _target(), _starters())
    assert len(p1) == 22 and all(0 < v < 1 for v in p1.values())
    # Later matches (with wild goal counts) change nothing: no look-ahead.
    later = synth_apps(start="2026-11-08", weeks=6, seed=9)
    later = later.assign(goals=later["goals"] * 7, match_id="L" + later["match_id"])
    p2, d2 = pgp.starter_chances(pd.concat([APPS, later]), INFO, _target(), _starters())
    assert p1 == p2
    assert d1["features_through"] == d2["features_through"] < KICKOFF.isoformat()
    # A forward is likelier to score than a goalkeeper.
    assert p1[_pid("Arsenal", 10)] > p1[_pid("Arsenal", 0)]


def test_blocks_follow_the_judged_read():
    # 28-day blocks from FORWARD_START (10 Oct 2026): 10 Oct, 7 Nov, 5 Dec.
    assert pgp.b_block("2026-11-08T15:00Z") == pd.Timestamp("2026-11-07", tz="UTC")
    assert pgp.b_block("2026-11-06T15:00Z") == pd.Timestamp("2026-10-10", tz="UTC")
    # backtest.walk_forward's weekly grid from 2022-07-01 (a Friday).
    w = pgp.mm_window("2026-11-09T20:00Z")
    assert (w - pgp.MM_START).days % 7 == 0 and w <= pd.Timestamp("2026-11-09")
    assert pd.Timestamp("2026-11-09") - w < pd.Timedelta(days=7)


class FakeSource:
    def __init__(self, apps=APPS):
        self.apps, self.matches, self.err = apps, None, None
        self.loads = 0

    def load(self):
        self.loads += 1
        return True

    def history(self, since):
        return INFO

    def match_model(self, home, away, kickoff):
        return {home: (1.6, 0.2), away: (1.1, -0.2)}

    def rosters(self, team, kickoff):
        a = self.apps[(self.apps["team"] == team) & (self.apps["kickoff"] < kickoff)]
        return dict(a.drop_duplicates("player_id", keep="last")[["player_id", "player"]].values)


def _live_rows(prices, close=None, home="Arsenal", away="Chelsea"):
    """Live log rows: entry at kickoff - 40 min, close at - 10 min."""
    rows = []
    for snap, table, mins in (("entry", prices, 40), ("close", close or {}, 10)):
        at = KICKOFF - pd.Timedelta(minutes=mins)
        for (book, player), yes in table.items():
            rows.append(
                {
                    "league": "E0",
                    "event_id": "ev1",
                    "home": home,
                    "away": away,
                    "kickoff": KICKOFF.isoformat(),
                    "snapshot": snap,
                    "book": book,
                    "player": player,
                    "yes": yes,
                    "no": None,
                    "fetched_at": at.isoformat(),
                }
            )
    return pd.DataFrame(rows)


def _news(home="Arsenal", away="Chelsea", drop=None, at_min=60):
    rows = []
    for side, team in (("home", home), ("away", away)):
        xi = [_name(team, i) for i in range(11) if _name(team, i) != drop]
        rows.append(
            {
                "league": "E0",
                "home": home,
                "away": away,
                "kickoff": KICKOFF.isoformat(),
                "team": team,
                "side": side,
                "confirmed": True,
                "starters": xi,
                "fetched_at": (KICKOFF - pd.Timedelta(minutes=at_min)).isoformat(),
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def chances():
    p, _ = pgp.starter_chances(APPS[APPS["kickoff"] < KICKOFF], INFO, _target(), _starters())
    return p


def _prices(chances):
    """Arsenal's PlayerK clears 20% at FanDuel (25%) and at DraftKings (21%); PlayerJ only at
    the exchange (never judged); everyone else is short."""
    k, j = _pid("Arsenal", 10), _pid("Arsenal", 9)
    out = {}
    for i in range(11):
        for team in ("Arsenal", "Chelsea"):
            out[("fanduel", _name(team, i))] = 1.0 / chances[_pid(team, i)]
    out[("fanduel", _name("Arsenal", 10))] = 1.25 / chances[k]
    out[("draftkings", _name("Arsenal", 10))] = 1.21 / chances[k]
    out[("betfair_ex_uk", _name("Arsenal", 9))] = 2.0 / chances[j]
    return out


def test_rule_bets_best_judged_book_and_draftkings_line(chances):
    src = FakeSource()
    rows = _live_rows(_prices(chances))
    entry = pgp.snapshots(rows, "E0", "entry")["ev1"]
    lineup = pgp.lineup_at(_news(), "Arsenal", "Chelsea", KICKOFF, at=entry["fetched_at"])
    defined = pd.Timestamp("2026-11-01T00:00:00Z")
    trades, rec = pgp.decide(entry, lineup, src, "E0", defined, KICKOFF - pd.Timedelta(minutes=35))
    by = {t["strategy"]: t for t in trades}
    assert set(by) == {"goalscorer", "goalscorer_dk"}
    main, dk = by["goalscorer"], by["goalscorer_dk"]
    pid = _pid("Arsenal", 10)
    assert main["id"] == f"E0|2627|Arsenal|Chelsea|goal|{pid}"
    assert dk["id"] == main["id"] + "|dk" and dk["variant_of"] == "goalscorer"
    assert main["book"] == "fanduel" and dk["book"] == "draftkings"
    assert main["edge"] == pytest.approx(0.25, abs=1e-4)
    assert main["portfolio"] == "goalscorer" and main["p_source"] == "model_b"
    assert main["stake"] == 10 and main["threshold"] == 0.20
    assert not main.get("backcast")  # decided before kickoff, after defined_at
    assert rec["bets"] == {"goalscorer": 1, "goalscorer_dk": 1}
    assert rec["starters_matched"] == 22 and rec["priced_matched"] == 22


def test_no_bet_below_threshold_or_without_lineup(chances):
    prices = {k: v for k, v in _prices(chances).items() if k[0] != "draftkings"}
    prices[("fanduel", _name("Arsenal", 10))] = 1.19 / chances[_pid("Arsenal", 10)]
    rows = _live_rows(prices)
    ledger, checked = {}, {}
    ev, recs, counts = pgp.update_league(
        ledger, checked, rows, _news(), "E0", "2026-11-01T00:00Z",
        KICKOFF - pd.Timedelta(minutes=35), FakeSource(), lambda t: None,
    )  # fmt: skip
    assert ledger == {} and counts["decided"] == 1 and recs[0]["bets"]["goalscorer"] == 0
    # No confirmed lineup by the entry: recorded, no bets, no model run.
    src = FakeSource()
    ledger, checked = {}, {}
    _, recs, counts = pgp.update_league(
        ledger, checked, rows, _news(at_min=5), "E0", "2026-11-01T00:00Z",
        KICKOFF - pd.Timedelta(minutes=35), src, lambda t: None,
    )  # fmt: skip
    assert counts["no_lineup"] == 1 and src.loads == 0 and recs[0]["why"]


def test_decided_once_backcast_close_and_settle(chances):
    pid = _pid("Arsenal", 10)
    k_name = _name("Arsenal", 10)
    close = {("fanduel", k_name): 1.0 / chances[pid], ("draftkings", k_name): 1.1 / chances[pid]}
    rows = _live_rows(_prices(chances), close)
    ledger, checked = {}, {}
    defined = "2026-11-09T12:00:00Z"  # after the match: a backcast
    now = KICKOFF + pd.Timedelta(hours=3)
    goal = {"score90": [1, 1], "goals": [
        {"name": k_name, "own_goal": False, "team_id": "1"},
        {"name": _name("Chelsea", 3), "own_goal": False, "team_id": "2"}]}  # fmt: skip
    ev, _, _ = pgp.update_league(
        ledger, checked, rows, _news(), "E0", defined, now, FakeSource(), lambda t: goal
    )
    main = ledger[f"E0|2627|Arsenal|Chelsea|goal|{pid}"]
    assert main["backcast"] and main["backcast_from"] == "goalscorer_log"
    assert main["status"] == "won" and main["profit"] == pytest.approx(
        10 * (main["odds"] - 1), 0.01
    )
    # Best close is DraftKings' 1.1/p; FanDuel's own close is 1/p.
    assert main["clv_best"] == pytest.approx(1.25 / 1.1 - 1, abs=1e-3)
    assert main["close_book"] == "draftkings"
    assert main["clv_same"] == pytest.approx(0.25, abs=1e-3)
    dk = ledger[main["id"] + "|dk"]
    assert dk["clv_best"] == pytest.approx(1.21 / 1.1 - 1, abs=1e-3)
    assert sum(e["type"] == "open" for e in ev) == 2
    # Decided once: a second run never loads the model again or reopens.
    src = FakeSource()
    ev2, recs2, _ = pgp.update_league(
        ledger, checked, rows, _news(), "E0", defined, now, src, lambda t: goal
    )
    assert src.loads == 0 and recs2 == [] and not [e for e in ev2 if e["type"] == "open"]


def test_settlement_own_goal_and_not_starting():
    t = {"home": "Arsenal", "away": "Chelsea", "team": "Arsenal", "espn_name": "Bukayo Saka",
         "odds": 3.0, "stake": 10.0}  # fmt: skip
    xi = {"home": ["Bukayo Saka"], "away": ["Cole Palmer"]}
    own = {"score90": [0, 1], "goals": [{"name": "Bukayo Saka", "own_goal": True}]}
    assert pgp.settle_fields(t, own, xi)["status"] == "lost"
    real = {"score90": [1, 0], "goals": [{"name": "Bukayo Saka", "own_goal": False}]}
    won = pgp.settle_fields(t, real, xi)
    assert won["status"] == "won" and won["profit"] == 20.0 and won["scored"] == 1
    void = pgp.settle_fields(t, real, {"home": ["Someone Else"], "away": []})
    assert void["status"] == "void" and void["profit"] == 0.0


def test_goal_events_from_the_scoreboard():
    ev = {"competitions": [{"details": [
        {"scoringPlay": True, "ownGoal": False, "team": {"id": "359"},
         "athletesInvolved": [{"displayName": "Bukayo Saka"}]},
        {"scoringPlay": True, "ownGoal": True, "team": {"id": "363"},
         "athletesInvolved": [{"displayName": "Levi Colwill"}]},
        {"scoringPlay": False, "yellowCard": True, "athletesInvolved": [{"displayName": "X"}]},
        {"scoringPlay": True, "shootout": True, "athletesInvolved": [{"displayName": "Y"}]},
    ]}]}  # fmt: skip
    got = pgp.goal_events(ev)
    assert [(g["name"], g["own_goal"]) for g in got] == [
        ("Bukayo Saka", False),
        ("Levi Colwill", True),
    ]


def test_sizing_uses_model_b():
    assert sizing.sizing_source(pgp.rule()) == "model"
    t = {"model_p": 0.31, "strategy": "goalscorer"}
    assert sizing.chance(t, sizing.sizing_source(pgp.rule())) == 0.31


def test_ledger_files_stay_out_of_the_match_record(tmp_path):
    d = tmp_path / "paper_trades"
    d.mkdir()
    t = {"id": "E0|2627|Arsenal|Chelsea|goal|1", "home": "Arsenal", "away": "Chelsea"}
    pgp.append_events(tmp_path, [{"type": "open", **t}])
    assert (d / "E0_goalscorer_2627.jsonl").exists()
    assert paper.load_ledger(tmp_path, "E0") == {}
    assert set(pgp.load_ledger(tmp_path, "E0")) == {t["id"]}


def test_portfolio_section_and_run_without_a_log(tmp_path):
    (tmp_path / "paper_trades").mkdir()
    res = pgp.run(tmp_path, KICKOFF)  # no live goalscorer log: nothing written
    assert res["trades"] == [] and res["events"] == 0
    assert not (tmp_path / "paper_trades" / pgp.DEFINED_FILE).exists()
    trade = {
        "id": "E0|2627|Arsenal|Chelsea|goal|1", "strategy": "goalscorer", "league": "E0",
        "season": "2627", "home": "Arsenal", "away": "Chelsea", "kickoff": KICKOFF.isoformat(),
        "opened_at": (KICKOFF - pd.Timedelta(minutes=40)).isoformat(), "market": pgp.MARKET,
        "odds": 4.0, "model_p": 0.3, "edge": 0.2, "stake": 10.0, "status": "won",
        "profit": 30.0, "portfolio": "goalscorer", "bet_type": "goalscorer", "clv_best": 0.05,
        "settled_at": KICKOFF.isoformat(),
    }  # fmt: skip
    out = paper.portfolios_section(
        {}, [trade], {}, goalscorer={"trades": [trade], "defined_at": "2026-11-09T12:00:00Z"}
    )
    gs = next(p for p in out if p["id"] == "goalscorer")
    keys = [s["key"] for s in gs["live"]["strategies"]]
    assert keys == ["goalscorer", "goalscorer_dk"]
    main = gs["live"]["strategies"][0]
    assert main["summary"]["trades"] == 1 and main["descriptive"]
    assert main["clv"]["best"]["mean"] == 0.05 and main["backcast"]["trades"] == 1
    assert main["sizing"]["kelly"]["bets"] == 1
    json.dumps(out, default=str)
    mm = next(p for p in out if p["id"] == "moneyline")
    assert mm["live"]["trades"] == []
