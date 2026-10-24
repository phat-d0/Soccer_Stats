"""Draw no bet replayed on Pinnacle's early prices (docs/lab.md, "Draw no bet")."""

import numpy as np
import pandas as pd
import pytest
from test_lean_backtest import synth

from soccer_stats import dnb_backtest as dnb
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats.odds import devig_shin


def test_synthetic_price_backs_the_side_and_covers_the_draw():
    # $1 split so a draw returns $1: side stake x, draw stake 1/draw; side wins x * side.
    side, draw = 2.4, 3.5
    price = dnb.dnb_odds(side, draw)
    d_stake = 1 / draw
    assert price == pytest.approx((1 - d_stake) * side)
    assert d_stake * draw == pytest.approx(1.0)  # a draw refunds the dollar
    assert dnb.dnb_odds(None, 3.5) is None and dnb.dnb_odds(2.0, 1.0) is None


def test_fair_dnb_takes_the_draw_out_of_the_shin_price():
    prices = [2.1, 3.4, 3.9]
    f = devig_shin(np.array(prices))
    got = dnb.fair_dnb(prices)
    assert got["home"] == pytest.approx(f[0] / (f[0] + f[2]))
    assert got["home"] + got["away"] == pytest.approx(1.0)
    assert got["draw"] == pytest.approx(f[1])
    assert dnb.fair_dnb([2.1, None, 3.9]) is None
    assert dnb.without_draw({"home": 0.5, "draw": 0.3, "away": 0.2}) == pytest.approx(
        {"home": 0.5 / 0.7, "away": 0.2 / 0.7}
    )


def test_rules_pick_the_better_side_at_their_thresholds():
    q = {"home": 0.60, "away": 0.40}
    # home edge 0.6 * 1.9 - 1 = 14%; away 0.4 * 2.2 - 1 = -12%
    pk = dnb.pick_12(q, {"home": 1.9, "away": 2.2})
    assert pk["market"] == "home" and pk["edge"] == pytest.approx(0.14)
    assert dnb.pick_12(q, {"home": 1.8, "away": 2.2}) is None  # 8%
    assert dnb.pick_12(q, {"home": 1.12 / 0.6, "away": 2.0})["market"] == "home"  # exactly 12%
    # Lean: z = (0.6 - 1/1.9) / 0.05 = 1.47
    pk = dnb.pick_lean(q, {"home": 1.9, "away": 2.2}, 0.05)
    assert pk["market"] == "home" and pk["z"] == pytest.approx((0.6 - 1 / 1.9) / 0.05)
    assert dnb.pick_lean(q, {"home": 1.9, "away": 2.2}, 0.2) is None
    assert dnb.pick_lean(q, {"home": 1.9, "away": 2.2}, None) is None


def test_a_draw_refunds_the_stake():
    assert dnb.result("home", 1, 1) == "drawn"
    assert dnb.result("home", 2, 1) == "won" and dnb.result("away", 2, 1) == "lost"
    b = pd.DataFrame(
        [
            {"result": "won", "profit": 1.0, "odds": 2.0, "p": 0.55, "market": "home"},
            {"result": "drawn", "profit": 0.0, "odds": 2.0, "p": 0.55, "market": "home"},
            {"result": "lost", "profit": -1.0, "odds": 2.0, "p": 0.55, "market": "away"},
            {"result": "won", "profit": 1.0, "odds": 2.0, "p": 0.55, "market": "away"},
        ]
    ).assign(clv=[0.02, 0.0, -0.01, 0.01], clv_staked=[0.015, 0.0, -0.007, 0.007])
    m = dnb.metrics(b)
    assert (m["won"], m["drawn"], m["lost"]) == (2, 1, 1)
    assert m["hit_rate"] == pytest.approx(2 / 3)
    assert m["roi"] == pytest.approx(1 / 4)
    assert m["breakeven"] == pytest.approx(0.5)
    assert m["bets_for_5pct"] == int(np.ceil((1.96 * b["profit"].std() / 0.05) ** 2))


def ah_frame(pins, zero_every=3):
    """Pinnacle AH rows: line 0 on every `zero_every`th match, priced from the early 1X2."""
    rows = []
    for i, r in enumerate(pins.to_dict("records")):
        f = dnb.fair_dnb([r["odds_home"], r["odds_draw"], r["odds_away"]])
        line = 0.0 if i % zero_every == 0 else -0.5
        rows.append(
            {
                "season": r["season"],
                "home": r["home"],
                "away": r["away"],
                "ah_early_line": line,
                "ah_early_home": 1 / (f["home"] * 1.02),
                "ah_early_away": 1 / (f["away"] * 1.02),
                "ah_close_line": line,
                "ah_close_home": np.nan,
                "ah_close_away": np.nan,
            }
        )
    return pd.DataFrame(rows)


def test_league_report_scores_dnb_beside_the_1x2_rules_on_the_same_matches():
    preds, pins = synth()
    rep, b, ab = dnb.league_report("E0", preds, pins, ah_frame(pins))
    assert set(b["rule"]) <= set(dnb.RULES) and {"dnb12", "x12"} <= set(b["rule"])
    key = ["date", "home", "away"]
    cand = set(map(tuple, b[key].to_numpy()))
    # Every rule bets the same scored matches, at most once per match.
    assert not b.duplicated(["rule", *key]).any()
    assert len(cand) <= rep["candidates"]
    # DNB bets push on draws and pay price - 1 on wins; 1X2 bets never push.
    d = b[b["rule"].isin(dnb.DNB_RULES)]
    assert (d.loc[d["result"] == "drawn", "profit"] == 0).all()
    w = d[d["result"] == "won"]
    assert np.allclose(w["profit"], w["odds"] - 1)
    assert (b.loc[b["rule"].isin(("x12", "x_lean")), "result"] != "drawn").all()
    # DNB prices are shorter than the side's 1X2 price.
    assert rep["rules"]["dnb12"]["avg_odds"] < rep["rules"]["x12"]["avg_odds"]
    assert rep["ah0"]["matches"] > 0 and rep["ah0"]["margin_ah0"] == pytest.approx(0.02)
    assert len(ab) and set(ab["rule"]) <= set(dnb.DNB_RULES)


def test_sigma_dnb_uses_only_each_fits_own_earlier_rows():
    preds, pins = synth()
    merged = lb.with_pinnacle(preds, pins)
    train = mc.training_rows(merged, "h2h")
    _, fits = lb.walk_forward(train, lb.target_rows(merged))
    sig = dnb.sigma_dnb(train, fits)
    f = fits[1]
    rows = train[pd.to_datetime(train["date"]) < pd.Timestamp(f["from"])]
    mk = rows[[f"mkt_{m}" for m in mc.H2H]].to_numpy()
    bl = mc.apply(f["coef"], mk, rows[[f"p_{m}" for m in mc.H2H]].to_numpy())
    want = (bl[:, 0] / (bl[:, 0] + bl[:, 2]) - mk[:, 0] / (mk[:, 0] + mk[:, 2])).std()
    assert sig[f["from"]] == pytest.approx(want)
    # A later match changing never moves an earlier fit's sigma.
    later = train.copy()
    later.loc[pd.to_datetime(later["date"]) >= pd.Timestamp(f["from"]), "mkt_home"] = 0.9
    assert dnb.sigma_dnb(later, fits)[f["from"]] == pytest.approx(want)


def test_build_pools_the_leagues_and_applies_the_pass_rule():
    preds, pins = synth()
    rep, b, ab = dnb.league_report("E0", preds, pins, ah_frame(pins))
    out = dnb.build(
        {"E0": rep, "SP1": {"error": "x"}},
        {"E0": b, "SP1": b.iloc[0:0]},
        {"E0": ab},
        from_season=2021,
    )
    assert out["kind"] == dnb.KIND and out["all"]["leagues"] == ["E0"]
    assert out["all"]["rules"]["dnb12"]["bets"] == rep["rules"]["dnb12"]["bets"]
    v = out["verdict"]["dnb12"]
    lo = out["all"]["rules"]["dnb12"]["clv_ci975"][0]
    assert v["passes"] == (lo > 0) and v["baseline"] == "x12"
    bands = out["all"]["rules"]["dnb12"]["by_odds_band"]
    assert sum(x["bets"] for x in bands) == out["all"]["rules"]["dnb12"]["bets"]
