"""Model equations and calibration for Portfolio (calibration.py, equations.py; owner,
22 Oct): the helper, the backtests' calibration blocks, the live trades' and every logged
corner line's. Synthetic data only: nothing reaches the network."""

import json

import numpy as np
import pandas as pd
import pytest
from test_cup_backtest import frames
from test_lean_backtest import synth

from soccer_stats import calibration as cal
from soccer_stats import corners_live as cl
from soccer_stats import cup_backtest as cb
from soccer_stats import edge12_backtest as e12b
from soccer_stats import equations as eq
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import paper, publish
from soccer_stats.models import DixonColes


def test_wilson_matches_the_textbook_range():
    lo, hi = cal.wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4) and hi == pytest.approx(0.9433, abs=1e-4)
    assert cal.wilson(0, 0) == (None, None)
    lo, hi = cal.wilson(0, 25)
    assert lo == 0.0 and 0 < hi < 0.2


def test_bins_drop_empty_ones_and_merge_small_ones_into_a_neighbour():
    pred = [0.05] * 5 + [0.15] * 30 + [0.45] * 40 + [0.85] * 3
    won = [0] * 5 + [1] * 6 + [0] * 24 + [1] * 20 + [0] * 20 + [1] * 3
    b = cal.bins(pred, won, implied=[p * 0.9 for p in pred])
    # 0-10% (5 rows) joins 10-20%; 80-90% (3 rows, the last) joins the one before.
    assert [(x["lo"], x["hi"], x["n"]) for x in b] == [(0.0, 0.2, 35), (0.4, 1.0, 43)]
    assert b[0]["wins"] == 6 and b[0]["realized"] == pytest.approx(6 / 35, abs=1e-4)
    assert b[0]["predicted"] == pytest.approx((5 * 0.05 + 30 * 0.15) / 35, abs=1e-4)
    assert b[0]["implied"] == pytest.approx(0.9 * b[0]["predicted"], abs=1e-4)
    assert b[0]["realized_lo"] < b[0]["realized"] < b[0]["realized_hi"]
    assert sum(x["n"] for x in cal.bins(np.linspace(0, 1, 500), np.ones(500))) == 500
    assert [x["hi"] for x in cal.bins(np.linspace(0, 1, 500), np.ones(500))][-1] == 1.0


def test_recalibration_slope_is_one_when_calibrated_and_below_one_when_over_confident():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.03, 0.9, 30000)
    y = rng.random(len(p)) < p
    good = cal.reliability(p, y)
    lo, hi = good["slope_ci95"]
    assert lo < 1 < hi and good["intercept_ci95"][0] < 0 < good["intercept_ci95"][1]
    assert good["n"] == len(p) and good["realized"] == pytest.approx(y.mean(), abs=1e-4)
    assert good["brier"] == pytest.approx(np.mean((p - y) ** 2), abs=1e-5)
    # Chances pushed away from 50% (too spread out) give a slope below 1.
    logit = np.log(p / (1 - p))
    wide = 1 / (1 + np.exp(-1.6 * logit))
    bad = cal.reliability(wide, y)
    assert bad["slope_ci95"][1] < 1 and bad["slope"] == pytest.approx(1 / 1.6, abs=0.05)
    assert cal.reliability([], [])["n"] == 0
    few = cal.reliability([0.5, 0.4], [1, 0])
    assert few["n"] == 2 and few["slope"] is None and few["bins"]


def test_clustered_ranges_are_wider_when_a_match_repeats_its_rows():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.1, 0.8, 2000)
    y = (rng.random(len(p)) < p).astype(float)
    alone = cal.recalibration(p, y, groups=np.arange(len(p)))
    twice = cal.recalibration(
        np.r_[p, p], np.r_[y, y], groups=np.r_[np.arange(len(p)), np.arange(len(p))]
    )
    naive = cal.recalibration(np.r_[p, p], np.r_[y, y])
    width = lambda r: r["slope_ci95"][1] - r["slope_ci95"][0]  # noqa: E731
    assert twice["slope"] == pytest.approx(alone["slope"], abs=1e-3)
    assert width(twice) == pytest.approx(width(alone), rel=0.05)
    assert width(twice) > width(naive) * 1.3  # a naive range would count each match twice


def test_lean_backtest_scores_every_outcome_of_every_blended_match_and_its_bets():
    preds, pins = synth()
    rep, b = lb.league_report("E0", preds, pins)
    c = rep["calibration"]
    series = c["all_outcomes"]
    assert set(series) == {"model", "blend", "price"}
    n = {k: v["n"] for k, v in series.items()}
    assert len(set(n.values())) == 1 and n["model"] % 3 == 0  # three outcomes a match
    long = rep[lb.OUTCOMES]
    assert len(long) == n["model"] and long.groupby("group")["won"].sum().eq(1).all()
    assert series["model"]["implied"] == pytest.approx(long["price"].mean(), abs=1e-4)
    assert "implied" not in series["price"]
    assert c["bets"]["n"] == len(b) and c["bets"]["predicted_from"] == "p_bet"
    assert c["bets"]["implied"] == pytest.approx((1 / b["odds"]).mean(), abs=1e-4)
    out = lb.build({"E0": rep, "SP1": {"error": "down"}}, [b, b.iloc[:0]], from_season=2021)
    assert lb.OUTCOMES not in out["leagues"]["E0"] and lb.OUTCOMES in rep  # never saved
    assert out["all"]["calibration"]["all_outcomes"]["blend"]["n"] == n["blend"]
    assert out["all"]["calibration"]["bets"]["n"] == len(b)
    json.dumps(out)


def test_the_pooled_view_adds_every_early_league_and_keeps_matches_apart():
    preds, pins = synth()
    a, ba = lb.league_report("E0", preds, pins)
    b, bb = lb.league_report("SP1", preds, pins)
    out = lb.build({"E0": a, "SP1": b}, [ba, bb], from_season=2021)
    pooled = out["all"]["calibration"]["all_outcomes"]["model"]
    assert pooled["n"] == 2 * a["calibration"]["all_outcomes"]["model"]["n"]
    # Same synthetic matches in both leagues: clustering keeps them as separate matches.
    assert a[lb.OUTCOMES]["group"].str.startswith("E0|").all()


def test_edge12_backtest_scores_model_and_price_on_every_candidate():
    preds, pins = synth()
    rep, b = e12b.league_report("E0", preds, pins)
    c = rep["calibration"]
    assert set(c["all_outcomes"]) == {"model", "price"}
    target = lb.target_rows(lb.with_pinnacle(preds, pins))
    included = {x["season"] for x in rep["coverage"] if x["included"]}
    rows = target[target["season"].astype(str).isin(included)]
    assert c["all_outcomes"]["model"]["n"] == 3 * len(rows)
    assert c["bets"]["n"] == len(b) and c["bets"]["predicted_from"] == "model_p"
    assert c["bets"]["predicted"] == pytest.approx(b["model_p"].mean(), abs=1e-4)
    out = e12b.build({"E0": rep}, {"E0": b}, from_season=2021)
    assert lb.OUTCOMES not in out["leagues"]["E0"]
    assert out["all"]["calibration"]["all_outcomes"]["price"]["n"] == 3 * len(rows)
    json.dumps(out)


def test_the_cup_at_the_close_uses_draftkings_fair_close_as_the_price(tmp_path):
    preds, pin, dk = frames(tmp_path)
    rep, b = e12b.cup_report(preds, pin, dk)
    t = cb.target_rows(cb.joined(preds, pin, dk))
    price = rep["calibration"]["all_outcomes"]["price"]
    assert price["n"] == 3 * len(t)
    assert price["predicted"] == pytest.approx(
        t[["mkt_home", "mkt_draw", "mkt_away"]].stack().mean(), abs=1e-4
    )
    lean, _ = cb.lean_report(*frames(tmp_path, n=700))
    assert set(lean["calibration"]["all_outcomes"]) == {"model", "blend", "price"}
    assert lean["calibration"]["all_outcomes"]["blend"]["n"] == 3 * lean["scored"]


def trade(i, status, p, odds, league="E0", **kw):
    profit = {"won": 10 * (odds - 1), "lost": -10.0}.get(status, 0.0 if status == "void" else None)
    return {
        "id": f"t{i}",
        "source": "live",
        "kickoff": "2026-10-11T14:00:00+00:00",
        "opened_at": "2026-10-10T14:00:00+00:00",
        "home": f"H{i}",
        "away": f"A{i}",
        "market": "home",
        "stake": 10.0,
        "status": status,
        "profit": profit,
        "model_p": p,
        "odds": odds,
        "edge": p * odds - 1,
        "league": league,
        **kw,
    }


def test_live_trades_calibrate_settled_ones_and_count_pushes_apart():
    trades = [
        trade(1, "won", 0.6, 1.8),
        trade(2, "lost", 0.4, 2.6, league="SP1"),
        trade(3, "open", 0.5, 2.0),
        trade(4, "void", 0.5, 1.9, market="team_corners", push=True, p_push=0.2),
        trade(5, "won", 0.4, 2.2, market="team_corners", p_push=0.2),
        trade(6, "void", 0.5, 2.0),  # a moved match
    ]
    c = paper.trade_calibration(trades)
    assert c["n"] == 3 and c["wins"] == 2 and c["pushes"] == 1 and c["voids"] == 1
    # The corner chance is taken given no push: 0.4 / (1 - 0.2).
    assert c["predicted"] == pytest.approx((0.6 + 0.4 + 0.5) / 3, abs=1e-4)
    assert c["implied"] == pytest.approx((1 / 1.8 + 1 / 2.6 + 1 / 2.2) / 3, abs=1e-4)
    assert c["by_league"]["SP1"]["n"] == 1 and c["by_league"]["E0"]["n"] == 2
    assert paper.trade_calibration([])["n"] == 0


def test_every_strategy_section_carries_its_own_calibration():
    trades = [
        trade(1, "won", 0.6, 1.8, strategy="lean"),
        trade(2, "lost", 0.3, 4.0),
        trade(3, "lost", 0.35, 3.5),
    ]
    lean = paper.strategy_section(trades, "lean", "Lean", None)
    edge = paper.strategy_section(trades, "edge12", "12%", None)
    assert lean["calibration"]["n"] == 1 and edge["calibration"]["n"] == 2
    assert edge["calibration"]["realized"] == 0


K = pd.Timestamp("2026-10-11 14:00", tz="UTC")


def quote(line, p_model, fair, book="pinnacle", side="home", got=None, **kw):
    got = K - pd.Timedelta(hours=1) if got is None else got
    return {
        "market": "team_corners",
        "league": "E0",
        "home": "Arsenal",
        "away": "Leeds",
        "kickoff": K.isoformat(),
        "book": book,
        "side": side,
        "line": line,
        "fair_over": fair,
        "p_model_over": p_model,
        "downloaded_at": got.isoformat(),
        **kw,
    }


def corner_results(hc=6, ac=3):
    return pd.DataFrame(
        {
            "league": ["E0"],
            "season": ["2627"],
            "date": [pd.Timestamp("2026-10-11")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_corners": [hc],
            "away_corners": [ac],
        }
    )


def test_every_logged_corner_line_is_scored_on_its_newest_pre_kickoff_quote():
    rows = [
        quote(4.5, 0.50, 0.45, got=K - pd.Timedelta(hours=24)),  # older look
        quote(4.5, 0.60, 0.55),  # the close: newest before kickoff
        quote(4.5, 0.99, 0.99, got=K + pd.Timedelta(minutes=5)),  # after kickoff: never
        quote(3.5, 0.40, 0.42, side="away"),  # 3 corners: under
        quote(5.0, 0.55, 0.52),  # whole line, 6 corners: counted apart, no push
        quote(6.0, 0.45, 0.44),  # whole line, 6 corners: a push
        quote(4.5, 0.62, None, book="draftkings"),  # one-sided: no fair price
    ]
    c = cl.quote_calibration(rows, corner_results())
    pin = c["pinnacle"]
    assert pin["quotes"] == 4 and pin["settled"] == 2 and pin["matches"] == 1
    assert pin["whole_lines"] == 2 and pin["pushes"] == 1 and pin["unsettled"] == 0
    assert pin["model"]["n"] == 2 and pin["model"]["wins"] == 1  # 6 > 4.5; 3 < 3.5
    assert pin["model"]["predicted"] == pytest.approx(0.5, abs=1e-4)  # (0.60 + 0.40) / 2
    assert pin["price"]["predicted"] == pytest.approx(0.485, abs=1e-4)
    dk = c["draftkings"]
    assert dk["model"]["n"] == 1 and dk["price"]["n"] == 0
    none = cl.quote_calibration(rows)  # no results yet
    assert none["pinnacle"]["unsettled"] == 4 and none["pinnacle"]["model"]["n"] == 0
    espn = corner_results(hc=4).assign(kickoff=K.isoformat())
    assert cl.quote_calibration(rows, espn)["pinnacle"]["model"]["wins"] == 0


def test_paper_run_publishes_the_quote_calibration_and_survives_a_bad_row(tmp_path):
    from test_paper import NOW, card, src

    (tmp_path / "odds_log").mkdir()
    (tmp_path / "odds_log" / "E0_corners_2026-10.jsonl").write_text(
        json.dumps(quote(4.5, 0.6, 0.55)) + "\n"
    )
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW, corner_results=corner_results())
    corners = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "corners")
    qc = corners["live"]["quote_calibration"]
    assert qc["pinnacle"]["model"]["n"] == 1 and "note" in qc
    for s in corners["live"]["strategies"]:
        assert s["calibration"]["n"] == 0
    assert data["portfolio"]["models"]["order"] == ["match", "blend", "corners_f"]


def test_models_section_reads_each_leagues_fit_from_data_json():
    matches = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"] * 3),
            "home": ["A", "B", "C", "A", "B", "C", "A", "B", "C", "A", "B", "C"],
            "away": ["B", "C", "A", "C", "A", "B", "B", "C", "A", "C", "A", "B"],
            "home_goals": [2, 1, 0, 3, 1, 1, 2, 0, 1, 2, 2, 0],
            "away_goals": [0, 1, 1, 1, 2, 0, 1, 0, 1, 0, 1, 1],
        }
    )
    model = DixonColes().fit(matches)
    fit = publish.fit_info(model, matches)
    assert fit["matches"] == 12 and fit["fit_from"] == "2026-08-01" and fit["teams"] == 3
    assert fit["half_life_days"] == round(np.log(2) / model.xi)
    assert fit["home_adv"] == round(model.params["home_adv"], 4) and fit["ridge"] == 0.0
    data = {
        "match_model": {"E0": fit, "UCL": {**fit, "kind": "cross_league", "ridge": 0.1}},
        "match_blends": {
            "generated_at": "2026-10-13T20:16:47+00:00",
            "leagues": {
                "SP1": {"coef": [0.076, 0.058, 1.073, 0.106], "sigma": 0.019, "matches": 1287}
            },
        },
        "corners_model": {"E0": {"model": "f", "matches": 770, "k_for": 9.1}, "E2": {"error": "x"}},
    }
    s = eq.section(data)
    assert s["match"]["leagues"]["UCL"]["name"] == "Champions League"
    assert s["match"]["leagues"]["E0"]["rho"] == fit["rho"] and s["match"]["equation"]
    assert s["blend"]["leagues"]["SP1"]["coef"] == {
        "a_draw": 0.076,
        "a_away": 0.058,
        "b": 1.073,
        "c": 0.106,
    }
    assert s["blend"]["generated_at"].startswith("2026-10-13")
    assert s["corners_f"]["leagues"]["E2"]["error"] == "x"
    assert eq.section({})["match"]["leagues"] == {}
    json.dumps(s)


def test_the_corner_fit_info_names_its_dispersion():
    from test_corners_live import _league

    m = cl.TeamCornersF().fit(_league(seasons=range(2014, 2018)))
    info = m.info()
    assert info["alpha_home"] == round(m.alpha_[0], 4) and info["alpha_away"] > 0


def test_outcome_rows_drop_matches_missing_any_series():
    df = pd.DataFrame(
        {"p_home": [0.5, 0.4], "p_draw": [0.3, 0.3], "p_away": [0.2, 0.3], "y": [0, 2]}
    )
    df["pb_home"], df["pb_draw"], df["pb_away"] = [0.5, np.nan], [0.3, 0.3], [0.2, 0.3]
    long = cal.outcome_rows(df, {"model": "p", "blend": "pb"}, mc.H2H, key="E0")
    assert len(long) == 5 and long["won"].sum() == 2
    assert set(long["group"]) == {"E0|0", "E0|1"}
