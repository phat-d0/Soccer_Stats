"""Bake-off candidates for 1X2. Each has fit(train) and predict(test) -> (n, 3).

(a) DixonColesBaseline: the live model's walk-forward chances, computed beforehand
    (backtest.walk_forward, weekly refits); fit does nothing.
(b) HierPoisson: team attack/defence and team home edges with Gaussian priors (MAP,
    the empirical-Bayes prior scale chosen by nested tuning), time-decayed, fitted to
    a goals/xG blend; independent Poisson scores give 1X2.
(c) GBM: LightGBM multiclass on features.FEATURES.
(d) Logit: multinomial logistic regression on the same features (standardized).
(e) the stack with the opening price is built in run.py from (a)-(d)'s predictions.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

from soccer_stats.lab.features import FEATURES

MAX_GOALS = 10


def one_x_two(lh: np.ndarray, la: np.ndarray) -> np.ndarray:
    """Home/draw/away chances from independent Poisson means."""
    g = np.arange(MAX_GOALS + 1)
    ph = poisson.pmf(g[None, :], lh[:, None])
    pa = poisson.pmf(g[None, :], la[:, None])
    m = ph[:, :, None] * pa[:, None, :]
    hw = np.tril(np.ones((len(g), len(g))), -1)  # home goals > away goals
    p = np.stack([(m * hw).sum((1, 2)), np.trace(m, axis1=1, axis2=2), (m * hw.T).sum((1, 2))], 1)
    return p / p.sum(1, keepdims=True)


@dataclass
class DixonColesBaseline:
    cols: tuple = ("dc_p_home", "dc_p_draw", "dc_p_away")

    def fit(self, train: pd.DataFrame) -> None:
        pass

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return test[list(self.cols)].to_numpy(float)


@dataclass
class HierPoisson:
    half_life: float = 180.0  # days
    prior_sd: float = 0.3  # attack/defence prior scale
    home_sd: float = 0.05  # team-specific home edge prior scale (0 = one shared edge)
    xg_weight: float = 0.7
    lookback: int = 730

    def fit(self, train: pd.DataFrame) -> None:
        now = pd.to_datetime(train["time"]).max() + pd.Timedelta(days=1)
        d = train[pd.to_datetime(train["time"]) >= now - pd.Timedelta(days=self.lookback)]
        teams = sorted(set(d["home"]) | set(d["away"]))
        ix = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        hi, ai = d["home"].map(ix).to_numpy(), d["away"].map(ix).to_numpy()
        th, ta = d["home_goals"].to_numpy(float), d["away_goals"].to_numpy(float)
        hx, ax = d["home_xg"].to_numpy(float), d["away_xg"].to_numpy(float)
        has = np.isfinite(hx) & np.isfinite(ax)
        th[has] = self.xg_weight * hx[has] + (1 - self.xg_weight) * th[has]
        ta[has] = self.xg_weight * ax[has] + (1 - self.xg_weight) * ta[has]
        age = (now - pd.to_datetime(d["time"])).dt.days.to_numpy(float)
        w = 0.5 ** (age / self.half_life)
        w = w / w.sum() * len(w)
        hs = self.home_sd

        def unpack(x):
            return x[0], x[1], x[2 : 2 + n], x[2 + n : 2 + 2 * n], x[2 + 2 * n :]

        def loss(x):
            mu, h, att, dfn, hte = unpack(x)
            eh = mu + h + att[hi] + dfn[ai] + (hte[hi] if hs > 0 else 0)
            ea = mu + att[ai] + dfn[hi]
            lh, la = np.exp(eh), np.exp(ea)
            nll = (w * (lh - th * eh + la - ta * ea)).sum()
            pen = ((att**2).sum() + (dfn**2).sum()) / (2 * self.prior_sd**2)
            if hs > 0:
                pen += (hte**2).sum() / (2 * hs**2)
            ga, gd = np.zeros(n), np.zeros(n)
            rh, ra = w * (lh - th), w * (la - ta)
            np.add.at(ga, hi, rh)
            np.add.at(ga, ai, ra)
            np.add.at(gd, ai, rh)
            np.add.at(gd, hi, ra)
            gh = np.zeros(n)
            if hs > 0:
                np.add.at(gh, hi, rh)
                gh += hte / hs**2
            g = np.concatenate(
                [
                    [rh.sum() + ra.sum(), rh.sum()],
                    ga + att / self.prior_sd**2,
                    gd + dfn / self.prior_sd**2,
                    gh,
                ]
            )
            return nll + pen, g

        x0 = np.zeros(2 + 3 * n)
        x0[0] = np.log(max(np.average(np.r_[th, ta], weights=np.r_[w, w]), 0.1))
        res = minimize(loss, x0, jac=True, method="L-BFGS-B")
        mu, h, att, dfn, hte = unpack(res.x)
        self.mu_, self.h_ = mu, h
        self.att_ = dict(zip(teams, att, strict=True))
        self.def_ = dict(zip(teams, dfn, strict=True))
        self.hte_ = dict(zip(teams, hte, strict=True))

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        def get(d, ts):
            return np.array([d.get(t, 0.0) for t in ts])

        h, a = test["home"].to_numpy(), test["away"].to_numpy()
        lh = np.exp(self.mu_ + self.h_ + get(self.att_, h) + get(self.def_, a) + get(self.hte_, h))
        la = np.exp(self.mu_ + get(self.att_, a) + get(self.def_, h))
        return one_x_two(lh, la)


@dataclass
class GBM:
    num_leaves: int = 7
    min_child_samples: int = 80
    n_estimators: int = 200
    learning_rate: float = 0.03
    features: tuple = tuple(FEATURES)

    def fit(self, train: pd.DataFrame) -> None:
        import lightgbm as lgb

        self.model_ = lgb.LGBMClassifier(
            objective="multiclass",
            num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples,
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            random_state=0,
            n_jobs=1,
            verbose=-1,
        )
        self.model_.fit(train[list(self.features)], train["y"].to_numpy(int))

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        p = np.zeros((len(test), 3))
        p[:, self.model_.classes_] = self.model_.predict_proba(test[list(self.features)])
        return p


@dataclass
class Logit:
    C: float = 0.1
    features: tuple = tuple(FEATURES)

    def fit(self, train: pd.DataFrame) -> None:
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        self.model_ = make_pipeline(
            SimpleImputer(strategy="mean"),
            StandardScaler(),
            LogisticRegression(C=self.C, max_iter=2000),
        )
        self.model_.fit(train[list(self.features)], train["y"].to_numpy(int))

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        p = np.zeros((len(test), 3))
        cls = self.model_.classes_
        p[:, cls] = self.model_.predict_proba(test[list(self.features)])
        return p


# Pre-registered tuning grids (docs/lab.md). Tuning is nested: each season's settings
# are chosen on the season before it, from models trained on still earlier matches.
GRIDS = {
    "a_dixon_coles": [{}],
    "b_hier_poisson": [
        {"half_life": hl, "prior_sd": ps, "home_sd": hs}
        for hl in (120.0, 365.0)
        for ps in (0.15, 0.4)
        for hs in (0.0, 0.1)
    ],
    "c_gbm": [
        {"num_leaves": nl, "min_child_samples": mcs, "n_estimators": ne}
        for nl in (4, 8)
        for mcs in (40, 160)
        for ne in (150, 400)
    ],
    "d_logit": [{"C": c} for c in (0.01, 0.1, 1.0)],
}
FACTORIES = {
    "a_dixon_coles": DixonColesBaseline,
    "b_hier_poisson": HierPoisson,
    "c_gbm": GBM,
    "d_logit": Logit,
}
