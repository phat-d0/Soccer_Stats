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
# Research-only groups (match_markets.py): not in GROUPS, so the live blend, the
# DraftKings backtest and the app never look for them.
AH = ("ah_home", "ah_away")
RESEARCH_GROUPS = {"ah": AH}
MIN_ROWS = 300  # settled matches needed before a fit is trusted
REFIT = "28D"
RIDGE = 1e-3  # pulls b toward 1 and c toward 0 a little


def _log(p) -> np.ndarray:
    return np.log(np.clip(np.asarray(p, dtype=float), 1e-6, 1.0))


def names_of(group: str) -> tuple[str, ...]:
    return GROUPS.get(group) or RESEARCH_GROUPS[group]


def feature_sign(k: int) -> np.ndarray:
    """How an extra feature x moves the scores: +x home / -x away for 1X2 (draw 0);
    a logit shift toward the first outcome (over, home covers) with two."""
    return np.array([1.0, 0.0, -1.0]) if k == 3 else np.array([1.0, 0.0])


def _features(extra, n: int) -> np.ndarray:
    if extra is None:
        return np.zeros((n, 0))
    x = np.asarray(extra, dtype=float)
    return x.reshape(n, 1) if x.ndim == 1 else x


def _scores(w: np.ndarray, lm: np.ndarray, lq: np.ndarray, x: np.ndarray | None = None):
    k = lm.shape[1]
    a = np.concatenate([[0.0], w[: k - 1]])
    s = a + w[k - 1] * lm + w[k] * lq
    if x is not None and x.shape[1]:
        s = s + (x @ w[k + 1 :])[:, None] * feature_sign(k)
    return s


def fit(
    market, model, outcome, min_rows: int = MIN_ROWS, weight=None, extra=None
) -> list[float] | None:
    """Coefficients [a_2..a_K, b, c, d_1..d_F], or None with too few matches.

    `market` and `model` are (n, K) probabilities, `outcome` the index of what happened.
    `weight` (optional, n) weights each row: Asian handicap half-wins and half-losses
    count 0.5, pushes 0. `extra` (optional, (n, F)) are signals the price might lack;
    each gets a coefficient d (see feature_sign). Without them the result is [a, b, c].
    """
    lm, lq = _log(market), _log(model)
    y = np.asarray(outcome, dtype=int)
    n, k = lm.shape
    x = _features(extra, n)
    wt = np.ones(n) if weight is None else np.asarray(weight, dtype=float)
    if n < min_rows or len(np.unique(y[wt > 0])) < 2:
        return None
    wt = wt / wt.sum()
    onehot = np.eye(k)[y]
    f = x.shape[1]
    sign = feature_sign(k)

    def loss(w):
        s = _scores(w, lm, lq, x)
        nll = -((log_softmax(s, axis=1) * onehot).sum(1) * wt).sum()
        return nll + RIDGE * ((w[k - 1] - 1) ** 2 + w[k] ** 2 + (w[k + 1 :] ** 2).sum())

    def grad(w):
        d = (softmax(_scores(w, lm, lq, x), axis=1) - onehot) * wt[:, None]
        g = np.concatenate([d[:, 1:].sum(0), [(d * lm).sum(), (d * lq).sum()], x.T @ (d @ sign)])
        g[k - 1] += 2 * RIDGE * (w[k - 1] - 1)
        g[k] += 2 * RIDGE * w[k]
        g[k + 1 :] += 2 * RIDGE * w[k + 1 :]
        return g

    w0 = np.concatenate([np.zeros(k - 1), [1.0, 0.0], np.zeros(f)])
    res = minimize(loss, w0, jac=grad, method="L-BFGS-B")
    return [round(float(v), 5) for v in res.x]


def apply(coef: list[float] | None, market, model, extra=None) -> np.ndarray:
    """Blended probabilities, (n, K); NaN without coefficients or inputs."""
    market = np.atleast_2d(np.asarray(market, dtype=float))
    model = np.atleast_2d(np.asarray(model, dtype=float))
    if coef is None:
        return np.full(market.shape, np.nan)
    n, k = market.shape
    w = np.asarray(coef, dtype=float)
    x = _features(extra, n) if len(w) > k + 1 else None
    out = softmax(_scores(w, _log(market), _log(model), x), axis=1)
    bad = ~(np.isfinite(market).all(1) & np.isfinite(model).all(1))
    if x is not None:
        bad |= ~np.isfinite(x).all(1)
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


def sigma(coef: list[float] | None, market, model) -> float | None:
    """How far the blend usually strays from the price: the sd of p_blend - p_book over
    every row and outcome (pooled; ddof 0), on the rows the coefficients were fitted on.

    The confidence tiers (trades.lean_pick) measure a side's gap over break-even in these
    units. None without coefficients or rows.
    """
    if coef is None:
        return None
    mk = np.atleast_2d(np.asarray(market, dtype=float))
    if not mk.size:
        return None
    gap = apply(coef, mk, model) - mk
    gap = gap[np.isfinite(gap).all(1)]
    return float(gap.std(ddof=0)) if len(gap) else None


def walk_forward(
    train: pd.DataFrame,
    target: pd.DataFrame,
    group: str,
    refit: str = REFIT,
    min_rows: int = MIN_ROWS,
    features: tuple[str, ...] = (),
    with_sigma: bool = False,
) -> tuple[pd.DataFrame, list[dict]]:
    """Out-of-sample blended chances for `target` rows.

    `target` has `date` (the day the bet is decided, naive), mkt_<m> and p_<m>. Each
    block of `refit` days uses a fit on `train` matches played before the block starts;
    earlier blocks get NaN (no trade). A `w` column in `train` weights its rows;
    `features` names extra signal columns present in both frames. `with_sigma` adds a
    `sigma` column (and key in each fit): `sigma` of that block's fit on its own
    training rows, so a tier decided with it never sees a later match either.
    """
    names = names_of(group)
    feats = list(features)
    out = pd.DataFrame(np.nan, index=target.index, columns=list(names))
    if with_sigma:
        out["sigma"] = np.nan
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
        if feats:
            tr = tr.dropna(subset=feats)
        wt = tr["w"] if "w" in tr else None
        x = tr[feats] if feats else None
        coef = fit(tr[mk_cols], tr[p_cols], tr["y"], min_rows, weight=wt, extra=x)
        if coef is None:
            continue
        bx = block[feats] if feats else None
        out.loc[block.index, list(names)] = apply(coef, block[mk_cols], block[p_cols], bx)
        fit_row = {"from": lo.date().isoformat(), "matches": len(tr), "coef": coef}
        if with_sigma:
            sd = sigma(coef, tr[mk_cols], tr[p_cols])
            out.loc[block.index, "sigma"] = sd
            fit_row["sigma"] = None if sd is None else round(sd, 5)
        fits.append(fit_row)
    return out, fits


def live_fit(train: pd.DataFrame, group: str, min_rows: int = MIN_ROWS) -> dict:
    """A fit on every settled match, for the live app."""
    names = GROUPS[group]
    mk = train[[f"mkt_{m}" for m in names]]
    md = train[[f"p_{m}" for m in names]]
    coef = fit(mk, md, train["y"], min_rows)
    sd = sigma(coef, mk, md) if len(train) else None
    return {"coef": coef, "matches": len(train), "sigma": None if sd is None else round(sd, 5)}


# ---------- per-league h2h blends (fit-match-blends; the Lean strategy) ----------


def league_fit(rows: pd.DataFrame, fit_date, min_rows: int = MIN_ROWS) -> dict:
    """The live h2h blend for one league from its training rows (training_rows output).

    Only matches played before `fit_date` are used (no look-ahead): the coefficients, the
    tier unit `sigma` (sd of p_blend - p_book, pooled over home/draw/away, p_book =
    Pinnacle's margin-free close) and the date range. Also an out-of-sample check: the
    same blend walk-forward (refit every REFIT on earlier matches only), log loss of the
    model, the blend and Pinnacle on the matches it covers.
    """
    fit_date = pd.Timestamp(fit_date)
    fit_date = fit_date.tz_convert(None) if fit_date.tz is not None else fit_date
    rows = rows[pd.to_datetime(rows["date"]) < fit_date] if len(rows) else rows
    mk_cols, p_cols = [f"mkt_{m}" for m in H2H], [f"p_{m}" for m in H2H]
    out = {
        "coef": None,
        "sigma": None,
        "matches": len(rows),
        "fit_from": None,
        "fit_to": None,
        "fit_date": fit_date.date().isoformat(),
        "refit": REFIT,
    }
    if rows.empty:
        return out
    dates = pd.to_datetime(rows["date"])
    out.update(fit_from=dates.min().date().isoformat(), fit_to=dates.max().date().isoformat())
    coef = fit(rows[mk_cols], rows[p_cols], rows["y"], min_rows)
    out["coef"] = coef
    sd = sigma(coef, rows[mk_cols], rows[p_cols])
    out["sigma"] = None if sd is None else round(sd, 5)
    blended, fits = walk_forward(rows, rows, "h2h", min_rows=min_rows)
    ok = blended[list(H2H)].notna().all(axis=1).to_numpy()
    if ok.any():
        y = rows["y"].to_numpy(dtype=int)[ok]
        idx = np.arange(ok.sum())

        def ll(p) -> float:
            return round(float(-np.log(np.clip(np.asarray(p)[idx, y], 1e-12, 1)).mean()), 5)

        out["walk_forward"] = {
            "matches": int(ok.sum()),
            "refits": len(fits),
            "model": ll(rows[p_cols].to_numpy(dtype=float)[ok]),
            "blend": ll(blended[list(H2H)].to_numpy(dtype=float)[ok]),
            "pinnacle": ll(rows[mk_cols].to_numpy(dtype=float)[ok]),
        }
    return out
