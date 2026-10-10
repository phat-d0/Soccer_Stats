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
* Flat STAKE dollars. Live match paper trades follow PAPER_RULE (paper_threshold):
  "fixed_raw" (owner's live test, 10 Oct 2026) opens at PAPER_EDGE on the model's own
  chance in every live league; "learned" uses the minimum edge learned from history
  (E0_dk.json -> edge_threshold.min_edge) on the blended chance, where no learned level
  means no new match trades and no file means PAPER_EDGE.
* A second live match strategy, Blend Lean (owner, 10 Oct 2026), runs beside it: the
  per-league blend `p_bet` against DraftKings' break-even in units of sigma (lean_pick,
  z >= LEAN_Z), one trade per match with an id ending "|lean". Trades carry `strategy`
  ("lean" or "edge12"; missing = "edge12").

This module does no network or file access, so it can be tested on synthetic leagues.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np
import pandas as pd

from soccer_stats.odds import devig_shin

PAPER_EDGE = 0.12  # the fixed edge: backtest sweeps, and live trades before any learned level
FILTER_PRESETS = (0.02, 0.05, 0.08, 0.12)  # the app's minimum-edge buttons
STAKE = 10.0  # dollars per trade
MIN_TEAM_MATCHES = 6
CAP_ODDS = 6.0  # the optional cap reported beside the uncapped results
EPS = 1e-9  # so an edge of exactly the threshold isn't lost to rounding

MARKETS = ("home", "draw", "away", "over25", "under25")
PLAYER_MARKETS = ("player_shots", "player_shots_on_target")
MAX_PLAYER_TRADES = 4  # per match: they all ride on the same team's shot volume
# Owner decision (6 Oct 2026): live player paper trades stay OFF until some player rule
# backtests positive. Every FanDuel strategy loses (-17% to -35% at a 12% edge). Player
# lines still show in the app with the blended chance; trades already open still settle.
PLAYER_PAPER_TRADES = False
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
STALE_CLOSE_MINUTES = 60  # a live close quoted earlier than this before kickoff is stale
SWEEP = (0.02, 0.05, 0.08, 0.12, 0.15, 0.20)

# Owner decision (10 Oct 2026): live match paper trades run the old fixed rule as a live
# test in every live league: PAPER_EDGE (12%) on the model's own chance (the card's `p`,
# not the blend `p_bet`), edge against the quoted DraftKings price. "learned" is the
# learned-minimum path (blend chance, per-league min_edge, null = no new trades), kept
# intact: reverting is this one line.
PAPER_RULE = "fixed_raw"
PAPER_RULES = ("fixed_raw", "learned")
OWNER_FIXED_NOTE = (
    "Owner's live test (10 Oct): fixed 12% edge on the model alone. Backtests of this rule "
    "lost money; this tests it live."
)


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def paper_threshold(dk: dict | None, league: str = "E0", rule: str | None = None) -> dict:
    """The rule live match paper trades open at: {threshold, source, note, rule, p_source}.

    `rule` defaults to PAPER_RULE. Under "fixed_raw" every league gets PAPER_EDGE on the
    model's own chance (p_source "model"), source "owner_fixed", whatever `dk` says.
    Under "learned" (p_source "blend": p_bet when set, else p) it is the learned level:

    `dk` is <league>_dk.json (or None). A league other than the Premier League trades only
    with its own learned level: no file or no edge_threshold means no trades. The file's
    top-level `edge_threshold.min_edge` is the level learned from history for the chance
    the app trades on (lab/thresholds.py), the same one the app's bestPick flags with
    (matchEdge):
    - a learned level: threshold = min_edge, source "history";
    - min_edge null (no level beat the market): threshold None, so no new match trades,
      and the note says why;
    - no file or no edge_threshold (older data, first run): PAPER_EDGE, source "default"
      for the Premier League; None, source "none", for any other league.
    """
    rule = rule or PAPER_RULE
    if rule not in PAPER_RULES:
        raise ValueError(f"unknown paper rule {rule!r}")
    if rule == "fixed_raw":
        return {
            "threshold": PAPER_EDGE,
            "source": "owner_fixed",
            "note": OWNER_FIXED_NOTE,
            "rule": "fixed_raw",
            "p_source": "model",
        }
    return {**_learned_threshold(dk, league), "rule": "learned", "p_source": "blend"}


def paper_rule_line(rule: str | None = None) -> str:
    """The paper CLI's log line naming the live match rule."""
    if (rule or PAPER_RULE) == "fixed_raw":
        return f"Paper rule: fixed_raw ({PAPER_EDGE:.0%} on the model's own chance, all leagues)"
    return "Paper rule: learned (each league's learned minimum edge on the blended chance)"


def _learned_threshold(dk: dict | None, league: str) -> dict:
    et = (dk or {}).get("edge_threshold")
    if league != "E0" and (not isinstance(et, dict) or "min_edge" not in et):
        return {
            "threshold": None,
            "source": "none",
            "note": "No new paper trades: no minimum edge has been learned for this league "
            "from its own past bets yet.",
        }
    if not isinstance(et, dict) or "min_edge" not in et:
        return {
            "threshold": PAPER_EDGE,
            "source": "default",
            "note": f"No minimum edge learned from past bets yet, so paper trades use the "
            f"fixed {PAPER_EDGE:.0%} rule.",
        }
    if et["min_edge"] is None:
        why = et.get("note") or "No edge level has beaten the market in past bets."
        return {
            "threshold": None,
            "source": "history",
            "note": f"No new match paper trades: {why}",
        }
    return {"threshold": float(et["min_edge"]), "source": "history", "note": None}


# ---------- Blend Lean: the second live match strategy (owner, 10 Oct 2026) ----------

# Ported from the owner's baseball app (confidence tiers): the blended chance `p_bet` (the
# per-league h2h blend, match_calibration.league_fit) against the break-even rate of
# DraftKings' quoted price, in units of sigma (how far the blend usually strays from the
# margin-free price on its fit rows). z = (p_bet - 1/decimal) / sigma; Lean z >= 1,
# Strong z >= 2 (cumulative: a Strong pick is a Lean one). One bet per match, home/draw/
# away only: the side with the highest z. It runs beside the fixed 12% test, not instead.
LEAN_Z = 1.0
STRONG_Z = 2.0
EDGE_TIERS = [
    {"key": "lean", "label": "Lean", "min_z": LEAN_Z},
    {"key": "strong", "label": "Strong", "min_z": STRONG_Z},
]
LEAN_RULE = "lean_1sigma"
LEAN_MARKETS = MARKETS[:3]
LEAN_NOTE = (
    "Owner's second live test (10 Oct), ported from the baseball app: the blend of model "
    "and DraftKings' margin-free price, one bet per match on the side whose chance beats the "
    "price's break-even by at least one sigma. The blend gives the model almost no weight, so "
    "expect few picks; baseball's own tier backtest was noise."
)
# Match strategies paper traded side by side, in the order the app lists them. A match
# trade without a `strategy` field (all of them before 10 Oct) is the fixed 12% test.
STRATEGY_ORDER = ["lean", "edge12"]
STRATEGY_LABELS = {"lean": "Blend Lean (1σ+)", "edge12": "Model 12% (your live test)"}


def strategy_of(trade: dict) -> str:
    """A match trade's strategy: its own `strategy` field, else "edge12"."""
    return trade.get("strategy") or "edge12"


def side_z(p_bet: dict | None, odds: dict | None, sigma: float | None) -> dict | None:
    """z per home/draw/away: (p_bet - 1/decimal price) / sigma; None when unknown.

    The price keeps its margin (break-even = 1/decimal), as in the baseball app.
    """
    if not p_bet or not odds or not _ok(sigma) or sigma <= 0:
        return None
    out = {}
    for m in LEAN_MARKETS:
        p, o = p_bet.get(m), odds.get(m)
        out[m] = float((p - 1 / o) / sigma) if _ok(p) and _ok(o) and o > 1 else None
    return out


def lean_pick(
    p_bet: dict | None, odds: dict | None, sigma: float | None, min_z: float = LEAN_Z
) -> dict | None:
    """The Lean pick: the home/draw/away side with the highest z, if z >= min_z.

    Returns {market, odds, p_bet, model_p (= p_bet, the chance it trades on), edge (p_bet x
    odds - 1), z, tier ("strong" at z >= STRONG_Z, else "lean"), sigma}, or None. Ties keep
    the first side in market order, as best_pick does.
    """
    z = side_z(p_bet, odds, sigma)
    if not z:
        return None
    best = None
    for m in LEAN_MARKETS:
        if z[m] is not None and (best is None or z[m] > z[best]):
            best = m
    if best is None or z[best] < min_z - EPS:
        return None
    p, o = float(p_bet[best]), float(odds[best])
    return {
        "market": best,
        "odds": o,
        "p_bet": p,
        "model_p": p,
        "edge": p * o - 1,
        "z": z[best],
        "tier": "strong" if z[best] >= STRONG_Z - EPS else "lean",
        "sigma": float(sigma),
    }


def lean_rule(blends: dict | None, league: str) -> dict:
    """The Lean rule for one league: {key, rule, min_z, strong_z, p_source, stake, sigma,
    matches, source, note}. `blends` is data.json's `match_blends` (publish.add_lean); no
    file or no fit for the league = sigma None, so no Lean picks, and the note says why."""
    entry = ((blends or {}).get("leagues") or {}).get(league) or {}
    sd = entry.get("sigma")
    ok = bool(entry.get("coef")) and _ok(sd) and sd > 0
    if ok:
        note = LEAN_NOTE
    elif not (blends or {}).get("leagues"):
        note = (
            "No Lean picks: the per-league blend fits (backtest/match_blends.json) aren't "
            "available yet."
        )
    else:
        note = "No Lean picks in this league: it has no blend fit yet."
    return {
        "key": "lean",
        "rule": LEAN_RULE,
        "min_z": LEAN_Z,
        "strong_z": STRONG_Z,
        "p_source": "blend",
        "stake": STAKE,
        "sigma": float(sd) if ok else None,
        "matches": entry.get("matches"),
        "source": "match_blends" if ok else "none",
        "note": note,
    }


def best_pick(
    probs: dict, odds: dict, threshold: float | None = PAPER_EDGE, max_odds: float | None = None
) -> dict | None:
    """The qualifying market with the highest edge, or None. Mirrors bestPick in app.js.

    `probs` and `odds` map market -> model probability / decimal price. A threshold of
    None (no learned minimum edge) qualifies nothing, as in the app.
    """
    if threshold is None:
        return None
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


# The portfolios the app shows, one per strategy. Adding a strategy is one entry here:
# id, display name, status ("live", "testing" or "retired"), a plain-English note, and
# the stem of its backtest file on data-log (backtest/<league>_<backtest>.json).
PORTFOLIOS = [
    {
        "id": "moneyline",
        "name": "Moneyline",
        "status": "live",
        "note": "Match bets (home, draw, away) at DraftKings' price, one per match on the "
        "best edge.",
        "backtest": "dk",
    },
    {
        "id": "goalscorer",
        "name": "Anytime goalscorer",
        "status": "testing",
        "note": "Testing: the model predicts scorers better than season averages in all five "
        "leagues tested, but bookmakers price 'to score' about 50% above how often players "
        "score, so there are no trades. A forward check on this season runs in December.",
        "backtest": "goalscorer",
    },
    {
        "id": "corners",
        "name": "Team corners",
        "status": "testing",
        "note": "Owner's live test (10 Oct): each team's corners, model (f) at a 12% edge "
        "against Pinnacle's team-corner prices in six leagues, $10 a trade. In development the "
        "model beat the league average in all six leagues, but its chances were well "
        "calibrated only in the Premier League, Serie A and Ligue 1; the locked February test "
        "decides it. There are no historical corner prices, so its backtest is the research "
        "record, not money.",
        "backtest": "corners",
    },
    {
        "id": "player_shots",
        "name": "Player shots",
        "status": "retired",
        "note": "Retired on 6 Oct 2026: no player shot rule made money in testing, because "
        "FanDuel's over-only lines carry too big a margin. Kept for its history.",
        "backtest": "players",
    },
]
PORTFOLIO_IDS = [p["id"] for p in PORTFOLIOS]
# Markets that decide a trade's portfolio when it carries no `portfolio` field.
PORTFOLIO_MARKETS = {
    "player_shots": "player_shots",
    "player_shots_on_target": "player_shots",
    "player_goal_scorer_anytime": "goalscorer",
    "team_corners": "corners",
}


def portfolio_of(trade: dict) -> str:
    """The portfolio a trade belongs to: its own `portfolio` field (set when it opens),
    else by market (player markets), else by bet type (match bets are moneyline)."""
    if trade.get("portfolio") in PORTFOLIO_IDS:
        return trade["portfolio"]
    if trade.get("market") in PORTFOLIO_MARKETS:
        return PORTFOLIO_MARKETS[trade["market"]]
    if trade.get("bet_type") == "corners":
        return "corners"
    return "player_shots" if trade.get("bet_type") == "player" else "moneyline"


def trade_id(league: str, season: str, home: str, away: str, *extra: str) -> str:
    """League, season, home and away (plus player and market for player bets)."""
    return "|".join([league, str(season), home, away, *extra])


ENTRY_FIELDS = (
    "id source bet_type league season opened_at kickoff hours_to_kickoff home away market "
    "odds odds_fetched_at model_p model_p_base edge threshold stake news_applied model_ref look "
    "player player_id team line side position portfolio rule p_source strategy"
).split()
# A Lean trade also records how it qualified (trades.lean_pick) and the raw model chance.
LEAN_ENTRY_FIELDS = (*ENTRY_FIELDS, "z", "tier", "sigma", "p_bet", "p_model")


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
    rule: str | None = None,
    p_source: str | None = None,
    strategy: str | None = None,
) -> dict:
    """The record both the backtest and the live ledger store for a trade (one shape).

    Live match trades record the paper `rule` ("fixed_raw", "learned" or "lean_1sigma"),
    `p_source` ("model": the raw chance; "blend": p_bet when set) beside `threshold`, and
    their `strategy` ("edge12" or "lean"; STRATEGY_ORDER). A Lean trade's id ends "|lean",
    so a match can hold one trade per strategy.

    Entry fields (ENTRY_FIELDS) never change after this; close, CLV and settlement
    fields are filled in later.
    """
    kickoff, opened_at = pd.Timestamp(kickoff), pd.Timestamp(opened_at)
    season = season_label(kickoff)
    extra = (player["player"], pick["market"]) if player else ()
    if strategy == "lean":
        extra = ("lean",)
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
        "portfolio": portfolio_of({"bet_type": "player" if player else "match", **pick}),
        "rule": rule,
        "p_source": p_source,
        "strategy": strategy,
        "started": None,
        "actual": None,
        "close_odds": None,
        "close_fetched_at": None,
        "close_minutes_before": None,
        "clv_dk": None,
        "beat_close_dk": None,
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
    over = actual >= math.ceil(trade["line"])  # 1.5 = 2 or more; FanDuel's 1.0 = 1 or more
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
    ("over"/"under"), odds and p (the chance of that side, given he plays: live, the
    blend of model and price; None or NaN never trades).
    Keeps, per player, match and market, the line and side with the highest edge if it
    reaches `threshold`; then at most `cap_per_match` per match, the highest edges first.
    """
    if lines.empty:
        return lines.assign(edge=[])
    df = lines.copy()
    df["p"] = pd.to_numeric(df["p"], errors="coerce")  # no chance (None) = no trade
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


# ---------- team corners (owner's live test, 10 Oct 2026) ----------

# Corners model (f) (edge/corners2.py) against Pinnacle's team-corner prices in every live
# league: the model's own chance, edge = P(win) x Pinnacle's decimal price + P(push) - 1
# (= p x odds - 1 on a half line), at least CORNERS_EDGE, $10, at most one trade per match,
# team and line (the better of over and under). Paper only; P/L and CLV only, no metric of
# the locked February test (docs/totals.md, corners bake-off 2).
CORNERS_EDGE = PAPER_EDGE
CORNERS_MARKET = "team_corners"
CORNERS_NOTE = (
    "Owner's live test (10 Oct): corners model (f) at a fixed 12% edge on its own chance "
    "against Pinnacle's team-corner prices, all six leagues. The February test is unchanged."
)

CORNER_ENTRY_FIELDS = (*ENTRY_FIELDS, "team_side", "p_push", "bookmaker")


def corners_rule() -> dict:
    """The corners paper rule, as paper_threshold's dict (the app reads the same shape)."""
    return {
        "threshold": CORNERS_EDGE,
        "source": "owner_fixed",
        "note": CORNERS_NOTE,
        "rule": "fixed_raw",
        "p_source": "model_f",
        "book": "pinnacle",
        "stake": STAKE,
    }


def new_corner_trade(
    pick: dict,
    *,
    league: str,
    home: str,
    away: str,
    team_side: str,
    line: float,
    kickoff: pd.Timestamp,
    opened_at: pd.Timestamp,
    odds_fetched_at: str | None,
    snapshot: str | None,
    ref: dict | None = None,
) -> dict:
    """A corners paper trade: new_trade's shape plus the team line. `pick` is
    corners_live.pick's {side, odds, model_p, p_push, edge}. The id adds the team side and
    line, so a match can hold one trade per team line."""
    t = new_trade(
        {
            "market": CORNERS_MARKET,
            "odds": pick["odds"],
            "model_p": pick["model_p"],
            "edge": pick["edge"],
            "line": float(line),
            "side": pick["side"],
        },
        source="live",
        league=league,
        home=home,
        away=away,
        kickoff=kickoff,
        opened_at=opened_at,
        odds_fetched_at=odds_fetched_at,
        threshold=CORNERS_EDGE,
        model_ref=ref,
        look=snapshot,
        rule="fixed_raw",
        p_source="model_f",
    )
    t.update(
        id=trade_id(league, t["season"], home, away, "corners", team_side, f"{float(line):g}"),
        bet_type="corners",
        portfolio="corners",
        team=home if team_side == "home" else away,
        team_side=team_side,
        p_push=pick.get("p_push", 0.0),
        bookmaker="pinnacle",
    )
    return t


def settle_corner(trade: dict, count: int | None) -> dict:
    """Settlement for a team-corner bet from the team's corner count: over wins at count
    > line, under at count < line; count == line (a whole line) is a push, voided with the
    stake back. No count: void."""
    if count is None or (isinstance(count, float) and math.isnan(count)):
        return {"status": "void", "actual": None, "push": False, "profit": 0.0}
    count, line = int(count), float(trade["line"])
    if count == line:
        return {"status": "void", "actual": count, "push": True, "profit": 0.0}
    w = count > line if trade["side"] == "over" else count < line
    return {
        "status": "won" if w else "lost",
        "actual": count,
        "push": False,
        "profit": round(trade["stake"] * (trade["odds"] - 1), 2) if w else -trade["stake"],
    }


def corner_clv(entry_odds: float, side: str, over: float | None, under: float | None):
    """Entry odds x Pinnacle's margin-free closing chance of the side - 1 (Shin)."""
    fair = devig_pair(over, under)
    if fair is None:
        return None
    return float(entry_odds * (fair[0] if side == "over" else fair[1]) - 1)


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
    if "close_minutes_before" in settled and "clv_dk" in settled:
        # Live closes come from throttled scheduled builds: count the stale ones.
        mins = pd.to_numeric(settled.loc[settled["clv_dk"].notna(), "close_minutes_before"])
        if mins.notna().any():
            out["close_early"] = int((mins > STALE_CLOSE_MINUTES).sum())
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
