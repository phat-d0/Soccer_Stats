"""Model 12% replayed per league on Pinnacle's early prices (edge12_backtest.py; owner,
21 Oct) and what data.json carries of it. Synthetic data only: nothing reaches the network."""

import argparse
import json

import numpy as np
import pytest
from test_lean_backtest import synth
from test_paper import NOW, card, src

from soccer_stats import edge12_backtest as e12b
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import paper
from soccer_stats import trades as tr
from soccer_stats.lab import edge12_filters as ef


def report(seed=0):
    preds, pins = synth(seed=seed)
    return e12b.league_report("E0", preds, pins), preds, pins


def test_bets_are_the_live_rule_at_the_early_price_with_clv_against_the_close():
    (rep, b), preds, pins = report()
    assert rep["kind"] == "early" and len(b) > 50
    target = lb.target_rows(lb.with_pinnacle(preds, pins))
    for r in b.head(30).to_dict("records"):
        row = target[(target["home"] == r["home"]) & (target["away"] == r["away"])].iloc[0]
        probs = {m: row[f"p_{m}"] for m in mc.H2H}
        odds = {m: row[f"odds_{m}"] for m in mc.H2H}  # Pinnacle early, quoted
        pick = tr.best_pick(probs, odds, tr.PAPER_EDGE)
        assert pick["market"] == r["market"] and r["odds"] == pytest.approx(pick["odds"])
        assert r["edge"] == pytest.approx(r["model_p"] * r["odds"] - 1)
        assert r["edge"] >= tr.PAPER_EDGE - 1e-9
        close = {m: row[f"close_{m}"] for m in mc.H2H}
        assert r["clv"] == pytest.approx(tr.clv(r["odds"], r["market"], close))
    assert b.groupby(["home", "away"]).size().max() == 1
    assert np.allclose(b["profit"], np.where(b["won"], b["odds"] - 1, -1.0))
    m = rep["edge12"]
    assert m["bets"] == len(b) and m["roi"] == pytest.approx(b["profit"].mean())
    assert m["breakeven"] == pytest.approx((1 / b["odds"]).mean())
    assert m["model_p"] == pytest.approx(b["model_p"].mean()) and "avg_z" not in m
    assert sum(s["bets"] for s in rep["by_season"]) == len(b)


def test_only_seasons_both_prices_cover_are_scored():
    preds, pins = synth()
    last = sorted(pins["season"].unique())[-1]
    idx = pins.index[pins["season"] == last]
    pins.loc[idx[: int(len(idx) * 0.4)], ["odds_home", "odds_draw", "odds_away"]] = np.nan
    rep, b = e12b.league_report("E0", preds, pins)
    assert lb.season_label(last) in rep["seasons_excluded"]
    assert last not in set(b["season"]) and len(b)


def test_build_pools_the_early_leagues_and_keeps_the_cup_apart():
    (rep, b), _, _ = report()
    cup_bets = b.head(5).assign(league="UCL")
    cup = {"kind": "close", "note": "at the close", **e12b.summary(cup_bets)}
    dk = {
        "seasons": "2025",
        "threshold": 0.12,
        "summary": {"trades": 173, "roi": -0.1343, "clv_dk": -0.0617, "max_drawdown": 401},
    }
    out = e12b.build(
        {"E0": rep, "SP1": {"error": "down"}, "UCL": cup},
        {"E0": b, "SP1": e12b.empty_bets("SP1"), "UCL": cup_bets},
        from_season=2021,
        dk=dk,
        names={"E0": "Premier League", "UCL": "Champions League"},
        max_bets=20,
    )
    assert out["kind"] == "edge12_backtest" and out["leagues"]["E0"]["name"] == "Premier League"
    assert out["all"]["leagues"] == ["E0", "SP1"]  # the cup is reported on its own
    assert out["all"]["edge12"]["bets"] == len(b) and "UCL" not in out["all"]["by_league"]
    assert out["all"]["by_league"]["SP1"] == {"bets": 0}
    assert out["leagues"]["UCL"]["kind"] == "close" and out["leagues"]["UCL"]["edge12"]["bets"] == 5
    assert out["bets_total"] == len(b) + 5 and len(out["bets"]) == 20
    rows = [dict(zip(e12b.BET_FIELDS, r, strict=True)) for r in out["bets"]]
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)
    assert len(json.dumps(out["bets"][0])) < 120
    rep_dk = out["draftkings_replay"]
    assert rep_dk["book"] == "DraftKings" and "E0_dk.json" in rep_dk["source"]
    assert rep_dk["summary"] == {"trades": 173, "roi": -0.1343, "clv_dk": -0.0617}
    assert e12b.dk_replay(None) is None
    assert out["rule"]["threshold"] == ef.THRESHOLD and "Pinnacle" in out["note"]


def test_publish_carries_the_backtest_and_says_when_it_is_missing(tmp_path):
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW)
    sb = data["portfolio"]["strategy_backtests"]
    assert sb["edge12"]["available"] is False and "hasn't been run" in sb["edge12"]["note"]
    assert sb["lean"]["available"] is False  # unchanged beside it
    (rep, b), _, _ = report()
    out = e12b.build({"E0": rep}, {"E0": b}, from_season=2021)
    (tmp_path / "backtest").mkdir()
    (tmp_path / "backtest" / "edge12_backtest.json").write_text(json.dumps(out, default=str))
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW)
    sb = data["portfolio"]["strategy_backtests"]["edge12"]
    assert sb["available"] is True and sb["bets_total"] == len(b)
    assert sb["bets_shown"] == min(len(b), e12b.APP_BETS) == len(sb["bets"])
    assert sb["all"]["edge12"]["bets"] == len(b)
    assert e12b.app_section(out, max_bets=3)["bets"] == out["bets"][:3]
    assert e12b.app_section({"kind": "lean_backtest"})["available"] is False


def test_cli_registers_the_command(monkeypatch):
    from soccer_stats import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_backtest_edge12", lambda a: seen.update(vars(a)))
    cli.main(["backtest-edge12", "--leagues", "E0", "--cup-closes", "c.jsonl"])
    assert seen["leagues"] == ["E0"] and seen["from_season"] == 2019
    assert seen["cup_closes"] == "c.jsonl" and seen["burn_in"] == 2


def test_command_runs_every_league_and_never_buys_cup_prices(monkeypatch, tmp_path, capsys):
    from soccer_stats import cli, cup_model

    preds, pins = synth()
    monkeypatch.setattr(cli, "_early_preds", lambda code, a, b: (preds, pins, 0.7))

    def no_history():
        raise AssertionError("no closes file: the cup history must not even be built")

    monkeypatch.setattr(cup_model, "history", no_history)
    path = tmp_path / "e12.json"
    args = argparse.Namespace(
        leagues=["E0", "SP1", "UCL"],
        from_season=2021,
        burn_in=2,
        dk_file=None,
        cup_closes=str(tmp_path / "missing.jsonl"),
        json=str(path),
        log_dir=None,
    )
    cli.cmd_backtest_edge12(args)
    out = json.loads(path.read_text())
    assert (
        out["leagues"]["UCL"]["kind"] == "close" and "never buys" in out["leagues"]["UCL"]["error"]
    )
    assert out["all"]["leagues"] == ["E0", "SP1"]
    assert out["leagues"]["E0"]["edge12"]["bets"] == out["leagues"]["SP1"]["edge12"]["bets"] > 0
    printed = capsys.readouterr().out
    assert "EDGE12_BACKTEST_JSON " in printed and "Model 12% backtest written" in printed
