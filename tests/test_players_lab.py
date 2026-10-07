"""Player shot lines through the lab's metrics: de-margining without look-ahead, the
locked holdout, and the raw-price comparison."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats import player_lab as pl


def lines(n_matches=400, seed=0, margin=1.35, informative=True):
    """Over-only lines: the true chance q; the book sees q with noise and prices
    1/(seen·margin); the model knows q more closely when informative, else it is noise
    around the book's view."""
    rng = np.random.default_rng(seed)
    rows = []
    k0 = pd.Timestamp("2023-08-12 14:00", tz="UTC")
    for m in range(n_matches):
        k = k0 + pd.Timedelta(days=2 * m)
        for j in range(12):
            q = float(np.clip(rng.beta(2, 5), 0.03, 0.9))
            seen = float(np.clip(q + rng.normal(0, 0.08), 0.02, 0.9))  # the book's view
            imp = min(seen * margin, 0.97)
            won = int(rng.random() < q)
            noise = rng.normal(0, 0.03)
            pm = float(
                np.clip((q if informative else seen * rng.uniform(0.6, 1.4)) + noise, 0.01, 0.99)
            )
            for kind, drift in (("look", 1.0), ("close", 1.0)):
                rows.append(
                    {
                        "kind": kind,
                        "kickoff": k,
                        "home": f"H{m}",
                        "away": f"A{m}",
                        "player": f"P{j}",
                        "player_id": f"{m}_{j}",
                        "team": f"H{m}",
                        "position": "FWD",
                        "started": True,
                        "market": "player_shots",
                        "line": 1.0,
                        "side": "over",
                        "odds": round(1 / (imp * drift), 3),
                        "implied": imp * drift,
                        "p_model": pm,
                        "p": pm,
                        "exp_count": 1.0,
                        "actual": won,
                        "won": won,
                    }
                )
    return pd.DataFrame(rows)


def test_recalibrate_uses_only_earlier_lines():
    df = lines(200)
    look = df[df["kind"] == "look"].reset_index(drop=True)
    a = pl.recalibrate(look["implied"], look["won"], look["kickoff"])
    later = look["kickoff"] >= look["kickoff"].quantile(0.6)
    flipped = look["won"].where(~later, 1 - look["won"])
    b = pl.recalibrate(look["implied"], flipped, look["kickoff"])
    cut = look.loc[later, "kickoff"].min()
    before = (look["kickoff"] < cut).to_numpy()
    np.testing.assert_allclose(a[before], b[before])  # future outcomes change nothing earlier
    ok = ~np.isnan(a)
    assert abs(np.nanmean(a) - look.loc[ok, "won"].mean()) < 0.02  # margin taken out


def test_holdout_rows_are_never_scored():
    df = lines(500)  # runs past 2025-07-01
    assert (df["kickoff"] >= pl.HOLDOUT_START).any()
    r = pl.run(df)
    assert r["holdout_opened"] is False
    assert r["seasons"] == ["2324", "2425"]
    n_dev = (df["kickoff"] < pl.HOLDOUT_START).sum()
    assert r["development_lines"] == n_dev


@pytest.mark.parametrize("informative", [True, False])
def test_fair_market_vs_raw_price(informative):
    df = lines(300, seed=1, informative=informative)
    rows = pl.strategy_rows(df, "blend_lineup")
    fair = pl.evaluate(rows, "fair", n_boot_blend=30)
    raw = pl.evaluate(rows, "raw", n_boot_blend=30)
    # Against the raw over-only price, any calibrated model "beats the market"...
    assert raw["gain_vs_market"]["mean"] > 0
    # ...against the de-margined price only an informative one does.
    if informative:
        assert fair["gain_vs_market"]["range"][0] > 0
    else:
        assert fair["gain_vs_market"]["mean"] < 0.005
    assert fair["passes"] is False  # bet at the close: no CLV, so no pass
