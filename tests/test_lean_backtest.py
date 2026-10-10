"""Blend Lean's driver tag (market vs model) and its Pinnacle-early backtest."""

import json

import numpy as np
import pandas as pd
import pytest
from scipy.special import softmax
from test_blend_lean import P_BET_HOME, SIGMA, implied, lean
from test_paper import NOW, card, src

from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import paper
from soccer_stats import trades as tr
from soccer_stats.data import pinnacle_early_close
from soccer_stats.odds import devig_shin
from soccer_stats.publish import add_lean

ODDS = {"home": 2.1, "draw": 3.4, "away": 3.9}


def fair(odds=ODDS):
    return devig_shin(np.array([odds[m] for m in tr.LEAN_MARKETS], dtype=float))


# ---------- the driver tag ----------


def test_without_model_drops_only_the_model_term():
    mk = np.array([[0.5, 0.3, 0.2]])
    md = np.array([[0.2, 0.3, 0.5]])
    coef = [0.1, -0.05, 1.2, 0.4]
    got = mc.without_model(coef, mk)
    want = mc.apply([0.1, -0.05, 1.2, 0.0], mk, md)
    assert np.allclose(got, want)
    # It never depends on the model; the full blend does.
    assert not np.allclose(mc.apply(coef, mk, md), got)
    assert np.isnan(mc.without_model(None, mk)).all()


def test_market_side_z_is_the_price_alone_through_a_and_b():
    coef = [0.0, 0.0, 1.3, 0.5]
    zm = tr.market_side_z(coef, ODDS, SIGMA)
    pm = mc.without_model(coef, [fair()])[0]
    for i, m in enumerate(tr.LEAN_MARKETS):
        assert zm[m] == pytest.approx((pm[i] - 1 / ODDS[m]) / SIGMA)
    # The identity blend on the price alone is the fair price: never above break-even.
    assert max(tr.market_side_z([0, 0, 1, 0.9], ODDS, SIGMA).values()) < 0
    assert tr.market_side_z(None, ODDS, SIGMA) is None
    assert tr.market_side_z(coef, {"home": 2.1}, SIGMA) is None


def test_driver_is_market_at_one_sigma_without_the_model_else_model():
    assert tr.lean_driver(1.0) == "market" and tr.lean_driver(2.4) == "market"
    assert tr.lean_driver(0.99) == "model" and tr.lean_driver(-1.0) == "model"
    assert tr.lean_driver(None) is None and tr.lean_driver(float("nan")) is None
    pb = {"home": 0.52, "draw": 0.27, "away": 0.21}
    pick = tr.lean_pick(pb, ODDS, SIGMA, z_market={"home": 0.4, "draw": 0.0, "away": 0.0})
    assert pick["market"] == "home" and pick["z"] >= 1
    assert pick["z_market"] == 0.4 and pick["driver"] == "model"
    pick = tr.lean_pick(pb, ODDS, SIGMA, z_market={"home": 1.2})
    assert pick["driver"] == "market"
    # Without z_market the pick keeps its old shape.
    assert "driver" not in tr.lean_pick(pb, ODDS, SIGMA)


def test_add_lean_tags_the_card_pick_with_z_market_and_driver():
    # b > 1 sharpens the price: the favourite clears 1 sigma on the price alone.
    sharp = {"coef": [0.0, 0.0, 1.6, 0.0], "sigma": 0.01, "matches": 900}
    # The model term alone (b = 1, no intercepts): the price contributes nothing.
    model = {"coef": [0.0, 0.0, 1.0, 1.0], "sigma": 0.01, "matches": 900}
    for entry, driver in ((sharp, "market"), (model, "model")):
        c = card(league="SP1")
        c["implied"] = implied(c)
        data = {"fixtures": [c]}
        assert add_lean(data, {"leagues": {"SP1": entry}}) == 1
        fx = data["fixtures"][0]
        pick = fx["lean_pick"]
        assert pick["driver"] == driver
        assert pick["z_market"] == fx["z_market"][pick["market"]]
        zm = tr.market_side_z(entry["coef"], c["odds"], entry["sigma"])[pick["market"]]
        assert pick["z_market"] == pytest.approx(zm, abs=1e-3)
        assert (pick["z_market"] >= 1) == (driver == "market")


def test_new_lean_trades_record_z_market_and_driver():
    led = {}
    rule = tr.lean_rule({"leagues": {"E0": {"coef": [0, 0, 1, 0], "sigma": SIGMA}}}, "E0")
    assert rule["coef"] == [0, 0, 1, 0]
    paper.update_ledger(led, [card(p_bet=P_BET_HOME)], src(), None, NOW, threshold=None, lean=rule)
    t = led["E0|2627|Arsenal|Leeds|lean"]
    zm = tr.market_side_z([0, 0, 1, 0], card()["odds"], SIGMA)["home"]
    assert t["z_market"] == pytest.approx(zm, abs=1e-3)
    assert t["driver"] == "model"  # the identity blend on the price alone never clears 1σ
    assert {"z_market", "driver"} <= set(tr.LEAN_ENTRY_FIELDS)


# ---------- existing Lean trades without z_market ----------


def old_lean_trade():
    """A Lean trade as PR #43 opened it: no z_market, no driver."""
    led = {}
    paper.update_ledger(
        led, [card(p_bet=P_BET_HOME)], src(), None, NOW, threshold=None, lean=lean()
    )
    t = led["E0|2627|Arsenal|Leeds|lean"]
    t.pop("z_market"), t.pop("driver")
    return led, t


def odds_log_row(t, prices, downloaded):
    return {
        "league": "E0",
        "home": t["home"],
        "away": t["away"],
        "kickoff": pd.Timestamp(t["kickoff"]),
        "market": "h2h",
        "prices": prices,
        "fetched_at": pd.Timestamp(downloaded),
        "downloaded_at": pd.Timestamp(downloaded).isoformat(),
        "logged_at": NOW.isoformat(),
    }


def test_old_lean_trades_get_their_driver_once_from_the_odds_log(tmp_path):
    led, t = old_lean_trade()
    prices = {m: card()["odds"][m] for m in tr.LEAN_MARKETS}
    log = pd.DataFrame(
        [
            odds_log_row(t, {"home": 1.3, "draw": 6.0, "away": 9.0}, NOW - pd.Timedelta(hours=5)),
            odds_log_row(t, prices, t["odds_fetched_at"]),
        ]
    )
    coef = [0.0, 0.0, 1.6, 0.0]
    rule = {**lean(), "coef": coef}
    later = NOW + pd.Timedelta(hours=1)
    ev, _ = paper.update_ledger(led, [], src(later), None, later, odds_log=log, lean=rule)
    upd = [e for e in ev if "driver" in e]
    zm = tr.market_side_z(coef, prices, t["sigma"])["home"]
    assert len(upd) == 1 and upd[0]["type"] == "update" and upd[0]["driver_from"] == "odds_log"
    assert upd[0]["z_market"] == pytest.approx(zm, abs=1e-3)
    assert upd[0]["driver"] == tr.lean_driver(zm)
    # Entry fields never change, and the next build doesn't repeat it.
    assert t["z"] == pytest.approx(1.29, abs=0.01) and t["odds"] == 1.4
    ev, _ = paper.update_ledger(led, [], src(later), None, later, odds_log=log, lean=rule)
    assert not [e for e in ev if "driver" in e]
    # The update survives a reload (UPDATE_FIELDS).
    entry = {k: v for k, v in t.items() if k not in ("z_market", "driver", "driver_from")}
    paper.append_events(tmp_path, [{"type": "open", **entry}])
    assert "driver" not in paper.load_ledger(tmp_path)["E0|2627|Arsenal|Leeds|lean"]
    paper.append_events(tmp_path, upd)
    back = paper.load_ledger(tmp_path)["E0|2627|Arsenal|Leeds|lean"]
    assert back["driver"] == upd[0]["driver"] and back["driver_from"] == "odds_log"


def test_driver_update_leaves_twelve_percent_trades_alone():
    """Only Lean trades get the one-time driver event: a 12% trade on the same match (no
    strategy field, or "edge12") never does, and its fields are untouched."""
    led, t = old_lean_trade()
    for tid, strategy in (("E0|2627|Arsenal|Leeds", None), ("E0|2627|Arsenal|Leeds|x", "edge12")):
        e12 = {k: v for k, v in t.items() if k not in ("z", "tier", "sigma", "strategy")}
        led[tid] = {**e12, "id": tid, "rule": "fixed_raw", "threshold": 0.12, "sigma": 0.02}
        if strategy:
            led[tid]["strategy"] = strategy
    before = {tid: dict(v) for tid, v in led.items() if tid != t["id"]}
    prices = {m: card()["odds"][m] for m in tr.LEAN_MARKETS}
    log = pd.DataFrame([odds_log_row(t, prices, t["odds_fetched_at"])])
    rule = {**lean(), "coef": [0.0, 0.0, 1.6, 0.0]}
    later = NOW + pd.Timedelta(hours=1)
    ev, _ = paper.update_ledger(led, [], src(later), None, later, odds_log=log, lean=rule)
    assert [e["id"] for e in ev if "driver" in e] == [t["id"]]
    for tid, was in before.items():
        assert "driver" not in led[tid] and "z_market" not in led[tid]
        entry = {k for k in was if k not in paper.UPDATE_FIELDS}
        assert {k: led[tid][k] for k in entry} == {k: was[k] for k in entry}


def test_betis_barcelona_is_model_driven_through_the_relative_model_term():
    """The live SP1 trade of 10 Oct (Betis v Barcelona, away at 1.41): the model gave
    Barcelona 52.7%, below DraftKings' fair 68.5%, yet the pick is model-driven. The
    conditional logit's model term c * log(model_k) only matters relative to the other
    outcomes (softmax), and with SP1's c > 0 it lifts the model's own favourite
    (Barcelona, 52.7% against 26.8% and 20.5%). Without it (c = 0, a and b kept) Barcelona
    is 71.1% against a 70.9% break-even: z 0.095, short of 1."""
    coef = [0.0758, 0.0576, 1.0725, 0.1057]  # match_blends.json SP1, data-log 696a25b
    sigma = 0.019
    odds = {"home": 6.0, "draw": 5.5, "away": 1.41}
    model = {"home": 0.2683, "draw": 0.2045, "away": 0.5271}
    fair_p = fair(odds)
    assert fair_p[2] == pytest.approx(0.685, abs=1e-3)
    pb = mc.apply(coef, [fair_p], [[model[m] for m in tr.LEAN_MARKETS]])[0]
    pick = tr.lean_pick(
        dict(zip(tr.LEAN_MARKETS, pb, strict=True)),
        odds,
        sigma,
        z_market=tr.market_side_z(coef, odds, sigma),
    )
    assert pick["market"] == "away" and pick["z"] == pytest.approx(1.017, abs=2e-3)
    assert pick["z_market"] == pytest.approx(0.095, abs=2e-3)
    assert pick["driver"] == "model"
    pm = mc.without_model(coef, [fair_p])[0]
    assert pm[2] == pytest.approx(0.711, abs=1e-3) and pb[2] > pm[2]
    # The same model chance with c < 0 would pull Barcelona down instead: the direction
    # comes from c's sign and the model's ranking across outcomes, not from model < market.
    neg = [*coef[:3], -coef[3]]
    assert mc.apply(neg, [fair_p], [[model[m] for m in tr.LEAN_MARKETS]])[0][2] < pm[2]


def test_by_driver_splits_lean_trades_and_counts_unknown_ones():
    base = {"kickoff": "2026-10-10T14:00:00+00:00", "stake": 10.0, "edge": 0.05}
    trades = [
        {**base, "id": "a|lean", "strategy": "lean", "driver": "market", "status": "won",
         "odds": 1.5, "profit": 5.0, "clv_dk": 0.02},
        {**base, "id": "b|lean", "strategy": "lean", "driver": "model", "status": "lost",
         "odds": 2.0, "profit": -10.0, "clv_dk": -0.01},
        {**base, "id": "c|lean", "strategy": "lean", "status": "open", "odds": 2.0},
        {**base, "id": "d", "status": "lost", "odds": 2.0, "profit": -10.0},
    ]  # fmt: skip
    sec = paper.strategy_section(trades, "lean", "Blend Lean", None)
    bd = sec["by_driver"]
    assert set(bd) == {"market", "model", "unknown"}
    assert bd["market"]["settled"] == 1 and bd["market"]["roi"] == pytest.approx(0.5)
    assert bd["market"]["clv_dk"] == pytest.approx(0.02) and bd["market"]["beat_close_dk"] == 1
    assert bd["model"]["roi"] == pytest.approx(-1.0) and bd["unknown"]["open"] == 1
    assert sum(v["trades"] for v in bd.values()) == 3  # the 12% trade isn't Lean
    assert "by_driver" not in paper.strategy_section(trades, "edge12", "12%", None)
    # Every trade with a driver: no "unknown" key.
    assert set(paper.by_driver(trades[:2])) == {"market", "model"}


# ---------- the backtest ----------


def synth(n=1400, seed=0, start="2021-08-01", freq="20h"):
    """Matches with model chances, Pinnacle early and close prices and results."""
    rng = np.random.default_rng(seed)
    true = rng.dirichlet([4, 3, 3], n)
    early = softmax(np.log(true) + rng.normal(0, 0.15, (n, 3)), axis=1)
    close = softmax(np.log(true) + rng.normal(0, 0.08, (n, 3)), axis=1)
    model = softmax(np.log(true) + rng.normal(0, 0.3, (n, 3)), axis=1)
    y = np.array([rng.choice(3, p=row) for row in true])
    dates = pd.date_range(start, periods=n, freq=freq).normalize()
    goals = {0: (2, 0), 1: (1, 1), 2: (0, 1)}
    df = pd.DataFrame(
        {
            "date": dates,
            "season": [tr.season_label(d) for d in dates],
            "home": [f"H{i}" for i in range(n)],
            "away": [f"A{i}" for i in range(n)],
            "home_goals": [goals[v][0] for v in y],
            "away_goals": [goals[v][1] for v in y],
            **{f"p_{m}": model[:, i] for i, m in enumerate(mc.H2H)},
        }
    )
    pins = df[["season", "date", "home", "away"]].copy()
    for i, m in enumerate(mc.H2H):
        pins[f"odds_{m}"] = 1 / (early[:, i] * 1.03)
        pins[f"close_{m}"] = 1 / (close[:, i] * 1.025)
    # load_matches' fallback columns, which with_pinnacle must replace
    preds = df.assign(odds_home=9.9, odds_draw=9.9, odds_away=9.9, close_home=9.9)
    return preds, pins


def scored(min_rows=300):
    preds, pins = synth()
    merged = lb.with_pinnacle(preds, pins)
    train = mc.training_rows(merged, "h2h")
    target = lb.target_rows(merged)
    out, fits = lb.walk_forward(train, target, min_rows=min_rows)
    return train, target, out, fits


def test_with_pinnacle_replaces_the_fallback_prices():
    preds, pins = synth(50)
    m = lb.with_pinnacle(preds, pins)
    assert (m["odds_home"] != 9.9).all() and (m["close_home"] != 9.9).all()
    assert np.allclose(m["odds_draw"], pins["odds_draw"])


def test_pinnacle_early_close_never_falls_back():
    raw = pd.DataFrame(
        {
            "Date": ["01/08/2025", "02/08/2025"],
            "HomeTeam": ["A", "C"],
            "AwayTeam": ["B", "D"],
            "FTHG": [1, 0],
            "FTAG": [0, 0],
            "PSH": [2.0, None],
            "PSD": [3.4, None],
            "PSA": [4.0, None],
            "AvgH": [2.05, 1.9],
            "AvgD": [3.3, 3.5],
            "AvgA": [3.9, 4.2],
            "PSCH": [2.1, 1.8],
            "PSCD": [3.3, 3.6],
            "PSCA": [3.8, 4.4],
        }
    )
    pin = pinnacle_early_close(raw)
    assert pin.loc[0, "odds_home"] == 2.0 and pin.loc[1, "close_home"] == 1.8
    assert pin.loc[1, ["odds_home", "odds_draw", "odds_away"]].isna().all()


def test_walk_forward_fits_on_earlier_matches_only_with_their_own_sigma():
    train, target, out, fits = scored()
    done = out[out["sigma"].notna()]
    assert len(fits) > 5 and len(done) > 300
    # Every fit ended before its block began, which is before each bet's decision day,
    # which is EARLY_LEAD before kickoff.
    assert (done["fit_to"] < done["fit_from"]).all()
    assert (done["fit_from"] <= done["day"]).all()
    assert (done["day"] == done["date"] - lb.EARLY_LEAD).all()
    # The fit and sigma of a block come from the training rows before it, and only those.
    f = fits[-1]
    rows = train[pd.to_datetime(train["date"]) < pd.Timestamp(f["from"])]
    mk, pc = rows[[f"mkt_{m}" for m in mc.H2H]], rows[[f"p_{m}" for m in mc.H2H]]
    assert f["coef"] == mc.fit(mk, pc, rows["y"])
    assert f["sigma"] == pytest.approx(mc.sigma(f["coef"], mk, pc), abs=1e-5)
    block = out["fit_from"] == pd.Timestamp(f["from"])
    assert np.allclose(out.loc[block, "sigma"], mc.sigma(f["coef"], mk, pc))
    assert (out.loc[block, "fit_rows"] == len(rows)).all()
    # Rewriting every later result changes nothing before it.
    cut = pd.Timestamp(fits[3]["from"])
    wild = train.copy()
    wild.loc[pd.to_datetime(wild["date"]) >= cut, "y"] = 0
    out2, _ = lb.walk_forward(wild, target)
    early_rows = out["fit_from"] <= cut
    cols = ["pb_home", "pb_draw", "pb_away", "pm_home", "sigma"]
    assert np.allclose(
        out.loc[early_rows, cols].to_numpy(dtype=float),
        out2.loc[early_rows, cols].to_numpy(dtype=float),
        equal_nan=True,
    )
    # The first ~300 matches have no fit, so no bet.
    assert out["sigma"].isna().iloc[:200].all()


def test_bets_follow_the_live_rule_against_the_early_price():
    _, _, out, _ = scored()
    b = lb.bets(out, "E0")
    assert len(b) > 20
    r = b.iloc[0]
    row = out[(out["home"] == r["home"])].iloc[0]
    pbet = {m: row[f"pb_{m}"] for m in mc.H2H}
    odds = {m: row[f"odds_{m}"] for m in mc.H2H}
    pick = tr.lean_pick(pbet, odds, row["sigma"])
    assert pick["market"] == r["market"] and pick["z"] == pytest.approx(r["z"])
    assert r["odds"] == pytest.approx(row[f"odds_{r['market']}"])  # quoted, margin kept
    assert (b["z"] >= 1 - 1e-9).all() and set(b["tier"]) <= {"lean", "strong"}
    assert ((b["tier"] == "strong") == (b["z"] >= 2 - 1e-9)).all()
    assert ((b["driver"] == "market") == (b["z_market"] >= 1 - 1e-9)).all()
    assert np.allclose(b["profit"], np.where(b["won"], b["odds"] - 1, -1.0))
    want_clv = tr.clv(r["odds"], r["market"], {m: row[f"close_{m}"] for m in mc.H2H})
    assert r["clv"] == pytest.approx(want_clv)
    assert b.groupby(["home", "away"]).size().max() == 1  # one per match


def test_metrics_and_summary_by_tier_season_and_driver():
    df = pd.DataFrame(
        {
            "date": ["2023-01-01", "2023-02-01", "2024-01-01", "2024-02-01"],
            "season": ["2223", "2223", "2324", "2324"],
            "league": "E0",
            "home": list("ABCD"),
            "away": list("EFGH"),
            "market": ["home", "away", "home", "draw"],
            "odds": [2.0, 4.0, 2.0, 3.0],
            "p_bet": 0.5,
            "p_model": 0.5,
            "z": [1.2, 2.5, 1.1, 2.1],
            "z_market": [1.1, 0.2, 0.5, 1.5],
            "driver": ["market", "model", "model", "market"],
            "tier": ["lean", "strong", "lean", "strong"],
            "won": [True, False, False, True],
            "profit": [1.0, -1.0, -1.0, 2.0],
            "clv": [0.02, -0.04, None, 0.01],
        }
    )
    m = lb.metrics(df)
    assert m["bets"] == 4 and m["wins"] == 2 and m["win_rate"] == 0.5
    assert m["roi"] == pytest.approx(0.25) and m["profit"] == pytest.approx(1.0)
    assert m["breakeven"] == pytest.approx(np.mean([0.5, 0.25, 0.5, 1 / 3]))
    assert m["clv_bets"] == 3 and m["clv"] == pytest.approx(np.mean([0.02, -0.04, 0.01]))
    assert m["beat_close"] == pytest.approx(2 / 3)
    lo, hi = m["roi_ci95"]
    assert lo <= m["roi"] <= hi and m["by_market"] == {"home": 2, "away": 1, "draw": 1}
    s = lb.summary(df)
    assert s["lean"]["bets"] == 4 and s["strong"]["bets"] == 2
    assert s["strong"]["roi"] == pytest.approx(0.5)
    assert s["lean"]["by_driver"]["market"]["roi"] == pytest.approx(1.5)
    assert s["lean"]["by_driver"]["model"]["roi"] == pytest.approx(-1.0)
    assert s["strong"]["by_driver"]["model"]["bets"] == 1
    seasons = {x["season"]: x for x in s["by_season"]}
    assert seasons["2223"]["label"] == "2022/23" and seasons["2223"]["roi"] == 0.0
    assert seasons["2324"]["market_bets"] == 1 and seasons["2324"]["strong_bets"] == 1
    assert lb.metrics(df.iloc[:0]) == {"bets": 0}
    assert lb.summary(df.iloc[:0])["lean"]["by_driver"]["market"] == {"bets": 0}


def test_coverage_filter_scores_only_well_priced_seasons():
    preds, pins = synth()
    # Blank the early price on 40% of the latest season, and the close on 15% of another.
    last = sorted(pins["season"].unique())[-1]
    mid = sorted(pins["season"].unique())[-2]
    idx = pins.index[pins["season"] == last]
    pins.loc[idx[: int(len(idx) * 0.4)], ["odds_home", "odds_draw", "odds_away"]] = np.nan
    idx = pins.index[pins["season"] == mid]
    pins.loc[idx[: int(len(idx) * 0.15)], ["close_home"]] = np.nan
    cov = {c["season"]: c for c in lb.coverage(pins)}
    assert not cov[last]["included"] and cov[last]["early_share"] == pytest.approx(0.6, abs=0.01)
    assert not cov[mid]["included"] and cov[mid]["close_share"] < 0.9
    rep, b = lb.league_report("E0", preds, pins)
    assert set(b["season"]) <= {s for s, c in cov.items() if c["included"]}
    assert len(b) > 0
    assert lb.season_label(last) in rep["seasons_excluded"]
    assert rep["fits"] > 0 and rep["sigma"]["min"] > 0
    assert rep["lean"]["bets"] == len(b)
    # Exactly 90% passes.
    ten = pins.assign(season="x").iloc[:10].assign(odds_home=[np.nan] + [2.0] * 9)
    assert lb.coverage(ten)[0]["included"]
    eight = ten.assign(odds_home=[np.nan, np.nan] + [2.0] * 8)
    assert not lb.coverage(eight)[0]["included"]


def test_build_pools_leagues_and_keeps_compact_rows():
    preds, pins = synth()
    rep, b = lb.league_report("E0", preds, pins)
    dk = {"seasons": "2025", "tiers": {"lean": {"bets": 1, "roi": 0.4}, "strong": {"bets": 0}}}
    out = lb.build(
        {"E0": rep, "SP1": {"error": "x"}},
        [b, lb.bets(pd.DataFrame(), "SP1")],
        from_season=2021,
        dk=dk,
        names={"E0": "Premier League"},
        max_bets=10,
    )
    assert out["kind"] == "lean_backtest" and out["leagues"]["E0"]["name"] == "Premier League"
    assert out["all"]["lean"]["bets"] == len(b) and out["bets_total"] == len(b)
    assert out["all"]["by_league"]["E0"]["bets"] == len(b)
    assert out["all"]["by_league"]["SP1"] == {
        "bets": 0,
        "by_driver": {"market": {"bets": 0}, "model": {"bets": 0}},
    }
    assert len(out["bets"]) == 10 and out["bet_fields"] == lb.BET_FIELDS
    row = dict(zip(lb.BET_FIELDS, out["bets"][0], strict=True))
    assert row["date"] == max(b["date"]) and row["driver"] in ("market", "model")
    assert len(json.dumps(out["bets"][0])) < 130
    assert out["draftkings_replay"]["book"] == "DraftKings"
    assert "DraftKings replay" in out["draftkings_replay"]["label"]
    assert out["draftkings_replay"]["lean"]["roi"] == 0.4
    assert lb.dk_replay(None) is None and lb.dk_replay({"summary": {}}) is None
    assert "Pinnacle" in out["note"] and "tougher" in out["note"]


def test_publish_trims_the_backtest_and_says_when_it_is_missing(tmp_path):
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW)
    sb = data["portfolio"]["strategy_backtests"]["lean"]
    assert sb["available"] is False and "hasn't been run" in sb["note"]
    paper.run({"fixtures": [], "portfolio": {}}, None, None, now=NOW)  # no ledger at all
    preds, pins = synth()
    rep, b = lb.league_report("E0", preds, pins)
    out = lb.build({"E0": rep}, [b], from_season=2021)
    (tmp_path / "backtest").mkdir()
    (tmp_path / "backtest" / "lean_backtest.json").write_text(json.dumps(out, default=str))
    data = {"fixtures": [card()], "odds_source": src(), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW)
    sb = data["portfolio"]["strategy_backtests"]["lean"]
    assert sb["available"] is True and sb["bets_total"] == len(b)
    assert sb["bets_shown"] == min(len(b), lb.APP_BETS) == len(sb["bets"])
    assert sb["all"]["lean"]["bets"] == len(b) and sb["leagues"]["E0"]["lean"]["bets"] == len(b)
    small = lb.app_section(out, max_bets=3)
    assert len(small["bets"]) == 3 and small["bets"] == out["bets"][:3]
    assert lb.app_section({"something": "else"})["available"] is False
