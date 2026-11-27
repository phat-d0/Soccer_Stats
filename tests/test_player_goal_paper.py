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
        self.last = None

    def load(self):
        self.loads += 1
        return True

    def history(self, since):
        return INFO

    def match_model(self, home, away, kickoff):
        return {home: (1.6, 0.2), away: (1.1, -0.2)}

    def chances(self, target, starters, known=()):
        return pgp.starter_chances(self.apps, INFO, target, starters)

    def last_kickoff(self, kickoff, known=()):
        return self.last

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


def _matches(seed=1):
    """Football-data-like results for the six clubs, weekly from Aug 2024."""
    rng = np.random.default_rng(seed)
    rows = []
    for w in range(115):
        order = rng.permutation(TEAMS)
        for j in range(3):
            rows.append(
                {
                    "date": pd.Timestamp("2024-08-10") + pd.Timedelta(days=7 * w),
                    "home": order[2 * j],
                    "away": order[2 * j + 1],
                    "home_goals": int(rng.poisson(1.5)),
                    "away_goals": int(rng.poisson(1.1)),
                    "season": "2425",
                }
            )
    return pd.DataFrame(rows)


def test_match_model_equals_walk_forward():
    from soccer_stats import backtest

    m = _matches()
    target = m.iloc[-1]
    got = pgp.match_model(m, None, target["home"], target["away"], target["date"])
    lo = pgp.mm_window(target["date"])
    wf = backtest.walk_forward(m, start=lo, model_factory=_dc)
    row = wf[(wf["home"] == target["home"]) & (wf["date"] == target["date"])].iloc[0]
    xg, gs = got[target["home"]]
    assert xg == pytest.approx(row["exp_home"], rel=1e-9)
    assert gs == pytest.approx(row["p_home"] - row["p_away"], rel=1e-9)
    assert got[target["away"]][1] == pytest.approx(-gs)


def _dc():
    import functools

    from soccer_stats.models import DixonColes
    from soccer_stats.models.player_goals import FIT_OPTIONS

    return functools.partial(DixonColes, xg_weight=0.7, options=FIT_OPTIONS)()


def test_stale_features_are_flagged_and_counted(chances):
    src = FakeSource()
    src.last = KICKOFF - pd.Timedelta(days=3)  # a league match the files don't hold
    rows = _live_rows(_prices(chances))
    entry = pgp.snapshots(rows, "E0", "entry")["ev1"]
    lineup = pgp.lineup_at(_news(), "Arsenal", "Chelsea", KICKOFF, at=entry["fetched_at"])
    trades, rec = pgp.decide(entry, lineup, src, "E0", KICKOFF, KICKOFF - pd.Timedelta(hours=1))
    assert trades and all(t["features_stale"] for t in trades) and rec["features_stale"]
    sec = pgp.sections(trades, "2026-11-01T00:00:00Z", KICKOFF)[0]
    assert sec["stale_features"]["stale"] == sec["stale_features"]["of"] == 1
    src.last = pd.Timestamp("2026-10-10T15:00Z")  # before the last file: not stale
    trades, rec = pgp.decide(entry, lineup, src, "E0", KICKOFF, KICKOFF - pd.Timedelta(hours=1))
    assert not any(t["features_stale"] for t in trades)


def test_time_budget_leaves_matches_for_the_next_run(chances):
    import time

    src = FakeSource()
    ledger, checked = {}, {}
    _, recs, counts = pgp.update_league(
        ledger, checked, _live_rows(_prices(chances)), _news(), "E0", "2026-11-01T00:00Z",
        KICKOFF - pd.Timedelta(minutes=35), src, lambda t: None, time.monotonic() - 1,
        lambda t: None,
    )  # fmt: skip
    assert counts["over_budget"] == 1 and recs == [] and checked == {} and src.loads == 0


def test_block_model_is_cached_and_refitted_on_new_files(tmp_path):
    src = pgp.BSource("E0", KICKOFF)
    src.apps = APPS[APPS["kickoff"] < KICKOFF]
    src.matches, src.err = _matches().assign(home=lambda d: d["home"]), None
    src.cache_dir = tmp_path
    p1, d1 = src.chances(_target(), _starters())
    assert src.refits == 1 and src.cache_hits == 0 and len(p1) == 22
    p2, d2 = src.chances(_target(), _starters())
    assert src.cache_hits == 1 and p2 == pytest.approx(p1) and d2["train_rows"] == d1["train_rows"]
    # A new Understat file before the block refits B.
    extra = APPS[APPS["kickoff"] < pgp.b_block(KICKOFF)].tail(22).assign(match_id="new")
    src.apps = pd.concat([src.apps, extra]).sort_values("kickoff").reset_index(drop=True)
    src.chances(_target(), _starters())
    assert src.refits == 2


def test_understat_check(monkeypatch, tmp_path):
    from soccer_stats import player_data

    meta = {"match_id": "99", "kickoff": KICKOFF, "home": "Arsenal", "away": "Chelsea",
            "played": True, "season": "2627"}  # fmt: skip
    monkeypatch.setattr(player_data, "season_matches", lambda lg, y, raw: pd.DataFrame([meta]))
    monkeypatch.setattr(player_data, "match_path", lambda mid, raw: tmp_path / f"{mid}.json")
    monkeypatch.setattr(
        player_data, "parse_match", lambda body, m: [{"player_id": "7", "goals": 1}]
    )
    t = {"league": "E0", "home": "Arsenal", "away": "Chelsea", "kickoff": KICKOFF.isoformat(),
         "player_id": "7", "status": "won"}  # fmt: skip
    assert pgp.understat_check(t, tmp_path) is None  # no match file yet: waits
    (tmp_path / "99.json").write_text("{}")
    assert pgp.understat_check(t, tmp_path) == {"result_check": "ok", "understat_goals": 1}
    assert pgp.understat_check({**t, "status": "lost"}, tmp_path)["result_check"] == "mismatch"
    assert pgp.understat_check({**t, "player_id": "8"}, tmp_path)["understat_goals"] is None


# ---------- B-espn (§16 amendment, owner-approved 27 Nov) ----------


def _info(apps):
    keys = apps.drop_duplicates(["match_id", "team"])[["match_id", "team"]]
    n = keys["match_id"].astype(int)
    return keys.assign(
        team_xg=1.1 + (n % 7) / 10 + keys["team"].str.len() / 50, game_state=(n % 5 - 2) / 10
    ).reset_index(drop=True)


def test_b_espn_live_chance_equals_the_judged_read():
    from soccer_stats import espn_players as ep
    from soccer_stats import player_assist_lab as al
    from soccer_stats import player_goals as pg
    from soccer_stats.factors import build_features
    from soccer_stats.lab import harness

    apps = APPS.copy()
    info = _info(apps)
    last = apps[apps["kickoff"] == apps["kickoff"].max()]
    mid, k = last["match_id"].iloc[0], last["kickoff"].iloc[0]
    home = last[last["home"]]["team"].iloc[0]
    away = last[~last["home"]]["team"].iloc[0]
    # The judged read: espn_players.b_espn_predict's steps through lab.harness.
    fe = ep.goal_rate_features(pg.goal_features(build_features(apps, info)))
    pr = harness.walk_forward(
        fe, lambda **kw: al.CountCandidate(**kw),
        {"target": "goals", "factors": ep.B_ESPN_FACTORS}, pg.FORWARD_START,
        k + pd.Timedelta(days=1), time_col="kickoff", refit=al.REFIT, min_train=1,
    )  # fmt: skip
    judged = fe.assign(p=pr["p_1"])
    judged = judged[(judged["match_id"] == mid) & judged["p"].notna()]
    # The paper track's path: placeholder rows, the block's fit, the prediction.
    mi = info[info["match_id"] == mid].set_index("team")
    target = {"home": home, "away": away, "kickoff": k, "season": "2627",
              "match_model": {t: (mi.loc[t, "team_xg"], mi.loc[t, "game_state"])
                              for t in (home, away)}}  # fmt: skip
    starters = {t: list(last[last["team"] == t]["player_id"]) for t in (home, away)}
    before = apps[apps["kickoff"] < k]
    live, past, _ = pgp.target_features(
        before, info[info["match_id"].isin(before["match_id"])], target, starters,
        ep.goal_rate_features,
    )  # fmt: skip
    bs = pgp.b_block(k)
    first = apps.loc[apps["kickoff"] >= bs, "kickoff"].min()
    model, _n = pgp.fit_block_espn(past, k, first)
    got = pgp.predict_espn(model, live)
    want = dict(zip(judged["player_id"], judged["p"], strict=True))
    assert len(want) == 22 and set(got) == set(want)
    for pid, p in want.items():
        assert got[pid] == pytest.approx(p, rel=1e-9)


def _espn_board(home, away, k, eid="77"):
    return {"events": [{"id": eid, "date": k.isoformat(), "competitions": [{"competitors": [
        {"homeAway": "home", "team": {"displayName": home}},
        {"homeAway": "away", "team": {"displayName": away}}]}]}]}  # fmt: skip


def _espn_summary(home, away, scorer="Bukayo Saka"):
    def side(team, ha, names):
        return {"homeAway": ha, "team": {"displayName": team}, "roster": [
            {"athlete": {"id": str(100 + i if ha == "home" else 200 + i), "displayName": n},
             "starter": True, "position": {"abbreviation": "F"},
             "stats": [{"name": "totalGoals", "value": 1 if n == scorer else 0},
                       {"name": "totalShots", "value": 2}]}
            for i, n in enumerate(names)]}  # fmt: skip

    return {"rosters": [side(home, "home", ["Bukayo Saka", "Declan Rice"]),
                        side(away, "away", ["Cole Palmer"])]}  # fmt: skip


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def test_espn_cache_fills_once_retries_recent_misses_and_loads(tmp_path):
    from soccer_stats import espn_players as ep

    k = pd.Timestamp("2026-11-01T15:00:00Z")
    fx = pd.DataFrame([
        {"home": "Arsenal", "away": "Chelsea", "kickoff": k},
        {"home": "Everton", "away": "Fulham", "kickoff": k + pd.Timedelta(hours=2)},
        {"home": "Leeds", "away": "Wolves", "kickoff": pd.Timestamp("2026-11-30T15:00Z")},
    ])  # fmt: skip
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/scoreboard"):
            return _Resp(_espn_board("Arsenal", "Chelsea", k))  # Everton v Fulham not listed
        return _Resp(_espn_summary("Arsenal", "Chelsea"))

    now = pd.Timestamp("2026-11-02T12:00:00Z")
    res = ep.fill_cache("E0", [2026], tmp_path, get, 60, now, pause=0,
                        matches=lambda lg, y: fx)  # fmt: skip
    assert res["fetched"] == 2 and res["found"] == 1 and res["not_found"] == 1  # Nov 30 not played
    rows, counts = ep.load_cache("E0", [2026], tmp_path)
    assert counts == {"2627": {"matches": 1, "not_found": 1}}
    assert set(rows["player"]) == {"Bukayo Saka", "Declan Rice", "Cole Palmer"}
    assert rows["match_id"].iloc[0] == "fd:Arsenal|Chelsea|20261101"
    assert int(rows.loc[rows["player"] == "Bukayo Saka", "goals"].iloc[0]) == 1
    # A held match is never fetched again; a miss is retried only after RETRY_AFTER.
    n = len(calls)
    again = ep.fill_cache("E0", [2026], tmp_path, get, 60, now + pd.Timedelta(hours=1),
                          pause=0, matches=lambda lg, y: fx)  # fmt: skip
    assert again["fetched"] == 0 and len(calls) == n
    later = ep.fill_cache("E0", [2026], tmp_path, get, 60, now + pd.Timedelta(hours=7),
                          pause=0, matches=lambda lg, y: fx)  # fmt: skip
    assert later["fetched"] == 1 and later["not_found"] == 1
    # The ESPN check on a settled B-espn trade.
    t = {"league": "E0", "home": "Arsenal", "away": "Chelsea", "kickoff": k.isoformat(),
         "player_id": "e100", "status": "won", "feature_source": "espn"}  # fmt: skip
    assert pgp.result_check(t, tmp_path) == {"result_check": "ok", "espn_goals": 1}
    assert pgp.result_check({**t, "player_id": "e101"}, tmp_path)["result_check"] == "mismatch"
    assert pgp.understat_check(t, tmp_path) is None  # ESPN ids never go to Understat


def test_espn_leagues_and_b_stays_in_serie_a():
    now = pd.Timestamp("2026-11-28T12:00:00Z")
    for lg in ("E0", "SP1", "D1", "F1"):
        src = pgp.source_for(lg, now)
        assert isinstance(src, pgp.EspnSource) and src.model_tags["model_source"] == "B-espn"
    i1 = pgp.source_for("I1", now)
    assert type(i1) is pgp.BSource and i1.model_tags == pgp.MODEL_B


def test_espn_source_waits_without_a_seeded_cache(tmp_path):
    src = pgp.EspnSource("SP1", pd.Timestamp("2026-11-28T12:00:00Z"), fetch=False,
                         raw_dir=tmp_path)  # fmt: skip
    assert src.load() is False and src.seasons_missing


def test_sections_count_trades_per_model_and_old_trades_stay_b(chances):
    old = {"id": "E0|2627|A|B|goal|1", "strategy": pgp.KEY, "status": "open",
           "opened_at": "2026-11-05T14:00:00+00:00", "kickoff": "2026-11-05T15:00:00+00:00",
           "league": "E0", "stake": 10.0, "odds": 3.0, "model_p": 0.4, "edge": 0.2,
           "market": "player_goal_scorer_anytime", "home": "A", "away": "B"}  # fmt: skip
    new = {**old, "id": "E0|2627|C|D|goal|e5", "model_source": "B-espn", "feature_source": "espn",
           "home": "C", "away": "D"}  # fmt: skip
    secs = pgp.sections([old, new], "2026-11-09T00:00:00+00:00", KICKOFF)
    main = next(s for s in secs if s["key"] == pgp.KEY)
    assert {m: v["trades"] for m, v in main["models"].items()} == {"B": 1, "B-espn": 1}
    assert pgp.model_of(old) == "B" and "model_source" not in old


def _seed(tmp_path, years, n):
    from soccer_stats import espn_players as ep

    for y in years:
        for i in range(n):
            k = pd.Timestamp(f"{y}-09-01T15:00Z") + pd.Timedelta(days=i)
            f = ep.match_file("SP1", y, "Alaves", "Betis", k, tmp_path)
            f.parent.mkdir(parents=True, exist_ok=True)
            row = {"match_id": f"fd:{i}", "season": ep.season_code(y), "kickoff": k.isoformat(),
                   "team": "Alaves", "player_id": "e1", "player": "A", "goals": 0}  # fmt: skip
            f.write_text(json.dumps({"found": True, "rows": [row]}))


def test_espn_source_never_decides_on_a_current_season_only_cache(tmp_path, monkeypatch):
    """27 Nov: a publish top-up alone (this season's first weeks, no history) once decided
    La Liga's matches. A league is decided only once every earlier season is seeded."""
    from soccer_stats import espn_players as ep
    from soccer_stats.data import current_season

    cur = current_season()
    _seed(tmp_path, [cur], 5)
    calls = []
    monkeypatch.setattr(ep, "fill_cache", lambda *a, **k: calls.append(1) or {"left": 0})
    now = pd.Timestamp("2026-11-28T12:00:00Z")
    src = pgp.EspnSource("SP1", now, fetch=True, raw_dir=tmp_path)
    assert src.load() is False and "seeded" in src.why and not calls  # no top-up either
    # Seeded history, but this run's top-up ran out of time: wait for a complete season.
    _seed(tmp_path, range(pgp.FIRST_YEAR, cur), pgp.SEEDED_MIN)
    monkeypatch.setattr(ep, "fill_cache", lambda *a, **k: {"left": 3, "fetched": 9})
    src = pgp.EspnSource("SP1", now, fetch=True, raw_dir=tmp_path)
    assert src.load() is False and "time budget" in src.why


def test_bug_sweep_voids_by_update_and_redecides_under_new_ids(tmp_path):
    """Lead, 27 Nov: the La Liga decisions made at 19:49 on a partial ESPN cache are voided by
    update events (the opens stay), their matches decided again under "|r2" ids."""
    bad = {"id": "SP1|2627|Alaves|Betis|goal|e1", "league": "SP1", "strategy": pgp.KEY,
           "status": "lost", "profit": -10.0, "model_source": "B-espn", "event_id": "ev1",
           "decided_at": "2026-11-27T19:49:30+00:00", "stake": 10.0}  # fmt: skip
    ok = {**bad, "id": "D1|2627|A|B|goal|e2", "league": "D1", "event_id": "ev2"}
    ledger = {bad["id"]: dict(bad)}
    checked = {"SP1|ev1": {"league": "SP1", "event_id": "ev1", "home": "Alaves",
                           "away": "Betis", "model_source": "B-espn",
                           "decided_at": "2026-11-27T19:49:30+00:00"},  # fmt: skip
               "SP1|ev9": {"league": "SP1", "event_id": "ev9", "model_source": "B",
                           "decided_at": "2026-11-05T10:00:00+00:00"}}  # fmt: skip
    now = pd.Timestamp("2026-11-28T09:00:00Z")
    ev, recs = pgp.bug_sweep(ledger, checked, "SP1", now)
    assert [e["type"] for e in ev] == ["update"] and ev[0]["void_reason"] == pgp.BUG_REASON
    assert ledger[bad["id"]]["status"] == "void" and ledger[bad["id"]]["profit"] == 0.0
    assert len(recs) == 1 and checked["SP1|ev1"]["redecide"] and checked["SP1|ev1"]["attempt"] == 2
    assert pgp.bug_sweep(ledger, checked, "SP1", now) == ([], [])  # idempotent
    assert pgp.bug_sweep({ok["id"]: dict(ok)}, {}, "D1", now) == ([], [])  # other leagues
    # The checked file keeps both records; the superseding one wins on load.
    pgp.append_checked(tmp_path, [{"league": "SP1", "event_id": "ev1", "decided_at": "x"}])
    pgp.append_checked(tmp_path, recs)
    assert pgp.load_checked(tmp_path)["SP1|ev1"]["redecide"] is True
    # Sections leave the void out of every figure but voided_bug.
    secs = pgp.sections([ledger[bad["id"]]], "2026-11-09T00:00:00+00:00", KICKOFF,
                        voided=[ledger[bad["id"]]])  # fmt: skip
    main = next(s for s in secs if s["key"] == pgp.KEY)
    assert main["goalscorer_trades"] == 0 and main["voided_bug"]["trades"] == 1


def test_redecided_trade_gets_a_new_id_pointing_at_the_old_one(chances):
    entry = {"home": "Arsenal", "away": "Chelsea", "kickoff": KICKOFF, "event_id": "ev",
             "fetched_at": KICKOFF - pd.Timedelta(minutes=50), "prices": None}  # fmt: skip
    name = {"model": "X", "espn": "X", "odds": "X"}
    t = pgp._new_trade(pgp.DK_KEY, "SP1", entry, "e7", name, "Arsenal", 0.3, 5.0, "draftkings",
                       {}, {}, KICKOFF, KICKOFF + pd.Timedelta(hours=9), attempt=2)  # fmt: skip
    assert t["id"].endswith("|goal|e7|r2|dk") and t["redecided_of"].endswith("|goal|e7|dk")
    assert t["backcast"] is True and t["attempt"] == 2


def test_judged_read_guard_needs_every_season_seeded_and_fetched(tmp_path):
    from soccer_stats import espn_players as ep

    assert pgp.SEEDED_MIN == ep.SEEDED_MIN
    now = pd.Timestamp("2026-11-28T12:00:00Z")
    fx = {2025: pd.DataFrame([{"home": "Celta", "away": "Getafe",
                               "kickoff": pd.Timestamp("2025-09-01T15:00Z")}]),  # fmt: skip
          2026: pd.DataFrame([{"home": "Celta", "away": "Getafe",
                               "kickoff": pd.Timestamp("2026-09-01T15:00Z")}])}  # fmt: skip
    ready, d = ep.judged_ready("SP1", [2025, 2026], tmp_path, now, lambda lg, y: fx[y])
    assert not ready and d["short"] == ["2526"] and d["unfetched"] == {"2526": 1, "2627": 1}
    _seed(tmp_path, [2025], ep.SEEDED_MIN)  # 2025/26 seeded; this season's match not fetched
    ready, d = ep.judged_ready("SP1", [2025, 2026], tmp_path, now, lambda lg, y: fx[y])
    assert not ready and d["short"] == [] and d["unfetched"] == {"2526": 1, "2627": 1}
    for y in (2025, 2026):  # both tried (a not-found marker counts)
        f = ep.match_file("SP1", y, "Celta", "Getafe", fx[y]["kickoff"][0], tmp_path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"found": False, "home": "Celta", "away": "Getafe"}))
    ready, d = ep.judged_ready("SP1", [2025, 2026], tmp_path, now, lambda lg, y: fx[y])
    assert ready, d
    assert ep.not_found_clubs("SP1", [2025], tmp_path) == {"2526": {"Celta": 1, "Getafe": 1}}


def test_espn_names_heidenheim_and_st_etienne_and_refetch_old_misses(tmp_path):
    from soccer_stats import espn_players as ep
    from soccer_stats.espn_news import team_name

    assert team_name("1. FC Heidenheim 1846", {"Heidenheim", "Bayern Munich"}) == "Heidenheim"
    assert team_name("1. FC Heidenheim", {"Heidenheim", "Bayern Munich"}) == "Heidenheim"
    for n in ("Saint-Étienne", "AS Saint-Étienne", "Saint Etienne"):
        assert team_name(n, {"St Etienne", "Lyon"}) == "St Etienne"
    k = pd.Timestamp("2024-09-01T15:00:00Z")
    fx = pd.DataFrame([{"home": "St Etienne", "away": "Lyon", "kickoff": k}])
    board = {"v": _espn_board("Unknown Saint FC", "Lyon", k)}

    def get(url, params=None, timeout=None):
        if url.endswith("/scoreboard"):
            return _Resp(board["v"])
        return _Resp(_espn_summary("AS Saint-Étienne", "Lyon"))

    now = pd.Timestamp("2026-11-28T12:00:00Z")
    kw = {"pause": 0, "matches": lambda lg, y: fx}
    res = ep.fill_cache("F1", [2024], tmp_path, get, 60, now, **kw)
    assert res["not_found"] == 1
    rec = ep.cached_match("F1", "St Etienne", "Lyon", k, tmp_path)
    assert rec["board_names"] == ["Lyon", "Unknown Saint FC"]
    assert ep.unmapped_board_names("F1", [2024], tmp_path) == {"Unknown Saint FC": 1}
    # Two years old: no plain retry, but a refetch of the club fetches it again.
    board["v"] = _espn_board("AS Saint-Étienne", "Lyon", k)
    assert ep.fill_cache("F1", [2024], tmp_path, get, 60, now, **kw)["fetched"] == 0
    again = ep.fill_cache("F1", [2024], tmp_path, get, 60, now, refetch=["St Etienne"], **kw)
    assert again["found"] == 1
    assert ep.cached_match("F1", "St Etienne", "Lyon", k, tmp_path)["found"] is True
    # A found match is never refetched.
    assert ep.fill_cache("F1", [2024], tmp_path, get, 60, now, refetch=["St Etienne"],
                         **kw)["fetched"] == 0  # fmt: skip
