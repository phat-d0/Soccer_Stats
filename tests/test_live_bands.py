"""Live strategy sections (owner, 24 Oct): finer calibration bins (merged only under 3
trades, `few` under 10) and odds bands on the paper trades, the backtests' bands and
fields. Synthetic trades only."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats import bands as bd
from soccer_stats import calibration as cal
from soccer_stats import paper
from soccer_stats import trades as tr


def trade(i, odds, status="won", clv=0.0, p=0.5, match=None, league="E0", **kw):
    m = match if match is not None else i
    profit = {"won": 10 * (odds - 1), "lost": -10.0, "void": 0.0}.get(status)
    return {
        "id": f"{league}|2627|H{m}|A{m}|{i}",
        "league": league,
        "season": "2627",
        "home": f"H{m}",
        "away": f"A{m}",
        "kickoff": f"2026-10-{m % 28 + 1:02d}T15:00:00+00:00",
        "opened_at": "2026-10-01T10:00:00+00:00",
        "market": "home",
        "odds": odds,
        "stake": 10.0,
        "model_p": p,
        "edge": p * odds - 1,
        "status": status,
        "profit": profit,
        "clv_dk": clv,
        "strategy": "edge12",
        **kw,
    }


def test_live_bins_merge_only_under_three_and_mark_few_under_ten():
    # 15 settled trades spread over the bins: the backtests' 20-row rule made one bin.
    ps = [0.15] * 4 + [0.25] * 2 + [0.35] * 1 + [0.45] * 8
    trades = [trade(i, 2.0, "won" if i % 2 else "lost", p=p) for i, p in enumerate(ps)]
    c = paper.trade_calibration(trades)
    assert [(b["lo"], b["hi"], b["n"]) for b in c["bins"]] == [
        (0.1, 0.2, 4),
        (0.2, 0.4, 3),  # 2 rows join the next bin: 2 + 1
        (0.4, 0.5, 8),
    ]
    assert all(b["few"] for b in c["bins"])
    assert len(cal.reliability(ps, [i % 2 for i in range(15)])["bins"]) == 1
    many = [trade(i, 2.0, "won" if i % 2 else "lost", p=0.45) for i in range(12)]
    assert "few" not in paper.trade_calibration(many)["bins"][0]
    assert c["by_league"]["E0"]["bins"] == c["bins"]


def test_live_odds_bands_keep_every_band_and_split_settled_from_closed():
    trades = [
        trade(1, 1.4, "won", clv=0.02),
        trade(2, 1.8, "lost", clv=-0.04),
        trade(3, 1.9, "won", clv=0.06),
        trade(4, 1.95, "open", clv=0.10),  # started, awaiting: CLV only
        trade(5, 1.7, "open", clv=None),  # no close yet: counted only
        trade(6, 1.6, "void", clv=0.50),  # a moved match: never in CLV
        trade(7, 1.6, "open", clv=0.40, match=27),  # kicks off on the 28th: close not final
    ]
    out = paper.live_odds_bands(trades, now=pd.Timestamp("2026-10-20T12:00:00Z"))
    assert [(b["lo"], b["hi"]) for b in out] == bd.ODDS_BANDS
    assert [b["bets"] for b in out] == [1, 6, 0, 0, 0]
    assert out[2] == {"lo": 2.0, "hi": 3.0, "bets": 0, "share": 0.0}
    b = out[1]
    assert b["share"] == pytest.approx(6 / 7, abs=1e-4)
    assert b["settled"] == 2 and b["wins"] == 1 and b["win_rate"] == 0.5
    assert b["breakeven"] == pytest.approx((1 / 1.8 + 1 / 1.9) / 2, abs=1e-4)
    assert b["roi"] == pytest.approx((-1 + 0.9) / 2, abs=1e-4)
    assert b["clv_trades"] == 3 and b["clv"] == pytest.approx(np.mean([-0.04, 0.06, 0.10]))
    lo, hi = b["clv_ci95"]
    assert lo <= b["clv"] <= hi
    assert out[0]["roi_ci95"] is None  # one match: no range
    assert sum(x["share"] for x in out) == pytest.approx(1, abs=1e-3)
    assert [x["bets"] for x in paper.live_odds_bands([])] == [0] * 5
    later = paper.live_odds_bands(trades, now=pd.Timestamp("2026-10-29T12:00:00Z"))
    assert later[1]["clv_trades"] == 4  # once it has kicked off, its close counts


def test_ranges_resample_matches_so_a_matchs_corner_lines_move_together():
    same = [trade(i, 1.9, "won", clv=0.1, match=1) for i in range(3)]
    other = [trade(9, 1.9, "lost", clv=-0.1, match=2)]
    b = paper.live_odds_bands(same + other)[1]
    assert b["roi_ci95"] == bd.boot_mean([0.9, 0.9, 0.9, -1], ["m1"] * 3 + ["m2"])


def test_every_live_section_carries_bands_with_its_tracks_clv():
    m = [trade(1, 2.5, "won", clv=0.05), trade(2, 4.0, "lost", clv=-0.2, league="SP1")]
    sec = paper.strategy_section(m, "edge12", "Model 12%", {"p_source": "model"})
    assert sec["odds_bands"]["clv_field"] == "clv_dk"
    assert sec["by_odds_band"][2]["clv"] == 0.05
    assert sec["by_league"]["SP1"]["by_odds_band"][3]["bets"] == 1
    assert sum(b["bets"] for b in sec["by_league"]["E0"]["by_odds_band"]) == 1
    corners = [
        trade(
            i,
            1.9,
            clv=0.01 * i,
            clv_pinnacle=0.03,
            bet_type="corners",
            p=0.55,
            p_push=0.0,
            strategy="pinnacle",
            bookmaker="pinnacle",
            market="team_corners",
        )
        for i in range(3)
    ]
    secs = {s["key"]: s for s in paper.corner_strategies(corners)}
    for k in ("pinnacle", "pinnacle_below60", "draftkings", "draftkings_below60"):
        assert secs[k]["odds_bands"]["clv_field"] == "clv_pinnacle"
        assert len(secs[k]["by_odds_band"]) == 5
    assert secs["pinnacle"]["by_odds_band"][1]["clv"] == pytest.approx(0.03)
    assert secs["pinnacle_below60"]["by_odds_band"][1]["bets"] == 3
    assert paper.live_clv_field(tr.corners_rule("draftkings")) == "clv_pinnacle"
    assert paper.live_clv_field(None) == "clv_dk"
