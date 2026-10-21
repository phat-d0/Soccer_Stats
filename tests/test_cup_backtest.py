"""The Champions League's match backtests at DraftKings' bought close (cup_backtest.py;
owner, 21 Oct). Synthetic walk-forward rows and closes: nothing reaches the network."""

import argparse
import json

import numpy as np
import pandas as pd
import pytest
from test_cup_model import closes_file, synthetic_preds
from test_lean_backtest import synth

from soccer_stats import cup_backtest as cb
from soccer_stats import cup_model as cm
from soccer_stats import edge12_backtest as e12b
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.lab import edge12_filters as ef
from soccer_stats.odds import devig_shin


def frames(tmp_path, n=400, seed=0):
    preds = synthetic_preds(n=n)
    pin, dk = cb.load(closes_file(tmp_path, preds, np.random.default_rng(seed)))
    return preds, pin, dk


def test_targets_are_dk_closes_with_pinnacle_for_clv_and_skip_what_live_skips(tmp_path):
    preds, pin, dk = frames(tmp_path, n=60)
    preds.loc[0, "covered"] = False
    preds.loc[0, ["p_home", "p_draw", "p_away"]] = np.nan
    preds.loc[1, "low_data"] = True
    preds.loc[2, ["home_goals", "away_goals"]] = np.nan  # no 90-minute score
    dk = dk[dk["espn_id"] != "3"]  # no DraftKings close
    pin = pin[pin["espn_id"] != "4"]  # no Pinnacle close: still a candidate, no CLV
    df = cb.joined(preds, pin, dk)
    t = cb.target_rows(df)
    assert set(t["espn_id"]) == {str(i) for i in range(5, 60)} | {"4"}
    r = t[t["espn_id"] == "10"].iloc[0]
    d = dk[dk["espn_id"] == "10"].iloc[0]
    p = pin[pin["espn_id"] == "10"].iloc[0]
    assert r["odds_home"] == d["close_home"] and r["close_away"] == p["close_away"]
    fair = devig_shin(np.array([d["close_home"], d["close_draw"], d["close_away"]], dtype=float))
    assert np.allclose([r["mkt_home"], r["mkt_draw"], r["mkt_away"]], fair)
    assert (t["day"] == t["date"]).all()  # the bet is taken at the close
    cov = {c["season"]: c for c in cb.coverage(df)}
    assert sum(c["candidates"] for c in cov.values()) == len(t)
    assert sum(c["matches"] for c in cov.values()) == 59
    assert cb.target_rows(cb.joined(preds.iloc[:0], pin, dk)).empty


def test_model_12_at_the_close_is_the_live_rule_with_clv_against_pinnacle(tmp_path):
    preds, pin, dk = frames(tmp_path)
    rep, b = e12b.cup_report(preds, pin, dk)
    assert rep["kind"] == "close" and "close, not early" in rep["note"]
    assert rep["edge12"]["bets"] == len(b) > 10 and set(b["league"]) == {"UCL"}
    t = cb.target_rows(cb.joined(preds, pin, dk))
    expect = ef.bets(t, "UCL", "baseline")
    pd.testing.assert_frame_equal(b.reset_index(drop=True), expect.reset_index(drop=True))
    r = b.iloc[0]
    row = t[(t["date"].dt.date.astype(str) == r["date"])].iloc[0]
    close = {m: row[f"close_{m}"] for m in mc.H2H}
    assert r["clv"] == pytest.approx(tr.clv(r["odds"], r["market"], close))
    assert r["odds"] == row[f"odds_{r['market']}"]  # DraftKings' quoted close


def test_lean_at_the_close_never_lowers_the_minimum_and_says_how_few_bets(tmp_path):
    preds, pin, dk = frames(tmp_path, n=302)  # as many rows as the real 2024/25-2025/26
    rep, b = cb.lean_report(preds, pin, dk)
    assert rep["min_rows"] == mc.MIN_ROWS == 300 and rep["training_matches"] == 302
    # Only blocks starting after 300 earlier matches are scored: almost nothing.
    assert rep["scored"] <= 2 and len(b) <= rep["scored"]
    assert rep["lean"]["bets"] == len(b)
    assert f"{mc.MIN_ROWS} earlier matches" in rep["note"] and "302" in rep["note"]
    small, nb = cb.lean_report(*frames(tmp_path, n=250))
    assert small["scored"] == 0 and nb.empty and "0 bets" in small["note"]


def test_lean_at_the_close_fits_on_earlier_matches_only(tmp_path):
    preds, pin, dk = frames(tmp_path, n=700)
    train = mc.training_rows(cm.blend_preds(preds, pin), "h2h")
    target = cb.target_rows(cb.joined(preds, pin, dk))
    out, fits = lb.walk_forward(train, target)
    done = out[out["sigma"].notna()]
    assert len(done) > 300 and len(fits) > 5
    assert (done["fit_to"] < done["fit_from"]).all() and (done["fit_from"] <= done["day"]).all()
    assert (done["fit_rows"] >= mc.MIN_ROWS).all()
    # Rewriting every result from a block on changes nothing scored before it.
    cut = pd.Timestamp(fits[3]["from"])
    wild = train.copy()
    wild.loc[pd.to_datetime(wild["date"]) >= cut, "y"] = 0
    out2, _ = lb.walk_forward(wild, target)
    keep = out["fit_from"] <= cut
    cols = ["pb_home", "pb_draw", "pb_away", "sigma"]
    assert np.allclose(
        out.loc[keep, cols].to_numpy(dtype=float),
        out2.loc[keep, cols].to_numpy(dtype=float),
        equal_nan=True,
    )
    rep, b = cb.lean_report(preds, pin, dk)
    assert rep["scored"] == len(done) and len(b) and set(b["league"]) == {"UCL"}
    assert (b["z"] >= 1 - 1e-9).all()


def test_the_cup_reports_need_the_bought_closes_and_complete_results(tmp_path, monkeypatch):
    from soccer_stats import cli

    calls = []
    monkeypatch.setattr(cm, "history", lambda: calls.append(1) or {"error": "ESPN down"})
    none = cli._cup_close_reports(["E0", "UCL"], tmp_path / "missing.jsonl", ("lean", "edge12"))
    assert set(none) == {"UCL"} and not calls  # no file: nothing built, nothing bought
    for rule in ("lean", "edge12"):
        rep, b = none["UCL"][rule]
        assert rep == cb.error_report(cb.NO_FILE) and b.empty
    preds, pin, dk = frames(tmp_path)
    path = tmp_path / "UCL_closes.jsonl"  # written by frames()
    down = cli._cup_close_reports(["UCL", "UEL"], path, ("edge12",))
    assert down["UCL"]["edge12"][0]["error"] == "ESPN down" and len(calls) == 1
    assert "No bought closes" in down["UEL"]["edge12"][0]["error"]
    monkeypatch.setattr(cm, "history", lambda: {"error": None, "preds": preds})
    ok = cli._cup_close_reports(["UCL"], path, ("lean", "edge12"))
    assert ok["UCL"]["edge12"][0]["edge12"]["bets"] > 0
    assert ok["UCL"]["lean"][0]["kind"] == "close"


def test_backtest_lean_adds_the_cup_at_the_close_beside_the_early_leagues(tmp_path, monkeypatch):
    from soccer_stats import cli

    early_preds, pins = synth()
    preds = synthetic_preds(n=700)
    path = closes_file(tmp_path, preds, np.random.default_rng(0))
    monkeypatch.setattr(cli, "_early_preds", lambda code, a, b: (early_preds, pins, 0.7))
    monkeypatch.setattr(cm, "history", lambda: {"error": None, "preds": preds})
    out_path = tmp_path / "lean.json"
    args = argparse.Namespace(
        leagues=["E0", "UCL"],
        from_season=2021,
        burn_in=2,
        dk_file=None,
        cup_closes=str(path),
        json=str(out_path),
        log_dir=None,
    )
    cli.cmd_backtest_lean(args)
    out = json.loads(out_path.read_text())
    ucl, e0 = out["leagues"]["UCL"], out["leagues"]["E0"]
    assert ucl["kind"] == "close" and e0["kind"] == "early"
    assert out["all"]["leagues"] == ["E0"] and out["all"]["lean"]["bets"] == e0["lean"]["bets"]
    assert out["bets_total"] == e0["lean"]["bets"] + ucl["lean"]["bets"]
    assert any(r[1] == "UCL" for r in out["bets"]) and ucl["lean"]["bets"] > 0
