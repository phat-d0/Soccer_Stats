import json

import numpy as np
import pandas as pd
import pytest
from player_sim import simulate_players

from soccer_stats import paper, player_odds
from soccer_stats import player_backtest as pb
from soccer_stats import trades as tr
from soccer_stats.factors import build_features
from soccer_stats.player_live import player_cards

NOW = pd.Timestamp("2026-10-08 12:00", tz="UTC")
KICKOFF = pd.Timestamp("2026-10-10 14:00", tz="UTC")


def event(eid="e1", home="Arsenal", away="Leeds United", players=(("Bukayo Saka", 1.5, 2.6, 1.5),)):
    outcomes = []
    for name, line, over, under in players:
        outcomes += [
            {"name": "Over", "description": name, "price": over, "point": line},
            {"name": "Under", "description": name, "price": under, "point": line},
        ]
    return {
        "id": eid,
        "commence_time": KICKOFF.isoformat().replace("+00:00", "Z"),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "fanduel",
                "markets": [
                    {
                        "key": "player_shots",
                        "last_update": "2026-10-08T11:00:00Z",
                        "outcomes": outcomes,
                    }
                ],
            },
            {"key": "draftkings", "markets": [{"key": "player_shots", "outcomes": outcomes}]},
        ],
    }


def test_parse_event_keeps_player_bookmaker_sides():
    df = player_odds.parse_event(event(), {"Arsenal", "Leeds"})
    assert len(df) == 2 and set(df["side"]) == {"over", "under"}
    assert df["away"].iloc[0] == "Leeds" and df["line"].iloc[0] == 1.5


class Resp:
    def __init__(self, body, cost=2, remaining=5000, ok=True):
        self.body, self.ok, self.status_code = body, ok, 200 if ok else 401
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(remaining)}

    def json(self):
        return self.body


def test_live_player_odds_budget_and_cache(tmp_path):
    soon = {"id": "e1", "commence_time": (NOW + pd.Timedelta(hours=10)).isoformat()}
    later = {"id": "e2", "commence_time": (NOW + pd.Timedelta(days=4)).isoformat()}
    calls = []

    def get(url, params, timeout):
        calls.append(url)
        if url.endswith("/events"):
            return Resp([soon, later], cost=0)
        return Resp({**event("e1"), "commence_time": soon["commence_time"]})

    evs, st = player_odds.fetch_live(
        raw_dir=tmp_path, api_key="SECRET", credits_left=8000, now=NOW, get=get
    )
    assert st["fetched"] == 1 and len(evs) == 1  # only the match within 30 hours
    calls.clear()
    evs, st = player_odds.fetch_live(
        raw_dir=tmp_path,
        api_key="SECRET",
        credits_left=8000,
        now=NOW + pd.Timedelta(hours=1),
        get=get,
    )
    assert st["cached"] == 1 and not any(c.endswith("/odds") for c in calls)  # refreshed < 3h ago
    evs, st = player_odds.fetch_live(
        raw_dir=tmp_path / "b", api_key="k", credits_left=3005, now=NOW, get=get
    )
    assert st["fetched"] == 0 and "keeping" in st["error"]  # match odds reserve
    for p in tmp_path.rglob("*.json"):
        assert "SECRET" not in p.read_text()


def test_historical_player_backfill_dry_run_and_cap(tmp_path):
    ks = [KICKOFF, KICKOFF, KICKOFF + pd.Timedelta(hours=2)]
    rep = player_odds.backfill(
        ks,
        max_credits=100,
        dry_run=True,
        raw_dir=tmp_path,
        get=lambda *a, **k: (_ for _ in ()).throw(AssertionError),
    )
    assert rep.planned == 6 and rep.estimated_credits == 2 + 6 * 20

    def get(url, params, timeout):
        if url.endswith("/events"):
            return Resp(
                {"data": [event("e1"), event("e2", "Chelsea", "Wolves")]}, cost=1, remaining=9000
            )
        return Resp({"timestamp": params["date"], "data": event("e1")}, cost=20, remaining=8900)

    rep = player_odds.backfill(ks, max_credits=45, api_key="k", raw_dir=tmp_path, get=get)
    assert rep.credits_used <= 45 and rep.fetched == 2 and "max-credits" in rep.stopped
    rep2 = player_odds.backfill(ks, max_credits=1000, api_key="k", raw_dir=tmp_path, get=get)
    assert rep2.cached >= 2  # never fetched twice


def card(players):
    return {
        "home": "Arsenal",
        "away": "Leeds",
        "kickoff": KICKOFF.isoformat(),
        "p": {"home": 0.6},
        "odds": {},
        "low_data": False,
        "players": players,
    }


def prow(pid, name, lines, team="Arsenal"):
    return {"player": name, "player_id": pid, "team": team, "position": "FWD", "lines": lines}


def line(p, odds, ln=1.5, side="over", market="player_shots", fetched=None):
    fetched = fetched if fetched is not None else NOW - pd.Timedelta(minutes=20)
    return {
        "market": market,
        "line": ln,
        "side": side,
        "odds": odds,
        "p": p,
        "edge": p * odds - 1,
        "implied": 0.45,
        "fetched_at": fetched.isoformat(),
    }


def test_player_paper_trades_cap_and_settle(tmp_path):
    players = [prow(str(i), f"P{i}", [line(0.5 + i / 100, 2.4)]) for i in range(6)]
    players[0]["lines"].append(line(0.6, 3.0, ln=2.5))  # better edge on another line: kept instead
    src = {"name": "DraftKings", "fetched_at": NOW.isoformat()}
    ledger = {}
    ev, _ = paper.update_ledger(ledger, [card(players)], src, None, NOW, players_on=True)
    opened = [e for e in ev if e["type"] == "open"]
    assert len(opened) == tr.MAX_PLAYER_TRADES
    p0 = ledger.get("E0|2627|Arsenal|Leeds|P0|player_shots")
    assert p0 and p0["line"] == 2.5 and p0["bet_type"] == "player"
    # Again: nothing new (cap reached, ids already open)
    ev, _ = paper.update_ledger(ledger, [card(players)], src, None, NOW, players_on=True)
    assert [e for e in ev if e["type"] == "open"] == []
    # Off without the gate
    ev, _ = paper.update_ledger({}, [card(players)], src, None, NOW, players_on=False)
    assert ev == []

    after = KICKOFF + pd.Timedelta(hours=3)
    apps = pd.DataFrame(
        {
            "team": ["Arsenal", "Leeds"],
            "opponent": ["Leeds", "Arsenal"],
            "kickoff": [KICKOFF, KICKOFF],
            "player_id": ["0", "x"],
            "shots": [3, 1],
            "sot": [1, 0],
            "started": [True, True],
        }
    )
    ev, _ = paper.update_ledger(ledger, [], src, None, after, apps=apps, players_on=True)
    assert ledger["E0|2627|Arsenal|Leeds|P0|player_shots"]["status"] == "won"  # 3 > 2.5
    voids = [t for t in ledger.values() if t["status"] == "void"]
    assert len(voids) == 3  # the others didn't play


@pytest.fixture(scope="module")
def sim():
    apps, _ = simulate_players(n_teams=6, seasons=2, seed=3)
    return apps


def test_player_cards_price_lines(sim):
    apps = sim
    last = apps["kickoff"].max()
    k = last + pd.Timedelta(days=4)
    fixtures = [
        {
            "home": "T00",
            "away": "T01",
            "kickoff": k.isoformat(),
            "xg": [1.6, 1.1],
            "p": {"home": 0.5, "draw": 0.25, "away": 0.25},
        }
    ]
    top = apps[apps["team"] == "T00"].groupby("player")["shots"].sum().idxmax()
    ev = event("e9", "T00", "T01", ((top, 1.5, 2.2, 1.65), ("Nobody Known", 0.5, 1.9, 1.9)))
    ev["commence_time"] = k.isoformat()
    ev["fetched_at"] = (k - pd.Timedelta(hours=5)).isoformat()
    cards, st = player_cards(apps, fixtures, None, [ev], now=k - pd.Timedelta(hours=5))
    rows = cards[("T00", "T01")]
    priced = [r for r in rows if r["lines"]]
    assert len(priced) == 1 and priced[0]["player"] == top
    ln = {x["side"]: x for x in priced[0]["lines"]}
    assert ln["over"]["p"] + ln["under"]["p"] == pytest.approx(1)
    assert ln["over"]["implied"] + ln["under"]["implied"] == pytest.approx(1)
    assert st["unmatched_odds"] == 1  # "Nobody Known": skipped and counted
    assert 0 < priced[0]["exp_shots"] < 10


def test_priced_player_backtest(sim):
    apps = sim
    feats = build_features(apps)
    start = feats["kickoff"].min() + pd.Timedelta(days=200)
    preds = pb.walk_forward(feats, start, refit_every="28D")
    rng = np.random.default_rng(0)
    rows = []
    for r in (
        preds.drop_duplicates(["match_id", "player_id"]).iloc[::3].itertuples()
    ):
        for kind, at in (
            ("look", r.kickoff - pd.Timedelta(hours=3)),
            ("close", r.kickoff - pd.Timedelta(minutes=1)),
        ):
            fair = 2.0
            for side in ("over", "under"):
                rows.append(
                    {
                        "event_id": r.match_id,
                        "kickoff": r.kickoff,
                        "home": r.team,
                        "away": r.opponent,
                        "player": r.player,
                        "market": "player_shots",
                        "line": 0.5,
                        "side": side,
                        "odds": round(fair * rng.uniform(0.85, 1.25), 2),
                        "odds_updated": None,
                        "kind": kind,
                        "snapshot_ts": at,
                    }
                )
    hist = pd.DataFrame(rows)
    trades, info = pb.priced_trades(preds, hist, apps, cal_min_lines=100)
    assert len(trades) > 5
    assert not trades.duplicated(["home", "away", "player", "market"]).any()
    assert trades.groupby(["home", "away"]).size().max() <= tr.MAX_PLAYER_TRADES
    assert set(trades["status"]) <= {"won", "lost"}
    assert trades["started"].all() and set(trades["look"]) == {"lineup"}  # starters, after lineups
    assert trades["implied"].between(0, 1).all()  # margin-free chance at entry
    assert set(info["strategies"]) == set(pb.STRATEGIES)
    raw = info["strategies"]["raw_3h"]["summary"]
    assert raw["trades"] > 0 and raw["clv_dk"] is not None  # 3-hour trades have a close
    assert info["calibration"]["look"]["coef"] is not None
    rep = tr.report(trades)
    assert {"position", "started", "line"} <= set(rep["breakdowns"])
    # totals of the two bet types add up to the combined view
    combined = paper.portfolio_section(
        trades.to_dict("records")
        + [
            {
                **trades.iloc[0].to_dict(),
                "id": "m",
                "bet_type": "match",
                "market": "home",
                "profit": 5.0,
            }
        ]
    )
    parts = combined["by_bet_type"]
    assert (
        parts["match"]["summary"]["trades"] + parts["player"]["summary"]["trades"]
        == combined["summary"]["trades"]
    )
    assert parts["match"]["summary"]["profit"] + parts["player"]["summary"][
        "profit"
    ] == pytest.approx(combined["summary"]["profit"])
    json.dumps(trades.astype(str).to_dict("records"))


def test_over_only_lines_use_price_as_implied(sim):
    apps = sim
    feats = build_features(apps)
    start = feats["kickoff"].min() + pd.Timedelta(days=200)
    preds = pb.walk_forward(feats, start, refit_every="28D")
    rows = []
    for r in preds.drop_duplicates(["match_id", "player_id"]).head(300).itertuples():
        for kind, at, odds in (
            ("look", r.kickoff - pd.Timedelta(hours=3), 4.0),
            ("close", r.kickoff - pd.Timedelta(minutes=1), 3.2),
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
                    "odds": odds,
                    "odds_updated": None,
                    "kind": kind,
                    "snapshot_ts": at,
                }
            )
    _, info = pb.priced_trades(preds, pd.DataFrame(rows), apps)
    assert info["implied_from_price_only"] > 0
    lines = info["_lines"]
    assert lines["look"]["implied"].tolist() == pytest.approx([0.25] * len(lines["look"]))
    assert (lines["close"]["implied"] == 1 / 3.2).all()
    look = lines["look"]
    assert (look.loc[look["won"] == 1, "actual"] >= 1).all()  # 1.0 = one or more
    assert (look.loc[look["won"] == 0, "actual"] == 0).all()
    raw = info["strategies"]["raw_3h"]["summary"]  # 4.0 at entry, 3.2 at the close
    if raw["trades"]:
        assert raw["clv_dk"] == pytest.approx(4.0 / 3.2 - 1)
