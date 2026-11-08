import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from soccer_stats.lab import model_assumptions as ma


def _preds(n=3000, seed=0, corr=0.0):
    rng = np.random.default_rng(seed)
    lh = rng.uniform(0.8, 2.2, n)
    la = rng.uniform(0.6, 1.8, n)
    hg = rng.poisson(lh)
    ag = rng.poisson(la)
    k = np.arange(11)
    mats = [np.outer(poisson.pmf(k, a), poisson.pmf(k, b)) for a, b in zip(lh, la, strict=True)]
    return pd.DataFrame(
        {"home_goals": hg, "away_goals": ag, "exp_home": lh, "exp_away": la, "matrix": mats}
    )


def test_poisson_goals_look_poisson():
    g = ma.goals_checks(_preds(), n_boot=200)
    assert g["dispersion_ci95"][0] < 1 < g["dispersion_ci95"][1]
    assert g["residual_corr_ci95"][0] < 0 < g["residual_corr_ci95"][1]
    c = g["low_score_cells"]["0-0"]
    assert c["obs_minus_model_ci95"][0] < 0 < c["obs_minus_model_ci95"][1]
    assert c["model"] == pytest.approx(c["independent"])


def test_overdispersed_counts_show_it():
    p = _preds()
    rng = np.random.default_rng(1)
    p["home_goals"] = rng.negative_binomial(2, 2 / (2 + p["exp_home"]))
    g = ma.goals_checks(p, n_boot=200)
    assert g["dispersion_ci95"][0] > 1


def test_corner_dispersion_under_nb2_is_one():
    rng = np.random.default_rng(2)
    n, alpha = 4000, 0.08
    m = rng.uniform(3, 7, (n, 2))
    r = 1 / alpha
    y = rng.negative_binomial(r, r / (r + m))
    rows = pd.DataFrame(
        {
            "home_corners": y[:, 0],
            "away_corners": y[:, 1],
            "m_home": m[:, 0],
            "m_away": m[:, 1],
            "alpha_home": alpha,
            "alpha_away": alpha,
        }
    )
    c = ma.corner_checks(rows, n_boot=200)
    assert c["dispersion_nb2_ci95"][0] < 1 < c["dispersion_nb2_ci95"][1]
    assert c["dispersion_poisson"] > 1.2


def test_corner_walk_fits_only_on_earlier_matches():
    rng = np.random.default_rng(3)
    teams = [f"T{i}" for i in range(10)]
    rows = []
    d = pd.Timestamp("2016-08-01")
    for _ in range(900):
        h, a = rng.choice(teams, 2, replace=False)
        rows.append({"date": d, "home": h, "away": a, "home_corners": int(rng.poisson(5)),
                     "away_corners": int(rng.poisson(4))})  # fmt: skip
        d += pd.Timedelta(days=1)
    cm = pd.DataFrame(rows)
    out = ma.corner_walk(cm, "2017-07-01", "2018-07-01")
    assert len(out) > 100
    assert (pd.to_datetime(out["fit_to"]) < pd.to_datetime(out["date"])).all()
