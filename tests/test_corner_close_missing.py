"""A corner trade's close must be a real close (lead-approved 23 Nov): a quote from the close
snapshot or logged after the entry. Without one at the trade's line the close stays empty
(`close_missing`), CLV is None, and the CLV summaries count it apart. Both books."""

import pandas as pd
import pytest
from test_corners_dk import DK_TID, NOW, _book, dk_quote
from test_corners_live import TID, K, card, corner_results, quote

from soccer_stats import paper
from soccer_stats import trades as tr

MISSING = paper.NO_CLOSE_AT_LINE
CLOSE_AT = K - pd.Timedelta(minutes=14)


def test_line_moved_leaves_both_books_without_a_close(tmp_path):
    # Entry at the look (4.5); the close snapshot quotes the team at 5.5 only.
    rows = [quote(), dk_quote()]
    rows += [
        quote(line=5.5, over=2.4, under=1.6, at=CLOSE_AT, snapshot="close"),
        dk_quote(line=5.5, over=2.5, under=1.5, at=CLOSE_AT, snapshot="close"),
    ]
    _book(tmp_path, [card()], rows[:2], NOW)
    led, _ = _book(tmp_path, [card()], rows, K - pd.Timedelta(minutes=10))
    for tid in (TID, DK_TID):
        t = led[tid]
        assert t["close_missing"] == MISSING and t.get("close_odds") is None
        assert t.get("clv_pinnacle") is None and t.get("clv_dk") is None
    led, _ = _book(tmp_path, [], rows, K + pd.Timedelta(hours=3), corner_results(7))
    for tid in (TID, DK_TID):
        assert led[tid]["status"] == "won" and led[tid]["close_missing"] == MISSING
    s = tr.summarize(pd.DataFrame([led[TID], led[DK_TID]]))
    assert s.get("clv_pinnacle") is None and s.get("clv_dk") is None  # no trade with a CLV


def test_a_real_close_clears_the_flag_on_both_books(tmp_path):
    _book(tmp_path, [card()], [quote(), dk_quote()], NOW)
    rows = [
        quote(),
        dk_quote(),
        quote(over=1.8, under=2.05, at=CLOSE_AT, snapshot="close"),
        dk_quote(over=1.9, under=1.9, at=CLOSE_AT, snapshot="close"),
    ]
    led, _ = _book(tmp_path, [card()], rows, K - pd.Timedelta(minutes=10))
    p, d = led[TID], led[DK_TID]
    assert p["close_missing"] is None and p["close_snapshot"] == "close"
    assert p["clv_pinnacle"] == pytest.approx(2.0 * tr.devig_pair(1.8, 2.05)[0] - 1)
    assert d["close_missing"] is None and d["clv_dk"] is not None and d["clv_pinnacle"] is not None


def test_an_entry_at_the_close_snapshot_is_its_own_close(tmp_path):
    at = K - pd.Timedelta(minutes=14)
    rows = [quote(at=at, snapshot="close"), dk_quote(at=at, snapshot="close")]
    led, _ = _book(tmp_path, [card()], rows, K - pd.Timedelta(minutes=12))
    for tid in (TID, DK_TID):
        assert led[tid].get("close_missing") is None and led[tid]["close_snapshot"] == "close"
    assert led[TID]["clv_pinnacle"] is not None and led[DK_TID]["clv_dk"] is not None


def test_clv_difference_counts_trades_without_a_close():
    def t(i, clv):
        base = {"id": f"m{i}|x", "home": f"H{i}", "away": "A", "kickoff": "2026-11-21T15:00"}
        return {**base, "status": "won", "clv_pinnacle": clv}

    trades = [t(1, -0.05), t(2, -0.06), t(3, None)]
    d = paper.clv_difference(trades, {"m1|x"}, "clv_pinnacle", n_boot=50)
    assert d["trades"] == 2 and d["no_value"] == 1 and d["left_out"] == 0
