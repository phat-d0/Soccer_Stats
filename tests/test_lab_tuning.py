import functools

import numpy as np
import pandas as pd
import pytest

from soccer_stats import backtest
from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab import tuning_bias as tbias
from soccer_stats.lab.harness import Holdout, HoldoutLocked
from soccer_stats.models import DixonColes


def _league(seasons=3, teams=10, seed=1, first=2016):
    rng = np.random.default_rng(seed)
    names = [f"T{i}" for i in range(teams)]
    strength = rng.normal(0, 0.35, teams)
    rows = []
    for s in range(seasons):
        day = pd.Timestamp(f"{first + s}-08-09")
        fixtures = [(h, a) for h in range(teams) for a in range(teams) if h != a]
        rng.shuffle(fixtures)
        for h, a in fixtures:
            lh = np.exp(0.25 + strength[h] - strength[a])
            la = np.exp(0.05 + strength[a] - strength[h])
            hg, ag = int(rng.poisson(lh)), int(rng.poisson(la))
            # A fair-ish price from the true strengths, with a margin.
            p = np.array([0.45 + 0.3 * (strength[h] - strength[a]), 0.27, 0.0])
            p[0] = np.clip(p[0], 0.1, 0.8)
            p[2] = 1 - p[0] - p[1]
            rows.append(
                {
                    "date": day,
                    "season": f"{(first + s) % 100:02d}{(first + s + 1) % 100:02d}",
                    "home": names[h],
                    "away": names[a],
                    "home_goals": hg,
                    "away_goals": ag,
                    "home_xg": float(rng.gamma(4, lh / 4)),
                    "away_xg": float(rng.gamma(4, la / 4)),
                    "odds_home": 0.97 / p[0],
                    "odds_draw": 0.97 / p[1],
                    "odds_away": 0.97 / p[2],
                    "close_home": 0.975 / p[0],
                    "close_draw": 0.975 / p[1],
                    "close_away": 0.975 / p[2],
                }
            )
            day += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


def test_walk_reproduces_backtest_walk_forward_at_the_defaults():
    m = _league()
    start = "2017-08-01"
    cfg = tb.Config(xi=0.0019, xg_weight=0.7, window=730, min_team=6)
    mine, refits = tb.walk(m, cfg, start=start, end="2030-01-01")
    ref = backtest.walk_forward(
        m, start=start, model_factory=functools.partial(DixonColes, xg_weight=0.7)
    )
    assert len(mine) == len(ref) > 50
    a = mine.sort_values(["date", "home"]).reset_index(drop=True)
    b = ref.sort_values(["date", "home"]).reset_index(drop=True)
    for s in tb.SIDES:
        np.testing.assert_allclose(a[f"p_{s}"], b[f"p_{s}"], rtol=1e-9)
    assert any(r.get("ll_in_recent") is not None for r in refits)
    assert all("ratings" in r for r in refits)


def test_walk_trains_only_on_earlier_matches_within_the_window():
    m = _league()
    cfg = tb.Config(window=200)
    preds, refits = tb.walk(m, cfg, start="2017-08-01", end="2030-01-01", insample_every=1)
    for r in refits:
        # every recorded training size is the count of matches in [as_of - window, as_of)
        lo = r["as_of"]
        n = int(((m["date"] < lo) & (m["date"] >= lo - pd.Timedelta(days=200))).sum())
        assert r["n_train"] == n
    assert (preds["date"] >= preds["refit"]).all()
    assert (preds["date"] < preds["refit"] + pd.Timedelta(days=7)).all()


def test_walk_refuses_the_locked_holdout():
    m = _league(first=2023, seasons=2)
    with pytest.raises(HoldoutLocked):
        tb.walk(m, tb.Config(), start="2024-08-01", end="2030-01-01", holdout=Holdout(tb.LOCK))


def test_model_and_blend_rows_and_decomposition():
    m = _league(seasons=4)
    preds = {}
    ins = {}
    for w in tbias.WINDOWS:
        p, refits = tb.walk(m, tb.Config(window=w), start="2017-08-01", end="2030-01-01")
        preds[w] = p
        ins[str(w)] = tbias._insample(refits)
    rows = tbias.model_rows(preds, m)
    assert len(rows) > 50
    assert {"ll_close", "ll_early", "ll_w730"} <= set(rows.columns)
    dec = tbias.decompose(rows, ins, n_boot=100)
    w = dec["windows"]["730"]
    assert w["bias"] == pytest.approx(w["oos"] - dec["ll_close"])
    assert w["variance"] == pytest.approx(w["oos"] - w["in_recent"])
    assert dec["dominant"] in ("bias", "variance")
    brows, bins = tbias.blend_rows(preds[730], m, caps=(1, None), min_rows=60,
                                   start="2017-08-01", end="2030-01-01")  # fmt: skip
    assert len(brows) > 0 and {"b1", "ball"} <= set(bins)
    bd = tbias.blend_decompose(brows, bins, n_boot=100)
    assert bd["caps"]["ball"]["oos_close"] > 0


def test_blend_fits_use_only_matches_kicked_off_before_the_block():
    m = _league(seasons=4)
    p, _ = tb.walk(m, tb.Config(), start="2017-08-01", end="2030-01-01")
    seen = []
    orig = tbias.mc.fit

    def spy(market, model, y, min_rows):
        seen.append(len(y))
        return orig(market, model, y, min_rows)

    tbias.mc.fit = spy
    try:
        brows, bins = tbias.blend_rows(p, m, caps=(None,), min_rows=60, start="2017-08-01",
                                       end="2030-01-01")  # fmt: skip
    finally:
        tbias.mc.fit = orig
    # Each fit's row count equals the matches before its block start.
    d = tb.with_prices(p, m)
    dates = pd.to_datetime(d["date"])
    for rec in bins["ball"]:
        lo = pd.Timestamp(rec["from"])
        assert rec["rows"] == int((dates < lo).sum())


def test_stability_measures():
    r1 = {"ratings": {f"T{i}": (0.1 * i, -0.1 * i) for i in range(8)}}
    r2 = {"ratings": {f"T{i}": (0.1 * i + 0.01, -0.1 * i) for i in range(8)}}
    s = tbias.stability([r1, r2, r1, r2, r1], lag=4)
    assert s["median_weekly_change"]["attack"] == pytest.approx(0.01)
    assert s["median_weekly_change"]["defence"] == pytest.approx(0.0)
    assert s["corr_28d"]["attack"] == pytest.approx(1.0)
