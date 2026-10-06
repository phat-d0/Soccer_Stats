import numpy as np
import pandas as pd
import pytest
from scipy.special import softmax
from test_dk_backtest import _probs, make_history

from soccer_stats import backtest
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr

MIN = 60  # small synthetic league


def test_fit_recovers_a_known_blend():
    rng = np.random.default_rng(0)
    n = 20000
    true = rng.dirichlet([4, 3, 3], n)
    market = softmax(np.log(true) + rng.normal(0, 0.15, (n, 3)), axis=1)
    model = softmax(np.log(true) + rng.normal(0, 0.4, (n, 3)), axis=1)
    coef = [0.1, -0.2, 0.8, 0.3]
    p = mc.apply(coef, market, model)
    y = np.array([rng.choice(3, p=row) for row in p])
    got = mc.fit(market, model, y)
    assert got == pytest.approx(coef, abs=0.08)
    assert np.allclose(mc.apply(got, market, model).sum(1), 1)


def test_two_outcomes_is_the_logistic_blend():
    coef = [0.2, 0.9, 0.3]
    m, q = np.array([[0.6, 0.4]]), np.array([[0.7, 0.3]])
    p = mc.apply(coef, m, q)[0, 1]
    logit = lambda x: np.log(x / (1 - x))  # noqa: E731
    z = 0.2 + 0.9 * logit(0.4) + 0.3 * logit(0.3)
    assert p == pytest.approx(1 / (1 + np.exp(-z)))


def test_no_fit_with_too_few_matches():
    m = np.full((10, 3), 1 / 3)
    assert mc.fit(m, m, np.arange(10) % 3) is None
    assert np.isnan(mc.apply(None, m, m)).all()


@pytest.fixture(scope="module")
def setup(league):
    df, _ = league
    df = df.copy()
    df["league"] = "E0"
    df["season"] = [tr.season_label(d) for d in df["date"]]
    # Pinnacle-like closing odds for every match (the blend's training prices).
    rng = np.random.default_rng(5)
    close = []
    for r in df.itertuples(index=False):
        true = np.clip(np.array(_probs(r.lam, r.mu)) * rng.lognormal(0, 0.05, 5), 0.02, 0.97)
        h2h, ou = true[:3] / true[:3].sum(), true[3:] / true[3:].sum()
        close.append(np.concatenate([1 / (h2h * 1.03), 1 / (ou * 1.03)]))
    df[[f"close_{m}" for m in tr.MARKETS]] = np.round(close, 2)
    hist = make_history(df[df["date"] >= "2022-07-01"])
    now = pd.Timestamp("2030-01-01", tz="UTC")
    cands = backtest.dk_candidates(df, hist, start="2022-07-01", now=now)
    preds = backtest.walk_forward(df, start="2021-01-01")
    pool = {g: mc.training_rows(preds, g) for g in mc.GROUPS}
    blended, fits = backtest.add_blend(cands, pool, min_rows=MIN)
    return df, hist, preds, pool, blended, fits, now


def test_blend_is_added_and_fitted_on_earlier_matches(setup):
    *_, pool, blended, fits, _ = setup
    assert len(pool["h2h"]) > MIN and len(pool["totals"]) > MIN
    assert blended["pb_home"].notna().mean() > 0.8
    sums = blended[["pb_home", "pb_draw", "pb_away"]].sum(axis=1).dropna()
    assert np.allclose(sums, 1)
    ou = blended[["pb_over25", "pb_under25"]].sum(axis=1).dropna()
    assert np.allclose(ou, 1)
    for g, fs in fits.items():
        assert fs, g
        for f in fs:
            used = pool[g][pool[g]["date"] < pd.Timestamp(f["from"])]
            assert f["matches"] == len(used)


def test_blend_has_no_look_ahead(setup):
    df, hist, preds, pool, blended, _, now = setup
    cut = pd.Timestamp("2023-03-01")
    # Scramble every result and price on or after the cut.
    pool2 = {}
    for g, d in pool.items():
        d = d.copy()
        late = d["date"] >= cut
        d.loc[late, "y"] = (d.loc[late, "y"] + 1) % len(mc.GROUPS[g])
        d.loc[late, [c for c in d if c.startswith("mkt_")]] = 1 / len(mc.GROUPS[g])
        pool2[g] = d
    cands = blended.drop(columns=[c for c in blended if c.startswith("pb_")])
    again, _ = backtest.add_blend(cands, pool2, min_rows=MIN)
    early = pd.to_datetime(cands["look_at"], utc=True).dt.tz_convert(None) < cut
    cols = [f"pb_{m}" for m in tr.MARKETS]
    pd.testing.assert_frame_equal(blended.loc[early, cols], again.loc[early, cols])


def test_strategies_sweep_and_settlement(setup):
    *_, blended, _, _ = setup
    st = backtest.dk_strategies(blended, threshold=0.05, thresholds=(0.02, 0.05, 0.12))
    assert set(st) == {"raw", "blend"}
    for s in st.values():
        assert len(s["sweep"]) == 6
        for r in s["sweep"]:
            assert {"threshold", "max_odds", "trades", "roi_ci95"} <= set(r)
            if r.get("settled", 0) > 1:
                lo, hi = r["roi_ci95"]
                assert lo <= r["roi"] <= hi and "avg_p" in r
    raw = backtest.dk_trades(blended, 0.02)
    blend = backtest.dk_trades(blended, 0.02, probs="blend")
    assert len(blend) > 0
    # The blend measures edge with pb_, and settles exactly like the raw strategy.
    for t in blend.to_dict("records"):
        c = blended[
            (blended["home"] == t["home"])
            & (blended["away"] == t["away"])
            & (blended["look"] == t["look"])
        ].iloc[0]
        assert t["model_p"] == pytest.approx(c[f"pb_{t['market']}"], abs=1e-4)
        assert t["status"] == tr.settle(t, c["home_goals"], c["away_goals"])["status"]
    # The raw strategy is unchanged by the blend columns.
    plain = blended.drop(columns=[c for c in blended if c.startswith("pb_")])
    cols = ["id", "market", "odds", "model_p", "edge", "status", "profit"]
    pd.testing.assert_frame_equal(raw[cols], backtest.dk_trades(plain, 0.02)[cols])


def test_log_loss_reports_blend(setup):
    *_, blended, _, _ = setup
    ll = backtest.dk_log_loss(blended)
    assert ll["blend_matches"] > 50
    for k in ("model_on_blend", "blend", "draftkings_on_blend"):
        assert 0.5 < ll[k] < 1.5
    assert ll["totals"]["blend_matches"] > 50
