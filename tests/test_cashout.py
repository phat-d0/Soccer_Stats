import numpy as np
import pytest

from soccer_stats.lab import cashout as co


def row(fav="home", ft=(1, 2), close=(1.8, 3.6, 5.0), dk=(4.0, 3.2, 2.0), model=0.2, draw=0.3):
    return {
        "favourite": fav,
        "league": "E0",
        "ft": list(ft),
        "close": list(close),
        "dk_prices": None if dk is None else dict(zip(("home", "draw", "away"), dk, strict=True)),
        "model": model,
        "model_draw": draw,
    }


def test_position_takes_the_underdog_side_and_its_result():
    p = co.position(row(), "C")
    assert p["entry"] == 5.0 and p["live_odds"] == 2.0 and p["won"] == 1
    assert 0.4 < p["q"] < 0.5 and p["p_model"] == pytest.approx(0.5)
    away_fav = co.position(row(fav="away", ft=(2, 2)), "B")
    assert away_fav["entry"] == 1.8 and away_fav["live_odds"] == 4.0 and away_fav["won"] == 0
    assert co.position(row(dk=None), "C") is None


def test_fair_cash_out_matches_hold_in_expectation():
    p = co.position(row(), "C")
    pl = co.profits(p)
    assert pl["hold"] == 4.0
    assert pl["cash_0.00"] == pytest.approx(5.0 * p["q"] - 1)
    assert pl["cash_0.10"] == pytest.approx(5.0 * p["q"] * 0.9 - 1)
    # won with chance q: expected hold equals the fair cash-out
    exp_hold = p["q"] * 4.0 + (1 - p["q"]) * -1.0
    assert exp_hold == pytest.approx(pl["cash_0.00"])


def test_kelly_holds_only_with_an_edge_at_the_live_price():
    edge = co.position(row(model=0.2, draw=0.3), "C")  # p 0.5 at 2.0: no edge
    assert co.kelly_hold(edge) == 0.0
    big = co.position(row(model=0.1, draw=0.2), "C")  # p 0.7 at 2.0
    h = co.kelly_hold(big)
    f = 0.25 * (0.7 * 2.0 - 1) / (2.0 - 1)
    assert h == pytest.approx(min(1.0, f * co.BANKROLL / (5.0 / 2.0)))
    no_model = co.position(row(model=None), "C")
    assert co.kelly_hold(no_model) == 0.0


def test_summary_and_bands():
    pos = [co.position(row(ft=ft), d) for ft, d in [((1, 2), "C"), ((2, 2), "C"), ((3, 1), "B")]]
    res = co.analyse(pos)
    a = res["all"]
    assert a["matches"] == 3 and a["won"] == pytest.approx(1 / 3, abs=1e-4)
    assert a["hold"]["mean"] == pytest.approx(np.mean([4.0, -1.0, -1.0]), abs=1e-4)
    assert res["by_design"]["C"]["matches"] == 2 and res["by_design"]["B"]["matches"] == 1
    assert res["by_entry"]["4.0+"]["matches"] == 3 and res["by_entry"]["<3.0"] == {"matches": 0}
    assert sum(v["matches"] for v in res["by_live"].values()) == 3
    assert co.band_label(0.4, 0.55, pct=True) == "40%-55%" and co.band_label(0, 0.4, True) == "<40%"
