"""Turn a score matrix into market probabilities."""

from __future__ import annotations

import numpy as np


def match_odds(m: np.ndarray) -> np.ndarray:
    """[P(home win), P(draw), P(away win)]."""
    return np.array([np.tril(m, -1).sum(), np.trace(m), np.triu(m, 1).sum()])


def over_under(m: np.ndarray, line: float = 2.5) -> np.ndarray:
    """[P(over line), P(under line)] for a half-goal line."""
    totals = np.add.outer(np.arange(m.shape[0]), np.arange(m.shape[1]))
    over = m[totals > line].sum()
    return np.array([over, 1.0 - over])


def btts(m: np.ndarray) -> np.ndarray:
    """[P(both teams score), P(not)]."""
    yes = m[1:, 1:].sum()
    return np.array([yes, 1.0 - yes])


def asian_handicap_home(m: np.ndarray, line: float) -> float:
    """P(home covers) at a half-ball handicap line (e.g. -0.5, -1.5, +0.5)."""
    if line * 2 % 2 != 1:
        raise ValueError("Only half-ball lines are supported for now")
    diff = np.subtract.outer(np.arange(m.shape[0]), np.arange(m.shape[1]))
    return float(m[diff + line > 0].sum())
