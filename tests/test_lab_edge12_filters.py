"""The Model 12% filters backtest (lab/edge12_filters.py; docs/lab.md, owner 19 Oct)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats import backtest
from soccer_stats import trades as tr
from soccer_stats.lab import edge12_filters as ef

# The baseline's best edge is a long shot (away: 0.20 x 6.0 - 1 = +20%); home is a model
# favourite at a short price with a smaller edge (0.55 x 2.10 - 1 = +15.5%).
PROBS = {"home": 0.55, "draw": 0.25, "away": 0.20}
ODDS = {"home": 2.10, "draw": 3.40, "away": 6.00}


# ---------- the three rules ----------


def test_baseline_is_the_live_best_pick_on_home_draw_away():
    got = ef.pick("baseline", PROBS, ODDS)
    assert got == tr.best_pick(PROBS, ODDS)
    assert got["market"] == "away" and got["edge"] == pytest.approx(0.20)
    # Ties keep the earlier side (home, draw, away), as best_pick.
    tie = ef.pick("baseline", {"home": 0.4, "draw": 0.4, "away": 0.2}, dict.fromkeys(ODDS, 3.0))
    assert tie["market"] == "home"
    # 12% exactly qualifies (EPS); under it nothing does.
    assert ef.pick("baseline", {"home": 0.56, "draw": 0.2, "away": 0.2}, {"home": 2.0})["edge"] == (
        pytest.approx(0.12)
    )
    assert ef.pick("baseline", {"home": 0.555, "draw": 0.2, "away": 0.2}, {"home": 2.0}) is None
    # Totals never enter (the live DraftKings call is h2h only).
    assert (
        ef.pick("baseline", {**PROBS, "over25": 0.9}, {**ODDS, "over25": 2.0})["market"] == "away"
    )


def test_filters_choose_among_the_sides_that_pass_not_the_baselines_pick():
    a = ef.pick("A", PROBS, ODDS)
    b = ef.pick("B", PROBS, ODDS)
    assert a["market"] == "home" and a["edge"] == pytest.approx(0.155)
    assert b["market"] == "home" and b["odds"] == 2.10


def test_a_needs_the_model_strictly_above_half():
    assert ef.pick("A", {**PROBS, "home": 0.50}, {**ODDS, "home": 2.5}) is None
    assert ef.pick("A", {**PROBS, "home": 0.501}, {**ODDS, "home": 2.5})["market"] == "home"
    # A long shot with a big edge never qualifies under A.
    assert ef.pick("A", {"home": 0.3, "draw": 0.3, "away": 0.4}, {"away": 4.0}) is None


def test_b_takes_three_exactly_but_not_longer():
    p = {"home": 0.2, "draw": 0.4, "away": 0.4}
    assert ef.pick("B", p, {"home": 2.0, "draw": 3.00, "away": 3.01})["market"] == "draw"
    assert ef.pick("B", p, {"home": 2.0, "draw": 3.01, "away": 3.01}) is None
    assert ef.pick("baseline", p, {"home": 2.0, "draw": 3.01, "away": 3.01})["market"] == "draw"


def test_unknown_rule_and_missing_prices():
    with pytest.raises(ValueError):
        ef.pick("C", PROBS, ODDS)
    assert ef.pick("baseline", PROBS, {"home": float("nan")}) is None
    assert ef.pick("A", {"home": None}, ODDS) is None


# ---------- bets on Pinnacle's early prices ----------


def _target(n=6, season="2324"):
    rows = []
    for i in range(n):
        rows.append(
            {
                "date": pd.Timestamp("2024-01-06") + pd.Timedelta(days=7 * i),
                "season": season,
                "home": f"H{i}",
                "away": f"A{i}",
                "home_goals": i % 3,
                "away_goals": 1,
                **{f"p_{m}": v for m, v in PROBS.items()},
                **{f"odds_{m}": v for m, v in ODDS.items()},
                "close_home": 2.0,
                "close_draw": 3.5,
                "close_away": 6.5,
            }
        )
    return pd.DataFrame(rows)


def test_one_bet_per_match_with_dollar_profit_and_clv():
    t = _target()
    for rule in ef.RULES:
        b = ef.bets(t, "E0", rule)
        assert len(b) == len(t) and b[["home", "away"]].drop_duplicates().shape[0] == len(b)
    base, fav = ef.bets(t, "E0", "baseline"), ef.bets(t, "E0", "A")
    assert set(base["market"]) == {"away"} and set(fav["market"]) == {"home"}
    # Home wins when home_goals 2 (i = 2, 5): +1.10 each; the rest -1.
    assert fav["profit"].sum() == pytest.approx(2 * 1.10 - 4)
    close = {"home": 2.0, "draw": 3.5, "away": 6.5}
    assert fav["clv"].iloc[0] == pytest.approx(tr.clv(2.10, "home", close))
    assert fav["implied"].iloc[0] == pytest.approx(1 / 2.10)
    # No full close: still a bet, but no CLV.
    t.loc[0, "close_draw"] = np.nan
    assert ef.bets(t, "E0", "A")["clv"].isna().sum() == 1


def test_metrics_report_win_rate_model_implied_and_wider_decision_range():
    b = ef.bets(_target(40), "E0", "A")
    m = ef.metrics(b)
    assert m["bets"] == 40 and m["model_p"] == pytest.approx(0.55)
    assert m["implied"] == pytest.approx(1 / 2.10)
    lo95, hi95 = m["roi_ci95"]
    assert lo95 <= m["roi"] <= hi95
    c95, c975 = m["clv_ci95"], m["clv_ci975"]
    assert c975[0] <= c95[0] and c975[1] >= c95[1]
    assert ef.metrics(b.iloc[:0]) == {"bets": 0}


def _pins(seasons, cover=1.0):
    rows = []
    for s in seasons:
        for i in range(20):
            priced = i < cover * 20
            rows.append(
                {
                    "season": s,
                    "date": pd.Timestamp(f"20{s[:2]}-09-01") + pd.Timedelta(days=7 * i),
                    "home": f"H{i}",
                    "away": f"A{i}",
                    **{f"odds_{m}": (ODDS[m] if priced else np.nan) for m in ODDS},
                    "close_home": 2.0 if priced else np.nan,
                    "close_draw": 3.5 if priced else np.nan,
                    "close_away": 6.5 if priced else np.nan,
                }
            )
    return pd.DataFrame(rows)


def _preds(pins):
    p = pins[["season", "date", "home", "away"]].copy()
    p["home_goals"], p["away_goals"] = 2, 0
    for m, v in PROBS.items():
        p[f"p_{m}"] = v
    return p


def test_seasons_scored_need_coverage_and_stop_at_2024_25():
    pins = pd.concat([_pins(["2223", "2425", "2526"]), _pins(["2324"], cover=0.85)])
    info, rb = ef.league_bets(_preds(pins), pins, "E0")
    assert info["seasons_scored"] == ["2022/23", "2024/25"]
    assert set(info["seasons_left_out"]) == {"2023/24", "2025/26"}
    for rule in ef.RULES:
        assert set(rb[rule]["season"]) == {"2223", "2425"}
        assert len(rb[rule]) == 40
    assert ef.season_ok("2425") and not ef.season_ok("2526") and not ef.season_ok("x")


def test_model_chances_come_from_earlier_matches_only(league):
    """walk_forward (the source of every chance) fits each window on earlier matches."""
    df, _ = league
    seen = []

    class Recording(backtest.DixonColes):
        def fit(self, matches, as_of=None):
            seen.append((matches["date"].max(), as_of))
            return super().fit(matches, as_of=as_of)

    start = df["date"].max() - pd.Timedelta(days=90)
    preds = backtest.walk_forward(df, start=start, refit_every="30D", model_factory=Recording)
    assert len(preds) and seen and all(last < as_of for last, as_of in seen)


# ---------- the decision rule ----------


def _pooled(a_bets, a_clv, a_rng, b_bets, b_clv, b_rng):
    return {
        "baseline": {"bets": 5000, "clv": -0.04, "clv_ci975": [-0.05, -0.03]},
        "A": {"bets": a_bets, "clv": a_clv, "clv_ci975": a_rng},
        "B": {"bets": b_bets, "clv": b_clv, "clv_ci975": b_rng},
    }


def test_decision_neither_passes():
    d = ef.decide(_pooled(800, -0.01, [-0.02, 0.0], 900, 0.004, [-0.001, 0.009]))
    assert d["keep"] is None and "No live change" in d["verdict"]
    assert not d["checks"]["A"]["passes"] and not d["checks"]["B"]["passes"]


def test_decision_needs_300_bets_and_a_range_above_zero():
    d = ef.decide(_pooled(299, 0.03, [0.01, 0.05], 300, 0.01, [0.001, 0.02]))
    assert not d["checks"]["A"]["passes"] and d["checks"]["B"]["passes"]
    assert d["keep"] == "B"
    # A lower bound of exactly 0 isn't above 0.
    assert ef.decide(_pooled(400, 0.01, [0.0, 0.02], 0, None, None))["keep"] is None


def test_decision_both_pass_keeps_the_higher_pooled_clv():
    d = ef.decide(_pooled(400, 0.012, [0.002, 0.02], 700, 0.008, [0.001, 0.015]))
    assert d["keep"] == "A"
    d = ef.decide(_pooled(400, 0.006, [0.002, 0.01], 700, 0.008, [0.001, 0.015]))
    assert d["keep"] == "B"


def test_build_pools_leagues_and_decides():
    pins = _pins(["2223", "2324"])
    infos, rb = {}, {}
    for code in ("E0", "SP1"):
        infos[code], rb[code] = ef.league_bets(_preds(pins), pins, code)
    out = ef.build(infos, rb, names={"E0": "Premier League"})
    assert out["pooled"]["A"]["bets"] == 80 and out["leagues"]["E0"]["A"]["bets"] == 40
    assert out["leagues"]["E0"]["name"] == "Premier League"
    assert [s["label"] for s in out["by_season"]["B"]] == ["2022/23", "2023/24"]
    # Every home bet won (2-0): CLV is the price against the close, not the result.
    assert out["pooled"]["A"]["roi"] == pytest.approx(1.10)
    assert out["decision"]["keep"] in (None, "A", "B")


# ---------- the DraftKings check (descriptive) ----------


def _cand(look, probs, odds, **kw):
    return {
        "season": "2526",
        "home": "X",
        "away": "Y",
        "kickoff": pd.Timestamp("2025-09-13 14:00", tz="UTC"),
        "look": look,
        "home_n": 20,
        "away_n": 20,
        **{f"p_{m}": v for m, v in probs.items()},
        **{f"odds_{m}": v for m, v in odds.items()},
        "close": {"home": 2.0, "draw": 3.5, "away": 6.5},
        "pinnacle_close": {"home": 2.05, "draw": 3.6, "away": 6.8},
        "home_goals": 1,
        "away_goals": 0,
        "void": False,
        **kw,
    }


def test_dk_first_qualifying_look_under_each_rule():
    c = pd.DataFrame(
        [
            # 48 h: only the long shot qualifies.
            _cand("48h", PROBS, {**ODDS, "home": 1.9}),
            # 3 h: home now qualifies too.
            _cand("3h", PROBS, ODDS),
        ]
    )
    base = ef.dk_bets(c, "baseline")
    fav = ef.dk_bets(c, "A")
    assert list(base["look"]) == ["48h"] and base["market"].iloc[0] == "away"
    assert list(fav["look"]) == ["3h"] and fav["market"].iloc[0] == "home"
    assert fav["won"].iloc[0] and fav["profit"].iloc[0] == pytest.approx(1.10)
    assert fav["clv"].iloc[0] == pytest.approx(tr.clv(2.10, "home", c["close"].iloc[0]))
    assert fav["clv_pinnacle"].iloc[0] == pytest.approx(
        tr.clv(2.10, "home", c["pinnacle_close"].iloc[0])
    )
    rep = ef.dk_check(c)
    assert rep["matches"] == 1 and rep["A"]["bets"] == 1
    assert "clv" in rep["A"]["clv_pinnacle"]


def test_dk_skips_voids_thin_teams_and_other_seasons():
    assert ef.dk_bets(pd.DataFrame([_cand("3h", PROBS, ODDS, void=True)]), "A").empty
    assert ef.dk_bets(pd.DataFrame([_cand("3h", PROBS, ODDS, home_n=5)]), "A").empty
    assert ef.dk_bets(pd.DataFrame([_cand("3h", PROBS, ODDS, season="2425")]), "A").empty
    assert ef.dk_bets(pd.DataFrame(), "A").empty


def test_prefetch_retries_network_errors_then_gives_up(monkeypatch):
    import requests

    from soccer_stats import cli, data

    calls = []

    def flaky(code, year):
        calls.append(year)
        if len(calls) < 3:
            raise requests.ReadTimeout("slow")

    monkeypatch.setattr(data, "download", flaky)
    cli._prefetch_seasons("F1", [2019], tries=4, wait=0)
    assert calls == [2019, 2019, 2019]

    def down(code, year):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(data, "download", down)
    with pytest.raises(requests.ConnectionError):
        cli._prefetch_seasons("F1", [2019], tries=2, wait=0)
