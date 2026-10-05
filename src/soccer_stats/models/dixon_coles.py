"""Dixon-Coles (1997) bivariate Poisson model with time decay.

    log(lambda_home) = intercept + home_adv + attack[home] + defence[away]
    log(lambda_away) = intercept +            attack[away] + defence[home]

`defence` is "goals conceded" strength (higher = worse defence). Low-scoring
results (0-0, 1-0, 0-1, 1-1) get the Dixon-Coles tau correction controlled by
`rho`, and each match is weighted by exp(-xi * age_in_days) so recent form
counts more.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

# Penalty that pins sum(attack) = sum(defence) = 0 so the model is identifiable.
_ID_PENALTY = 100.0


@dataclass
class DixonColes:
    xi: float = 0.0019  # time decay per day (~1 year half-life ≈ 0.0019)
    max_goals: int = 10
    teams: list[str] = field(default_factory=list)
    params: dict[str, float] = field(default_factory=dict)
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)

    # ---------- fitting ----------

    def fit(self, matches: pd.DataFrame, as_of: pd.Timestamp | None = None) -> DixonColes:
        """Fit on matches with columns date, home, away, home_goals, away_goals."""
        self.teams = sorted(set(matches["home"]) | set(matches["away"]))
        idx = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)

        hi = matches["home"].map(idx).to_numpy()
        ai = matches["away"].map(idx).to_numpy()
        x = matches["home_goals"].to_numpy()
        y = matches["away_goals"].to_numpy()
        as_of = as_of or matches["date"].max()
        age = (as_of - matches["date"]).dt.days.to_numpy()
        w = np.exp(-self.xi * age)

        x0 = np.concatenate([np.zeros(2 * n), [0.2, 0.3, -0.05]])  # att, def, intercept, home, rho
        bounds = [(None, None)] * (2 * n + 2) + [(-0.3, 0.3)]
        res = minimize(
            _neg_loglik,
            x0,
            args=(hi, ai, x, y, w, n),
            jac=True,
            method="L-BFGS-B",
            bounds=bounds,
        )
        if not res.success:
            raise RuntimeError(f"Dixon-Coles fit failed: {res.message}")

        att, dfn = res.x[:n], res.x[n : 2 * n]
        self.attack = dict(zip(self.teams, att, strict=True))
        self.defence = dict(zip(self.teams, dfn, strict=True))
        self.params = {
            "intercept": res.x[2 * n],
            "home_adv": res.x[2 * n + 1],
            "rho": res.x[2 * n + 2],
        }
        return self

    # ---------- prediction ----------

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        # Teams unseen in training (e.g. promoted sides) get league-average ratings.
        p = self.params
        lam = np.exp(
            p["intercept"]
            + p["home_adv"]
            + self.attack.get(home, 0.0)
            + self.defence.get(away, 0.0)
        )
        mu = np.exp(p["intercept"] + self.attack.get(away, 0.0) + self.defence.get(home, 0.0))
        return float(lam), float(mu)

    def score_matrix(self, home: str, away: str) -> np.ndarray:
        """P(home scores i, away scores j) for i, j in 0..max_goals."""
        lam, mu = self.expected_goals(home, away)
        goals = np.arange(self.max_goals + 1)
        m = np.outer(poisson.pmf(goals, lam), poisson.pmf(goals, mu))
        rho = self.params["rho"]
        m[0, 0] *= 1 - lam * mu * rho
        m[0, 1] *= 1 + lam * rho
        m[1, 0] *= 1 + mu * rho
        m[1, 1] *= 1 - rho
        return m / m.sum()


def _neg_loglik(theta, hi, ai, x, y, w, n):
    att, dfn = theta[:n], theta[n : 2 * n]
    c, h, rho = theta[2 * n], theta[2 * n + 1], theta[2 * n + 2]

    eta_h = c + h + att[hi] + dfn[ai]
    eta_a = c + att[ai] + dfn[hi]
    lam, mu = np.exp(eta_h), np.exp(eta_a)

    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)

    tau = np.ones_like(lam)
    tau[m00] = 1 - lam[m00] * mu[m00] * rho
    tau[m01] = 1 + lam[m01] * rho
    tau[m10] = 1 + mu[m10] * rho
    tau[m11] = 1 - rho
    tau = np.maximum(tau, 1e-10)

    ll = w * (np.log(tau) + x * eta_h - lam + y * eta_a - mu)

    # d log(tau) / d eta_h, d eta_a, d rho
    dt_h = np.zeros_like(lam)
    dt_a = np.zeros_like(lam)
    dt_r = np.zeros_like(lam)
    dt_h[m00] = -lam[m00] * mu[m00] * rho / tau[m00]
    dt_a[m00] = dt_h[m00]
    dt_r[m00] = -lam[m00] * mu[m00] / tau[m00]
    dt_h[m01] = lam[m01] * rho / tau[m01]
    dt_r[m01] = lam[m01] / tau[m01]
    dt_a[m10] = mu[m10] * rho / tau[m10]
    dt_r[m10] = mu[m10] / tau[m10]
    dt_r[m11] = -1 / tau[m11]

    g_h = w * (x - lam + dt_h)
    g_a = w * (y - mu + dt_a)

    grad = np.empty_like(theta)
    grad[:n] = np.bincount(hi, g_h, n) + np.bincount(ai, g_a, n)
    grad[n : 2 * n] = np.bincount(ai, g_h, n) + np.bincount(hi, g_a, n)
    grad[2 * n] = g_h.sum() + g_a.sum()
    grad[2 * n + 1] = g_h.sum()
    grad[2 * n + 2] = (w * dt_r).sum()

    sa, sd = att.sum(), dfn.sum()
    obj = -ll.sum() + _ID_PENALTY * (sa**2 + sd**2)
    grad = -grad
    grad[:n] += 2 * _ID_PENALTY * sa
    grad[n : 2 * n] += 2 * _ID_PENALTY * sd
    return obj, grad
