import re
from pathlib import Path

import pandas as pd
import pytest

from soccer_stats import trades as tr


def _cand(**kw):
    row = {
        "home": "A",
        "away": "B",
        "p_home": 0.5,
        "p_draw": 0.25,
        "p_away": 0.25,
        "p_over25": 0.5,
        "p_under25": 0.5,
        "odds_home": 1.9,
        "odds_draw": 3.5,
        "odds_away": 4.0,
        "odds_over25": 1.9,
        "odds_under25": 1.9,
        "home_n": 20,
        "away_n": 20,
    }
    row.update(kw)
    return row


def test_edge_of_exactly_the_threshold_qualifies():
    # 0.4 x 2.8 - 1 = 0.12 (in floating point a hair under)
    pick = tr.best_pick({"home": 0.4}, {"home": 2.8}, threshold=0.12)
    assert pick is not None and pick["market"] == "home"
    assert tr.best_pick({"home": 0.4}, {"home": 2.79}, threshold=0.12) is None


def test_one_trade_per_match_on_the_best_edge():
    # draw: 0.25 x 5.0 - 1 = +25%; away: 0.25 x 4.6 - 1 = +15%: both qualify
    df = pd.DataFrame([_cand(odds_draw=5.0, odds_away=4.6)])
    out = tr.select_trades(df, threshold=0.12)
    assert len(out) == 1
    assert out.iloc[0]["market"] == "draw"
    assert out.iloc[0]["edge"] == pytest.approx(0.25)


def test_thin_data_matches_are_skipped():
    df = pd.DataFrame([_cand(odds_draw=5.0, home_n=5), _cand(home="C", odds_draw=5.0)])
    out = tr.select_trades(df, threshold=0.12)
    assert list(out["home"]) == ["C"]


def test_odds_cap_and_missing_prices():
    df = pd.DataFrame([_cand(odds_draw=8.0, odds_away=None)])
    assert tr.select_trades(df).iloc[0]["market"] == "draw"
    assert tr.select_trades(df, max_odds=6.0).empty


def _trade(market="home", odds=2.5):
    return {"market": market, "odds": odds, "stake": 10.0}


def test_settle_win_loss_void():
    assert tr.settle(_trade(), 2, 1) == {"status": "won", "score": "2-1", "profit": 15.0}
    assert tr.settle(_trade(), 0, 0) == {"status": "lost", "score": "0-0", "profit": -10.0}
    assert tr.settle(_trade("under25"), 1, 1)["status"] == "won"
    assert tr.settle(_trade(), None, None) == {"status": "void", "score": None, "profit": 0.0}
    assert tr.settle(_trade(), 2, 1, void=True)["profit"] == 0.0


def test_clv_needs_the_whole_market():
    close = {"home": 2.0, "draw": 3.5, "away": 4.0}
    v = tr.clv(2.5, "home", close)
    assert v is not None and v > 0  # 2.5 is a better price than the 2.0 close
    assert tr.clv(2.5, "home", {"home": 2.0}) is None
    assert tr.clv(1.9, "over25", {"over25": 1.9, "under25": 1.9}) == pytest.approx(-0.05)


def test_summary_and_drawdown():
    df = pd.DataFrame(
        {
            "kickoff": pd.date_range("2025-08-01", periods=4, freq="7D", tz="UTC"),
            "status": ["won", "lost", "lost", "open"],
            "odds": [3.0, 2.0, 2.0, 2.0],
            "stake": 10.0,
            "edge": [0.15, 0.13, 0.12, 0.2],
            "profit": [20.0, -10.0, -10.0, None],
            "clv_dk": [0.05, -0.02, 0.01, None],
        }
    )
    s = tr.summarize(df)
    assert s["trades"] == 4 and s["open"] == 1 and s["settled"] == 3
    assert s["profit"] == 0.0 and s["roi"] == 0.0
    assert s["max_drawdown"] == 20.0
    assert s["beat_close_dk"] == pytest.approx(2 / 3)
    assert s["breakeven"] == pytest.approx((1 / 3 + 0.5 + 0.5) / 3)
    rep = tr.report(df)
    assert {r["group"] for r in rep["breakdowns"]["edge_bucket"]} == {"12–15%", "15–20%", "20%+"}


def test_app_filter_presets_match():
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text()
    steps = re.search(r"const EDGE_STEPS = \[([^\]]*)\]", js).group(1)
    assert tuple(float(x) for x in steps.split(",")) == tr.FILTER_PRESETS
    assert f"const DEFAULT_EDGE = {tr.DEFAULT_FILTER}" in js
    assert f"const PAPER_EDGE = {tr.PAPER_EDGE}" in js
