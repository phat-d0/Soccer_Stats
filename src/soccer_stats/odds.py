"""Odds conversion, margin removal, edge and stake sizing."""

from __future__ import annotations

import numpy as np
from scipy.optimize import brentq


def implied(odds: np.ndarray) -> np.ndarray:
    """Decimal odds -> raw implied probabilities (still includes the bookmaker margin)."""
    return 1.0 / np.asarray(odds, dtype=float)


def overround(odds: np.ndarray) -> float:
    return float(implied(odds).sum() - 1.0)


def devig_proportional(odds: np.ndarray) -> np.ndarray:
    """Remove the margin by scaling implied probabilities to sum to 1."""
    p = implied(odds)
    return p / p.sum()


def devig_shin(odds: np.ndarray) -> np.ndarray:
    """Shin's method: attributes the margin to insider trading.

    Shades longshots more than favourites, which tends to match the
    favourite-longshot bias better than proportional scaling.
    """
    pi = implied(odds)
    total = pi.sum()
    if total <= 1.0:
        return pi / total

    def probs(z: float) -> np.ndarray:
        return (np.sqrt(z**2 + 4 * (1 - z) * pi**2 / total) - z) / (2 * (1 - z))

    z = brentq(lambda z: probs(z).sum() - 1.0, 0.0, 0.999)
    return probs(z)


def edge(model_prob: np.ndarray, odds: np.ndarray) -> np.ndarray:
    """Expected return per unit staked: p * odds - 1."""
    return np.asarray(model_prob) * np.asarray(odds, dtype=float) - 1.0


def kelly(model_prob: np.ndarray, odds: np.ndarray, fraction: float = 0.25) -> np.ndarray:
    """Fractional Kelly stake as a share of bankroll (0 when there is no edge)."""
    b = np.asarray(odds, dtype=float) - 1.0
    p = np.asarray(model_prob, dtype=float)
    full = (b * p - (1 - p)) / b
    return np.clip(full * fraction, 0.0, None)
