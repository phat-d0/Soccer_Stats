"""Scores for out-of-sample chances, each with a bootstrap range over whole groups.

Inputs are arrays: `p` (n, K) the candidate's chances, `y` (n,) the outcome index,
`market` (n, K) the margin-free market chances when the bet is decided, `odds` (n, K)
the decimal prices that could be bet then (NaN = not offered), `fair_close` (n, K)
the margin-free closing chances (for CLV), and `groups` (n,) a cluster id (a match):
ranges resample whole groups, since rows of one match are not independent.

Order of importance (docs/lab.md): blend weight beside the market and CLV decide;
log loss, Brier and calibration describe; ROI comes last and never passes on its own.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import match_calibration as mc

PAPER_EDGE = 0.12  # the live trade rule (trades.PAPER_EDGE)


def _rows(p, y):
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1.0)
    return p, np.asarray(y, dtype=int)


def log_loss_rows(p, y) -> np.ndarray:
    p, y = _rows(p, y)
    return -np.log(p[np.arange(len(y)), y])


def brier_rows(p, y) -> np.ndarray:
    p, y = _rows(p, y)
    return ((p - np.eye(p.shape[1])[y]) ** 2).sum(axis=1)


def calibration_table(p, y, bins: int = 10) -> pd.DataFrame:
    """Every outcome's chance against how often it happened, in equal-width bins."""
    p, y = _rows(p, y)
    hit = np.eye(p.shape[1])[y]
    pf, hf = p.ravel(), hit.ravel()
    edges = np.linspace(0, 1, bins + 1)
    b = np.clip(np.digitize(pf, edges) - 1, 0, bins - 1)
    df = pd.DataFrame({"bin": b, "p": pf, "hit": hf})
    t = df.groupby("bin").agg(n=("p", "size"), predicted=("p", "mean"), observed=("hit", "mean"))
    t.index = [f"{edges[i]:.1f}-{edges[i + 1]:.1f}" for i in t.index]
    return t


def _cluster_draws(groups, n_boot: int, seed: int):
    codes, _ = pd.factorize(pd.Series(np.asarray(groups)).astype(str))
    k = codes.max() + 1
    members = [np.flatnonzero(codes == g) for g in range(k)]
    rng = np.random.default_rng(seed)
    for _ in range(n_boot):
        pick = rng.integers(0, k, size=k)
        yield np.concatenate([members[g] for g in pick])


def boot_range(values, groups=None, n_boot: int = 2000, level: float = 0.95, seed: int = 0):
    """Percentile range of the mean, resampling whole groups (rows if none)."""
    v = np.asarray(values, dtype=float)
    if len(v) < 2:
        return None
    g = np.arange(len(v)) if groups is None else np.asarray(groups)
    codes, _ = pd.factorize(pd.Series(g).astype(str))
    k = codes.max() + 1
    sums = np.bincount(codes, weights=v, minlength=k)
    cnt = np.bincount(codes, minlength=k).astype(float)
    draw = np.random.default_rng(seed).integers(0, k, size=(n_boot, k))
    stats = sums[draw].sum(1) / cnt[draw].sum(1)
    lo, hi = np.quantile(stats, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def paired_gain(a_rows, b_rows, groups, level=0.95, seed=0) -> dict:
    """Mean of a - b (e.g. log loss of the market minus the candidate) with its range."""
    d = np.asarray(a_rows, dtype=float) - np.asarray(b_rows, dtype=float)
    return {"mean": float(d.mean()), "range": boot_range(d, groups, level=level, seed=seed)}


# ---------- blend weight beside the market ----------


def blend_weight(
    market, p, y, groups, level: float = 0.95, n_boot: int = 300, seed: int = 0
) -> dict:
    """Weight c the candidate earns in score_k = a_k + b·log(market_k) + c·log(p_k).

    Fitted on all the out-of-sample rows given (only three or four coefficients, so the
    in-sample fit is not the risk; the range is). The range refits on whole-group
    bootstrap samples. c > 0 means the candidate adds to the market.
    """
    m, q = np.asarray(market, float), np.asarray(p, float)
    y = np.asarray(y, int)
    ok = np.isfinite(m).all(1) & np.isfinite(q).all(1)
    m, q, y, g = m[ok], q[ok], y[ok], np.asarray(groups)[ok]
    k = m.shape[1]
    coef = mc.fit(m, q, y, min_rows=50)
    if coef is None:
        return {"rows": int(ok.sum())}
    cs = []
    for idx in _cluster_draws(g, n_boot, seed):
        w = mc.fit(m[idx], q[idx], y[idx], min_rows=50)
        if w is not None:
            cs.append(w[k])
    lo, hi = np.quantile(cs, [(1 - level) / 2, 1 - (1 - level) / 2])
    return {
        "rows": int(ok.sum()),
        "b": coef[k - 1],
        "c": coef[k],
        "c_range": (float(lo), float(hi)),
    }


def walk_forward_blend(market, p, y, times, refit: str = "28D", min_rows: int = 300):
    """Blended chances, each block fitted on earlier rows only (NaN before min_rows)."""
    m, q = np.asarray(market, float), np.asarray(p, float)
    y = np.asarray(y, int)
    t = pd.to_datetime(pd.Series(np.asarray(times))).reset_index(drop=True)
    out = np.full(m.shape, np.nan)
    ok = np.isfinite(m).all(1) & np.isfinite(q).all(1)
    edges = pd.date_range(t.min().normalize(), t.max() + pd.Timedelta(refit), freq=refit)
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        te = ((t >= lo) & (t < hi)).to_numpy() & ok
        if not te.any():
            continue
        tr = (t < lo).to_numpy() & ok
        coef = mc.fit(m[tr], q[tr], y[tr], min_rows=min_rows)
        if coef is not None:
            out[te] = mc.apply(coef, m[te], q[te])
    return out


# ---------- bets: CLV and ROI ----------


def pick_bets(p, odds, threshold: float = PAPER_EDGE) -> tuple[np.ndarray, np.ndarray]:
    """The live rule: per row, the outcome with the largest edge p·odds − 1, if above
    `threshold`. Returns (row index, outcome index)."""
    p, o = np.asarray(p, float), np.asarray(odds, float)
    edge = np.where(np.isfinite(o) & np.isfinite(p), p * o - 1, -np.inf)
    best = edge.argmax(axis=1)
    rows = np.flatnonzero(edge[np.arange(len(edge)), best] > threshold)
    return rows, best[rows]


def bet_scores(
    p, y, odds, fair_close, groups, threshold: float = PAPER_EDGE, level: float = 0.95
) -> dict:
    """CLV (price × fair closing chance − 1) and ROI of the live rule, with ranges."""
    rows, k = pick_bets(p, odds, threshold)
    if len(rows) == 0:
        return {"bets": 0}
    o = np.asarray(odds, float)[rows, k]
    won = np.asarray(y, int)[rows] == k
    profit = np.where(won, o - 1, -1.0)
    fair = np.asarray(fair_close, float)[rows, k]
    has = np.isfinite(fair)
    clv = o[has] * fair[has] - 1
    g = np.asarray(groups)[rows]
    return {
        "bets": int(len(rows)),
        "avg_odds": float(o.mean()),
        "clv": float(clv.mean()) if has.any() else None,
        "clv_range": boot_range(clv, g[has], level=level) if has.sum() > 1 else None,
        "beat_close": float((clv > 0).mean()) if has.any() else None,
        "roi": float(profit.mean()),
        "roi_range": boot_range(profit, g, level=level),
    }


def evaluate(
    p,
    y,
    market,
    odds,
    fair_close,
    groups,
    level: float = 0.95,
    threshold: float = PAPER_EDGE,
    n_boot_blend: int = 300,
) -> dict:
    """Every metric for one candidate's out-of-sample chances (rows with a market)."""
    p, m = np.asarray(p, float), np.asarray(market, float)
    ok = np.isfinite(p).all(1) & np.isfinite(m).all(1)
    p, m, y = p[ok], m[ok], np.asarray(y, int)[ok]
    g = np.asarray(groups)[ok]
    ll, llm = log_loss_rows(p, y), log_loss_rows(m, y)
    br = brier_rows(p, y)
    return {
        "rows": int(ok.sum()),
        "log_loss": float(ll.mean()),
        "log_loss_range": boot_range(ll, g, level=level),
        "market_log_loss": float(llm.mean()),
        "gain_vs_market": paired_gain(llm, ll, g, level=level),
        "brier": float(br.mean()),
        "blend": blend_weight(m, p, y, g, level=level, n_boot=n_boot_blend),
        "bets": bet_scores(
            p,
            y,
            np.asarray(odds, float)[ok],
            np.asarray(fair_close, float)[ok],
            g,
            threshold,
            level,
        ),
    }


def passes(result: dict) -> bool:
    """The pre-registered pass rule: blend weight range above 0 AND CLV range above 0."""
    cr = (result.get("blend") or {}).get("c_range")
    vr = (result.get("bets") or {}).get("clv_range")
    return bool(cr and vr and cr[0] > 0 and vr[0] > 0)
