"""Model 12% replayed per league (soccer-stats backtest-edge12; owner, 21 Oct; 0 credits).

The live 12% rule (`trades.best_pick` at 12% on the model's own raw chance, home/draw/away,
one pick per match) on history, the same way lean_backtest replays Blend Lean:

* domestic leagues: the model's walk-forward chances (`backtest.walk_forward`) bet at
  Pinnacle's **early** quoted price (football-data PSH/PSD/PSA, no average or Bet365
  fallback), CLV against Pinnacle's Shin-fair close, every season where both prices cover
  COVERAGE_MIN of the played matches (lean_backtest.coverage); the bets are
  lab/edge12_filters.bets' baseline (the 19 Oct test's rule, here without its 2024/25 cap);
* the Champions League (`kind: "close"`, cup_backtest): the cross-league model at
  DraftKings' bought close, CLV against Pinnacle's close at the same moment.

$1 a bet. The pooled view (`all`) is the early-price leagues only; the Champions League is
reported on its own. E0_dk.json (the Premier League backtest on DraftKings' own historical
prices) is a separate file and stays as it is; this file names it under `draftkings_replay`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import cup_backtest as cb
from soccer_stats import lean_backtest as lb
from soccer_stats.lab import edge12_filters as ef

KIND = "edge12_backtest"
MAX_BETS = 6000  # compact rows kept in the file (newest first); aggregates use every bet
APP_BETS = 200  # compact rows in data.json (newest first); every aggregate stays
BET_FIELDS = [
    "date",
    "league",
    "home",
    "away",
    "market",
    "odds",
    "model_p",
    "edge",
    "won",
    "profit",
    "clv",
]
NOTE = (
    "Model 12% replayed on Pinnacle's early prices from football-data, not on DraftKings: "
    "the model's own chance (no blend), $1 on the side with the biggest edge of at least 12% "
    "against the early quoted price, one bet per match, CLV against Pinnacle's margin-free "
    "close. Pinnacle is a sharper price than DraftKings, so this is a tougher test than the "
    "live one. The Champions League has no early prices: it is replayed at DraftKings' close "
    "and kept out of the pooled numbers."
)
DK_NOTE = (
    "The same 12% rule on DraftKings' own historical prices, Premier League 2025/26 only "
    "(backtest/E0_dk.json, the DraftKings backtest): its first qualifying look per match."
)
# Calibration series: the model's raw walk-forward chance and the margin-free price
# (Pinnacle's early; DraftKings' close for the cup) on every outcome of every candidate.
SERIES = {"model": "p", "price": "mkt"}
MISSING_NOTE = (
    "The Model 12% backtest hasn't been run yet (backtest/edge12_backtest.json is missing), "
    "so there is no history to show."
)


def metrics(df: pd.DataFrame) -> dict:
    """lean_backtest.metrics (bets, wins, win rate vs break-even, ROI and its range,
    profit, CLV and its range, beat-the-close, average odds, bets per market) plus the
    model's average chance and the average claimed edge."""
    out = lb.metrics(df)
    if out.get("bets"):
        out["model_p"] = float(df["model_p"].astype(float).mean())
        out["avg_edge"] = float(df["edge"].astype(float).mean())
    return out


def summary(df: pd.DataFrame) -> dict:
    """{edge12: metrics of every bet, by_season: metrics per season}."""
    seasons = []
    for season, g in df.groupby("season", sort=True) if len(df) else []:
        seasons.append({"season": str(season), "label": lb.season_label(season), **metrics(g)})
    return {"edge12": metrics(df), "by_season": seasons}


def empty_bets(league: str = "") -> pd.DataFrame:
    return ef.bets(pd.DataFrame(), league, "baseline")


def league_report(
    league: str, preds: pd.DataFrame, pins: pd.DataFrame
) -> tuple[dict, pd.DataFrame]:
    """One early-price league from the model's walk-forward chances (`preds`) and
    Pinnacle's prices (`pins`, data.pinnacle_prices for the seasons predicted): coverage,
    the seasons scored, candidates, summary(); plus its bets (covered seasons only)."""
    merged = lb.with_pinnacle(preds, pins)
    cov = lb.coverage(pins)
    included = {c["season"] for c in cov if c["included"]}
    target = lb.target_rows(merged)
    if len(target):
        target = target[target["season"].astype(str).isin(included)]
    b = ef.bets(target, league, "baseline")
    calib, long = lb.calibration(target, b, SERIES, "model_p", league)
    return {
        "kind": "early",
        "coverage": cov,
        "seasons_included": [c["label"] for c in cov if c["included"]],
        "seasons_excluded": [c["label"] for c in cov if not c["included"]],
        "candidates": len(target),
        **summary(b),
        "calibration": calib,
        lb.OUTCOMES: long,
    }, b


def cup_report(
    preds: pd.DataFrame, pin: pd.DataFrame, dk: pd.DataFrame
) -> tuple[dict, pd.DataFrame]:
    """The Champions League at DraftKings' close (cup_backtest): the same rule on the
    cross-league model's walk-forward chances, CLV against Pinnacle's close."""
    rep, target = cb.base(preds, pin, dk)
    b = ef.bets(target, "UCL", "baseline")
    calib, _ = lb.calibration(target, b, SERIES, "model_p", "UCL")
    return {**rep, **summary(b), "calibration": calib}, b


def dk_replay(dk: dict | None) -> dict | None:
    """E0_dk.json's own summary (the DraftKings backtest at 12%), labelled as such."""
    s = (dk or {}).get("summary")
    if not isinstance(s, dict):
        return None
    seasons = (dk or {}).get("seasons")
    keep = (
        "trades",
        "settled",
        "profit",
        "roi",
        "roi_ci95",
        "win_rate",
        "breakeven",
        "avg_odds",
        "avg_edge",
        "clv_dk",
        "beat_close_dk",
        "clv_pinnacle",
        "beat_close_pinnacle",
    )
    return {
        "label": "DraftKings backtest, Premier League"
        + (f" (seasons {seasons})" if seasons else ""),
        "source": "backtest/E0_dk.json -> summary",
        "book": "DraftKings",
        "threshold": (dk or {}).get("threshold"),
        "stake": 10,
        "summary": {k: s.get(k) for k in keep if k in s},
        "note": DK_NOTE,
    }


def _clv(v) -> float | None:
    return None if v is None or not np.isfinite(v) else round(float(v), 4)


def compact(df: pd.DataFrame) -> list[list]:
    """BET_FIELDS rows, newest first."""
    if df.empty:
        return []
    df = df.sort_values(["date", "league", "home"], ascending=[False, True, True])
    return [
        [
            r["date"],
            r["league"],
            r["home"],
            r["away"],
            r["market"],
            round(float(r["odds"]), 3),
            round(float(r["model_p"]), 4),
            round(float(r["edge"]), 4),
            int(bool(r["won"])),
            round(float(r["profit"]), 3),
            _clv(r["clv"]),
        ]
        for r in df.to_dict("records")
    ]


def build(
    reports: dict[str, dict],
    league_bets: dict[str, pd.DataFrame],
    *,
    from_season: int,
    dk: dict | None = None,
    names: dict[str, str] | None = None,
    max_bets: int = MAX_BETS,
    now: pd.Timestamp | None = None,
) -> dict:
    """The saved file (backtest/edge12_backtest.json): the rule, per-league reports, the
    pooled view across the early-price leagues, the DraftKings file named beside it and
    the compact bets (every league, the Champions League's too)."""
    now = now or pd.Timestamp.now(tz="UTC")
    early = [c for c, r in reports.items() if r.get("kind") != cb.KIND]
    frames = [league_bets[c] for c in early if len(league_bets.get(c, []))]
    pooled = pd.concat(frames, ignore_index=True) if frames else empty_bets()
    every = [b for b in league_bets.values() if len(b)]
    rows = compact(pd.concat(every, ignore_index=True)) if every else []
    calib = lb.pooled_calibration(reports, pooled, SERIES, "model_p", early)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "kind": KIND,
        "title": "Model 12% replayed on Pinnacle's early prices",
        "note": NOTE,
        "rule": {
            "price": "Pinnacle early (football-data PSH/PSD/PSA), break-even = 1/decimal",
            "chance": "the model's own walk-forward chance (no blend), as the live test",
            "threshold": ef.THRESHOLD,
            "pick": "biggest edge of at least 12% among home, draw and away (trades.best_pick)",
            "from_season": lb.season_label(f"{from_season % 100:02d}{(from_season + 1) % 100:02d}"),
            "stake": ef.STAKE,
            "coverage_min": lb.COVERAGE_MIN,
            "clv": "entry odds x Pinnacle's margin-free (Shin) close - 1",
            "cup": cb.PRICE + "; " + cb.CLV,
        },
        "leagues": {
            code: {"name": (names or {}).get(code, code), **lb.saved(rep)}
            for code, rep in reports.items()
        },
        "all": {
            **summary(pooled),
            "calibration": calib,
            "leagues": early,
            "by_league": {c: metrics(league_bets[c]) for c in early},
            "note": "Early-price leagues only; the Champions League (priced at the close) is "
            "reported on its own.",
        },
        "draftkings_replay": dk_replay(dk),
        "bet_fields": BET_FIELDS,
        "bets_total": len(rows),
        "bets": rows[:max_bets],
    }


def app_section(found: dict | None, max_bets: int = APP_BETS) -> dict:
    """data.json's portfolio.strategy_backtests.edge12: the file with its bets trimmed to
    the newest `max_bets` (`bets_shown`, `bets_total`), `available` True; without a file,
    {available: False, note}."""
    if not isinstance(found, dict) or found.get("kind") != KIND:
        return {"available": False, "note": MISSING_NOTE}
    rows = found.get("bets") or []
    return {
        **{k: v for k, v in found.items() if k != "bets"},
        "available": True,
        "bets": rows[:max_bets],
        "bets_shown": min(len(rows), max_bets),
        "bets_total": found.get("bets_total", len(rows)),
    }
