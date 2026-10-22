"""How a set of predicted chances lines up with what happened (owner, 22 Oct; no network).

One helper for every model and strategy: `reliability(pred, won, implied=None)` puts the
rows in fixed bins of the predicted chance (EDGES: 0-10%, ..., 60-70%, 70-100%; a bin with
fewer than MIN_BIN rows is merged into its neighbour, empty bins are dropped) and gives per
bin the mean predicted chance, the mean implied chance (1/odds or a fair price, when
given), the realized win rate and its Wilson 95% range; plus Brier, log loss and the
recalibration fit logit(P(won)) = a + b * logit(pred) with 95% ranges. A slope b below 1
means the chances are too spread out (over-confident); above 1, too timid. With `groups`
(one id per match), the ranges are cluster-robust, since a match's home, draw and away rows
aren't independent.

Each row is one binary outcome (a side won or it didn't), so the log loss here is the
per-outcome binary one, not the three-way 1X2 log loss.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EDGES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0)
MIN_BIN = 20
Z95 = 1.959964
CLIP = 1e-6
NOTE = (
    "Each bin holds the rows whose predicted chance falls in it: predicted is their "
    "average chance, implied the price's, realized how often they won (95% range). On a "
    "well-calibrated model realized sits on predicted. The slope is from logit(won) = a + "
    "b x logit(predicted): below 1 the chances are too spread out (over-confident)."
)


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | tuple[None, None]:
    """Wilson score 95% range for k wins in n."""
    if n <= 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return float(max(0.0, mid - half)), float(min(1.0, mid + half))


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, CLIP, 1 - CLIP)
    return np.log(p / (1 - p))


def _r(v, digits: int = 4):
    return None if v is None or not np.isfinite(v) else round(float(v), digits)


def bins(
    pred,
    won,
    implied=None,
    edges=EDGES,
    min_n: int = MIN_BIN,
) -> list[dict]:
    """Rows binned on `pred` by `edges` (the last bin includes 1.0). Empty bins are
    dropped; a bin under `min_n` rows joins the next one (the last joins the one before).
    Each bin: lo, hi, n, wins, predicted, implied (when given), realized, realized_lo,
    realized_hi."""
    p = np.asarray(pred, dtype=float)
    y = np.asarray(won, dtype=float)
    imp = None if implied is None else np.asarray(implied, dtype=float)
    idx = np.clip(np.searchsorted(np.asarray(edges[1:-1]), p, side="right"), 0, len(edges) - 2)
    groups = [[i] for i in range(len(edges) - 1) if (idx == i).any()]
    merged: list[list[int]] = []
    for g in groups:
        if merged and int(np.isin(idx, merged[-1]).sum()) < min_n:
            merged[-1] += g
        else:
            merged.append(list(g))
    if len(merged) > 1 and int(np.isin(idx, merged[-1]).sum()) < min_n:
        last = merged.pop()
        merged[-1] += last
    out = []
    for g in merged:
        m = np.isin(idx, g)
        n = int(m.sum())
        k = int(y[m].sum())
        lo, hi = wilson(k, n)
        row = {
            "lo": float(edges[min(g)]),
            "hi": float(edges[max(g) + 1]),
            "n": n,
            "wins": k,
            "predicted": _r(p[m].mean()),
        }
        if imp is not None:
            row["implied"] = _r(np.nanmean(imp[m])) if np.isfinite(imp[m]).any() else None
        row.update(realized=_r(k / n), realized_lo=_r(lo), realized_hi=_r(hi))
        out.append(row)
    return out


def recalibration(pred, won, groups=None, iters: int = 50) -> dict | None:
    """Logistic fit logit(P(won)) = a + b * logit(pred) (Newton), with 95% ranges from the
    standard errors (cluster-robust over `groups` when given). None when it can't be
    fitted (fewer than 10 rows, one outcome only, or no convergence)."""
    p = np.asarray(pred, dtype=float)
    y = np.asarray(won, dtype=float)
    if len(p) < 10 or y.min() == y.max():
        return None
    x = np.column_stack([np.ones(len(p)), _logit(p)])
    beta = np.array([0.0, 1.0])
    for _ in range(iters):
        mu = 1 / (1 + np.exp(-(x @ beta)))
        w = mu * (1 - mu)
        h = x.T @ (x * w[:, None])
        try:
            step = np.linalg.solve(h, x.T @ (y - mu))
        except np.linalg.LinAlgError:
            return None
        beta = beta + step
        if np.abs(step).max() < 1e-9:
            break
    else:
        return None
    mu = 1 / (1 + np.exp(-(x @ beta)))
    h = x.T @ (x * (mu * (1 - mu))[:, None])
    try:
        bread = np.linalg.inv(h)
    except np.linalg.LinAlgError:
        return None
    if groups is None:
        cov = bread
    else:
        score = x * (y - mu)[:, None]
        codes = pd.factorize(pd.Series(groups).astype(str))[0]
        sums = np.zeros((codes.max() + 1, 2))
        np.add.at(sums, codes, score)
        cov = bread @ (sums.T @ sums) @ bread
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    if not np.all(np.isfinite(beta)) or not np.all(np.isfinite(se)):
        return None
    a, b = beta
    return {
        "intercept": _r(a),
        "intercept_ci95": [_r(a - Z95 * se[0]), _r(a + Z95 * se[0])],
        "slope": _r(b),
        "slope_ci95": [_r(b - Z95 * se[1]), _r(b + Z95 * se[1])],
    }


def reliability(
    pred,
    won,
    implied=None,
    edges=EDGES,
    min_n: int = MIN_BIN,
    groups=None,
) -> dict:
    """The calibration of one series: n, wins, predicted (mean), implied (mean, when
    given), realized, brier, log_loss, the recalibration fit (intercept, slope and their
    ranges; None when it can't be fitted) and the bins. Rows with a missing chance or
    outcome are left out. {n: 0} when nothing is left."""
    p = np.asarray(pred, dtype=float)
    y = np.asarray(won, dtype=float)
    keep = np.isfinite(p) & np.isfinite(y)
    imp = None if implied is None else np.asarray(implied, dtype=float)[keep]
    g = None if groups is None else np.asarray(groups, dtype=object)[keep]
    p, y = p[keep], y[keep]
    if not len(p):
        return {"n": 0}
    pc = np.clip(p, CLIP, 1 - CLIP)
    out = {
        "n": len(p),
        "wins": int(y.sum()),
        "predicted": _r(p.mean()),
    }
    if imp is not None:
        out["implied"] = _r(np.nanmean(imp)) if np.isfinite(imp).any() else None
    recal = recalibration(p, y, g) or {
        "intercept": None,
        "intercept_ci95": None,
        "slope": None,
        "slope_ci95": None,
    }
    out.update(
        realized=_r(y.mean()),
        brier=_r(np.mean((p - y) ** 2), 5),
        log_loss=_r(-np.mean(y * np.log(pc) + (1 - y) * np.log(1 - pc)), 5),
        **recal,
        bins=bins(p, y, imp, edges, min_n),
    )
    return out


def outcome_rows(
    df: pd.DataFrame, series: dict[str, str], outcomes, y_col: str = "y", key: str = ""
) -> pd.DataFrame:
    """One row per match and outcome from wide rows (`<prefix>_<outcome>` columns, `y` =
    the index of the outcome that happened): `group` (`key` + the match's row label, so
    leagues pooled later stay apart), one column per series name, `won` (0/1). Rows
    missing any series are dropped."""
    if df.empty:
        return pd.DataFrame(columns=["group", *series, "won"])
    parts = []
    y = df[y_col].to_numpy(dtype=float)
    for i, o in enumerate(outcomes):
        part = {"group": (key + "|" + df.index.astype(str)).to_numpy()}
        for name, prefix in series.items():
            part[name] = pd.to_numeric(df[f"{prefix}_{o}"], errors="coerce").to_numpy(dtype=float)
        part["won"] = (y == i).astype(float)
        parts.append(pd.DataFrame(part))
    long = pd.concat(parts, ignore_index=True)
    return long.dropna(subset=list(series)).reset_index(drop=True)


def series_report(long: pd.DataFrame, names, price: str | None = None) -> dict:
    """reliability() for each named column of an outcome_rows frame, every one on the same
    rows; `price` (one of the names) is given as each other series' implied chance."""
    out = {}
    for name in names:
        imp = long[price] if price and name != price else None
        out[name] = reliability(long[name], long["won"], imp, groups=long["group"])
    return out
