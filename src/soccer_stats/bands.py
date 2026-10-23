"""Per-band aggregates of a backtest's bets (owner via the Research lab, 23 Oct; display only).

For lean_backtest.json and edge12_backtest.json, per league and pooled, on every bet (not
the trimmed rows):

* `by_odds_band`: fixed decimal-odds bands (ODDS_BANDS: a bet is in a band when
  lo < odds <= hi), each with its share of the bets, win rate vs break-even, ROI and CLV
  with 95% ranges;
* `by_edge_band`: five bands of the edge the strategy claimed when it bet (Model 12%:
  model_p x odds - 1; Blend Lean: p_bet x odds - 1, with the mean z beside it), cut at
  the pooled quintiles of the domestic leagues' bets (`edge_cuts`, the same cuts in every
  league, the Champions League too). If the model had skill, CLV would rise with the
  claimed edge;
* `by_clv_band`: five bands of the bet's CLV, cut the same way (`clv_cuts`), with the ROI
  in each: how far beating the close goes with making money.

Ranges resample matches (a match's bets together; every rule here bets a match at most
once, so this is the same as resampling bets). Empty bands are kept with `bets: 0`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ODDS_BANDS = [(None, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, None)]
QUANTILES = (0.2, 0.4, 0.6, 0.8)
N_BOOT = 2000
MATCH = ("league", "date", "home", "away")
NOTE = (
    "Every bet, split by its decimal odds (lo < odds <= hi), by the edge the strategy "
    "claimed when it bet, and by its CLV; the edge and CLV bands are cut at the pooled "
    "domestic leagues' quintiles, the same cuts in every league. ROI and CLV are per $1 "
    "with 95% ranges from resampling matches. If the model had skill, CLV would rise with "
    "the claimed edge."
)


def _r(v, digits: int = 4):
    return None if v is None or not np.isfinite(v) else round(float(v), digits)


def _groups(df: pd.DataFrame) -> np.ndarray:
    keys = [c for c in MATCH if c in df]
    if not keys:
        return np.arange(len(df))
    return pd.factorize(df[keys].astype(str).agg("|".join, axis=1))[0]


def boot_mean(values, groups, seed: int = 0, n: int = N_BOOT) -> list[float] | None:
    """95% percentile range of the mean of `values` (NaN left out), resampling `groups`
    (matches) with replacement; None with fewer than 2 matches holding a value."""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    keep = np.isfinite(v)
    v, g = v[keep], g[keep]
    codes, g = np.unique(g, return_inverse=True)
    k = len(codes)
    if k < 2:
        return None
    sums = np.bincount(g, weights=v, minlength=k)
    counts = np.bincount(g, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, k, size=(n, k))
    means = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    return [_r(np.quantile(means, 0.025)), _r(np.quantile(means, 0.975))]


def band_metrics(df: pd.DataFrame, total: int | None = None) -> dict:
    """bets, share (of `total`), wins, win_rate, breakeven (mean 1/odds), roi and its
    range (profit per $1 staked), clv and its range (bets with a close)."""
    n = len(df)
    out: dict = {"bets": n}
    if total is not None:
        out["share"] = _r(n / total) if total else None
    if not n:
        return out
    g = _groups(df)
    won = df["won"].astype(bool).to_numpy()
    profit = df["profit"].to_numpy(dtype=float)
    clv = pd.to_numeric(df["clv"], errors="coerce").to_numpy(dtype=float)
    out.update(
        wins=int(won.sum()),
        win_rate=_r(won.mean()),
        breakeven=_r((1 / df["odds"].to_numpy(dtype=float)).mean()),
        roi=_r(profit.mean()),
        roi_ci95=boot_mean(profit, g),
        clv=_r(np.nanmean(clv)) if np.isfinite(clv).any() else None,
        clv_ci95=boot_mean(clv, g, seed=1),
    )
    return out


def _in(values: np.ndarray, lo, hi) -> np.ndarray:
    m = np.ones(len(values), dtype=bool)
    if lo is not None:
        m &= values > lo
    if hi is not None:
        m &= values <= hi
    return m


def by_odds_band(df: pd.DataFrame) -> list[dict]:
    """ODDS_BANDS, every band kept (bets 0 when empty); shares sum to 1."""
    odds = df["odds"].to_numpy(dtype=float) if len(df) else np.zeros(0)
    return [
        {"lo": lo, "hi": hi, **band_metrics(df[_in(odds, lo, hi)], len(df))}
        for lo, hi in ODDS_BANDS
    ]


def quintile_cuts(values) -> list[float] | None:
    """The 20/40/60/80% quantiles of `values` (NaN left out); None with fewer than 5."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 5:
        return None
    return [_r(q, 5) for q in np.quantile(v, QUANTILES)]


def by_cut_band(
    df: pd.DataFrame, col: str, cuts, mean_as: str | None = None, extra=()
) -> list[dict]:
    """Five bands of `col` at `cuts` (lo < value <= hi; open ends None), each with
    band_metrics, the band's mean `col` as `mean_as` (when given) and the mean of each
    `extra` column. Rows without a value in `col` are in no band."""
    if not cuts:
        return []
    edges = [None, *cuts, None]
    vals = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float) if len(df) else []
    vals = np.asarray(vals, dtype=float)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        sub = df[np.isfinite(vals) & _in(vals, lo, hi)] if len(df) else df
        row = {"lo": lo, "hi": hi, **band_metrics(sub)}
        if len(sub) and mean_as:
            row[mean_as] = _r(pd.to_numeric(sub[col]).mean())
        if len(sub):
            for c in extra:
                row[c] = _r(pd.to_numeric(sub[c], errors="coerce").mean())
        out.append(row)
    return out


def with_claimed(df: pd.DataFrame, chance: str) -> pd.DataFrame:
    """The bets with `claimed` = chance x odds - 1 (the edge the strategy saw)."""
    out = df.copy()
    if len(out):
        out["claimed"] = out[chance].astype(float) * out["odds"].astype(float) - 1
    else:
        out["claimed"] = pd.Series(dtype=float)
    return out


def cuts(pooled: pd.DataFrame) -> dict:
    """{edge_cuts, clv_cuts}: the quintile cuts of the pooled domestic bets' claimed edge
    and CLV (with_claimed first)."""
    if not len(pooled):
        return {"edge_cuts": None, "clv_cuts": None}
    return {
        "edge_cuts": quintile_cuts(pooled["claimed"]),
        "clv_cuts": quintile_cuts(pd.to_numeric(pooled["clv"], errors="coerce")),
    }


def blocks(df: pd.DataFrame, band_cuts: dict, extra=()) -> dict:
    """{by_odds_band, by_edge_band, by_clv_band} for one set of bets (with_claimed first)."""
    return {
        "by_odds_band": by_odds_band(df),
        "by_edge_band": by_cut_band(df, "claimed", band_cuts["edge_cuts"], "claimed", extra),
        "by_clv_band": by_cut_band(df, "clv", band_cuts["clv_cuts"]),
    }


def header(band_cuts: dict, edge_from: str) -> dict:
    """The file's top-level description of the bands."""
    return {
        "odds_bands": [[lo, hi] for lo, hi in ODDS_BANDS],
        "edge_cuts": band_cuts["edge_cuts"],
        "clv_cuts": band_cuts["clv_cuts"],
        "edge_from": edge_from,
        "note": NOTE,
    }
