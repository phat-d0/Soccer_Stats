"""Bootstrap ranges and multiple-testing helpers for edge claims."""

from __future__ import annotations

import numpy as np
import pandas as pd


def bootstrap_mean(
    values,
    clusters=None,
    n: int = 2000,
    level: float = 0.95,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile range of the mean of `values`.

    With `clusters`, whole clusters are resampled (bets on the same match or player-match
    are not independent), and the statistic is the overall mean of the resampled rows.
    """
    v = np.asarray(values, dtype=float)
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    if clusters is None:
        idx = rng.integers(0, len(v), size=(n, len(v)))
        stats = v[idx].mean(axis=1)
    else:
        codes, _ = pd.factorize(pd.Series(clusters).astype(str))
        k = codes.max() + 1
        sums = np.bincount(codes, weights=v, minlength=k)
        counts = np.bincount(codes, minlength=k).astype(float)
        draw = rng.integers(0, k, size=(n, k))
        stats = sums[draw].sum(axis=1) / counts[draw].sum(axis=1)
    lo, hi = np.quantile(stats, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def bonferroni_level(tests: int, alpha: float = 0.05) -> float:
    """Two-sided confidence level that keeps the family-wise error at `alpha`."""
    return 1 - alpha / max(int(tests), 1)
