"""Kelly sizing tracked beside the flat $10 record (owner, 24 Oct; display only).

Kelly needs a true edge, and every strategy's CLV is below 0, so this places no bets and
changes no record: it replays the same trades (or backtest bets) with a different stake and
reports what the bankroll would have done. Flat $10 stays the record.

* Paper bankroll BANKROLL_START at each strategy's first trade.
* Fractional Kelly: stake = bankroll x min(KELLY_FRACTION x f*, KELLY_CAP), with
  f* = (p x d - 1) / (d - 1) for decimal odds d taken and chance p; no stake when f* <= 0,
  when p is unknown or when the bankroll is gone.
* p is a calibrated chance where there is one: Lean's blend p_bet; Model 12%'s blend
  chance for its side (p_blend); model (f)'s chance given no push for corners. Model 12%
  also gets a second variant on its raw model_p, to show why it isn't used.
* Sequential, no look-ahead: a stake comes from the bankroll at the bet's open time, which
  holds only bets settled by then; profit and loss land at settlement. Pushes and voids
  return the stake. Open bets are staked but not yet in the bankroll.

`simulate(bets, stake_fn)` is the engine; `live_sizing` (paper trades) and
`backtest_sizing` (backtest bets) build each strategy's `sizing` block.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

KELLY_FRACTION = 0.25  # quarter Kelly
KELLY_CAP = 0.05  # never more than 5% of the bankroll on one bet
BANKROLL_START = 1000.0
FLAT_STAKE = 10.0
MAX_CURVE = 200  # bankroll curve points kept (evenly spaced, the last one always)
APP_LEAGUE_CURVE = 60  # per-league backtest curves in data.json (the pooled one keeps 200)
BACKTEST_LEAD = pd.Timedelta(days=4)  # backtest bets are decided 4 days before kickoff
NOTE = (
    "Tracked alternative only: the same trades with a different stake, no new bets; flat "
    "$10 stays the record. Paper bankroll $1,000 at the first trade; quarter Kelly "
    "(0.25 x f*, f* = (p x odds - 1) / (odds - 1)), at most 5% of the bankroll a bet, "
    "nothing when f* <= 0. Each stake uses only the bankroll from bets settled before it "
    "opened; pushes and voids return the stake. Kelly assumes p is right: every strategy's "
    "CLV is below 0, so the edge it sizes on is not proven."
)
P_NOTES = {
    "blend": "p = the blend's chance (p_bet for Lean; the blend's chance for Model 12%'s side).",
    "model": "p = the raw model's chance: Kelly on the raw model (over-confident). The model "
    "rates the side it likes higher than it wins, so this over-bets.",
    "model_f": "p = model (f)'s chance given no push. Model (f) is over-confident (slope "
    "about 0.56), so this is illustrative.",
}


def kelly_f(p, odds) -> float | None:
    """Full Kelly fraction f* = (p x d - 1) / (d - 1); None for an unknown chance or price."""
    try:
        p, d = float(p), float(odds)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(p) and math.isfinite(d)) or d <= 1 or not 0 <= p <= 1:
        return None
    return (p * d - 1) / (d - 1)


def kelly_stake(
    bankroll: float, p, odds, fraction: float = KELLY_FRACTION, cap: float = KELLY_CAP
) -> float:
    """bankroll x min(fraction x f*, cap); 0 when f* <= 0, p is unknown or bankroll <= 0."""
    f = kelly_f(p, odds)
    if f is None or f <= 0 or not bankroll > 0:
        return 0.0
    return round(bankroll * min(fraction * f, cap), 2)


def _ts(v) -> pd.Timestamp | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        t = pd.Timestamp(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(t):
        return None
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def downsample(points: list, n: int = MAX_CURVE) -> list:
    """At most `n` points, evenly spaced, keeping the first and the last."""
    if len(points) <= n:
        return list(points)
    idx = np.unique(np.linspace(0, len(points) - 1, n).round().astype(int))
    return [points[i] for i in idx]


def simulate(bets: list[dict], stake_fn, start: float = BANKROLL_START) -> dict:
    """Replay `bets` in time order. Each bet: {id, open, settle (None while open), ret
    (profit per $1 staked: odds - 1 won, -1 lost, 0 push/void; None while open), p, odds}.
    `stake_fn(bankroll, bet)` sizes a bet from the bankroll at its open time (bets settled
    at or before it only). Returns bankroll_now, profit, staked (on settled bets),
    roi_on_staked, max_drawdown ($ from the running peak) and max_drawdown_pct,
    growth_per_bet (log(bankroll_now / start) per settled bet), bets_staked,
    bets_zero_stake, median_stake, curve [{date, bankroll}] (at each settlement day,
    downsampled) and stakes {id: stake}."""
    events = []
    for i, b in enumerate(bets):
        op = _ts(b.get("open"))
        if op is None:
            continue
        events.append((op, 1, i))  # opens after settlements at the same moment
        st = _ts(b.get("settle"))
        if st is not None and b.get("ret") is not None:
            events.append((max(st, op), 0, i))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    bankroll, peak, max_dd, max_dd_pct = start, start, 0.0, 0.0
    stakes: dict[int, float] = {}
    staked = profit = 0.0
    settled = 0
    days: dict[str, float] = {}
    for when, kind, i in events:
        b = bets[i]
        if kind == 1:
            stakes[i] = round(max(float(stake_fn(bankroll, b) or 0.0), 0.0), 2)
            continue
        s = stakes.get(i, 0.0)
        pnl = s * float(b["ret"])
        bankroll += pnl
        profit += pnl
        settled += 1
        if float(b["ret"]) != 0:  # a push or void returns the stake: nothing at risk
            staked += s
        peak = max(peak, bankroll)
        max_dd = max(max_dd, peak - bankroll)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - bankroll) / peak)
        days[when.date().isoformat()] = bankroll
    curve = [{"date": d, "bankroll": round(v, 2)} for d, v in sorted(days.items())]
    opened = [stakes[i] for i in stakes]
    positive = [s for s in opened if s > 0]
    return {
        "bankroll_start": start,
        "bankroll_now": round(bankroll, 2),
        "profit": round(profit, 2),
        "staked": round(staked, 2),
        "roi_on_staked": round(profit / staked, 4) if staked else None,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "growth_per_bet": round(math.log(bankroll / start) / settled, 6)
        if settled and bankroll > 0
        else None,
        "bets": len(opened),
        "bets_settled": settled,
        "bets_staked": len(positive),
        "bets_zero_stake": len(opened) - len(positive),
        "median_stake": round(float(np.median(positive)), 2) if positive else None,
        "curve": downsample(curve),
        "stakes": {bets[i].get("id", i): s for i, s in stakes.items()},
    }


def flat_fn(bankroll: float, bet: dict) -> float:
    """The record's own stake: flat $10 whatever the bankroll."""
    return FLAT_STAKE


def kelly_fn(fraction: float = KELLY_FRACTION, cap: float = KELLY_CAP):
    def fn(bankroll: float, bet: dict) -> float:
        return kelly_stake(bankroll, bet.get("p"), bet.get("odds"), fraction, cap)

    return fn


def block(bets: list[dict], kind: str, p_source: str | None = None, keep_stakes=True) -> dict:
    """One sizing block: simulate with flat $10 (`kind` "flat") or quarter Kelly on each
    bet's `p` ("kelly"), with the settings, the p source, `bets_no_chance` and the note."""
    if kind == "flat":
        out = simulate(bets, flat_fn)
        out.update(fraction=None, cap=None, stake=FLAT_STAKE, p_source=None)
    else:
        out = simulate(bets, kelly_fn())
        out.update(fraction=KELLY_FRACTION, cap=KELLY_CAP, p_source=p_source)
        out["bets_no_chance"] = sum(1 for b in bets if not _finite(b.get("p")))
        out["p_note"] = P_NOTES.get(p_source or "")
    out["note"] = NOTE
    if not keep_stakes:
        out.pop("stakes")
    return out


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


# ---------- live paper trades ----------


def _ret(t: dict) -> float | None:
    """Profit per $1 staked once a trade is settled: odds - 1 won, -1 lost, 0 void/push."""
    st = t.get("status")
    if st == "won":
        return float(t["odds"]) - 1
    if st == "lost":
        return -1.0
    if st == "void":
        return 0.0
    return None


def _settled_at(t: dict):
    if t.get("settled_at"):
        return t["settled_at"]
    if t.get("status") in ("won", "lost", "void") and t.get("kickoff"):
        return _ts(t["kickoff"]) + pd.Timedelta(hours=2)  # older trades: about full time
    return None


def chance(t: dict, p_source: str) -> float | None:
    """The chance a trade is sized on: Lean's p_bet, Model 12%'s p_blend (the blend's
    chance for its side) or raw model_p, a corner's model_p given no push."""
    if p_source == "blend":
        v = t.get("p_bet") if t.get("strategy") == "lean" else t.get("p_blend")
    elif p_source == "model_f":
        p, push = t.get("model_p"), t.get("p_push") or 0.0
        v = None if p is None or push >= 1 else float(p) / (1 - float(push))
    else:
        v = t.get("model_p")
    return float(v) if _finite(v) else None


def live_bets(trades: list[dict], p_source: str) -> list[dict]:
    return [
        {
            "id": t["id"],
            "open": t.get("opened_at"),
            "settle": _settled_at(t),
            "ret": _ret(t),
            "p": chance(t, p_source),
            "odds": t.get("odds"),
        }
        for t in trades
        if t.get("odds")
    ]


def sizing_source(rule: dict | None) -> str:
    """What a live strategy sizes on, from its rule's p_source: the corners' model (f), else
    the blend (Lean trades on it; Model 12% gets its blend chance, p_blend)."""
    return "model_f" if (rule or {}).get("p_source") == "model_f" else "blend"


def live_sizing(trades: list[dict], rule: dict | None) -> dict:
    """A live strategy's `sizing`: {flat, kelly, kelly_raw (Model 12% only: on the raw
    model_p, over-confident), blend_from (Model 12%: where each trade's p_blend came
    from)}. `kelly.stakes` = {trade id: stake}, for the trade sheet (flat is $10 each)."""
    src = sizing_source(rule)
    out = {
        "flat": block(live_bets(trades, src), "flat", keep_stakes=False),  # all $10
        "kelly": block(live_bets(trades, src), "kelly", src),
    }
    if (rule or {}).get("p_source") == "model":  # Model 12%
        out["kelly_raw"] = block(live_bets(trades, "model"), "kelly", "model", keep_stakes=False)
        out["kelly_raw"]["label"] = "Kelly on the raw model (over-confident)"
        froms: dict[str, int] = {}
        for t in trades:
            k = t.get("p_blend_from") or ("missing" if t.get("p_blend") is None else "open")
            froms[k] = froms.get(k, 0) + 1
        out["blend_from"] = froms
    return out


# ---------- backtest bets ----------


def backtest_bets(df: pd.DataFrame, p_col: str | None, lead: pd.Timedelta = BACKTEST_LEAD):
    """Backtest bets as simulate() rows: open = kickoff day - `lead` (when the bet is
    decided), settle = the end of the kickoff day, ret = profit per $1, p = `p_col`."""
    if df is None or not len(df):
        return []
    d = pd.to_datetime(df["date"])
    d = d.dt.tz_localize("UTC") if d.dt.tz is None else d.dt.tz_convert("UTC")
    profit = df["profit"].astype(float).to_numpy()
    odds = df["odds"].astype(float).to_numpy()
    p = (
        pd.to_numeric(df[p_col], errors="coerce").to_numpy()
        if p_col and p_col in df
        else np.full(len(df), np.nan)
    )
    keys = (
        df["league"].astype(str)
        + "|"
        + df["date"].astype(str)
        + "|"
        + df["home"].astype(str)
        + "|"
        + df["away"].astype(str)
    ).to_numpy()
    return [
        {
            "id": keys[i],
            "open": d.iloc[i] - lead,
            "settle": d.iloc[i] + pd.Timedelta(hours=23),
            "ret": float(profit[i]),
            "p": float(p[i]) if np.isfinite(p[i]) else None,
            "odds": float(odds[i]),
        }
        for i in range(len(df))
    ]


def backtest_sizing(
    df: pd.DataFrame,
    p_col: str | None,
    p_source: str,
    raw_col: str | None = None,
    lead: pd.Timedelta = BACKTEST_LEAD,
) -> dict:
    """A backtest's `sizing` on every bet in time order: {flat, kelly, kelly_raw (when
    `raw_col`)}, each without per-bet stakes (curves only, at most MAX_CURVE points)."""
    out = {
        "flat": block(backtest_bets(df, None, lead), "flat", keep_stakes=False),
        "kelly": block(backtest_bets(df, p_col, lead), "kelly", p_source, keep_stakes=False),
    }
    if raw_col:
        out["kelly_raw"] = block(
            backtest_bets(df, raw_col, lead), "kelly", "model", keep_stakes=False
        )
        out["kelly_raw"]["label"] = "Kelly on the raw model (over-confident)"
    return out


def app_curves(leagues: dict, n: int = APP_LEAGUE_CURVE) -> dict:
    """A backtest file's `leagues` for data.json: each league's sizing curves downsampled to
    `n` points (the file and the pooled `all` keep MAX_CURVE)."""
    out = {}
    for code, rep in (leagues or {}).items():
        sizing = rep.get("sizing") if isinstance(rep, dict) else None
        if isinstance(sizing, dict) and not sizing.get("error"):
            rep = {
                **rep,
                "sizing": {
                    k: {**v, "curve": downsample(v.get("curve") or [], n)}
                    if isinstance(v, dict)
                    else v
                    for k, v in sizing.items()
                },
            }
        out[code] = rep
    return out
