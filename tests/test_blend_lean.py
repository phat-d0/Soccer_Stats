"""Blend Lean: the per-league h2h blend, sigma, z, tiers and the second paper strategy."""

import json

import numpy as np
import pandas as pd
import pytest
from scipy.special import softmax
from test_paper import NOW, card, src

from soccer_stats import match_calibration as mc
from soccer_stats import paper
from soccer_stats import trades as tr
from soccer_stats.cli import league_blend_rows
from soccer_stats.data import pinnacle_only
from soccer_stats.publish import add_lean, match_blends

SIGMA = 0.02
# Raw p (test_paper.card): the 12% test picks the draw (0.24 x 5.0 - 1 = 20%).
# The blend: draw 0.26 vs break-even 1/5.0 = 0.20 -> z = 3.0 (Strong).
P_BET_DRAW = {"home": 0.70, "draw": 0.26, "away": 0.04, "over25": None, "under25": None}
# The blend: home 0.74 vs 1/1.4 = 0.714 -> z = 1.29 (Lean); draw 0.20 -> z = 0.
P_BET_HOME = {"home": 0.74, "draw": 0.20, "away": 0.06, "over25": None, "under25": None}


def lean(sigma=SIGMA, league="E0"):
    blends = {"leagues": {league: {"coef": [0, 0, 1, 0], "sigma": sigma, "matches": 900}}}
    return tr.lean_rule(blends, league)


def rows(n=1500, seed=0, start="2022-08-01"):
    """Synthetic training rows (training_rows' shape): a known blend generates outcomes."""
    rng = np.random.default_rng(seed)
    true = rng.dirichlet([4, 3, 3], n)
    market = softmax(np.log(true) + rng.normal(0, 0.1, (n, 3)), axis=1)
    model = softmax(np.log(true) + rng.normal(0, 0.3, (n, 3)), axis=1)
    y = np.array([rng.choice(3, p=row) for row in true])
    dates = pd.date_range(start, periods=n, freq="16h")
    return pd.DataFrame(
        {
            "date": dates,
            **{f"mkt_{m}": market[:, i] for i, m in enumerate(mc.H2H)},
            **{f"p_{m}": model[:, i] for i, m in enumerate(mc.H2H)},
            "y": y,
        }
    )


# ---------- sigma, z and the tier ----------


def test_sigma_is_the_pooled_sd_of_blend_minus_price():
    r = rows(400)
    mk, md = r[[f"mkt_{m}" for m in mc.H2H]], r[[f"p_{m}" for m in mc.H2H]]
    coef = [0.05, -0.1, 0.9, 0.2]
    want = (mc.apply(coef, mk, md) - mk.to_numpy()).ravel().std(ddof=0)
    assert mc.sigma(coef, mk, md) == pytest.approx(want)
    # The identity blend (b = 1, c = 0, no intercepts) never strays from the price.
    assert mc.sigma([0, 0, 1, 0], mk, md) == pytest.approx(0, abs=1e-9)
    assert mc.sigma(None, mk, md) is None


def test_z_uses_the_quoted_price_and_sigma():
    odds = {"home": 2.1, "draw": 3.0, "away": 4.6}
    pb = {"home": 0.5, "draw": 0.3, "away": 0.2}
    z = tr.side_z(pb, odds, SIGMA)
    assert z["home"] == pytest.approx((0.5 - 1 / 2.1) / SIGMA)
    assert z["draw"] == pytest.approx((0.3 - 1 / 3.0) / SIGMA)
    assert tr.side_z(pb, odds, None) is None and tr.side_z(pb, odds, 0) is None
    pick = tr.lean_pick(pb, odds, SIGMA)
    assert pick["market"] == "home" and pick["tier"] == "lean"
    assert pick["z"] == pytest.approx(1.19, abs=0.01)
    assert pick["edge"] == pytest.approx(0.5 * 2.1 - 1)
    strong = tr.lean_pick(pb, {**odds, "home": 2.25}, SIGMA)
    assert strong["tier"] == "strong" and strong["z"] >= tr.STRONG_Z


def test_lean_pick_takes_the_highest_z_and_nothing_below_one():
    pb = {"home": 0.45, "draw": 0.30, "away": 0.25}
    # home z = (0.45 - 0.4167)/0.02 = 1.67; away z = (0.25 - 0.2)/0.02 = 2.5
    pick = tr.lean_pick(pb, {"home": 2.4, "draw": 3.4, "away": 5.0}, SIGMA)
    assert pick["market"] == "away" and pick["tier"] == "strong"
    # Every side under 1 sigma: no pick, even with a positive edge.
    assert tr.lean_pick(pb, {"home": 2.25, "draw": 3.4, "away": 4.1}, SIGMA) is None
    assert (0.45 * 2.25 - 1) > 0
    assert tr.lean_pick(pb, {"home": 2.4}, SIGMA)["market"] == "home"  # unpriced sides skip
    assert tr.lean_pick(None, {"home": 2.4}, SIGMA) is None


# ---------- the per-league fit ----------


def test_league_fit_uses_only_matches_before_its_fit_date():
    r = rows(1500)
    fit_date = r["date"].iloc[999] + pd.Timedelta(hours=1)
    got = mc.league_fit(r, fit_date, min_rows=300)
    before = r.iloc[:1000]
    assert got["matches"] == 1000
    assert pd.Timestamp(got["fit_to"]) < fit_date
    mk, md = before[[f"mkt_{m}" for m in mc.H2H]], before[[f"p_{m}" for m in mc.H2H]]
    assert got["coef"] == mc.fit(mk, md, before["y"], 300)
    assert got["sigma"] == pytest.approx(mc.sigma(got["coef"], mk, md), abs=1e-5)
    # Later matches (even wildly different ones) change nothing.
    wild = r.copy()
    wild.loc[1000:, "y"] = 0
    assert mc.league_fit(wild, fit_date, min_rows=300)["coef"] == got["coef"]
    wf = got["walk_forward"]
    assert wf["matches"] > 0 and {"model", "blend", "pinnacle"} <= set(wf)
    assert mc.league_fit(r.iloc[:0], fit_date)["coef"] is None


def test_walk_forward_sigma_comes_from_each_blocks_earlier_rows():
    r = rows(1200)
    out, fits = mc.walk_forward(r, r, "h2h", min_rows=300, with_sigma=True)
    assert fits and all("sigma" in f for f in fits)
    f = fits[-1]
    tr_rows = r[r["date"] < pd.Timestamp(f["from"])]
    mk, md = tr_rows[[f"mkt_{m}" for m in mc.H2H]], tr_rows[[f"p_{m}" for m in mc.H2H]]
    assert f["sigma"] == pytest.approx(mc.sigma(f["coef"], mk, md), abs=1e-5)
    block = r["date"] >= pd.Timestamp(f["from"])
    assert np.allclose(out.loc[block, "sigma"], mc.sigma(f["coef"], mk, md))


def test_blend_rows_use_pinnacles_own_close_only():
    raw = pd.DataFrame(
        {
            "Date": ["01/08/2025", "02/08/2025"],
            "HomeTeam": ["A", "C"],
            "AwayTeam": ["B", "D"],
            "FTHG": [1, 0],
            "FTAG": [0, 0],
            "PSCH": [2.0, None],
            "PSCD": [3.4, None],
            "PSCA": [4.0, None],
            "AvgCH": [2.05, 1.9],
            "AvgCD": [3.3, 3.5],
            "AvgCA": [3.9, 4.2],
        }
    )
    pin = pinnacle_only(raw)
    assert pin.loc[0, "close_home"] == 2.0
    assert pin.loc[1, ["close_home", "close_draw", "close_away"]].isna().all()


def test_league_blend_rows_join_pinnacle_and_report_coverage(league):
    df, _ = league
    df = df.copy()
    df["league"] = "SP1"
    df["season"] = [tr.season_label(d) for d in df["date"]]
    pins = df[["season", "date", "home", "away"]].copy()
    rng = np.random.default_rng(1)
    for m, base in (("home", 2.2), ("draw", 3.4), ("away", 3.6)):
        pins[f"close_{m}"] = base * rng.lognormal(0, 0.05, len(pins))
    pins.loc[pins.index[::2], ["close_home", "close_draw", "close_away"]] = np.nan
    r, cov = league_blend_rows("SP1", df, pins, "2022-07-01", mc_factory())
    assert cov and sum(c["pinnacle"] for c in cov) == len(r)
    assert sum(c["predicted"] for c in cov) > len(r)  # unpriced matches are left out
    assert set(r.columns) >= {"date", "mkt_home", "p_home", "y"}


def mc_factory():
    from soccer_stats.models import DixonColes

    return DixonColes


# ---------- the second paper strategy ----------


def test_lean_trade_opens_per_league_with_its_suffix():
    c = card(league="SP1", p_bet=P_BET_DRAW)
    led = {}
    kw = {"league": "SP1", "threshold": None, "lean": lean(league="SP1")}
    ev, _ = paper.update_ledger(led, [c], src(), None, NOW, **kw)
    t = led["SP1|2627|Arsenal|Leeds|lean"]
    assert [e["id"] for e in ev] == ["SP1|2627|Arsenal|Leeds|lean"]
    assert t["strategy"] == "lean" and t["rule"] == "lean_1sigma" and t["p_source"] == "blend"
    assert t["market"] == "draw" and t["tier"] == "strong" and t["z"] == pytest.approx(3.0)
    assert t["sigma"] == SIGMA and t["p_bet"] == 0.26 and t["p_model"] == 0.24
    assert t["model_p"] == 0.26 and t["edge"] == pytest.approx(0.3)
    assert t["stake"] == tr.STAKE and t["close_odds"] == 5.0
    assert set(tr.LEAN_ENTRY_FIELDS) <= set(t)
    # The same build again opens nothing.
    ev, _ = paper.update_ledger(led, [c], src(), None, NOW, **kw)
    assert ev == []


def test_both_strategies_on_one_match_and_settle_alike(tmp_path):
    c = card(p_bet=P_BET_HOME)
    rule = tr.paper_threshold(None, "E0")
    led = paper.load_ledger(tmp_path)
    ev, _ = paper.update_ledger(
        led, [c], src(), None, NOW, threshold=rule["threshold"], rule=rule, lean=lean()
    )
    paper.append_events(tmp_path, ev)
    led = paper.load_ledger(tmp_path)
    edge12, leant = led["E0|2627|Arsenal|Leeds"], led["E0|2627|Arsenal|Leeds|lean"]
    assert edge12["market"] == "draw" and edge12["strategy"] == "edge12"
    assert edge12["p_source"] == "model" and edge12["model_p"] == 0.24
    assert leant["market"] == "home" and leant["tier"] == "lean"
    # Settlement is the same code for both (draw 1-1).
    res = pd.DataFrame(
        {
            "season": ["2627"],
            "date": [pd.Timestamp("2026-10-10")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_goals": [1],
            "away_goals": [1],
            "close_home": [1.45],
            "close_draw": [4.6],
            "close_away": [7.0],
        }
    )
    later = pd.Timestamp("2026-10-11 12:00", tz="UTC")
    ev, _ = paper.update_ledger(led, [], src(), res, later, rule=rule, lean=lean())
    paper.append_events(tmp_path, ev)
    led = paper.load_ledger(tmp_path)
    assert led["E0|2627|Arsenal|Leeds"]["status"] == "won"
    assert led["E0|2627|Arsenal|Leeds|lean"]["status"] == "lost"
    assert led["E0|2627|Arsenal|Leeds|lean"]["clv_pinnacle"] is not None


def test_edge12_is_unchanged_by_the_lean_strategy():
    rule = tr.paper_threshold(None, "E0")
    a, b = {}, {}
    ev_a, _ = paper.update_ledger(
        a, [card()], src(), None, NOW, threshold=0.12, rule=rule, lean=None
    )
    ev_b, _ = paper.update_ledger(
        b, [card(p_bet=P_BET_HOME)], src(), None, NOW, threshold=0.12, rule=rule, lean=lean()
    )
    edge12 = [e for e in ev_b if not e["id"].endswith("|lean")]
    assert edge12 == ev_a
    assert ev_a[0]["strategy"] == "edge12"
    # Trades opened before the strategy field existed read as the 12% test.
    assert tr.strategy_of({"id": "E0|2627|A|B"}) == "edge12"
    assert tr.strategy_of(ev_b[0]) == "lean"


AFTER = pd.Timestamp("2026-10-10 15:00", tz="UTC")


def test_lean_follows_the_same_gates():
    lr = lean()
    for c, source, now in (
        (card(p_bet=P_BET_DRAW, low_data=True), src(), NOW),
        (card(p_bet=P_BET_DRAW), src(NOW - pd.Timedelta(hours=4)), NOW),  # stale odds
        (card(p_bet=P_BET_DRAW), {"name": "football-data"}, NOW),
        (card(p_bet=P_BET_DRAW), src(AFTER - pd.Timedelta(minutes=30)), AFTER),  # kicked off
        (card(), src(), NOW),  # no blend on the card
    ):
        led = {}
        ev, _ = paper.update_ledger(led, [c], source, None, now, threshold=None, lean=lr)
        assert not [e for e in ev if e["id"].endswith("|lean")]
    led = {}
    ev, _ = paper.update_ledger(
        led, [card(p_bet=P_BET_DRAW)], src(), None, NOW, threshold=None, lean=lean(sigma=None)
    )
    assert ev == []


def test_missing_blends_file_means_no_lean_with_a_note(tmp_path):
    assert match_blends(str(tmp_path / "missing.json")) is None
    (tmp_path / "bad.json").write_text("{not json")
    assert match_blends(str(tmp_path / "bad.json")) is None
    rule = tr.lean_rule(None, "E0")
    assert rule["sigma"] is None and rule["source"] == "none" and "aren't available" in rule["note"]
    other = tr.lean_rule({"leagues": {"E0": {"coef": [0, 0, 1, 0], "sigma": 0.01}}}, "SP1")
    assert other["sigma"] is None and "this league" in other["note"]
    data = {"fixtures": [card(p_bet=P_BET_DRAW, implied={"home": 0.7})]}
    assert add_lean(data, None) == 0
    assert data["match_blends"]["note"] and data["fixtures"][0]["lean_pick"] is None
    # paper.run without match_blends: the Lean rule says why and opens nothing.
    data = {"fixtures": [card(p_bet=P_BET_DRAW)], "odds_source": src(), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW)
    r = data["portfolio"]["rules"]["E0"]
    assert r["sigma"] is None and r["lean"]["source"] == "none"
    ids = [t["id"] for t in data["portfolio"]["live"]["trades"]]
    assert ids == ["E0|2627|Arsenal|Leeds"]


def implied(c):
    from soccer_stats.publish import implied_probs

    o = c["odds"]
    return implied_probs({f"odds_{m}": o[m] for m in o})


def test_add_lean_sets_p_bet_z_and_the_pick(tmp_path):
    coef = [0.0, 0.0, 1.0, 0.0]  # the identity blend: p_bet = DraftKings' fair price
    path = tmp_path / "match_blends.json"
    blend = {"coef": coef, "sigma": 0.005, "matches": 900, "fit_from": "2022-08-01"}
    path.write_text(json.dumps({"generated_at": "x", "leagues": {"SP1": blend}}))
    blends = match_blends(str(path))
    c = card(league="SP1")
    c["implied"] = implied(c)
    e0 = card(league="E0", p_bet={"home": 0.6, "over25": 0.5, "under25": 0.5})
    e0["implied"] = implied(e0)
    data = {"league_code": "E0", "fixtures": [c, e0]}
    add_lean(data, blends)
    sp = data["fixtures"][0]
    assert sp["p_bet"]["home"] == pytest.approx(c["implied"]["home"])
    assert sp["p_bet"]["over25"] is None
    z_home = (sp["p_bet"]["home"] - 1 / 1.4) / 0.005
    assert sp["z"]["home"] == pytest.approx(z_home, abs=1e-3)
    # The fair price never beats its own break-even: no Lean pick at the identity blend.
    assert sp["lean_pick"] is None and max(sp["z"].values()) < 0
    # A league without a fit keeps its p_bet (E0's totals) and gets no z.
    assert data["fixtures"][1]["z"] is None and data["fixtures"][1]["p_bet"]["over25"] == 0.5
    assert data["match_blends"]["leagues"]["SP1"]["sigma"] == 0.005

    # A blend that leans on the model hard enough gives a pick; low_data never does.
    blends["leagues"]["SP1"] = {**blend, "coef": [0.0, 0.0, 0.0, 1.0]}
    data = {"fixtures": [card(league="SP1"), card(league="SP1", low_data=True, away="Spurs")]}
    for f in data["fixtures"]:
        f["implied"] = implied(f)
    assert add_lean(data, blends) == 1
    pick = data["fixtures"][0]["lean_pick"]
    assert pick["market"] == "draw" and pick["tier"] == "strong"  # 0.24 vs 1/5.0
    assert data["fixtures"][1]["lean_pick"] is None and data["fixtures"][1]["z"]


def test_strategies_listed_lean_first(tmp_path):
    blends = {"leagues": {"E0": {"coef": [0, 0, 1, 0], "sigma": SIGMA, "matches": 900}}}
    data = {
        "fixtures": [card(p_bet=P_BET_HOME)],
        "odds_source": src(),
        "portfolio": {},
        "match_blends": blends,
    }
    assert paper.run(data, tmp_path, None, now=NOW) == 2
    assert data["portfolio"]["strategy_order"] == ["lean", "edge12"]
    rules = data["portfolio"]["rules"]["E0"]
    assert rules["sigma"] == SIGMA and rules["lean"]["rule"] == "lean_1sigma"
    assert rules["threshold"] == 0.12 and rules["rule"] == "fixed_raw"  # the 12% test stays
    ml = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "moneyline")
    sts = ml["live"]["strategies"]
    assert [s["key"] for s in sts] == ["lean", "edge12"]
    assert sts[0]["label"] == "Blend Lean (1σ+)" and sts[1]["label"].startswith("Model 12%")
    assert sts[0]["trade_ids"] == ["E0|2627|Arsenal|Leeds|lean"]
    assert sts[1]["trade_ids"] == ["E0|2627|Arsenal|Leeds"]
    assert sts[0]["rule"]["sigma"] == {"E0": SIGMA} and sts[1]["rule"]["rule"] == "fixed_raw"
    assert sts[0]["summary"]["open"] == 1 and sts[1]["summary"]["open"] == 1
    # The combined view keeps both, for older app code.
    assert ml["live"]["summary"]["trades"] == 2
