"""Player shot lines through the research lab's metrics (lab/metrics.py), holdout locked.

Input: the priced lines the weekly player backtest saves on data-log
(`backtest/E0_player_lines.csv.gz`): one row per FanDuel over line with the model's
walk-forward chance (`p_model`), the walk-forward blend (`p`), 1 / odds (`implied`,
margin included: FanDuel lists overs only) and the outcome. The chances are already
out of sample (fitted on earlier matches only), so they go straight to
`metrics.evaluate`; the lab's `Holdout` keeps 2025/26 out: only development rows
(2023/24 and 2024/25) are scored here. The shots holdout has been reported on every
weekly run already, and is not reopened through the lab without a new
pre-registration (docs/player_props.md).

Each strategy (player_backtest.STRATEGIES) becomes a two-outcome problem: outcome 0 =
the over wins; the bettable odds are [odds, none]. The lab's rule needs a margin-free
market and a fair close, and FanDuel's over-only 1 / odds is neither (it runs about
10 points above what happens), so the market's chance is that price de-margined by a
walk-forward logistic recalibration on earlier lines (`recalibrate`), and CLV is
odds × the de-margined close − 1. Lineup lines are bet at the close: no CLV. The
same metrics on raw 1 / odds are kept beside them, labelled as not a valid test.

Differences from the backtest's own numbers: the lab's bet rule takes every line with
a 12% edge (no best-line-per-player choice, no four-per-match cap), so it makes more
bets; and only development seasons count here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit, logit

from soccer_stats.lab import metrics
from soccer_stats.lab.harness import Holdout
from soccer_stats.player_backtest import STRATEGIES

HOLDOUT_START = pd.Timestamp("2025-07-01", tz="UTC")
KEY = ["kickoff", "player_id", "market", "line", "side"]


REFIT = "28D"
MIN_FIT = 300


def _fit_logit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """y ~ expit(a + b·x) by maximum likelihood."""

    def nll(w):
        z = w[0] + w[1] * x
        return float(np.sum(np.logaddexp(0, z) - y * z))

    return tuple(minimize(nll, [0.0, 1.0], method="L-BFGS-B").x)


def recalibrate(implied, won, times, refit: str = REFIT, min_rows: int = MIN_FIT) -> np.ndarray:
    """FanDuel's implied chance with its margin taken out, walk-forward: each 28-day block
    maps logit(1 / odds) to the over's chance by a logistic fit on lines that kicked off
    before the block (NaN until MIN_FIT earlier lines). Over-only lines can't be
    de-margined from the price alone; this uses only the price and earlier outcomes, so
    it is the market's own information, made fair."""
    x = logit(np.clip(np.asarray(implied, float), 1e-4, 1 - 1e-4))
    y = np.asarray(won, float)
    t = pd.to_datetime(pd.Series(np.asarray(times))).reset_index(drop=True)
    out = np.full(len(x), np.nan)
    edges = pd.date_range(t.min().normalize(), t.max() + pd.Timedelta(refit), freq=refit)
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        te = ((t >= lo) & (t < hi)).to_numpy()
        tr = (t < lo).to_numpy()
        if te.any() and tr.sum() >= min_rows:
            a, b = _fit_logit(x[tr], y[tr])
            out[te] = expit(a + b * x[te])
    return out


def load_lines(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["kickoff"] = pd.to_datetime(df["kickoff"], utc=True)
    df["started"] = df["started"].astype(str).str.lower().isin(["true", "1"])
    return df


def strategy_rows(lines: pd.DataFrame, name: str) -> pd.DataFrame:
    """The lines a strategy scores, with `chance` (the one it bets on) and `close_odds`."""
    kind, chance, starters = STRATEGIES[name]
    df = lines[lines["kind"] == kind].copy()
    df["chance"] = df["p_model"] if chance == "model" else df["p"]
    if starters:
        df = df[df["started"]]
    df = df.dropna(subset=["chance", "odds", "implied"])
    if kind == "look":
        cl = lines[lines["kind"] == "close"].drop_duplicates(KEY).copy()
        cl["fair_close"] = recalibrate(cl["implied"], cl["won"], cl["kickoff"])
        df = df.join(cl.set_index(KEY)[["odds", "fair_close"]].add_prefix("c_"), on=KEY)
        df = df.rename(columns={"c_odds": "close_odds", "c_fair_close": "fair_close"})
    else:
        df["close_odds"] = np.nan
        df["fair_close"] = np.nan
    # The market's own chance with the margin out (fitted on earlier lines of this kind).
    ref = lines[lines["kind"] == kind].drop_duplicates(KEY).copy()
    ref["fair"] = recalibrate(ref["implied"], ref["won"], ref["kickoff"])
    df = df.join(ref.set_index(KEY)["fair"], on=KEY)
    return df.reset_index(drop=True)


def evaluate(
    df: pd.DataFrame, market: str = "fair", level: float = 0.95, n_boot_blend: int = 200
) -> dict:
    """metrics.evaluate on one strategy's rows, plus `passes`.

    market="fair": the market is FanDuel's price de-margined walk-forward (`recalibrate`)
    and CLV is against the de-margined close: the inputs the lab's rule assumes.
    market="raw": 1 / odds and the close's 1 / odds as they are; both carry FanDuel's
    margin, so any calibrated model looks informative and price drift reads as CLV.
    Kept for comparison only; it is not a valid pass test.
    """
    if market == "fair":
        df = df.dropna(subset=["fair"])
    c = df["chance"].clip(1e-6, 1 - 1e-6).to_numpy()
    mcol = "fair" if market == "fair" else "implied"
    m = df[mcol].clip(1e-6, 1 - 1e-6).to_numpy()
    y = np.where(df["won"].to_numpy() == 1, 0, 1)  # outcome 0 = the over wins
    odds = np.column_stack([df["odds"].to_numpy(), np.full(len(df), np.nan)])
    fc = (df["fair_close"] if market == "fair" else 1 / df["close_odds"]).to_numpy()
    fair_close = np.column_stack([fc, 1 - fc])
    groups = (df["home"] + "|" + df["away"] + "|" + df["kickoff"].dt.date.astype(str)).to_numpy()
    r = metrics.evaluate(
        np.column_stack([c, 1 - c]),
        y,
        np.column_stack([m, 1 - m]),
        odds,
        fair_close,
        groups,
        level=level,
        n_boot_blend=n_boot_blend,
    )
    r["matches"] = int(pd.Series(groups).nunique())
    r["passes"] = metrics.passes(r)
    return r


def run(lines: pd.DataFrame, holdout: Holdout | None = None) -> dict:
    """Development-only lab metrics for every strategy (the holdout stays locked)."""
    holdout = holdout or Holdout(HOLDOUT_START)
    dev = holdout.development(lines, "kickoff")
    out = {
        "holdout_start": str(holdout.start.date()),
        "holdout_opened": holdout.unlocked,
        "development_lines": len(dev),
        "seasons": sorted({_season(k) for k in dev["kickoff"]}),
        "strategies": {},
    }
    for name in STRATEGIES:
        df = strategy_rows(dev, name)
        if len(df) < 50:
            out["strategies"][name] = {"rows": len(df)}
            continue
        out["strategies"][name] = {
            "fair_market": _round(evaluate(df, "fair")),
            "raw_price_not_valid": _round(evaluate(df, "raw", n_boot_blend=50)),
        }
    return out


def backtest_dev_summary(players_json: dict) -> dict:
    """The weekly backtest's own trades (trade rule at 12%), development rows only, from
    E0_players.json -> priced.strategies.<name>.trades, for comparison."""
    pr = players_json.get("priced") or {}
    fields = pr.get("trade_fields") or []
    out = {}
    for name, st in (pr.get("strategies") or {}).items():
        rows = [dict(zip(fields, r, strict=True)) for r in st.get("trades") or []]
        dev = [
            r
            for r in rows
            if pd.Timestamp(r["date"], tz="UTC") < HOLDOUT_START and r["status"] in ("won", "lost")
        ]
        if not dev:
            out[name] = {"bets": 0}
            continue
        profit = sum(r["profit"] for r in dev)
        clv = [r["clv"] for r in dev if r.get("clv") is not None]
        out[name] = {
            "bets": len(dev),
            "roi": round(profit / (10 * len(dev)), 4),
            "clv": round(float(np.mean(clv)), 4) if clv else None,
        }
    return out


def _season(k: pd.Timestamp) -> str:
    y = k.year if k.month >= 7 else k.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def _round(x):
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_round(v) for v in x]
    if isinstance(x, float | np.floating):
        return round(float(x), 5)
    if isinstance(x, np.integer):
        return int(x)
    return x
