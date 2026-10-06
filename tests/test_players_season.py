"""Season readiness for player lines: transfers, promoted teams, unmatched names, the
credit reserve, per-strategy backtest trades and the weekly run's season choice."""

import json

import numpy as np
import pandas as pd
import pytest
from player_sim import simulate_players

from soccer_stats import cli, player_odds
from soccer_stats import player_backtest as pb
from soccer_stats import publish as pub
from soccer_stats.factors import build_features
from soccer_stats.player_live import _candidates, player_cards


@pytest.fixture(scope="module")
def sim():
    apps, _ = simulate_players(n_teams=6, seasons=2, seed=5)
    return apps


def _top(apps, team):
    return apps[apps["team"] == team].groupby("player_id")["shots"].sum().idxmax()


def _name(apps, pid):
    return apps.loc[apps["player_id"] == pid, "player"].iloc[0]


def _event(eid, home, away, kickoff, names):
    outcomes = [
        {"name": "Over", "description": n, "price": 2.4, "point": 1.0} for n in dict.fromkeys(names)
    ]
    return {
        "id": eid,
        "commence_time": kickoff.isoformat(),
        "home_team": home,
        "away_team": away,
        "fetched_at": (kickoff - pd.Timedelta(hours=4)).isoformat(),
        "bookmakers": [
            {"key": "fanduel", "markets": [{"key": "player_shots", "outcomes": outcomes}]}
        ],
    }


def test_candidates_follow_transfers(sim):
    left, moved_out = _top(sim, "T00"), "T00_3"
    signing = _top(sim, "T01")  # FPL now lists him at T00
    active = {
        left: {"active": False, "team": "T00", "status": "u"},
        moved_out: {"active": True, "team": "T02", "status": "a"},
        signing: {"active": True, "team": "T00", "status": "a"},
    }
    before = _candidates(sim, "T00")
    after = _candidates(sim, "T00", active)
    assert left in set(before["player_id"]) and left not in set(after["player_id"])
    assert moved_out not in set(after["player_id"])
    new = after[after["player_id"] == signing].iloc[0]
    assert new["apps"] == 0 and new["share"] == 0 and new["player"] == _name(sim, signing)
    # Players FPL doesn't match (no entry) are kept: a name mismatch never hides anyone.
    kept = set(before["player_id"]) - {left, moved_out}
    assert kept <= set(after["player_id"])
    assert after.attrs["moved_out"] == 2 and after.attrs["moved_in"] == 1


def test_season_start_cards_signing_and_promoted_team(sim):
    k = sim["kickoff"].max() + pd.Timedelta(days=60)  # a summer later
    signing = _top(sim, "T01")
    active = {signing: {"active": True, "team": "T00", "status": "a"}}
    fixtures = [
        {"home": "T00", "away": "Hull", "kickoff": k.isoformat(), "xg": [1.8, 0.9], "p": {}},
        {"home": "Coventry", "away": "Ipswich", "kickoff": k.isoformat(), "xg": None, "p": {}},
    ]
    ev = [
        _event("e1", "T00", "Hull City", k, [_name(sim, signing), "Hull Striker"]),
        _event("e2", "Coventry City", "Ipswich Town", k, ["A Cov", "B Ips", "C Ips"]),
    ]
    cards, st = player_cards(
        sim, fixtures, None, ev, now=k - pd.Timedelta(hours=4), calibration=[0, 1, 0], active=active
    )
    rows = cards[("T00", "Hull")]
    assert {r["team"] for r in rows} == {"T00"}  # Hull: no Premier League history
    (priced,) = [r for r in rows if r["lines"]]
    assert priced["player_id"] == signing  # priced at his new club from his history
    assert 0 < priced["exp_shots"] < 10
    assert priced["lines"][0]["p"] == pytest.approx(1 / 2.4, abs=1e-3)  # b=1, c=0: price
    assert st["teams_without_history"] == ["Hull", "Coventry", "Ipswich"]
    assert st["moved_in"] == 1
    # Hull's name and the whole unmodelled match are counted, with a sample kept.
    assert st["unmatched_odds"] == 4
    assert set(st["unmatched_names"]) == {"Hull Striker", "A Cov", "B Ips", "C Ips"}
    assert ("Coventry", "Ipswich") not in cards


class Resp:
    def __init__(self, body, cost=20, remaining=3010):
        self.body, self.ok, self.status_code = body, True, 200
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(remaining)}

    def json(self):
        return self.body


def test_live_player_odds_unknown_balance_keeps_reserve(tmp_path):
    now = pd.Timestamp("2026-10-10 08:00", tz="UTC")
    evs = [
        {"id": f"e{i}", "commence_time": (now + pd.Timedelta(hours=4 + i)).isoformat()}
        for i in range(3)
    ]
    calls = []

    def get(url, params, timeout):
        calls.append(url)
        if url.endswith("/events"):
            return Resp(evs, cost=0)
        return Resp({**evs[0], "bookmakers": []})  # leaves 3,010: under reserve + one call

    _, st = player_odds.fetch_live(
        raw_dir=tmp_path, api_key="SECRET", credits_left=None, now=now, get=get
    )
    # Balance unknown: one call learns it from the headers, then the reserve holds.
    assert st["fetched"] == 1 and "keeping" in st["error"] and st["credits_left"] == 3010
    assert sum(c.endswith("/odds") for c in calls) == 1
    for p in tmp_path.rglob("*.json"):
        assert "SECRET" not in p.read_text()


def _hist(preds, odds_fn):
    rows = []
    for r in preds.drop_duplicates(["match_id", "player_id"]).iloc[::2].itertuples():
        for kind, at in (
            ("look", r.kickoff - pd.Timedelta(hours=3)),
            ("close", r.kickoff - pd.Timedelta(minutes=1)),
        ):
            rows.append(
                {
                    "event_id": r.match_id,
                    "kickoff": r.kickoff,
                    "home": r.team,
                    "away": r.opponent,
                    "player": r.player,
                    "market": "player_shots",
                    "line": 1.0,
                    "side": "over",
                    "odds": odds_fn(),
                    "odds_updated": None,
                    "kind": kind,
                    "snapshot_ts": at,
                }
            )
    return pd.DataFrame(rows)


def test_every_strategy_keeps_compact_trades(sim):
    feats = build_features(sim)
    start = feats["kickoff"].min() + pd.Timedelta(days=200)
    preds = pb.walk_forward(feats, start, refit_every="28D")
    known = pb.walk_forward(feats, start, refit_every="28D", lineup_known=True)
    rng = np.random.default_rng(1)
    hist = _hist(preds, lambda: round(float(rng.uniform(1.3, 4.0)), 2))
    main, info = pb.priced_trades(preds, hist, sim, known=known, cal_min_lines=100)
    fields = info["trade_fields"]
    assert fields == list(pb.TRADE_FIELDS)
    for name, st in info["strategies"].items():
        rows = st["trades"]
        assert len(rows) == st["summary"]["trades"], name
        assert all(len(r) == len(fields) for r in rows)
        dates = [r[0] for r in rows]
        assert dates == sorted(dates)  # oldest first, for a running-profit chart
        rec = [dict(zip(fields, r, strict=True)) for r in rows]
        settled = [x for x in rec if x["status"] in ("won", "lost")]
        if settled:
            assert sum(x["profit"] for x in settled) == pytest.approx(
                st["summary"]["profit"], abs=0.05
            )
        for x in rec:
            assert x["market"] in ("shots", "sot") and x["side"] == "over"
            assert x["edge"] >= 0.12 - 1e-3
        if rows:
            assert len(json.dumps(rows)) / len(rows) < 150  # compact: sensible file size
    main_rows = info["strategies"][pb.MAIN_STRATEGY]["trades"]
    assert len(main_rows) == len(main)
    raw = [dict(zip(fields, r, strict=True)) for r in info["strategies"]["raw_3h"]["trades"]]
    assert raw and all(x["clv"] is not None for x in raw)  # 3-hour bets have a close
    json.dumps(info["strategies"])  # JSON-safe as saved


def test_score_by_season(sim):
    feats = build_features(sim)
    preds = pb.walk_forward(
        feats, feats["kickoff"].min() + pd.Timedelta(days=200), refit_every="28D"
    )
    by = pb.score_by_season(preds)
    assert set(by) == set(preds["season"].astype(str))
    assert sum(v["appearances"] for v in by.values()) == len(preds)
    assert {"model", "baseline", "beats_baseline"} <= set(by[max(by)]["shots"])


def test_years_now(monkeypatch):
    monkeypatch.setattr(cli, "current_season", lambda: 2026)
    assert cli._years("2023-now") == [2023, 2024, 2025, 2026]
    assert cli._years("now") == [2026]
    assert cli._years("2023-2025") == [2023, 2024, 2025]
    assert cli._years("2021") == [2021]


def test_season_not_ready(monkeypatch):
    from soccer_stats import player_data

    def none_played(league, year):
        return pd.DataFrame({"played": [False] * 10})

    monkeypatch.setattr(cli, "load_matches", lambda lg, ys: pd.DataFrame())
    monkeypatch.setattr(player_data, "season_matches", none_played)
    assert cli._season_not_ready("E0", 2026) == "no matches played yet"

    def down(league, years):
        raise OSError("404")

    monkeypatch.setattr(cli, "load_matches", down)
    assert "can't be read" in cli._season_not_ready("E0", 2026)
    monkeypatch.setattr(cli, "load_matches", lambda lg, ys: pd.DataFrame({"x": [1, 2]}))
    monkeypatch.setattr(
        player_data, "season_matches", lambda lg, y: pd.DataFrame({"played": [True, False]})
    )
    assert cli._season_not_ready("E0", 2026) is None


def test_add_players_season_start(sim, monkeypatch):
    """The publish path at a season's start: three seasons of appearances for the model,
    two in the season stats, transfers applied, FanDuel only after the gate, and player
    paper trades off."""
    from soccer_stats import player_data, player_live
    from soccer_stats import trades as tr

    signing = _top(sim, "T01")
    apps = sim.copy()
    # Sim names share surnames across clubs ("Player Ta Bh", "Player Tab Bh"); give the
    # signing a real-looking one so FPL's name finds him and not a new team-mate.
    apps.loc[apps["player_id"] == signing, "player"] = "Zeno Okafor"
    apps["season"] = apps["season"].map({"2324": "2425", "2425": "2526"})
    old = apps[apps["season"] == "2425"].assign(season="2324")  # an older season too
    old = old.assign(kickoff=old["kickoff"] - pd.Timedelta(days=365))
    apps = pd.concat([old, apps], ignore_index=True)
    k = apps["kickoff"].max() + pd.Timedelta(days=60)
    seen = {}

    def load(league, years, max_new=None):
        seen["years"] = list(years)
        return apps, 0

    fpl = pd.DataFrame(
        {
            "name": ["Okafor"],
            "full_name": ["Zeno Okafor"],
            "team": ["T00"],
            "status": ["a"],
            "p_play": [1.0],
        }
    )
    monkeypatch.setattr(pub, "current_season", lambda: 2026)
    monkeypatch.setattr(player_data, "load_appearances", load)
    gate = {**pub.player_gate(""), "passed": True, "calibration": None}
    monkeypatch.setattr(pub, "player_gate", lambda: gate)
    fetched = {}

    def fetch_live(league, credits_left=None):
        fetched["credits_left"] = credits_left
        return [], {"fetched": 0}

    monkeypatch.setattr(player_odds, "fetch_live", fetch_live)
    real_cards = player_live.player_cards

    def cards(*a, **kw):
        return real_cards(*a, now=k - pd.Timedelta(hours=4), **kw)

    monkeypatch.setattr(player_live, "player_cards", cards)
    data = {"fixtures": [{"home": "T00", "away": "T02", "kickoff": k.isoformat(), "p": {}}]}
    status, stats = pub.add_players(data, "E0", fpl, credits_left=9000)
    assert status["error"] is None, status["error"]
    assert seen["years"] == [2024, 2025, 2026] and fetched["credits_left"] == 9000
    assert {r["season"] for r in stats} == {"2526"}  # last two seasons only (2627 unplayed)
    assert status["paper_trades"] is tr.PLAYER_PAPER_TRADES is False
    assert status["moved_in"] == 1
    shown = data["fixtures"][0]["players"]  # no prices: each team's top 6
    assert len(shown) == 12 and {p["team"] for p in shown} == {"T00", "T02"}
    by_id = {r["player_id"]: r for r in stats if r["team"] == "T01"}
    assert by_id[signing]["current_team"] == "T00" and by_id[signing]["active"]
