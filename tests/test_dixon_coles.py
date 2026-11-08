import numpy as np
import pandas as pd
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


def _attack_error(df, truth, xg_weight):
    model = DixonColes(xi=0.0, xg_weight=xg_weight).fit(df)
    fitted = np.array([model.attack[t] for t in truth["attack"]])
    return np.sqrt(np.mean((fitted - np.array(list(truth["attack"].values()))) ** 2))


def test_xg_blend_recovers_strengths_better(league):
    # xG is a less noisy signal of the true scoring rate than goals, so ratings fit
    # with it should land closer to the truth.
    df, truth = league
    one_season = df[df["date"] < df["date"].min() + pd.Timedelta(days=300)]
    assert _attack_error(one_season, truth, 0.7) < _attack_error(one_season, truth, 0.0)


def test_xg_weight_ignored_without_xg_columns(league):
    df, _ = league
    plain = df.drop(columns=["home_xg", "away_xg"])
    a = DixonColes(xg_weight=0.7).fit(plain).params
    b = DixonColes(xg_weight=0.0).fit(plain).params
    assert a == pytest.approx(b)


def test_gradient_with_xg_targets(league):
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
        df["home_xg"].to_numpy(),
        df["away_xg"].to_numpy(),
    )
    theta = np.random.default_rng(1).normal(0, 0.2, 2 * n + 3)
    theta[-1] = -0.08
    f = lambda t: _neg_loglik(t, *args)[0]  # noqa: E731
    g = lambda t: _neg_loglik(t, *args)[1]  # noqa: E731
    assert check_grad(f, g, theta) < 1e-2


def test_default_rho_fit_is_pinned(league):
    # rho_option=None must stay today's fit (the live app). The pinned values hold to the
    # optimizer's convergence, which differs in the 6th digit between machines (CI saw the
    # intercept 1.1e-5 off in relative terms), so 1e-4; the exact checks below compare fits
    # made on the same machine.
    df, _ = league
    m = DixonColes().fit(df)
    assert m.params["intercept"] == pytest.approx(0.06761537680066211, rel=1e-4)
    assert m.params["home_adv"] == pytest.approx(0.2238702919763295, rel=1e-4)
    assert m.params["rho"] == pytest.approx(0.11335450748865242, rel=1e-4)
    expected = [[0.19111285, 0.09239938], [0.25285128, 0.08630114]]
    np.testing.assert_allclose(m.score_matrix("T00", "T01")[:2, :2], expected, rtol=1e-4)
    m2 = DixonColes(rho_option=None).fit(df)
    np.testing.assert_array_equal(m.score_matrix("T00", "T01"), m2.score_matrix("T00", "T01"))
    # A zero penalty is the same fit as None.
    m3 = DixonColes(rho_option={"penalty": 0.0}).fit(df)
    assert m3.params["rho"] == m.params["rho"]


def test_rho_fixed_holds_the_value(league):
    df, _ = league
    off = DixonColes(rho_option={"fixed": 0.0}).fit(df)
    assert off.params["rho"] == 0.0
    # rho = 0 is a plain double Poisson: the matrix is the outer product of two Poissons.
    lam, mu = off.expected_goals("T00", "T01")
    from scipy.stats import poisson

    g = np.arange(off.max_goals + 1)
    plain = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    np.testing.assert_allclose(off.score_matrix("T00", "T01"), plain / plain.sum(), rtol=1e-12)
    held = DixonColes(rho_option={"fixed": -0.1}).fit(df)
    assert held.params["rho"] == pytest.approx(-0.1)


def test_rho_penalty_shrinks_toward_zero(league):
    df, _ = league
    free = abs(DixonColes().fit(df).params["rho"])
    some = abs(DixonColes(rho_option={"penalty": 50.0}).fit(df).params["rho"])
    lots = abs(DixonColes(rho_option={"penalty": 5000.0}).fit(df).params["rho"])
    assert lots < some < free
    assert lots < 0.01


def test_rho_penalty_gradient(league):
    df, _ = league
    teams = sorted(set(df["home"]))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    x, y = df["home_goals"].to_numpy(), df["away_goals"].to_numpy()
    args = (
        df["home"].map(idx).to_numpy(),
        df["away"].map(idx).to_numpy(),
        x,
        y,
        np.ones(len(df)),
        n,
        x.astype(float),
        y.astype(float),
        None,
        0.0,
        25.0,
    )
    theta = np.random.default_rng(1).normal(0, 0.2, 2 * n + 3)
    theta[-1] = 0.07
    err = check_grad(lambda t: _neg_loglik(t, *args)[0], lambda t: _neg_loglik(t, *args)[1], theta)
    assert err < 1e-2


@pytest.mark.parametrize("bad", [{"fixed": 0.5}, {"penalty": -1}, {"other": 1}, {}, 0.1])
def test_rho_option_rejects_bad_values(league, bad):
    df, _ = league
    with pytest.raises(ValueError):
        DixonColes(rho_option=bad).fit(df)
