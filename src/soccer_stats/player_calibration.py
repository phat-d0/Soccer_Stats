"""Shrinking the player model's chances toward the bookmaker's price.

The raw model is too confident on long shots: in the FanDuel backtest it gave the
bets it picked a 12% chance, FanDuel's price said 9% and 7% won. So the chance we
bet on is a logistic blend of the two, fitted on settled lines:

    logit(p) = a + b * logit(bookmaker implied) + c * logit(model p)

The fit decides how much weight the model earns beside the price (c near 0 = trust
the price) and corrects the bookmaker's margin too: FanDuel lists only "over" sides,
so its implied chance (1 / odds) still includes the margin.

In the backtest the blend is refitted every few weeks on lines that kicked off
before the refit date only; the live app uses a fit on every settled line.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit

MIN_LINES = 3000  # settled lines needed before a fit is trusted
REFIT = "28D"
RIDGE = 1e-3


def _logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _design(implied, model_p) -> np.ndarray:
    implied = np.asarray(implied, dtype=float)
    return np.column_stack([np.ones(len(implied)), _logit(implied), _logit(model_p)])


def fit(implied, model_p, won, min_lines: int = MIN_LINES) -> list[float] | None:
    """Coefficients [a, b, c], or None with too few lines."""
    y = np.asarray(won, dtype=float)
    if len(y) < min_lines or y.min() == y.max():
        return None
    x = _design(implied, model_p)

    def loss(w):
        z = x @ w
        ll = np.logaddexp(0, z) - y * z
        return ll.mean() + RIDGE * (w[1:] ** 2).sum()

    def grad(w):
        g = x.T @ (expit(x @ w) - y) / len(y)
        g[1:] += 2 * RIDGE * w[1:]
        return g

    res = minimize(loss, np.array([0.0, 1.0, 0.0]), jac=grad, method="L-BFGS-B")
    return [round(float(v), 5) for v in res.x]


def apply(coef: list[float] | None, implied, model_p) -> np.ndarray:
    """The blended chance (NaN everywhere without coefficients)."""
    if coef is None:
        return np.full(len(np.asarray(implied)), np.nan)
    return expit(_design(implied, model_p) @ np.asarray(coef))


def walk_forward(
    lines: pd.DataFrame, refit: str = REFIT, min_lines: int = MIN_LINES
) -> tuple[pd.Series, list[dict]]:
    """Out-of-sample blended chances for `lines` (kickoff, implied, p_model, won).

    Each block of `refit` days uses a fit on lines that kicked off before it; lines
    before there are `min_lines` to fit on get NaN (no trade).
    """
    out = pd.Series(np.nan, index=lines.index)
    fits = []
    if lines.empty:
        return out, fits
    ko = lines["kickoff"]
    edges = pd.date_range(ko.min().normalize(), ko.max() + pd.Timedelta(refit), freq=refit)
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        block = lines[(ko >= lo) & (ko < hi)]
        if block.empty:
            continue
        train = lines[ko < lo]
        if len(train) < min_lines:
            continue
        coef = fit(train["implied"], train["p_model"], train["won"], min_lines)
        if coef is None:
            continue
        out.loc[block.index] = apply(coef, block["implied"], block["p_model"])
        fits.append({"from": lo.date().isoformat(), "lines": len(train), "coef": coef})
    return out, fits
