"""The portfolio registry: mapping trades to portfolios and packaging one section each."""

import json

import pytest
from test_paper import NOW, card, src

from soccer_stats import paper
from soccer_stats import trades as tr


def trade(i, bet_type="match", market="home", profit=10.0, status="won", **kw):
    t = {
        "id": f"t{i}",
        "bet_type": bet_type,
        "market": market,
        "kickoff": f"2026-09-{10 + i:02d}T14:00:00+00:00",
        "odds": 2.0,
        "edge": 0.15,
        "stake": 10.0,
        "status": status,
        "profit": profit,
        "season": "2627",
        "clv_dk": 0.02,
    }
    t.update(kw)
    return t


def test_registry_ids_unique_and_statuses_known():
    assert len(tr.PORTFOLIO_IDS) == len(set(tr.PORTFOLIO_IDS))
    assert {p["status"] for p in tr.PORTFOLIOS} <= {"live", "testing", "retired"}
    assert all(p["note"] and p["name"] and p["backtest"] for p in tr.PORTFOLIOS)


@pytest.mark.parametrize(
    "t, want",
    [
        ({"bet_type": "match", "market": "home"}, "moneyline"),
        ({"market": "draw"}, "moneyline"),  # older trades may lack bet_type
        ({"bet_type": "player", "market": "player_shots"}, "player_shots"),
        ({"bet_type": "player", "market": "player_shots_on_target"}, "player_shots"),
        ({"bet_type": "player", "market": "player_goal_scorer_anytime"}, "goalscorer"),
        ({"bet_type": "player", "market": "player_shots", "portfolio": "goalscorer"}, "goalscorer"),
        ({"bet_type": "match", "portfolio": "nonsense"}, "moneyline"),  # unknown id ignored
    ],
)
def test_portfolio_of(t, want):
    assert tr.portfolio_of(t) == want


def test_new_trades_carry_their_portfolio():
    kw = dict(
        source="live",
        league="E0",
        home="Arsenal",
        away="Leeds",
        kickoff=NOW,
        opened_at=NOW,
        odds_fetched_at=None,
        threshold=0.12,
    )
    pick = {"market": "home", "odds": 2.0, "model_p": 0.6, "edge": 0.2}
    assert tr.new_trade(pick, **kw)["portfolio"] == "moneyline"
    shot = {**pick, "market": "player_shots", "line": 1.0, "side": "over"}
    t = tr.new_trade(shot, player={"player": "Saka", "player_id": "1"}, **kw)
    assert t["portfolio"] == "player_shots"
    assert "portfolio" in tr.ENTRY_FIELDS and "portfolio" not in paper.UPDATE_FIELDS


def test_portfolio_summaries_add_up_to_the_combined_view():
    live_trades = [
        trade(1),
        trade(2, profit=-10.0, status="lost"),
        trade(3, bet_type="player", market="player_shots", profit=-10.0, status="lost"),
        trade(4, bet_type="player", market="player_shots_on_target"),
        trade(5, status="open", profit=None),
    ]
    live = {"error": None, "note": "ok"}
    pfs = paper.portfolios_section(live, live_trades, {})
    assert [p["id"] for p in pfs] == tr.PORTFOLIO_IDS
    combined = paper.portfolio_section(live_trades)["summary"]
    sums = [p["live"]["summary"] for p in pfs]
    assert sum(s["trades"] for s in sums) == combined["trades"]
    assert sum(s.get("profit", 0) for s in sums) == pytest.approx(combined["profit"])
    by_id = {p["id"]: p for p in pfs}
    assert by_id["moneyline"]["live"]["summary"]["open"] == 1
    assert by_id["player_shots"]["live"]["summary"]["settled"] == 2
    assert by_id["moneyline"]["live"]["note"] == "ok"


def test_empty_portfolio():
    pfs = paper.portfolios_section({"error": None, "note": None}, [trade(1)], {})
    gs = next(p for p in pfs if p["id"] == "goalscorer")
    assert gs["status"] == "testing" and "no trades" in gs["note"]
    assert gs["live"]["trades"] == [] and gs["live"]["summary"]["trades"] == 0
    assert gs["backtest"] is None
    json.dumps(pfs)  # serializable for data.json


def test_backtests_go_to_their_portfolio():
    dk = {"summary": {"trades": 3}, "strategies": {"raw": {}}, "trades": []}
    players = {
        "generated_at": "2026-10-06",
        "priced": {"x": 1},
        "edge_threshold": {"min_edge": None, "note": "No minimum edge works."},
        "trades": [trade(1, bet_type="player", market="player_shots")],
    }
    pfs = paper.portfolios_section({}, [], {"moneyline": dk, "player_shots": players})
    by_id = {p["id"]: p for p in pfs}
    assert by_id["moneyline"]["backtest"] is dk  # a full section passes through unchanged
    ps = by_id["player_shots"]["backtest"]
    assert ps["summary"]["trades"] == 1 and "priced" not in ps
    assert ps["generated_at"] == "2026-10-06"
    assert ps["edge_threshold"]["min_edge"] is None  # the research lab's level is kept


def test_run_fills_portfolios(tmp_path):
    (tmp_path / "backtest").mkdir()
    (tmp_path / "backtest" / "E0_dk.json").write_text(json.dumps({"summary": {"trades": 3}}))
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    assert paper.run(data, tmp_path, None, now=NOW) == 1
    pfs = {p["id"]: p for p in data["portfolio"]["portfolios"]}
    assert pfs["moneyline"]["live"]["summary"]["open"] == 1
    assert pfs["moneyline"]["backtest"]["summary"]["trades"] == 3
    assert pfs["goalscorer"]["live"]["summary"]["trades"] == 0
    assert data["portfolio"]["live"]["summary"]["open"] == 1  # old key kept for a release

    missing = {"fixtures": [], "portfolio": {}}
    paper.run(missing, tmp_path / "nope", None, now=NOW)
    assert all(p["live"]["error"] for p in missing["portfolio"]["portfolios"])


def test_by_league_splits_summaries_and_adds_up():
    trades = [
        trade(1),
        trade(2, profit=-10.0, status="lost", league="SP1"),
        trade(3, league="SP1"),
        trade(4, profit=-10.0, status="lost"),  # no league: counts as E0
    ]
    pfs = paper.portfolios_section({}, trades, {"moneyline": {"summary": {}, "trades": trades}})
    ml = next(p for p in pfs if p["id"] == "moneyline")
    lg = ml["live"]["by_league"]
    assert set(lg) == {"E0", "SP1"} and "trades" not in lg["SP1"]
    assert lg["E0"]["summary"]["trades"] + lg["SP1"]["summary"]["trades"] == 4
    assert lg["E0"]["summary"]["profit"] + lg["SP1"]["summary"]["profit"] == pytest.approx(
        ml["live"]["summary"]["profit"]
    )
    assert set(ml["backtest"]["by_league"]) == {"E0", "SP1"}  # passed-through file too
    assert paper.by_league([trade(1), trade(2)]) is None  # one league: no split
