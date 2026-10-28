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

from soccer_stats import match_calibration as mc
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
# The owner's odds range for Blend Lean (24 Oct; docs/leagues.md "Blend Lean odds range"):
# from LEAN_RANGE_FROM a Lean trade opens only when the picked side's DraftKings decimal
# odds are in lo < odds <= hi (the backtests' bands convention, so +200 = 3.00 stays in and
# -200 = 1.50 is out). The pick is still the highest-z side; outside the range no Lean
# trade opens (no fallback to another side) and a shadow entry records what the old rule
# would have bet (paper.py, `<code>_lean_shadow_<season>.jsonl`).
LEAN_ODDS_RANGE = (1.50, 3.00)
LEAN_RANGE_FROM = "2026-10-24T07:32:00+00:00"  # the commit that adds the rule
LEAN_RULE_VERSION = 2  # trades without `rule_version` are version 1 (any odds)
LEAN_RANGE_NOTE = (
    "Since 24 Oct, Blend Lean only trades between -200 and +200 (decimal 1.50 to 3.00): "
    "owner's decision after the first 15 settled trades. Picks outside the range open no "
    "trade; the old rule (all odds) is tracked beside it as a shadow."
)
SHADOW_SUFFIX = "|shadow"  # a Lean shadow entry's id: the Lean trade id plus this


def in_lean_range(odds: float | None, rng: tuple[float, float] = LEAN_ODDS_RANGE) -> bool:
    """Whether a decimal price is inside the Lean odds range (lo < odds <= hi)."""
    lo, hi = rng
    return _ok(odds) and lo < float(odds) <= hi + EPS


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


# What `driver` means, in plain English, for the app (Lean live section and the backtest).
DRIVER_NOTE = (
    "Market-driven: the pick still clears 1 sigma when the model's term is taken out of the "
    "fitted blend (the blend's other weights are kept as fitted, not refitted). Model-driven: "
    "it clears 1 sigma only with the model's term in. The model's term compares the model's "
    "chances across home, draw and away, not against the price, so where the model's weight "
    "is positive it lifts the outcome the model rates highest relative to the others, even "
    "when the model rates that outcome below the market."
)


def lean_driver(z_market: float | None) -> str | None:
    """What carried a Lean pick: "market" when it reaches LEAN_Z even with the model term
    removed (z_market, market_side_z), else "model"; None when z_market is unknown."""
    if not _ok(z_market):
        return None
    return "market" if z_market >= LEAN_Z - EPS else "model"


def market_side_z(coef: list | None, odds: dict | None, sigma: float | None) -> dict | None:
    """z per home/draw/away with the blend's model term removed (c = 0; the league's a_k
    and b kept): the blend of the price alone, from the margin-free (Shin) chances of
    these `odds`, against their break-even (1/decimal) in units of `sigma`. None without
    coefficients, sigma or a full priced home/draw/away."""
    if not coef or not odds:
        return None
    prices = [odds.get(m) for m in LEAN_MARKETS]
    if not all(_ok(o) and o > 1 for o in prices):
        return None
    fair = devig_shin(np.array(prices, dtype=float))
    pm = mc.without_model(coef, [fair])[0]
    if not np.isfinite(pm).all():
        return None
    return side_z({m: float(v) for m, v in zip(LEAN_MARKETS, pm, strict=True)}, odds, sigma)


def lean_pick(
    p_bet: dict | None,
    odds: dict | None,
    sigma: float | None,
    min_z: float = LEAN_Z,
    z_market: dict | None = None,
) -> dict | None:
    """The Lean pick: the home/draw/away side with the highest z, if z >= min_z.

    Returns {market, odds, p_bet, model_p (= p_bet, the chance it trades on), edge (p_bet x
    odds - 1), z, tier ("strong" at z >= STRONG_Z, else "lean"), sigma}, or None. Ties keep
    the first side in market order, as best_pick does. With `z_market` (market_side_z) the
    pick adds the picked side's `z_market` and its `driver` (lean_driver).
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
    out = {
        "market": best,
        "odds": o,
        "p_bet": p,
        "model_p": p,
        "edge": p * o - 1,
        "z": z[best],
        "tier": "strong" if z[best] >= STRONG_Z - EPS else "lean",
        "sigma": float(sigma),
    }
    if z_market is not None:
        zm = z_market.get(best)
        out["z_market"] = float(zm) if _ok(zm) else None
        out["driver"] = lean_driver(out["z_market"])
    return out


def lean_rule(blends: dict | None, league: str) -> dict:
    """The Lean rule for one league: {key, rule, min_z, strong_z, p_source, stake, sigma,
    sigma_dnb, coef, matches, source, note, fit_made_at, odds_range, odds_range_from,
    odds_range_note, rule_version}. `blends` is data.json's `match_blends` (publish.add_lean); no
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
        # The draw no bet track's unit (match_calibration.sigma_dnb); None = no DNB picks.
        "sigma_dnb": float(entry["sigma_dnb"])
        if ok and _ok(entry.get("sigma_dnb")) and entry["sigma_dnb"] > 0
        else None,
        "coef": entry.get("coef") if ok else None,  # for the driver test (market_side_z)
        "matches": entry.get("matches"),
        "source": "match_blends" if ok else "none",
        "note": note,
        # When the fit was made (match_blends.json's generated_at, else its fit_date): a
        # chance recomputed from it for a trade opened earlier uses a later fit.
        "fit_made_at": (blends or {}).get("generated_at") or entry.get("fit_date"),
        "odds_range": list(LEAN_ODDS_RANGE),
        "odds_range_from": LEAN_RANGE_FROM,
        "odds_range_note": LEAN_RANGE_NOTE,
        "rule_version": LEAN_RULE_VERSION,
    }


# ---------- draw no bet tracks (owner, 26 Oct; docs/leagues.md "Draw no bet tracks") ----------

# Each match strategy gets a draw no bet (DNB) track beside it: back home or away, the stake
# refunded on a draw. Paper only and descriptive: the 1X2 trades stay the record. The 24 Oct
# backtest (docs/lab.md "Draw no bet") found no edge either way (CLV per $ about the same as
# 1X2), with fewer losing streaks. The DNB price is built from DraftKings' h2h (back the side,
# cover the draw: side x (1 - 1/draw)), so no new market is fetched; the margin-free DNB
# chance is the Shin 1X2 chance with the draw taken out. The chances are the parent's own
# (the raw model for Model 12%, the blend for Lean), with the draw taken out.
DNB_SIDES = ("home", "away")
DNB_SUFFIX = "|dnb"  # a DNB trade's id: the parent trade's id plus this
DNB_FROM = "2026-10-26T08:23:00+00:00"  # the commit that adds the tracks
DNB_TRACKS = {"lean": "lean_dnb", "edge12": "edge12_dnb"}  # parent strategy -> its track
DNB_LABELS = {"lean_dnb": "Blend Lean, draw no bet", "edge12_dnb": "Model 12%, draw no bet"}
DNB_RULE_NAMES = {"lean_dnb": "dnb_lean_1sigma", "edge12_dnb": "dnb_fixed_raw"}
DNB_NOTE = (
    "Draw no bet, tracked beside the 1X2 trades (owner, 26 Oct), which stay the record: home or "
    "away only, and a draw refunds the stake (shown as a push). The price is built from "
    "DraftKings' 1X2 prices (back the side and cover the draw: side x (1 - 1/draw)); "
    "DraftKings' own draw no bet price may carry a bigger margin. ROI counts only bets that "
    "weren't refunded. The 24 Oct backtest found no edge: it loses to the closing price about "
    "as much per dollar as 1X2, with less swing."
)
DNB_LEAN_NOTE = (
    "Blend Lean's rule on draw no bet: the blend's chance with the draw taken out, at least 1 "
    "sigma above the DNB price's break-even (sigma for draw no bet, per league). The odds range "
    "(decimal 1.50 to 3.00) applies to the DNB price."
)
DNB_EDGE12_NOTE = (
    "Model 12%'s rule on draw no bet: the model's chance with the draw taken out, at least a "
    "12% edge at the DNB price (edge per stake not refunded)."
)


def dnb_odds(odds_side, odds_draw) -> float | None:
    """The DNB decimal price from a 1X2 book: back the side and cover the draw so a draw
    returns the whole stake, side x (1 - 1/draw); None without both prices above 1."""
    if not (_ok(odds_side) and _ok(odds_draw)) or odds_side <= 1 or odds_draw <= 1:
        return None
    return float(odds_side) * (1 - 1 / float(odds_draw))


def fair_dnb(prices) -> dict | None:
    """{home, away, draw}: the Shin-fair 1X2 chances of (home, draw, away) prices, home and
    away with the draw taken out (they sum to 1); `draw` is the fair draw chance."""
    vals = list(prices)
    if len(vals) != 3 or not all(_ok(v) and v > 1 for v in vals):
        return None
    f = devig_shin(np.array(vals, dtype=float))
    s = f[0] + f[2]
    return {"home": float(f[0] / s), "away": float(f[2] / s), "draw": float(f[1])}


def without_draw(p: dict | None) -> dict | None:
    """{home, away} = p_side / (p_home + p_away), or None."""
    h, a = (p or {}).get("home"), (p or {}).get("away")
    if not (_ok(h) and _ok(a)) or h + a <= 0:
        return None
    return {"home": float(h) / (h + a), "away": float(a) / (h + a)}


def dnb_prices(odds: dict | None) -> dict | None:
    """{home, away}: a 1X2 book's DNB prices (dnb_odds), or None unless both are priced."""
    o = odds or {}
    out = {s: dnb_odds(o.get(s), o.get("draw")) for s in DNB_SIDES}
    return out if all(v is not None for v in out.values()) else None


def dnb_pick_12(q: dict | None, odds: dict | None, threshold: float = PAPER_EDGE) -> dict | None:
    """DNB 12%: the side with the larger edge q x DNB price - 1, if at least `threshold`
    (and above 0); home first on a tie. {market, odds, model_p, edge, z: None}."""
    best = None
    for s in DNB_SIDES:
        p, o = (q or {}).get(s), (odds or {}).get(s)
        if not (_ok(p) and _ok(o)) or o <= 1:
            continue
        e = float(p) * float(o) - 1
        if e >= threshold - EPS and e > 0 and (best is None or e > best["edge"]):
            best = {"market": s, "odds": float(o), "model_p": float(p), "edge": e, "z": None}
    return best


def dnb_pick_lean(
    qb: dict | None, odds: dict | None, sigma: float | None, min_z: float = LEAN_Z
) -> dict | None:
    """DNB Lean: the side with the higher z = (q_blend - 1/DNB price) / sigma, if at least
    `min_z`; home first on a tie. {market, odds, model_p, edge, z}."""
    if not _ok(sigma) or sigma <= 0:
        return None
    best = None
    for s in DNB_SIDES:
        p, o = (qb or {}).get(s), (odds or {}).get(s)
        if not (_ok(p) and _ok(o)) or o <= 1:
            continue
        z = (float(p) - 1 / float(o)) / float(sigma)
        if z >= min_z - EPS and (best is None or z > best["z"]):
            best = {"market": s, "odds": float(o), "model_p": float(p), "edge": p * o - 1, "z": z}
    return best


def settle_dnb(trade: dict, home_goals, away_goals, void: bool = False) -> dict:
    """Settlement for a DNB trade: a draw refunds the stake (status "void", push True, as
    a corner push), else won or lost as the 1X2 side; no score or `void`: void."""
    if void or home_goals is None or away_goals is None:
        return {"status": "void", "score": None, "push": False, "profit": 0.0}
    h, a = int(home_goals), int(away_goals)
    score = f"{h}-{a}"
    if h == a:
        return {"status": "void", "score": score, "push": True, "profit": 0.0}
    w = won(trade["market"], h, a)
    return {
        "status": "won" if w else "lost",
        "score": score,
        "push": False,
        "profit": round(trade["stake"] * (trade["odds"] - 1), 2) if w else -trade["stake"],
    }


def dnb_clv(entry_odds: float, market: str, close: dict | None) -> tuple[float, float] | None:
    """(CLV, CLV per $ staked) of a DNB bet against a closing 1X2 book (home/draw/away):
    entry DNB odds x the close's fair DNB chance - 1, per stake not refunded, and the same
    x the close's no-draw chance. None without a full close."""
    c = close or {}
    f = fair_dnb([c.get(m) for m in LEAN_MARKETS])
    if f is None or market not in DNB_SIDES:
        return None
    v = float(entry_odds) * f[market] - 1
    return v, v * (1 - f["draw"])


def dnb_rule(parent: dict | None, track: str) -> dict:
    """A DNB track's rule from its parent's: Lean's (lean_rule, one league) gives min_z, the
    league's sigma_dnb as `sigma` and the odds range on the DNB price; Model 12%'s
    (paper_threshold) its threshold and p_source. Both add key, rule, variant_of, adds
    "dnb", stake, defined_at and the note."""
    parent = parent or {}
    base = {
        "key": track,
        "rule": DNB_RULE_NAMES[track],
        "variant_of": "lean" if track == "lean_dnb" else "edge12",
        "adds": "dnb",
        "stake": STAKE,
        "defined_at": DNB_FROM,
        "price": "DraftKings' 1X2: side x (1 - 1/draw)",
    }
    if track == "lean_dnb":
        sd = parent.get("sigma_dnb")
        return {
            **base,
            "min_z": parent.get("min_z", LEAN_Z),
            "p_source": "blend",
            "sigma": sd if _ok(sd) and sd > 0 else None,
            "odds_range": list(LEAN_ODDS_RANGE),
            "odds_range_on": "dnb_price",
            "fit_made_at": parent.get("fit_made_at"),
            "note": DNB_LEAN_NOTE + " " + DNB_NOTE,
        }
    return {
        **base,
        "threshold": parent.get("threshold"),
        "p_source": parent.get("p_source") or "model",
        "note": DNB_EDGE12_NOTE + " " + DNB_NOTE,
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
# id, display name, status ("live", "testing", "retired", or "closed" for research that
# finished without ever trading), a plain-English note, and
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
        "against Pinnacle's team-corner prices in six leagues (League One and League Two "
        "added on 24 Oct, where the model is untested), $10 a trade; since 20 Oct the "
        "same rule also runs against DraftKings' prices as a second track. In development the "
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
    {
        "id": "comebacks",
        "name": "In-play research",
        "status": "closed",
        "note": "Research on betting during the match, at the in-play price: each study is "
        "pre-registered, tested on bought historical prices and kept here, pass or fail. They "
        "are never traded here: one that passes becomes a paper track first. The first, "
        "backing the favourite behind at half-time, closed on 25 Oct 2026 with no edge (the "
        "price already expects the comeback and overrates it).",
        "backtest": "comebacks",
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


def holds(ledger, tid: str) -> bool:
    """Whether `ledger` (ids, or a dict keyed by them) already holds trade `tid`, ignoring
    case: ESPN renamed a Champions League club between builds ("VIKING FK" then "Viking
    FK", 18 Oct), and the second spelling must not open a second trade on the match."""
    if tid in ledger:
        return True
    key = tid.casefold()
    return any(x.casefold() == key for x in ledger)


ENTRY_FIELDS = (
    "id source bet_type league season opened_at kickoff hours_to_kickoff home away market "
    "odds odds_fetched_at model_p model_p_base edge threshold stake news_applied model_ref look "
    "player player_id team line side position portfolio rule p_source strategy"
).split()
# A Lean trade also records how it qualified (trades.lean_pick), the raw model chance and
# its driver (z with the model term removed; trades opened before 11 Oct get both from a
# paper update event instead, paper._lean_driver_update).
LEAN_ENTRY_FIELDS = (*ENTRY_FIELDS, "z", "tier", "sigma", "p_bet", "p_model", "z_market", "driver")


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
    "against Pinnacle's team-corner prices, in the six leagues and, from 24 Oct, League One "
    "and League Two (Pinnacle only there). The February test is unchanged."
)

CORNERS_DK_NOTE = (
    "Owner's second corner track (20 Oct): the same model (f) and fixed 12% edge on its own "
    "chance, against DraftKings' team-corner prices (Premier League, La Liga, Bundesliga, "
    "Serie A, Ligue 1; DraftKings lists no team corners in the Championship, League One or "
    "League Two). CLV against "
    "DraftKings' own close, and against Pinnacle's close where Pinnacle has the same line."
)

CORNER_ENTRY_FIELDS = (*ENTRY_FIELDS, "team_side", "p_push", "bookmaker")

# Corner strategies, one per book, in the order the app lists them. A corner trade without
# a `strategy` field (all of them before 20 Oct) is the Pinnacle track.
CORNER_STRATEGY_ORDER = ["pinnacle", "draftkings"]
CORNER_STRATEGY_LABELS = {
    "pinnacle": "Model (f) 12% vs Pinnacle",
    "draftkings": "Model (f) 12% vs DraftKings",
}
CORNER_ID_SUFFIX = {"pinnacle": (), "draftkings": ("dk",)}  # Pinnacle's ids are unchanged


def corner_strategy_of(trade: dict) -> str:
    """A corner trade's strategy: its own `strategy`, else its `bookmaker`, else
    "pinnacle" (every corner trade opened before 20 Oct)."""
    s = trade.get("strategy") or trade.get("bookmaker")
    return s if s in CORNER_STRATEGY_ORDER else "pinnacle"


def corners_rule(book: str = "pinnacle") -> dict:
    """The corners paper rule at a book, as paper_threshold's dict (the app reads the same
    shape). The default is the Pinnacle track's rule, unchanged."""
    out = {
        "threshold": CORNERS_EDGE,
        "source": "owner_fixed",
        "note": CORNERS_NOTE,
        "rule": "fixed_raw",
        "p_source": "model_f",
        "book": "pinnacle",
        "stake": STAKE,
    }
    if book == "draftkings":
        out.update(note=CORNERS_DK_NOTE, book="draftkings")
    return out


# ---------- shadow variants: a subset of a strategy's own trades (owner, 23 Oct) ----------

# The below-60% filter's pre-registration commit (e0b0bcd, docs/totals.md "Shadow filter:
# below 60%"): only trades opened at or after this time are judged.
SHADOW_DEFINED_AT = "2026-10-23T08:39:09+00:00"
BELOW60_MAX = 0.60
BELOW60_NOTE = (
    "A shadow of its track: the same trades, keeping only those where model (f) gave the "
    "bet less than a 60% chance (given no push). No bets of its own. Judged only on trades "
    "opened from 23 Oct 08:39 UTC, when the filter was pre-registered, on CLV against "
    "Pinnacle's close; the cut-off was chosen after seeing the earlier trades, so those are "
    "shown but not judged."
)


def chance_no_push(trade: dict) -> float | None:
    """The chance a trade was opened on, given no push: model_p / (1 - p_push) for a
    corner line (p_push missing = 0); model_p for anything else."""
    p = trade.get("model_p")
    if p is None:
        return None
    push = float(trade.get("p_push") or 0.0) if trade.get("market") == "team_corners" else 0.0
    return float(p) / (1 - push) if push < 1 else None


# Filter fields a variant can use: a name -> how to read it from a trade (anything else
# is read as the trade's own field).
FILTER_FIELDS = {"p_no_push": chance_no_push}


def filter_value(trade: dict, field: str):
    get = FILTER_FIELDS.get(field)
    return get(trade) if get else trade.get(field)


def passes_filter(trade: dict, flt: dict) -> bool:
    """A variant's filter {field, min?, max?, note}: min <= value < max; a trade without
    the value never passes."""
    v = filter_value(trade, flt["field"])
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    if flt.get("min") is not None and v < flt["min"]:
        return False
    return not (flt.get("max") is not None and v >= flt["max"])


def _below60(parent: str, label: str) -> dict:
    return {
        "key": f"{parent}_below60",
        "label": f"{label}, below 60%",
        "filter": {"field": "p_no_push", "max": BELOW60_MAX, "note": BELOW60_NOTE},
        "defined_at": SHADOW_DEFINED_AT,
    }


# Shadow variants per corner strategy, shown right after their parent. Moneyline has none
# yet; paper.with_variants takes the same shape for any strategy list.
CORNER_VARIANTS = {k: [_below60(k, CORNER_STRATEGY_LABELS[k])] for k in CORNER_STRATEGY_ORDER}


def variant_rule(rule: dict | None, spec: dict, parent: str) -> dict:
    """A variant's rule: its parent's plus the filter."""
    return {
        **(rule or {}),
        "variant_of": parent,
        "filter": spec["filter"],
        "defined_at": spec["defined_at"],
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
    book: str = "pinnacle",
) -> dict:
    """A corners paper trade: new_trade's shape plus the team line. `pick` is
    corners_live.pick's {side, odds, model_p, p_push, edge}. The id adds the team side and
    line, so a match can hold one trade per team line; a DraftKings trade's id also ends
    "|dk" (Pinnacle's ids are unchanged), so each book holds its own. `strategy` and
    `bookmaker` name the book ("pinnacle" or "draftkings")."""
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
        strategy=book,
    )
    t.update(
        id=trade_id(
            league,
            t["season"],
            home,
            away,
            "corners",
            team_side,
            f"{float(line):g}",
            *CORNER_ID_SUFFIX[book],
        ),
        bet_type="corners",
        portfolio="corners",
        team=home if team_side == "home" else away,
        team_side=team_side,
        p_push=pick.get("p_push", 0.0),
        bookmaker=book,
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
    """Entry odds x a book's margin-free closing chance of the side - 1 (Shin); None
    unless both sides of the close are priced."""
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
    for col, key in (
        ("clv_dk", "dk"),
        ("clv_pinnacle", "pinnacle"),
        ("clv_pinnacle_live", "pinnacle_live"),
    ):
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
