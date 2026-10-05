import numpy as np
import pytest
from scipy.optimize import check_grad

from soccer_stats.models import DixonColes
from soccer_stats.models.dixon_coles import _neg_loglik


def test_gradient_matches_finite_differences(league):
    df, _ = league
    teams = sorted(set(df["home"]))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    args = (
        df["home"].map(idx).to_numpy(),
        df["away"].map(idx).to_numpy(),
        df["home_goals"].to_numpy(),
        df["away_goals"].to_numpy(),
        np.ones(len(df)),
        n,
    )
    theta = np.random.default_rng(0).normal(0, 0.2, 2 * n + 3)
    theta[-1] = -0.08
    err = check_grad(lambda t: _neg_loglik(t, *args)[0], lambda t: _neg_loglik(t, *args)[1], theta)
    assert err < 1e-2


def test_recovers_true_parameters(league):
    df, truth = league
    model = DixonColes(xi=0.0).fit(df)
    att = np.array([model.attack[t] for t in truth["attack"]])
    assert np.corrcoef(att, list(truth["attack"].values()))[0, 1] > 0.9
    assert model.params["home_adv"] == pytest.approx(truth["home_adv"], abs=0.1)


def test_score_matrix_is_distribution(league):
    df, _ = league
    model = DixonColes().fit(df)
    m = model.score_matrix("T00", "T01")
    assert m.sum() == pytest.approx(1.0)
    assert (m >= 0).all()
    # Unseen teams fall back to average ratings rather than erroring.
    assert model.score_matrix("Newcomer", "T01").sum() == pytest.approx(1.0)
