"""Count models for player shots and shots on target.

Shots: negative binomial (NB2: variance = mean + alpha * mean^2) whose mean is
minutes / 90 x exp(intercept + beta . factors). log_rate (his shrunk shots per 90) is one
of the factors, so a coefficient near 1 means the other factors adjust his own rate.
The coefficients are L2-regularised (like the match model's ratings), fitted by maximum
likelihood with an analytic gradient.

Shots on target, two candidates (keep whichever has the lower out-of-sample log loss):
* "thin":  each shot is on target with his shrunk on-target rate, so shots on target is
           negative binomial with the shots mean x that rate and the same dispersion;
* "count": its own negative binomial on the same factors plus log(on-target rate).

Bets are void if a player doesn't play, so prices are conditional on playing: a mix of
"starts" (expected minutes as a starter) and "comes on" (as a substitute), weighted by
his chance of starting given he plays. With a known lineup, that chance is 0 or 1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import digamma, gammaln

from soccer_stats.factors import ALL_FACTORS

MAX_COUNT = 15  # the distribution is computed on 0..MAX_COUNT (the last holds the tail)


def nb_pmf(mean: np.ndarray, alpha: float, kmax: int = MAX_COUNT) -> np.ndarray:
    """NB2 probabilities for k = 0..kmax (rows sum to 1; the last bucket is k >= kmax)."""
    m = np.atleast_1d(np.asarray(mean, dtype=float))[:, None]
    k = np.arange(kmax + 1)[None, :]
    r = 1.0 / max(alpha, 1e-6)
    logp = (
        gammaln(k + r)
        - gammaln(r)
        - gammaln(k + 1)
        + r * np.log(r / (r + m))
        + k * np.log(np.where(m > 0, m / (r + m), 1e-300))
    )
    p = np.exp(logp)
    p[:, -1] = np.clip(1 - p[:, :-1].sum(axis=1), 0, None)
    return p / p.sum(axis=1, keepdims=True)


class NBRegression:
    """Regularised negative binomial regression with a log(exposure) offset."""

    def __init__(self, features: list[str], l2: float = 1.0):
        self.features = list(features)
        self.l2 = l2
        self.coef_: np.ndarray | None = None
        self.alpha_ = 0.5
        self.mu_ = None
        self.sd_ = None

    def _x(self, df: pd.DataFrame) -> np.ndarray:
        x = df[self.features].to_numpy(dtype=float) if self.features else np.zeros((len(df), 0))
        return np.column_stack([np.ones(len(df)), (x - self.mu_) / self.sd_])

    def fit(self, df: pd.DataFrame, y: np.ndarray, exposure: np.ndarray) -> NBRegression:
        x = df[self.features].to_numpy(dtype=float) if self.features else np.zeros((len(df), 0))
        self.mu_ = x.mean(axis=0) if len(x) else np.zeros(0)
        self.sd_ = np.where(x.std(axis=0) > 1e-9, x.std(axis=0), 1.0) if len(x) else np.ones(0)
        X = self._x(df)
        y = np.asarray(y, dtype=float)
        off = np.log(np.clip(exposure, 1e-6, None))
        n = len(y)
        pen = np.r_[0.0, np.full(X.shape[1] - 1, self.l2)]

        def f(theta):
            b, log_r = theta[:-1], theta[-1]
            r = np.exp(log_r)
            eta = X @ b + off
            m = np.exp(np.clip(eta, -20, 10))
            ll = (
                gammaln(y + r)
                - gammaln(r)
                - gammaln(y + 1)
                + r * (log_r - np.log(r + m))
                + y * (eta - np.log(r + m))
            )
            g_eta = (y - m) * r / (r + m)
            g_r = digamma(y + r) - digamma(r) + log_r - np.log(r + m) + 1 - (r + y) / (r + m)
            loss = -ll.sum() / n + 0.5 * (pen * b**2).sum() / n
            grad_b = -(X.T @ g_eta) / n + pen * b / n
            grad_lr = -(g_r * r).sum() / n
            return loss, np.r_[grad_b, grad_lr]

        b0 = np.zeros(X.shape[1])
        b0[0] = np.log(max(y.sum() / np.clip(exposure, 1e-6, None).sum(), 1e-3))
        res = minimize(f, np.r_[b0, 0.0], jac=True, method="L-BFGS-B")
        self.coef_ = res.x[:-1]
        self.alpha_ = float(1 / np.exp(res.x[-1]))
        return self

    def rate(self, df: pd.DataFrame) -> np.ndarray:
        """Expected count per unit of exposure (per 90 minutes)."""
        return np.exp(np.clip(self._x(df) @ self.coef_, -20, 5))

    def coefficients(self) -> dict[str, float]:
        """Coefficients on the original (unstandardised) scale."""
        if self.coef_ is None:
            return {}
        return {
            f: float(c / s) for f, c, s in zip(self.features, self.coef_[1:], self.sd_, strict=True)
        }


class PlayerShotModel:
    """Shots and shots-on-target distributions per player, conditional on playing."""

    def __init__(self, factors: list[str] | None = None, l2: float = 2.0, sot_method: str = "thin"):
        self.factors = list(ALL_FACTORS if factors is None else factors)
        self.l2 = l2
        self.sot_method = sot_method
        self.shots = NBRegression(self.factors, l2)
        self.sot = NBRegression([*self.factors, "log_sot_rate"], l2)

    @staticmethod
    def _prep(df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(log_sot_rate=np.log(df["sot_rate"].clip(0.05, 0.95)))

    def fit(self, train: pd.DataFrame) -> PlayerShotModel:
        df = self._prep(train)
        exp = df["minutes"].to_numpy() / 90
        self.shots.fit(df, df["shots"].to_numpy(), exp)
        self.sot.fit(df, df["sot"].to_numpy(), exp)
        return self

    def means(self, df: pd.DataFrame, lineup_known: bool) -> pd.DataFrame:
        """Expected shots / shots on target if he starts and if he comes on, and his chance
        of starting given he plays."""
        d = self._prep(df)
        rate = self.shots.rate(d)
        sot_rate = (
            rate * d["sot_rate"].to_numpy() if self.sot_method == "thin" else self.sot.rate(d)
        )
        p_start = (
            d["started"].astype(float).to_numpy() if lineup_known else d["start_rate"].to_numpy()
        )
        st, sb = d["start_minutes"].to_numpy() / 90, d["sub_minutes"].to_numpy() / 90
        return pd.DataFrame(
            {
                "p_start": p_start,
                "shots_start": rate * st,
                "shots_sub": rate * sb,
                "sot_start": sot_rate * st,
                "sot_sub": sot_rate * sb,
            },
            index=df.index,
        )

    def distributions(self, df: pd.DataFrame, lineup_known: bool = False) -> dict[str, np.ndarray]:
        """P(k shots) and P(k shots on target), k = 0..MAX_COUNT, given he plays."""
        m = self.means(df, lineup_known)
        ps = m["p_start"].to_numpy()[:, None]
        a_sh = self.shots.alpha_
        a_sot = self.shots.alpha_ if self.sot_method == "thin" else self.sot.alpha_
        shots = ps * nb_pmf(m["shots_start"], a_sh) + (1 - ps) * nb_pmf(m["shots_sub"], a_sh)
        sot = ps * nb_pmf(m["sot_start"], a_sot) + (1 - ps) * nb_pmf(m["sot_sub"], a_sot)
        return {"shots": shots, "sot": sot, "means": m}


def prob_over(pmf: np.ndarray, line: float) -> np.ndarray:
    """P(count > line) for half-point lines (e.g. 0.5, 1.5)."""
    k = int(np.floor(line))
    return pmf[:, k + 1 :].sum(axis=1)


def baseline_pmf(df: pd.DataFrame, col: str) -> np.ndarray:
    """The benchmark: Poisson at his season-to-date average per appearance (position
    average before his first appearance of the season)."""
    from scipy.stats import poisson

    mean = df[f"season_avg_{col}"].to_numpy()
    k = np.arange(MAX_COUNT + 1)
    p = poisson.pmf(k[None, :], np.clip(mean, 1e-3, None)[:, None])
    p[:, -1] = np.clip(1 - p[:, :-1].sum(axis=1), 0, None)
    return p / p.sum(axis=1, keepdims=True)


def season_averages(df: pd.DataFrame) -> pd.DataFrame:
    """season_avg_shots / season_avg_sot: his per-appearance average earlier this season."""
    out = df.copy()
    g = out.groupby(["player_id", "season"])
    for col in ("shots", "sot"):
        s = g[col].cumsum() - out[col]
        n = g.cumcount()
        pos_avg = (
            out.groupby("position")[col]
            .transform(lambda x: x.expanding().mean().shift(1))
            .fillna(out[col].mean())
        )
        out[f"season_avg_{col}"] = np.where(n > 0, s / n.replace(0, 1), pos_avg)
    return out
