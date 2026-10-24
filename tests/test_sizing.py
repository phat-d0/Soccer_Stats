"""Kelly sizing tracked beside the flat $10 record (owner, 24 Oct; sizing.py): the formula,
the cap and the zero stake, a sequential bankroll with no look-ahead, pushes and voids, and
the flat curve matching the record. Synthetic data only."""

import math

import numpy as np
import pandas as pd
import pytest
from test_blend_lean import ODDS_IN, P_BET_IN, lean
from test_lean_backtest import synth
from test_paper import NOW, card, src

from soccer_stats import edge12_backtest as e12b
from soccer_stats import lean_backtest as lb
from soccer_stats import paper
from soccer_stats import sizing as sz
from soccer_stats import trades as tr

T0 = pd.Timestamp("2026-10-10 12:00", tz="UTC")


def h(n):
    return T0 + pd.Timedelta(hours=n)


def test_the_formula_with_its_cap_and_the_zero_stake():
    assert sz.kelly_f(0.5, 2.5) == pytest.approx((0.5 * 2.5 - 1) / 1.5)
    # Quarter Kelly: 1,000 x 0.25 x 0.1667 = 41.67.
    assert sz.kelly_stake(1000, 0.5, 2.5) == pytest.approx(41.67)
    # f* = 0.8: a quarter is 20%, capped at 5% of the bankroll.
    assert sz.kelly_stake(1000, 0.9, 2.0) == 50.0
    # No edge (f* <= 0), no chance, no price, no bankroll: no stake.
    assert sz.kelly_stake(1000, 0.3, 2.0) == 0.0
    assert sz.kelly_stake(1000, 0.5, 2.0) == 0.0  # f* = 0 exactly
    assert sz.kelly_stake(1000, None, 2.0) == 0.0 and sz.kelly_stake(1000, 0.5, 1.0) == 0.0
    assert sz.kelly_stake(0, 0.9, 2.0) == 0.0 and sz.kelly_stake(-5, 0.9, 2.0) == 0.0
    assert sz.kelly_f(float("nan"), 2) is None and sz.kelly_f(1.2, 2) is None
    assert (sz.KELLY_FRACTION, sz.KELLY_CAP, sz.BANKROLL_START) == (0.25, 0.05, 1000.0)


def bet(i, open_h, settle_h, ret, p=0.6, odds=2.0):
    return {
        "id": f"b{i}",
        "open": h(open_h),
        "settle": None if settle_h is None else h(settle_h),
        "ret": ret,
        "p": p,
        "odds": odds,
    }


def test_a_stake_never_uses_later_profit_or_loss():
    # p 0.6 at 2.0: f* = 0.2, a quarter is 5% (at the cap).
    bets = [
        bet(1, 0, 10, 1.0),  # wins +50 at hour 10
        bet(2, 5, 12, -1.0),  # opened before bet 1 settled: sized on 1,000
        bet(3, 11, 20, 1.0),  # opened after: sized on 1,050
        bet(4, 13, None, None),  # still open: staked on 1,050 - 52.50
    ]
    out = sz.simulate(bets, sz.kelly_fn())
    st = out["stakes"]
    assert st["b1"] == 50.0 and st["b2"] == 50.0
    assert st["b3"] == pytest.approx(52.5) and st["b4"] == pytest.approx(0.05 * (1050 - 50))
    assert out["bankroll_now"] == pytest.approx(1000 + 50 - 50 + 52.5)
    assert out["bets"] == 4 and out["bets_settled"] == 3 and out["staked"] == pytest.approx(152.5)
    # Reordering the input changes nothing: the path follows the clock.
    assert sz.simulate(list(reversed(bets)), sz.kelly_fn())["stakes"] == st
    # A later loss can't reach back into an earlier stake.
    worse = [*bets[:2], {**bets[2], "ret": -1.0}, bets[3]]
    assert sz.simulate(worse, sz.kelly_fn())["stakes"]["b3"] == st["b3"]


def test_pushes_and_voids_return_the_stake():
    bets = [bet(1, 0, 5, 0.0), bet(2, 1, 6, 1.0), bet(3, 7, 9, 0.0)]
    out = sz.simulate(bets, sz.kelly_fn())
    assert out["bankroll_now"] == pytest.approx(1050.0)
    assert out["staked"] == pytest.approx(50.0)  # nothing was at risk on the push or void
    assert out["roi_on_staked"] == pytest.approx(1.0)
    assert out["bets_staked"] == 3 and out["bets_zero_stake"] == 0


def test_drawdown_growth_curve_and_zero_stakes():
    bets = [bet(1, 0, 1, 1.0), bet(2, 2, 3, -1.0), bet(3, 4, 5, -1.0), bet(4, 6, 7, 1.0, p=0.3)]
    out = sz.simulate(bets, sz.kelly_fn())
    peak = 1050.0
    low = peak - 52.5 - 0.05 * (peak - 52.5)
    assert out["max_drawdown"] == pytest.approx(peak - low, abs=0.01)
    assert out["max_drawdown_pct"] == pytest.approx((peak - low) / peak, abs=1e-4)
    assert out["bets_zero_stake"] == 1 and out["bets_staked"] == 3
    assert out["growth_per_bet"] == pytest.approx(math.log(low / 1000) / 4, abs=1e-5)
    last = out["curve"][-1]  # stakes are whole cents
    assert last["date"] == "2026-10-10" and last["bankroll"] == pytest.approx(low, abs=0.02)
    many = [bet(i, i, i + 0.5, 1.0 if i % 2 else -1.0) for i in range(0, 2000, 4)]
    curve = sz.simulate(many, sz.flat_fn)["curve"]
    assert len(curve) <= sz.MAX_CURVE
    assert curve[-1]["date"] == h(1996.5).date().isoformat()


def corner(i, status, p=0.55, push=0.1, odds=1.9):
    return {
        "id": f"E0|2627|H{i}|A{i}|corners|home|4.5",
        "source": "live",
        "bet_type": "corners",
        "portfolio": "corners",
        "market": "team_corners",
        "league": "E0",
        "season": "2627",
        "home": f"H{i}",
        "away": f"A{i}",
        "kickoff": h(24 * i + 20).isoformat(),
        "opened_at": h(24 * i).isoformat(),
        "settled_at": h(24 * i + 23).isoformat() if status != "open" else None,
        "odds": odds,
        "stake": 10.0,
        "model_p": p,
        "p_push": push,
        "edge": p * odds - 1,
        "status": status,
        "profit": {"won": 10 * (odds - 1), "lost": -10.0, "void": 0.0}.get(status),
        "push": status == "void",
        "clv_pinnacle": 0.0,
        "strategy": "pinnacle",
        "bookmaker": "pinnacle",
    }


def test_the_flat_curve_is_the_record_and_corners_size_on_model_f_given_no_push():
    trades = [corner(1, "won"), corner(2, "lost"), corner(3, "void"), corner(4, "open")]
    sec = paper.corner_strategies(trades)[0]
    s = sec["sizing"]
    assert s["flat"]["profit"] == pytest.approx(sec["summary"]["profit"])
    assert s["flat"]["profit"] == pytest.approx(sum(t["profit"] or 0 for t in trades))
    assert s["flat"]["bankroll_now"] == pytest.approx(1000 + sec["summary"]["profit"])
    assert s["flat"]["roi_on_staked"] == pytest.approx(sec["summary"]["roi"])
    assert "stakes" not in s["flat"] and s["kelly"]["p_source"] == "model_f"
    p = 0.55 / 0.9  # given no push
    assert s["kelly"]["stakes"][trades[0]["id"]] == sz.kelly_stake(1000, p, 1.9)
    assert "illustrative" in s["kelly"]["p_note"] and "kelly_raw" not in s
    # The below-60% shadow sizes its own trades on its own bankroll.
    assert "sizing" in paper.corner_strategies(trades)[1]


def test_model_12_percent_sizes_on_its_blend_chance_and_shows_the_raw_model_beside_it():
    rule = tr.paper_threshold(None, "E0")
    led = {}
    paper.update_ledger(
        led,
        [card(p_bet=P_BET_IN, odds=ODDS_IN)],
        src(),
        None,
        NOW,
        threshold=0.12,
        rule=rule,
        lean=lean(),
    )
    t = led["E0|2627|Arsenal|Leeds"]
    assert t["market"] == "draw" and t["p_blend"] == 0.24 and t["p_blend_from"] == "open"
    sec = paper.strategy_section(list(led.values()), "edge12", "Model 12%", rule)
    s = sec["sizing"]
    assert s["kelly"]["p_source"] == "blend" and s["kelly_raw"]["p_source"] == "model"
    assert s["kelly_raw"]["label"] == "Kelly on the raw model (over-confident)"
    assert s["blend_from"] == {"open": 1}
    # The blend's draw 0.24 at 5.0: f* = 0.05, a quarter is 1.25% of 1,000.
    assert s["kelly"]["stakes"][t["id"]] == sz.kelly_stake(1000, 0.24, 5.0) == 12.5
    lean_sec = paper.strategy_section(list(led.values()), "lean", "Lean", lean())
    assert lean_sec["sizing"]["kelly"]["stakes"]["E0|2627|Arsenal|Leeds|lean"] == (
        sz.kelly_stake(1000, 0.62, 1.8)
    )


def log_row(t, p_bet=None, prices=None):
    prices = prices or {"home": 1.8, "draw": 5.0, "away": 3.2}
    return {
        "league": "E0",
        "home": t["home"],
        "away": t["away"],
        "kickoff": pd.Timestamp(t["kickoff"]),
        "market": "h2h",
        "prices": prices,
        "fair": {"home": 0.52, "draw": 0.19, "away": 0.29},
        "p": {"home": 0.61, "draw": 0.24, "away": 0.15},
        "p_bet": p_bet,
        "fetched_at": pd.Timestamp(t["odds_fetched_at"]),
        "downloaded_at": pd.Timestamp(t["odds_fetched_at"]).isoformat(),
        "logged_at": NOW.isoformat(),
    }


def old_12(led):
    t = led["E0|2627|Arsenal|Leeds"]
    t.pop("p_blend"), t.pop("p_blend_from")
    return t


def test_older_twelve_percent_trades_get_their_blend_chance_once_from_the_odds_log():
    rule = tr.paper_threshold(None, "E0")
    later = NOW + pd.Timedelta(hours=1)
    opened = pd.Timestamp(NOW)
    for p_bet, want, source, made in (
        ({"home": 0.5, "draw": 0.21, "away": 0.29}, 0.21, "odds_log", None),
        (None, None, "recomputed", (opened - pd.Timedelta(days=1)).isoformat()),
        # A fit made after the trade opened: look-ahead, so it is flagged.
        (None, None, "approx_current_fit", (opened + pd.Timedelta(days=1)).isoformat()),
        (None, None, "approx_current_fit", None),  # unknown fit time: flagged too
    ):
        led = {}
        paper.update_ledger(
            led, [card(odds=ODDS_IN)], src(), None, NOW, threshold=0.12, rule=rule, lean=None
        )
        t = old_12(led)
        log = pd.DataFrame([log_row(t, p_bet)])
        coef = [0.0, 0.0, 0.5, 0.5]
        rule_l = {**lean(), "coef": coef, "fit_made_at": made}
        ev, _ = paper.update_ledger(led, [], src(later), None, later, odds_log=log, lean=rule_l)
        upd = [e for e in ev if "p_blend" in e]
        assert len(upd) == 1 and upd[0]["p_blend_from"] == source
        if want is None:  # the league's blend on the logged fair price and model chances
            from soccer_stats import match_calibration as mc

            want = round(float(mc.apply(coef, [[0.52, 0.19, 0.29]], [[0.61, 0.24, 0.15]])[0][1]), 4)
        assert upd[0]["p_blend"] == pytest.approx(want)
        led[t["id"]].update(p_blend=upd[0]["p_blend"], p_blend_from=source)
        ev, _ = paper.update_ledger(led, [], src(later), None, later, odds_log=log, lean=lean())
        assert not [e for e in ev if "p_blend" in e]  # once only
    assert {"p_blend", "p_blend_from"} <= paper.UPDATE_FIELDS


def test_both_backtest_files_carry_sizing_per_league_and_pooled():
    preds, pins = synth()
    rep, b = lb.league_report("E0", preds, pins)
    out = lb.build({"E0": rep}, [b], from_season=2021)
    for part in (out["leagues"]["E0"], out["all"]):
        s = part["sizing"]
        assert set(s) == {"flat", "kelly"} and s["kelly"]["p_source"] == "blend"
        assert s["flat"]["profit"] == pytest.approx(10 * b["profit"].sum(), abs=0.05)
        assert s["flat"]["bets"] == len(b) and len(s["flat"]["curve"]) <= sz.MAX_CURVE
        assert "stakes" not in s["kelly"] and "growth_per_bet" in s["kelly"]
    rep, b12 = e12b.league_report("E0", preds, pins)
    assert "p_blend" in b12 and b12["p_blend"].notna().any()
    assert b12["p_blend"].between(0, 1).all() or b12["p_blend"].isna().any()
    out = e12b.build({"E0": rep}, {"E0": b12}, from_season=2021)
    s = out["all"]["sizing"]
    assert set(s) == {"flat", "kelly", "kelly_raw"}
    assert s["kelly"]["bets_no_chance"] == int(b12["p_blend"].isna().sum())
    assert s["flat"]["profit"] == pytest.approx(10 * b12["profit"].sum(), abs=0.05)
    # Bets are decided 4 days before kickoff: none opens after its own settlement.
    rows = sz.backtest_bets(b12, "p_blend")
    assert all(r["open"] < r["settle"] for r in rows)
    assert np.isfinite([r["ret"] for r in rows]).all()


def test_data_json_keeps_per_league_curves_short():
    curve = [{"date": f"d{i}", "bankroll": float(i)} for i in range(200)]
    leagues = {
        "E0": {"sizing": {"flat": {"curve": curve}, "kelly": {"curve": curve}}},
        "D1": {"error": "down"},
    }
    out = sz.app_curves(leagues)
    assert len(out["E0"]["sizing"]["kelly"]["curve"]) == sz.APP_LEAGUE_CURVE
    assert out["E0"]["sizing"]["kelly"]["curve"][-1] == curve[-1] and out["D1"] == {"error": "down"}
    assert len(leagues["E0"]["sizing"]["flat"]["curve"]) == 200  # the file itself is untouched
