"""Per-band aggregates in both backtest files (bands.py; owner via the Research lab, 23 Oct):
odds bands, claimed-edge and CLV quintiles cut once on the pooled domestic bets, ranges
from resampling matches. Synthetic data only: nothing reaches the network."""

import json

import numpy as np
import pandas as pd
import pytest
from test_cup_backtest import frames
from test_lean_backtest import synth

from soccer_stats import bands as bd
from soccer_stats import edge12_backtest as e12b
from soccer_stats import lean_backtest as lb


def bets_frame(odds, won, clv=None, league="E0", p=None):
    n = len(odds)
    odds = np.asarray(odds, dtype=float)
    won = np.asarray(won, dtype=bool)
    return pd.DataFrame(
        {
            "date": [f"2025-01-{i % 28 + 1:02d}" for i in range(n)],
            "league": league,
            "home": [f"H{i}" for i in range(n)],
            "away": [f"A{i}" for i in range(n)],
            "odds": odds,
            "won": won,
            "profit": np.where(won, odds - 1, -1.0),
            "clv": np.zeros(n) if clv is None else clv,
            "model_p": np.full(n, 0.5) if p is None else p,
        }
    )


def test_odds_bands_have_fixed_edges_keep_empty_bands_and_shares_sum_to_one():
    df = bets_frame([1.4, 1.5, 1.51, 2.0, 2.5, 3.0, 4.9, 5.0, 5.01], [1, 0, 1, 1, 0, 0, 0, 1, 0])
    out = bd.by_odds_band(df)
    assert [(b["lo"], b["hi"]) for b in out] == bd.ODDS_BANDS
    # lo < odds <= hi: 1.5 is in the first band, 2.0 in the second, 5.0 in the fourth.
    assert [b["bets"] for b in out] == [2, 2, 2, 2, 1]
    assert sum(b["share"] for b in out) == pytest.approx(1, abs=1e-3)
    first = out[0]
    assert first["wins"] == 1 and first["win_rate"] == 0.5
    assert first["breakeven"] == pytest.approx((1 / 1.4 + 1 / 1.5) / 2, abs=1e-4)
    assert first["roi"] == pytest.approx((0.4 - 1) / 2, abs=1e-4)
    sparse = bd.by_odds_band(bets_frame([1.8, 1.9], [1, 0]))
    assert [b["bets"] for b in sparse] == [0, 2, 0, 0, 0] and sparse[0] == {
        "lo": None,
        "hi": 1.5,
        "bets": 0,
        "share": 0.0,
    }
    assert [b["bets"] for b in bd.by_odds_band(bets_frame([], []))] == [0] * 5


def test_ranges_resample_matches_not_rows():
    # One match with two bets counts once: its two rows move together in every draw.
    v = np.array([1.0, 1.0, 0.0, 0.0])
    lo, hi = bd.boot_mean(v, ["m1", "m1", "m2", "m3"])
    assert lo == 0.0 and hi == 1.0
    assert bd.boot_mean([1.0, 2.0], ["m1", "m1"]) is None  # a single match: no range
    assert bd.boot_mean([np.nan, 1.0, 2.0], ["a", "b", "c"]) is not None
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, 400)
    lo, hi = bd.boot_mean(x, np.arange(400))
    assert lo < x.mean() < hi and hi - lo == pytest.approx(2 * 1.96 / 20, rel=0.2)


def test_quintile_cuts_split_the_pooled_bets_into_fifths_and_bands_are_open_at_the_ends():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.2, 0.6, 500)
    odds = rng.uniform(1.12, 1.5, 500) / p  # claimed edges spread from +12% to +50%
    df = bd.with_claimed(bets_frame(odds, rng.random(500) < p, p=p), "model_p")
    assert np.allclose(df["claimed"], p * odds - 1)
    cuts = bd.quintile_cuts(df["claimed"])
    assert cuts == sorted(cuts) and len(cuts) == 4
    out = bd.by_cut_band(df, "claimed", cuts, "claimed")
    assert [b["bets"] for b in out] == [100] * 5
    assert out[0]["lo"] is None and out[-1]["hi"] is None and out[1]["lo"] == cuts[0]
    assert all(b["lo"] is None or b["claimed"] > b["lo"] for b in out)
    assert bd.quintile_cuts([0.1, 0.2]) is None and bd.by_cut_band(df, "claimed", None) == []


def test_lean_file_cuts_once_on_the_pooled_domestic_bets_and_applies_them_everywhere(tmp_path):
    preds, pins = synth()
    a, ba = lb.league_report("E0", preds, pins)
    b, bb = lb.league_report("SP1", preds, pins, refit="56D")
    cup_preds, pin, dk = frames(tmp_path, n=700)
    from soccer_stats import cup_backtest as cb

    ucl, bu = cb.lean_report(cup_preds, pin, dk)
    out = lb.build(
        {"E0": a, "SP1": b, "UCL": ucl, "D1": {"error": "down"}},
        [ba, bb, bu, lb.bets(pd.DataFrame(), "D1")],
        from_season=2021,
    )
    pooled = bd.with_claimed(pd.concat([ba, bb]), "p_bet")
    assert out["bands"]["edge_cuts"] == bd.quintile_cuts(pooled["claimed"])
    assert out["bands"]["clv_cuts"] == bd.quintile_cuts(pooled["clv"])
    for code in ("E0", "SP1", "UCL"):
        lg = out["leagues"][code]
        bands = lg["by_edge_band"]
        assert [x["lo"] for x in bands][1:] == out["bands"]["edge_cuts"]
        assert sum(x["bets"] for x in lg["by_odds_band"]) == lg["lean"]["bets"]
    assert "by_odds_band" not in out["leagues"]["D1"]
    # Pooled bands count every domestic bet, never the cup's.
    assert sum(x["bets"] for x in out["all"]["by_odds_band"]) == len(ba) + len(bb)
    assert sum(x["bets"] for x in out["all"]["by_edge_band"]) == len(ba) + len(bb)
    e0 = out["leagues"]["E0"]["by_edge_band"]
    band = [x for x in e0 if x["bets"]][0]
    claimed = bd.with_claimed(ba, "p_bet")
    v = claimed["claimed"].to_numpy()
    lo, hi = band["lo"], band["hi"]
    m = (v > lo if lo is not None else True) & (v <= hi if hi is not None else True)
    assert band["bets"] == int(np.sum(m)) and band["z"] == pytest.approx(
        claimed.loc[m, "z"].mean(), abs=1e-4
    )
    sec = lb.app_section(json.loads(json.dumps(out, default=str)), max_bets=3)
    assert sec["all"]["by_edge_band"] == out["all"]["by_edge_band"] and len(sec["bets"]) == 3
    assert sec["bands"] == out["bands"]


def test_edge12_file_bands_use_the_models_own_edge_on_every_bet():
    preds, pins = synth()
    rep, b = e12b.league_report("E0", preds, pins)
    out = e12b.build({"E0": rep}, {"E0": b}, from_season=2021, max_bets=5)
    assert len(out["bets"]) == 5  # the trimmed rows don't limit the bands
    assert sum(x["bets"] for x in out["all"]["by_odds_band"]) == len(b) == rep["edge12"]["bets"]
    assert out["bands"]["edge_cuts"] == bd.quintile_cuts(b["edge"])
    for x in out["all"]["by_edge_band"]:
        assert x["bets"] in (len(b) // 5, len(b) // 5 + 1, len(b) // 5 - 1)
        assert "z" not in x
    claimed = [x["claimed"] for x in out["all"]["by_edge_band"]]
    assert claimed == sorted(claimed)
    json.dumps(out)
