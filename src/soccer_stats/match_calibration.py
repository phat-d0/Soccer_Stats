"""Blending the match model's chances with the bookmaker's margin-free price.

The DraftKings backtest showed that the model's biggest "edges" are mostly its own
errors: ROI falls as the edge threshold rises. So, as for player bets
(player_calibration.py), the chance we bet on is a blend of the model and the price,
fitted on settled matches:

    score_k = a_k + b * log(market_k) + c * log(model_k)      (a_home = 0)
    p_k     = softmax(score)_k

for home/draw/away, and the same with two outcomes for over/under 2.5 (which is the
logistic blend logit p = a + b * logit(market) + c * logit(model)). `c` is the weight
the model earns beside the price (near 0 = trust the price); `a` corrects any draw or
home bias both share.

Fits use football-data's Pinnacle closing odds with the model's walk-forward
predictions (thousands of matches, free), refitted every few weeks on matches played
before the refit date only. The blend is then applied to DraftKings' margin-free price
at the moment of the bet.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import log_softmax, softmax

from soccer_stats.odds import devig_shin

H2H = ("home", "draw", "away")
TOTALS = ("over25", "under25")
GROUPS = {"h2h": H2H, "totals": TOTALS}
MIN_ROWS = 300  # settled matches needed before a fit is trusted
REFIT = "28D"
RIDGE = 1e-3  # pulls b toward 1 and c toward 0 a little


def _log(p) -> np.ndarray:
    return np.log(np.clip(np.asarray(p, dtype=float), 1e-6, 1.0))


def _scores(w: np.ndarray, lm: np.ndarray, lq: np.ndarray) -> np.ndarray:
    k = lm.shape[1]
    a = np.concatenate([[0.0], w[: k - 1]])
    return a + w[k - 1] * lm + w[k] * lq


def fit(market, model, outcome, min_rows: int = MIN_ROWS) -> list[float] | None:
    """Coefficients [a_2..a_K, b, c], or None with too few matches.

    `market` and `model` are (n, K) probabilities, `outcome` the index of what happened.
    """
    lm, lq = _log(market), _log(model)
    y = np.asarray(outcome, dtype=int)
    n, k = lm.shape
    if n < min_rows or len(np.unique(y)) < 2:
        return None
    onehot = np.eye(k)[y]

    def loss(w):
        s = _scores(w, lm, lq)
        nll = -(log_softmax(s, axis=1) * onehot).sum(1).mean()
        return nll + RIDGE * ((w[k - 1] - 1) ** 2 + w[k] ** 2)

    def grad(w):
        d = (softmax(_scores(w, lm, lq), axis=1) - onehot) / n
        g = np.concatenate([d[:, 1:].sum(0), [(d * lm).sum(), (d * lq).sum()]])
        g[k - 1] += 2 * RIDGE * (w[k - 1] - 1)
        g[k] += 2 * RIDGE * w[k]
        return g

    w0 = np.concatenate([np.zeros(k - 1), [1.0, 0.0]])
    res = minimize(loss, w0, jac=grad, method="L-BFGS-B")
    return [round(float(v), 5) for v in res.x]


def apply(coef: list[float] | None, market, model) -> np.ndarray:
    """Blended probabilities, (n, K); NaN without coefficients or inputs."""
    market = np.atleast_2d(np.asarray(market, dtype=float))
    model = np.atleast_2d(np.asarray(model, dtype=float))
    if coef is None:
        return np.full(market.shape, np.nan)
    out = softmax(_scores(np.asarray(coef), _log(market), _log(model)), axis=1)
    bad = ~(np.isfinite(market).all(1) & np.isfinite(model).all(1))
    out[bad] = np.nan
    return out


def blend_card(coefs: dict | None, probs: dict, implied: dict) -> dict | None:
    """The blended chance per market for one fixture (live), or None without a fit.

    `probs` is the model's chances, `implied` the bookmaker's margin-free ones (the app's
    fixture card fields). Markets without a price stay None.
    """
    if not coefs:
        return None
    out = {}
    for g, names in GROUPS.items():
        coef = (coefs.get(g) or {}).get("coef")
        mk = [implied.get(m) for m in names]
        md = [probs.get(m) for m in names]
        ok = coef is not None and all(x is not None for x in mk + md)
        p = apply(coef, [mk], [md])[0] if ok else [None] * len(names)
        out.update(
            {
                m: (None if v is None or np.isnan(v) else float(v))
                for m, v in zip(names, p, strict=True)
            }
        )
    return out


# ---------- training rows ----------


def _devig_rows(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    out = np.full((len(df), len(cols)), np.nan)
    vals = df[cols].to_numpy(dtype=float)
    for i, row in enumerate(vals):
        if np.isfinite(row).all() and (row > 1).all():
            out[i] = devig_shin(row)
    return out


def outcome_index(home_goals, away_goals, group: str) -> np.ndarray:
    hg = np.asarray(home_goals, dtype=float)
    ag = np.asarray(away_goals, dtype=float)
    if group == "h2h":
        return np.select([hg > ag, hg == ag], [0, 1], 2)
    return np.where(hg + ag > 2.5, 0, 1)


def training_rows(preds: pd.DataFrame, group: str, prefix: str = "close") -> pd.DataFrame:
    """Settled matches for a fit: date, mkt_<m>, p_<m>, y (index of the outcome).

    `preds` is backtest.walk_forward output (model chances made before each match) with
    football-data's `{prefix}_<m>` odds (Pinnacle's close by default).
    """
    names = GROUPS[group]
    cols = [f"{prefix}_{m}" for m in names]
    if preds.empty or not set(cols) <= set(preds.columns):
        return pd.DataFrame(columns=["date", *[f"mkt_{m}" for m in names], "y"])
    mk = _devig_rows(preds, cols)
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(preds["date"]).to_numpy(),
            **{f"mkt_{m}": mk[:, i] for i, m in enumerate(names)},
            **{f"p_{m}": preds[f"p_{m}"].to_numpy(dtype=float) for m in names},
            "y": outcome_index(preds["home_goals"], preds["away_goals"], group),
        }
    )
    return out.dropna().reset_index(drop=True)


def walk_forward(
    train: pd.DataFrame,
    target: pd.DataFrame,
    group: str,
    refit: str = REFIT,
    min_rows: int = MIN_ROWS,
) -> tuple[pd.DataFrame, list[dict]]:
    """Out-of-sample blended chances for `target` rows.

    `target` has `date` (the day the bet is decided, naive), mkt_<m> and p_<m>. Each
    block of `refit` days uses a fit on `train` matches played before the block starts;
    earlier blocks get NaN (no trade).
    """
    names = GROUPS[group]
    out = pd.DataFrame(np.nan, index=target.index, columns=list(names))
    fits: list[dict] = []
    if target.empty or train.empty:
        return out, fits
    day = pd.to_datetime(target["date"])
    edges = pd.date_range(
        day.min().normalize(), day.max().normalize() + pd.Timedelta(refit), freq=refit
    )
    mk_cols = [f"mkt_{m}" for m in names]
    p_cols = [f"p_{m}" for m in names]
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        block = target[(day >= lo) & (day < hi)]
        if block.empty:
            continue
        tr = train[pd.to_datetime(train["date"]) < lo]
        coef = fit(tr[mk_cols], tr[p_cols], tr["y"], min_rows)
        if coef is None:
            continue
        out.loc[block.index, list(names)] = apply(coef, block[mk_cols], block[p_cols])
        fits.append({"from": lo.date().isoformat(), "matches": len(tr), "coef": coef})
    return out, fits


def live_fit(train: pd.DataFrame, group: str, min_rows: int = MIN_ROWS) -> dict:
    """A fit on every settled match, for the live app."""
    names = GROUPS[group]
    mk = train[[f"mkt_{m}" for m in names]]
    coef = fit(mk, train[[f"p_{m}" for m in names]], train["y"], min_rows)
    return {"coef": coef, "matches": len(train)}
