"""The below-60% shadow filter on the corner tracks (owner, 23 Oct; pre-registered in
docs/totals.md "Shadow filter: below 60%"): a variant is a subset of its parent's own
trades, judged only on trades opened from `defined_at`. Synthetic trades only."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats import paper
from soccer_stats import trades as tr

DEFINED = pd.Timestamp(tr.SHADOW_DEFINED_AT)


def corner(
    i, p, *, push=0.0, book="pinnacle", status="won", hours=1.0, clv=0.0, dk=None, match=None
):
    """A corner trade opened `hours` after the filter's definition (negative = before)."""
    opened = DEFINED + pd.Timedelta(hours=hours)
    odds = 1.9
    profit = {"won": 10 * (odds - 1), "lost": -10.0, "void": 0.0}.get(status)
    t = {
        "id": f"E0|2627|H{match or i}|A{match or i}|corners|home|{4.5 + i}"
        + ("|dk" if book == "draftkings" else ""),
        "source": "live",
        "bet_type": "corners",
        "portfolio": "corners",
        "market": "team_corners",
        "league": "E0",
        "season": "2627",
        "home": f"H{match or i}",
        "away": f"A{match or i}",
        "kickoff": (opened + pd.Timedelta(hours=20)).isoformat(),
        "opened_at": opened.isoformat(),
        "odds": odds,
        "stake": 10.0,
        "model_p": p,
        "p_push": push,
        "edge": p * odds - 1,
        "status": status,
        "profit": profit,
        "push": status == "void",
        "clv_pinnacle": clv,
        "clv_dk": dk,
        "strategy": book,
        "bookmaker": book,
    }
    return t


def sections(trades):
    return {s["key"]: s for s in paper.corner_strategies(trades)}


def test_each_corner_track_is_followed_by_its_below60_shadow():
    out = paper.corner_strategies([corner(1, 0.55), corner(2, 0.7, book="draftkings")])
    keys = [s["key"] for s in out]
    assert keys == ["pinnacle", "pinnacle_below60", "draftkings", "draftkings_below60"]
    v = out[1]
    assert v["variant_of"] == "pinnacle" and v["label"] == "Model (f) 12% vs Pinnacle, below 60%"
    assert v["filter"] == {"field": "p_no_push", "max": 0.60, "note": tr.BELOW60_NOTE}
    assert v["defined_at"] == tr.SHADOW_DEFINED_AT == v["rule"]["defined_at"]
    assert v["rule"]["book"] == "pinnacle" and v["rule"]["threshold"] == tr.CORNERS_EDGE
    assert v["rule"]["filter"] == v["filter"] and v["rule"]["variant_of"] == "pinnacle"
    assert out[3]["label"] == "Model (f) 12% vs DraftKings, below 60%"
    # The parents are exactly what they were before variants existed.
    for k in ("pinnacle", "draftkings"):
        alone = paper.strategy_section(
            [corner(1, 0.55), corner(2, 0.7, book="draftkings")],
            k,
            tr.CORNER_STRATEGY_LABELS[k],
            tr.corners_rule(k),
            of=tr.corner_strategy_of,
        )
        assert sections([corner(1, 0.55), corner(2, 0.7, book="draftkings")])[k] == alone


def test_the_variant_is_exactly_the_parents_trades_below_60_given_no_push():
    trades = [
        corner(1, 0.55),  # kept
        corner(2, 0.60),  # exactly 0.60: dropped
        corner(3, 0.66, status="lost"),  # dropped
        corner(4, 0.45, push=0.2),  # 0.5625 given no push: kept
        corner(5, 0.50, push=0.2),  # 0.625 given no push: dropped
        corner(6, 0.40, book="draftkings"),  # the other track's
        {**corner(7, 0.30), "model_p": None},  # no chance: never kept
    ]
    s = sections(trades)
    pin = [t for t in trades if tr.corner_strategy_of(t) == "pinnacle"]
    expect = {t["id"] for t in pin if (tr.chance_no_push(t) or 1) < 0.60}
    assert set(s["pinnacle_below60"]["trade_ids"]) == expect == {pin[0]["id"], pin[3]["id"]}
    assert set(s["pinnacle_below60"]["trade_ids"]) <= set(s["pinnacle"]["trade_ids"])
    assert s["draftkings_below60"]["trade_ids"] == s["draftkings"]["trade_ids"]
    assert tr.chance_no_push(corner(4, 0.45, push=0.2)) == pytest.approx(0.5625)
    assert tr.chance_no_push({**corner(1, 0.5), "p_push": None}) == 0.5
    assert s["pinnacle_below60"]["summary"]["trades"] == 2
    assert s["pinnacle_below60"]["calibration"]["n"] == 2  # both settled


def test_since_defined_splits_on_when_the_trade_was_opened():
    trades = [
        corner(1, 0.55, hours=-5),  # before: shown, never judged
        corner(2, 0.70, hours=-1),
        corner(3, 0.55, hours=0),  # at defined_at: judged
        corner(4, 0.70, hours=3),
        corner(5, 0.50, hours=4, status="open"),
    ]
    sd = sections(trades)["pinnacle_below60"]["since_defined"]
    assert sd["defined_at"] == tr.SHADOW_DEFINED_AT
    assert sd["parent"]["trades"] == 3 and sd["variant"]["trades"] == 2
    assert sd["parent"]["summary"]["trades"] == 3 and sd["variant"]["summary"]["trades"] == 2
    assert sections(trades)["pinnacle_below60"]["summary"]["trades"] == 3  # every kept trade
    none = sections([corner(1, 0.55, hours=-5)])["pinnacle_below60"]["since_defined"]
    assert none["parent"]["trades"] == 0 and none["clv_diff"]["clv_pinnacle"]["diff"] is None


def test_the_clv_difference_uses_only_the_parents_settled_trades_in_the_window():
    trades = [
        corner(1, 0.55, clv=0.10, match=1),
        corner(2, 0.70, clv=-0.20, match=1),
        corner(3, 0.50, clv=0.00, match=2),
        corner(4, 0.65, clv=-0.06, match=3),
        corner(5, 0.52, clv=0.04, match=4),
        corner(6, 0.55, clv=0.90, hours=-2, match=5),  # before defined_at
        corner(7, 0.55, clv=0.90, status="open", match=6),  # close not final
        corner(8, 0.40, clv=-0.90, book="draftkings", match=7),  # the other track
    ]
    d = sections(trades)["pinnacle_below60"]["since_defined"]["clv_diff"]["clv_pinnacle"]
    all_clv = [0.10, -0.20, 0.00, -0.06, 0.04]
    kept = [0.10, 0.00, 0.04]
    assert d["trades"] == 5 and d["kept"] == 3 and d["matches"] == 4
    assert d["mean_all"] == pytest.approx(np.mean(all_clv))
    assert d["mean_kept"] == pytest.approx(np.mean(kept))
    assert d["diff"] == pytest.approx(np.mean(kept) - np.mean(all_clv))
    lo, hi = d["ci95"]
    assert lo <= d["diff"] <= hi and 0 < d["draws"] <= paper.N_BOOT_VARIANT
    # Resampling matches keeps a match's trades together: deterministic for the seed.
    again = paper.clv_difference(
        [t for t in trades if tr.corner_strategy_of(t) == "pinnacle"][:5],
        {trades[0]["id"], trades[2]["id"], trades[4]["id"]},
        "clv_pinnacle",
    )
    assert again["ci95"] == d["ci95"]
    dk = sections(trades)["draftkings_below60"]["since_defined"]["clv_diff"]
    assert dk["clv_pinnacle"]["trades"] == 1 and dk["clv_pinnacle"]["ci95"] is None
    assert dk["clv_dk"]["trades"] == 0  # the trade has no DraftKings close


def test_variants_are_generic_and_a_failing_one_costs_only_itself():
    spec = {
        "key": "edge12_short",
        "label": "Model 12%, price 3.00 or shorter",
        "filter": {"field": "odds", "max": 3.01, "note": "short prices"},
        "defined_at": tr.SHADOW_DEFINED_AT,
    }
    trades = [
        {**corner(1, 0.4), "strategy": "edge12", "odds": 2.5, "market": "home"},
        {**corner(2, 0.2), "strategy": "edge12", "odds": 6.0, "market": "away"},
    ]
    of = tr.strategy_of
    parent = paper.strategy_section(trades, "edge12", "Model 12%", {"threshold": 0.12}, of=of)
    out = paper.with_variants([parent], trades, {"edge12": [spec]}, of)
    assert [s["key"] for s in out] == ["edge12", "edge12_short"]
    assert out[1]["trade_ids"] == [trades[0]["id"]] and out[1]["variant_of"] == "edge12"
    bad = {**spec, "filter": None}
    out = paper.with_variants([parent], trades, {"edge12": [bad]}, of)
    assert out[0] == parent and out[1]["error"] and out[1]["variant_of"] == "edge12"


def test_the_difference_uses_the_same_trades_as_the_summaries_clv():
    trades = [
        corner(1, 0.55, clv=0.10, match=1),
        corner(2, 0.70, clv=-0.20, status="lost", match=1),
        corner(3, 0.50, clv=0.30, status="void", match=2),  # a push with a close: left out
        corner(4, 0.65, clv=0.25, status="void", match=3),  # a push in the dropped set
        corner(5, 0.52, clv=0.04, status="lost", match=4),
        corner(6, 0.58, clv=None, status="void", match=5),  # a moved match: left out too
        corner(7, 0.55, clv=0.90, hours=-2, match=6),  # before defined_at
    ]
    sd = sections(trades)["pinnacle_below60"]["since_defined"]
    d = sd["clv_diff"]["clv_pinnacle"]
    assert d["mean_all"] == pytest.approx(sd["parent"]["summary"]["clv_pinnacle"])
    assert d["mean_kept"] == pytest.approx(sd["variant"]["summary"]["clv_pinnacle"])
    assert d["trades"] == 3 and d["kept"] == 2 and d["left_out"] == 3
    assert d["diff"] == pytest.approx(np.mean([0.10, 0.04]) - np.mean([0.10, -0.20, 0.04]))
