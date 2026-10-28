"""The draw no bet paper tracks beside the match strategies (owner, 26 Oct; paper_dnb)."""

import numpy as np
import pandas as pd
import pytest
from test_blend_lean import rows
from test_paper import KICKOFF, NOW, card, results, src

from soccer_stats import match_calibration as mc
from soccer_stats import paper
from soccer_stats import paper_dnb as pdnb
from soccer_stats import trades as tr
from soccer_stats.odds import devig_shin

EDGE12_DNB = "E0|2627|Arsenal|Leeds|dnb"
LEAN_DNB = "E0|2627|Arsenal|Leeds|lean|dnb"
LEAN_ODDS = {"home": 2.6, "draw": 3.4, "away": 2.9, "over25": 1.7, "under25": 2.1}
LEAN_P_BET = {"home": 0.45, "draw": 0.27, "away": 0.28, "over25": None, "under25": None}
SIGMA_DNB = 0.03
AFTER = pd.Timestamp("2026-10-10 18:00", tz="UTC")


def blends(sigma_dnb=SIGMA_DNB):
    entry = {"coef": [0, 0, 1, 0], "sigma": 0.5, "matches": 900}  # a wide 1X2 sigma: no Lean
    if sigma_dnb is not None:
        entry["sigma_dnb"] = sigma_dnb
    return {"leagues": {"E0": entry}}


def run(tmp_path, fixtures, now=NOW, res=None, sigma_dnb=SIGMA_DNB):
    data = {
        "fixtures": fixtures,
        "odds_source": src(now - pd.Timedelta(minutes=30)),
        "portfolio": {},
        "match_blends": blends(sigma_dnb),
    }
    written = paper.run(data, tmp_path, res, now=now)
    ml = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "moneyline")
    return written, data, ml


# ---------- the maths ----------


def test_dnb_price_backs_the_side_and_covers_the_draw():
    assert tr.dnb_odds(7.5, 5.0) == pytest.approx(6.0)
    # $1 split: 1/5 on the draw returns $1 on a draw; the rest on the side pays 6.0 on a win.
    assert (1 - 1 / 5.0) * 7.5 == pytest.approx(6.0)
    assert tr.dnb_odds(None, 5.0) is None and tr.dnb_odds(2.0, 1.0) is None
    assert tr.dnb_prices({"home": 1.4, "draw": 5.0, "away": 7.5}) == pytest.approx(
        {"home": 1.12, "away": 6.0}
    )
    assert tr.dnb_prices({"home": 1.4, "away": 7.5}) is None


def test_fair_dnb_and_clv_take_the_draw_out_of_the_shin_price():
    close = {"home": 1.45, "draw": 4.6, "away": 7.0}
    f = devig_shin(np.array([1.45, 4.6, 7.0]))
    fair = tr.fair_dnb([1.45, 4.6, 7.0])
    assert fair["away"] == pytest.approx(f[2] / (f[0] + f[2])) and fair["draw"] == f[1]
    clv, per_dollar = tr.dnb_clv(6.0, "away", close)
    assert clv == pytest.approx(6.0 * fair["away"] - 1)
    assert per_dollar == pytest.approx(clv * (1 - f[1]))
    assert tr.dnb_clv(6.0, "draw", close) is None and tr.dnb_clv(6.0, "away", {}) is None


def test_a_draw_refunds_the_stake():
    t = {"market": "away", "odds": 6.0, "stake": 10.0}
    assert tr.settle_dnb(t, 1, 1) == {"status": "void", "score": "1-1", "push": True, "profit": 0.0}
    assert tr.settle_dnb(t, 0, 2)["profit"] == pytest.approx(50.0)
    assert tr.settle_dnb(t, 2, 0)["profit"] == -10.0
    assert tr.settle_dnb(t, None, None)["push"] is False  # no result: void, not a push


def test_sigma_dnb_is_fitted_on_earlier_matches_only():
    df = rows()
    fit_date = pd.Timestamp(df["date"].iloc[1000])
    fit = mc.league_fit(df, fit_date)
    early = df[pd.to_datetime(df["date"]) < fit_date]
    mk, pc = early[[f"mkt_{m}" for m in mc.H2H]], early[[f"p_{m}" for m in mc.H2H]]
    assert fit["sigma_dnb"] == pytest.approx(mc.sigma_dnb(fit["coef"], mk, pc), abs=1e-5)
    later = df.copy()
    later.loc[pd.to_datetime(later["date"]) >= fit_date, "mkt_home"] = 0.9
    assert mc.league_fit(later, fit_date)["sigma_dnb"] == fit["sigma_dnb"]


# ---------- the tracks ----------


def test_model_12_dnb_opens_on_the_raw_model_without_the_draw(tmp_path):
    written, data, ml = run(tmp_path, [card()], sigma_dnb=None)
    t = pdnb.load_ledger(tmp_path, "E0")[EDGE12_DNB]
    # Raw p 0.61 / 0.15: away 0.15 / 0.76 = 19.7% at the DNB price 7.5 x (1 - 1/5) = 6.0.
    assert t["market"] == "away" and t["odds"] == 6.0 and t["bet_type"] == "dnb"
    assert t["model_p"] == pytest.approx(0.15 / 0.76, abs=1e-4)
    assert t["edge"] == pytest.approx(0.15 / 0.76 * 6.0 - 1, abs=1e-4)
    assert t["strategy"] == "edge12_dnb" and t["variant_of"] == "edge12"
    assert t["threshold"] == tr.PAPER_EDGE and t["rule"] == "dnb_fixed_raw"
    assert t["entry_prices"] == {"home": 1.4, "draw": 5.0, "away": 7.5}
    assert t["clv_dk"] == pytest.approx(0.0, abs=0.2)  # its own price as the close so far
    # No sigma_dnb: Lean's DNB track opens nothing.
    assert LEAN_DNB not in pdnb.load_ledger(tmp_path, "E0")
    assert written == 2  # the 12% trade and its DNB one
    # The same build again opens nothing new.
    assert run(tmp_path, [card()], sigma_dnb=None)[0] == 0


def test_lean_dnb_needs_sigma_dnb_and_keeps_the_range_on_the_dnb_price(tmp_path):
    c = card(odds=LEAN_ODDS, p_bet=LEAN_P_BET)
    for sub in ("none", "with", "out"):
        (tmp_path / sub).mkdir()
    run(tmp_path / "none", [c], sigma_dnb=None)
    assert LEAN_DNB not in pdnb.load_ledger(tmp_path / "none", "E0")
    run(tmp_path / "with", [c])
    t = pdnb.load_ledger(tmp_path / "with", "E0")[LEAN_DNB]
    q = 0.45 / 0.73
    price = 2.6 * (1 - 1 / 3.4)
    assert t["market"] == "home" and t["odds"] == pytest.approx(price, abs=1e-4)
    assert t["z"] == pytest.approx((q - 1 / price) / SIGMA_DNB, abs=1e-3)
    assert t["sigma"] == SIGMA_DNB and t["p_bet"] == pytest.approx(q, abs=1e-4)
    assert t["odds_range"] == [1.5, 3.0] and t["strategy"] == "lean_dnb"
    # The pick's DNB price above 3.00: no trade (no other side is tried).
    out = card(
        odds={"home": 2.6, "draw": 3.4, "away": 4.6},
        p_bet={"home": 0.3, "draw": 0.27, "away": 0.43},
    )
    run(tmp_path / "out", [out])
    assert tr.dnb_odds(4.6, 3.4) > 3.0
    assert LEAN_DNB not in pdnb.load_ledger(tmp_path / "out", "E0")


def test_dnb_tracks_keep_the_parent_gates(tmp_path):
    stale = card()
    data = {
        "fixtures": [stale],
        "odds_source": src(NOW - pd.Timedelta(hours=4)),
        "portfolio": {},
        "match_blends": blends(),
    }
    paper.run(data, tmp_path, None, now=NOW)
    for c in (card(low_data=True), card(kickoff=NOW - pd.Timedelta(minutes=5)), card(odds={})):
        run(tmp_path, [c])
    assert pdnb.load_ledger(tmp_path, "E0") == {}


def test_dnb_trades_stay_out_of_the_record_and_settle_on_their_own(tmp_path):
    run(tmp_path, [card()])
    assert set(paper.load_ledger(tmp_path)) == {"E0|2627|Arsenal|Leeds"}  # no DNB rows
    assert (tmp_path / "paper_trades" / "E0_dnb_2627.jsonl").exists()
    # A draw: the 12% trade bet the draw and won; the DNB trade is refunded.
    written, data, ml = run(tmp_path, [], now=AFTER, res=results(1, 1))
    t = pdnb.load_ledger(tmp_path, "E0")[EDGE12_DNB]
    assert t["status"] == "void" and t["push"] is True and t["profit"] == 0.0
    assert t["clv_pinnacle"] is not None and t["clv_pinnacle_staked"] is not None
    live = ml["live"]
    assert [x["id"] for x in live["trades"]] == ["E0|2627|Arsenal|Leeds"]
    assert live["summary"]["trades"] == 1 and "live" not in data["portfolio"]
    assert [x["id"] for x in live["dnb_trades"]] == [EDGE12_DNB]
    sts = {s["key"]: s for s in live["strategies"]}
    dnb = sts["edge12_dnb"]
    assert dnb["variant_of"] == "edge12" and dnb["adds"] == "dnb" and dnb["filter"] is None
    assert dnb["defined_at"] == tr.DNB_FROM and dnb["trade_ids"] == [EDGE12_DNB]
    assert dnb["summary"]["void"] == 1 and dnb["calibration"]["pushes"] == 1
    assert dnb["rule"]["adds"] == "dnb" and "DraftKings' own draw no bet" in dnb["note"]
    assert dnb["compare"]["descriptive"] and "clv_dk" in dnb["compare"]["clv_per_dollar"]
    assert sts["lean_dnb"]["trade_ids"] == [] and sts["lean_dnb"]["rule"]["sigma"]["E0"]
    assert sts["edge12"]["trade_ids"] == ["E0|2627|Arsenal|Leeds"]  # the parent is unchanged
    # Nothing more to do on a rerun.
    assert run(tmp_path, [], now=AFTER, res=results(1, 1))[0] == 0


def test_an_away_win_pays_and_awaiting_results_lists_open_dnb_trades(tmp_path):
    run(tmp_path, [card()], sigma_dnb=None)
    waiting = paper.awaiting_results(tmp_path, ["E0"], AFTER)
    assert {(w["home"], w["away"]) for w in waiting} == {("Arsenal", "Leeds")}
    assert len(waiting) == 2  # the 12% trade and its DNB trade
    run(tmp_path, [], now=AFTER, res=results(0, 2), sigma_dnb=None)
    t = pdnb.load_ledger(tmp_path, "E0")[EDGE12_DNB]
    assert t["status"] == "won" and t["profit"] == pytest.approx(50.0) and t["score"] == "0-2"


def test_the_close_comes_from_the_odds_log_and_never_steps_back(tmp_path):
    led: dict = {}
    rules = {"edge12_dnb": tr.dnb_rule(tr.paper_threshold(None), "edge12_dnb")}
    ev = pdnb.update_ledger(led, [card()], src(), None, NOW, "E0", rules)
    assert [e["type"] for e in ev] == ["open"]

    def log(at, away):
        return pd.DataFrame(
            [
                {
                    "home": "Arsenal",
                    "away": "Leeds",
                    "market": "h2h",
                    "kickoff": KICKOFF,
                    "fetched_at": at,
                    "prices": {"home": 1.4, "draw": 5.0, "away": away},
                }
            ]
        )

    late = KICKOFF - pd.Timedelta(minutes=10)
    ev = pdnb.update_ledger(led, [], src(), None, NOW, "E0", rules, odds_log=log(late, 6.5))
    t = led[EDGE12_DNB]
    assert t["close_odds"] == pytest.approx(6.5 * 0.8)  # the price drifted: CLV below 0
    fair = tr.fair_dnb([1.4, 5.0, 6.5])
    assert t["clv_dk"] == pytest.approx(6.0 * fair["away"] - 1)
    earlier = KICKOFF - pd.Timedelta(hours=5)
    assert (
        pdnb.update_ledger(led, [], src(), None, NOW, "E0", rules, odds_log=log(earlier, 9.0)) == []
    )
