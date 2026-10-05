"""The trade rule, settlement and performance metrics shared by the backtest and the live log.

One rule decides every trade, whether it is replayed on historical DraftKings prices
(backtest.dk_trades) or logged live from the scheduled build (paper.py), and the app's
value pick (bestPick in web/app.js) mirrors it:

* Markets: home, draw, away, over 2.5, under 2.5 (the ones DraftKings prices in the feed).
* Edge = model probability x decimal odds - 1; a market qualifies when edge >= threshold
  (an edge of exactly the threshold qualifies).
* One trade per match: the qualifying market with the highest edge.
* No odds cap by default (results are reported with and without a 6.0 cap).
* Skip a match if either team has fewer than MIN_TEAM_MATCHES in the training window.
* Flat STAKE dollars; paper trades always use PAPER_EDGE, whatever the app's filter shows.

This module does no network or file access, so it can be tested on synthetic leagues.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
import pandas as pd

from soccer_stats.odds import devig_shin

PAPER_EDGE = 0.12  # paper trades open at this edge, whatever the app's filter is set to
FILTER_PRESETS = (0.02, 0.05, 0.08, 0.12)  # the app's minimum-edge buttons
DEFAULT_FILTER = 0.05
STAKE = 10.0  # dollars per trade
MIN_TEAM_MATCHES = 6
CAP_ODDS = 6.0  # the optional cap reported beside the uncapped results
EPS = 1e-9  # so an edge of exactly the threshold isn't lost to rounding

MARKETS = ("home", "draw", "away", "over25", "under25")
PLAYER_MARKETS = ("player_shots", "player_shots_on_target")
MAX_PLAYER_TRADES = 4  # per match: they all ride on the same team's shot volume
GROUPS = {"home": MARKETS[:3], "draw": MARKETS[:3], "away": MARKETS[:3]}
GROUPS.update({"over25": MARKETS[3:], "under25": MARKETS[3:]})
MARKET_LABELS = {
    "player_shots": "Shots",
    "player_shots_on_target": "Shots on target",
    "home": "Home",
    "draw": "Draw",
    "away": "Away",
    "over25": "Over 2.5",
    "under25": "Under 2.5",
}
EDGE_BUCKETS = [(0.12, 0.15, "12–15%"), (0.15, 0.20, "15–20%"), (0.20, math.inf, "20%+")]
ODDS_BUCKETS = [
    (0.0, 2.0, "under 2.0"),
    (2.0, 3.5, "2.0–3.5"),
    (3.5, 6.0, "3.5–6.0"),
    (6.0, math.inf, "over 6.0"),
]
SWEEP = (0.02, 0.05, 0.08, 0.12, 0.15, 0.20)


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def best_pick(
    probs: dict, odds: dict, threshold: float = PAPER_EDGE, max_odds: float | None = None
) -> dict | None:
    """The qualifying market with the highest edge, or None. Mirrors bestPick in app.js.

    `probs` and `odds` map market -> model probability / decimal price.
    """
    best = None
    for m in MARKETS:
        o, p = odds.get(m), probs.get(m)
        if not (_ok(o) and _ok(p)) or o <= 1 or (max_odds is not None and o > max_odds):
            continue
        e = p * o - 1
        if e > 0 and e >= threshold - EPS and (best is None or e > best["edge"]):
            best = {"market": m, "odds": float(o), "model_p": float(p), "edge": float(e)}
    return best


def select_trades(
    candidates: pd.DataFrame,
    threshold: float = PAPER_EDGE,
    max_odds: float | None = None,
    min_team_matches: int = MIN_TEAM_MATCHES,
) -> pd.DataFrame:
    """Apply the trade rule to candidate matches; returns one row per trade.

    Each candidate row has p_<market> and odds_<market> columns, and optionally
    home_n / away_n (team matches in the training window). Every input column is kept,
    plus market, odds, model_p and edge.
    """
    rows = []
    for r in candidates.to_dict("records"):
        n = min(r.get("home_n", math.inf), r.get("away_n", math.inf))
        if n < min_team_matches:
            continue
        pick = best_pick(
            {m: r.get(f"p_{m}") for m in MARKETS},
            {m: r.get(f"odds_{m}") for m in MARKETS},
            threshold,
            max_odds,
        )
        if pick:
            rows.append({**r, **pick})
    cols = list(candidates.columns) + ["market", "odds", "model_p", "edge"]
    return pd.DataFrame(rows, columns=list(dict.fromkeys(cols)))


def trade_id(league: str, season: str, home: str, away: str, *extra: str) -> str:
    """League, season, home and away (plus player and market for player bets)."""
    return "|".join([league, str(season), home, away, *extra])


ENTRY_FIELDS = (
    "id source bet_type league season opened_at kickoff hours_to_kickoff home away market "
    "odds odds_fetched_at model_p model_p_base edge threshold stake news_applied model_ref look "
    "player player_id team line side position"
).split()


def new_trade(
    pick: dict,
    *,
    source: str,
    league: str,
    home: str,
    away: str,
    kickoff: pd.Timestamp,
    opened_at: pd.Timestamp,
    odds_fetched_at: str | None,
    threshold: float,
    model_p_base: float | None = None,
    news_applied: bool = False,
    model_ref: dict | None = None,
    look: str | None = None,
    stake: float = STAKE,
    player: dict | None = None,
) -> dict:
    """The record both the backtest and the live ledger store for a trade (one shape).

    Entry fields (ENTRY_FIELDS) never change after this; close, CLV and settlement
    fields are filled in later.
    """
    kickoff, opened_at = pd.Timestamp(kickoff), pd.Timestamp(opened_at)
    season = season_label(kickoff)
    extra = (player["player"], pick["market"]) if player else ()
    return {
        "id": trade_id(league, season, home, away, *extra),
        "source": source,
        "bet_type": "player" if player else "match",
        "league": league,
        "season": season,
        "opened_at": opened_at.isoformat(timespec="seconds"),
        "kickoff": kickoff.isoformat(timespec="seconds"),
        "hours_to_kickoff": round((kickoff - opened_at) / pd.Timedelta(hours=1), 2),
        "home": home,
        "away": away,
        "market": pick["market"],
        "odds": round(pick["odds"], 4),
        "odds_fetched_at": odds_fetched_at,
        "model_p": round(pick["model_p"], 4),
        "model_p_base": round(model_p_base if _ok(model_p_base) else pick["model_p"], 4),
        "edge": round(pick["edge"], 4),
        "threshold": threshold,
        "stake": stake,
        "news_applied": bool(news_applied),
        "model_ref": model_ref or {},
        "look": look,
        "player": player["player"] if player else None,
        "player_id": player.get("player_id") if player else None,
        "team": player.get("team") if player else None,
        "line": pick.get("line"),
        "side": pick.get("side"),
        "position": None,
        "started": None,
        "actual": None,
        "close_odds": None,
        "close_fetched_at": None,
        "clv_dk": None,
        "clv_pinnacle": None,
        "status": "open",
        "score": None,
        "profit": None,
        "settled_at": None,
    }


def season_label(when: pd.Timestamp) -> str:
    """'2526' for a date in the 2025/26 season (football-data's season code)."""
    y = when.year if when.month >= 7 else when.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def won(market: str, home_goals: int, away_goals: int) -> bool:
    return {
        "home": home_goals > away_goals,
        "draw": home_goals == away_goals,
        "away": home_goals < away_goals,
        "over25": home_goals + away_goals > 2.5,
        "under25": home_goals + away_goals < 2.5,
    }[market]


def settle_player(trade: dict, actual: int | None, started: bool | None) -> dict:
    """Settlement for a player bet: void if he didn't play (actual is None)."""
    if actual is None:
        return {"status": "void", "actual": None, "started": None, "profit": 0.0}
    over = actual > trade["line"]
    w = over if trade["side"] == "over" else not over
    return {
        "status": "won" if w else "lost",
        "actual": int(actual),
        "started": bool(started),
        "profit": round(trade["stake"] * (trade["odds"] - 1), 2) if w else -trade["stake"],
    }


def player_picks(
    lines: pd.DataFrame, threshold: float = PAPER_EDGE, cap_per_match: int = MAX_PLAYER_TRADES
) -> pd.DataFrame:
    """The player trade rule.

    `lines` has one row per priced side: home, away, player, market, line, side
    ("over"/"under"), odds and p (the model's chance of that side, given he plays).
    Keeps, per player, match and market, the line and side with the highest edge if it
    reaches `threshold`; then at most `cap_per_match` per match, the highest edges first.
    """
    if lines.empty:
        return lines.assign(edge=[])
    df = lines.copy()
    df["edge"] = df["p"] * df["odds"] - 1
    df = df[(df["edge"] > 0) & (df["edge"] >= threshold - EPS) & (df["odds"] > 1)]
    if df.empty:
        return df
    df = df.sort_values("edge", ascending=False)
    df = df.drop_duplicates(["home", "away", "player", "market"])
    return df.groupby(["home", "away"], sort=False).head(cap_per_match).reset_index(drop=True)


def devig_pair(over: float | None, under: float | None) -> tuple[float, float] | None:
    """Margin-free over/under probabilities, when both sides are priced."""
    if not (_ok(over) and _ok(under)) or over <= 1 or under <= 1:
        return None
    p = devig_shin(np.array([over, under], dtype=float))
    return float(p[0]), float(p[1])


def settle(trade: dict, home_goals: int | None, away_goals: int | None, void: bool = False) -> dict:
    """Settlement fields for a trade: status, score and profit in dollars."""
    if void or home_goals is None or away_goals is None:
        return {"status": "void", "score": None, "profit": 0.0}
    w = won(trade["market"], int(home_goals), int(away_goals))
    return {
        "status": "won" if w else "lost",
        "score": f"{int(home_goals)}-{int(away_goals)}",
        "profit": round(trade["stake"] * (trade["odds"] - 1), 2) if w else -trade["stake"],
    }


def clv(entry_odds: float, market: str, close: dict) -> float | None:
    """Entry odds x margin-free closing probability - 1 (None without a full closing market).

    `close` maps market -> closing decimal price; the whole group (home/draw/away or
    over/under) is needed to remove the margin.
    """
    group = GROUPS[market]
    prices = [close.get(m) for m in group]
    if not all(_ok(p) and p > 1 for p in prices):
        return None
    p = devig_shin(np.array(prices, dtype=float))[group.index(market)]
    return float(entry_odds * p - 1)


def bucket(x: float, buckets) -> str | None:
    for lo, hi, label in buckets:
        if lo <= x < hi:
            return label
    return None


# ---------- metrics ----------


def max_drawdown(profits: Iterable[float]) -> float:
    """Largest peak-to-trough fall in cumulative profit, in dollars (>= 0)."""
    cum = np.cumsum([0.0, *profits])
    return float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0


def summarize(trades: pd.DataFrame) -> dict:
    """Headline numbers for a set of trades (open trades counted, but not in profit)."""
    n_all = len(trades)
    if n_all == 0:
        return {"trades": 0, "open": 0, "settled": 0}
    status = trades["status"] if "status" in trades else pd.Series("open", index=trades.index)
    settled = trades[status.isin(["won", "lost"])].sort_values("kickoff")
    out = {
        "trades": n_all,
        "open": int((status == "open").sum()),
        "void": int((status == "void").sum()),
        "settled": len(settled),
        "avg_edge": float(trades["edge"].mean()),
    }
    if settled.empty:
        return out
    staked = float(settled["stake"].sum())
    ret = settled["profit"] / settled["stake"]  # return per dollar, per trade
    out.update(
        staked=staked,
        profit=float(settled["profit"].sum()),
        roi=float(settled["profit"].sum() / staked),
        roi_se=float(ret.std(ddof=1) / math.sqrt(len(ret))) if len(ret) > 1 else None,
        win_rate=float((settled["status"] == "won").mean()),
        breakeven=float((1 / settled["odds"]).mean()),
        avg_odds=float(settled["odds"].mean()),
        max_drawdown=max_drawdown(settled["profit"]),
    )
    for col, key in (("clv_dk", "dk"), ("clv_pinnacle", "pinnacle")):
        if col in settled and settled[col].notna().any():
            v = settled[col].dropna()
            out[f"clv_{key}"] = float(v.mean())
            out[f"beat_close_{key}"] = float((v > 0).mean())
    return out


def breakdown(trades: pd.DataFrame, by: str) -> list[dict]:
    """summarize() per group of `by` (a column, or 'edge_bucket' / 'odds_bucket')."""
    if trades.empty:
        return []
    df = trades.copy()
    if by == "edge_bucket":
        df[by] = [bucket(e, EDGE_BUCKETS) or "under 12%" for e in df["edge"]]
    elif by == "odds_bucket":
        df[by] = [bucket(o, ODDS_BUCKETS) for o in df["odds"]]
    return [{"group": str(g), **summarize(sub)} for g, sub in df.groupby(by, sort=True)]


def bootstrap_roi(
    trades: pd.DataFrame, n: int = 2000, seed: int = 0, level: float = 0.95
) -> tuple[float, float] | None:
    """Percentile interval on ROI, resampling whole match weeks (trades in one week move
    together, so resampling single trades would understate the uncertainty)."""
    settled = trades[trades["status"].isin(["won", "lost"])]
    if len(settled) < 2:
        return None
    week = pd.to_datetime(settled["kickoff"], utc=True).dt.tz_convert(None).dt.to_period("W")
    g = settled.groupby(week.astype(str).to_numpy())[["profit", "stake"]].sum()
    if len(g) < 2:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), size=(n, len(g)))
    rois = g["profit"].to_numpy()[idx].sum(1) / g["stake"].to_numpy()[idx].sum(1)
    a = (1 - level) / 2
    return float(np.quantile(rois, a)), float(np.quantile(rois, 1 - a))


def sweep(
    candidates: pd.DataFrame,
    settle_fn,
    thresholds: Iterable[float] = SWEEP,
    caps: Iterable[float | None] = (None, CAP_ODDS),
) -> list[dict]:
    """Trades, ROI and closing line value at each threshold, with and without an odds cap.

    `settle_fn(trades) -> trades` adds status/profit/CLV to selected trades.
    """
    out = []
    for cap in caps:
        for t in thresholds:
            s = summarize(settle_fn(select_trades(candidates, threshold=t, max_odds=cap)))
            out.append({"threshold": t, "max_odds": cap, **s})
    return out


def report(trades: pd.DataFrame) -> dict:
    """Summary, breakdowns and interval for one set of trades (backtest or live)."""
    out = {"summary": summarize(trades)}
    if not trades.empty and "status" in trades:
        ci = bootstrap_roi(trades)
        out["summary"]["roi_ci95"] = list(ci) if ci else None
    out["breakdowns"] = {
        key: breakdown(trades, key)
        for key in (
            "market",
            "edge_bucket",
            "odds_bucket",
            "season",
            "look",
            "bet_type",
            "line",
            "position",
            "started",
        )
        if (key in trades and trades[key].notna().any()) or key.endswith("_bucket")
    }
    return out
