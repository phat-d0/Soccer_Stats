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


def simulate(bets: list[dict], stake_fn, start: float = BANKROLL_START, log: bool = False) -> dict:
    """Replay `bets` in time order. Each bet: {id, open, settle (None while open), ret
    (profit per $1 staked: odds - 1 won, -1 lost, 0 push/void; None while open), p, odds}.
    `stake_fn(bankroll, bet)` sizes a bet from the bankroll at its open time (bets settled
    at or before it only). Returns bankroll_now, profit, staked (on settled bets),
    roi_on_staked, max_drawdown ($ from the running peak) and max_drawdown_pct,
    growth_per_bet (log(bankroll_now / start) per settled bet), bets_staked,
    bets_zero_stake, median_stake, curve [{date, bankroll}] (at each settlement day,
    downsampled) and stakes {id: stake}. With `log`, also `log`: one entry per bet in the
    order it opened, {i (index into `bets`), bankroll_before (the bankroll that sized it),
    stake, profit and bankroll_after (at its settlement; None while open)}; the other
    numbers are the same either way."""
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
    entries: dict[int, dict] = {}
    for when, kind, i in events:
        b = bets[i]
        if kind == 1:
            stakes[i] = round(max(float(stake_fn(bankroll, b) or 0.0), 0.0), 2)
            if log:
                entries[i] = {
                    "i": i,
                    "bankroll_before": bankroll,
                    "stake": stakes[i],
                    "profit": None,
                    "bankroll_after": None,
                }
            continue
        s = stakes.get(i, 0.0)
        pnl = s * float(b["ret"])
        bankroll += pnl
        if log and i in entries:
            entries[i].update(profit=pnl, bankroll_after=bankroll)
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
    out = {
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
    if log:
        out["log"] = list(entries.values())  # dicts keep insertion (open) order
    return out


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
    chance for its side) or raw model_p, a corner's model_p given no push, the line shop's
    Pinnacle fair chance (model_p there)."""
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
    src = (rule or {}).get("p_source")
    if src == "pinnacle_fair":  # the line shop: Pinnacle's fair chance, stored as model_p
        return "model"
    return "model_f" if src == "model_f" else "blend"


def live_sizing(trades: list[dict], rule: dict | None) -> dict:
    """A live strategy's `sizing`: {flat, kelly, kelly_raw (Model 12% only: on the raw
    model_p, over-confident), blend_from (Model 12%: where each trade's p_blend came
    from)}. `kelly.stakes` = {trade id: stake}, for the trade sheet (flat is $10 each);
    `kelly.log` = kelly_log, how each stake was sized (newest last)."""
    src = sizing_source(rule)
    bets = live_bets(trades, src)
    out = {
        "flat": block(bets, "flat", keep_stakes=False),  # all $10
        "kelly": block(bets, "kelly", src),
    }
    out["kelly"]["log"] = kelly_log(trades, bets, simulate(bets, kelly_fn(), log=True))
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


KELLY_BET_FIELDS = ["kelly_stake", "kelly_profit", "bankroll_before", "kelly_from"]


def with_kelly_bets(
    df: pd.DataFrame, p_col: str | None, kelly_from: str, lead: pd.Timedelta = BACKTEST_LEAD
) -> pd.DataFrame:
    """`df` with each bet's quarter-Kelly stake, its P/L and the bankroll that sized it
    (owner via the lead, 7 Nov; display only): the same simulate() run on the same rows,
    order and chance column as backtest_sizing's `kelly` block, with log=True, so the
    stakes add up to that block's profit. `kelly_from` names the simulation ("pooled" or a
    league code). No chance (e.g. before a league's first blend fit) = stake 0."""
    out = df.copy()
    for c in KELLY_BET_FIELDS:
        out[c] = None
    if df is None or not len(df):
        return out
    sim = simulate(backtest_bets(df, p_col, lead), kelly_fn(), log=True)
    stake = [0.0] * len(df)
    pnl: list[float | None] = [None] * len(df)
    before: list[float | None] = [None] * len(df)
    for e in sim["log"]:
        stake[e["i"]] = e["stake"]
        pnl[e["i"]] = e["profit"]
        before[e["i"]] = e["bankroll_before"]
    out["kelly_stake"] = stake
    out["kelly_profit"] = pnl
    out["bankroll_before"] = before
    out["kelly_from"] = kelly_from
    return out


def kelly_cells(r: dict) -> list:
    """A compact row's KELLY_BET_FIELDS cells (cents, $ for the bankroll)."""

    def money(v, nd=2):
        return None if v is None or not np.isfinite(float(v)) else round(float(v), nd)

    return [
        money(r.get("kelly_stake")),
        money(r.get("kelly_profit"), 4),
        money(r.get("bankroll_before")),
        r.get("kelly_from"),
    ]


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


# ---------- the Kelly bet log (owner via the lead, 5 Nov; live strategies only) ----------

LOG_REASONS = {
    "chance": "no chance",
    "edge": "no edge on the chance",
    "bankroll": "no bankroll",
}


def _iso(v) -> str | None:
    t = _ts(v)
    return t.isoformat(timespec="seconds") if t is not None else None


def _pick(t: dict) -> str:
    """The bet in words: a corner's "Brighton over 5.5", else the side (home/draw/away)."""
    if t.get("bet_type") == "corners":
        team = t.get("team") or (t.get("away") if t.get("team_side") == "away" else t.get("home"))
        return f"{team} {t.get('side')} {t.get('line')}"
    return str(t.get("market") or "")


def kelly_log(trades: list[dict], bets: list[dict], sim: dict) -> list[dict]:
    """One row per sized trade, in the order simulate() opened them: how its quarter-Kelly
    stake was worked out (chance, full Kelly f, the fraction applied after the 5% cap, the
    bankroll that sized it), what it did (status, profit, bankroll after its settlement)
    and the flat $10 result beside it; `capped`, `push` and `reason` appear only when set.
    `sim` is simulate(..., log=True) on `bets`."""
    by_id = {t["id"]: t for t in trades if "id" in t}
    rows = []
    for e in sim.get("log") or []:
        b = bets[e["i"]]
        t = by_id.get(b["id"], {})
        f = kelly_f(b.get("p"), b.get("odds"))
        if b.get("p") is None or f is None:
            reason = LOG_REASONS["chance"]
        elif f <= 0:
            reason = LOG_REASONS["edge"]
        elif not e["bankroll_before"] > 0:
            reason = LOG_REASONS["bankroll"]
        else:
            reason = None
        applied = min(KELLY_FRACTION * f, KELLY_CAP) if reason is None else 0.0
        ret = b.get("ret")
        rows.append(
            {
                "id": b["id"],
                "opened_at": _iso(b.get("open")),
                "settled_at": _iso(b.get("settle")) if ret is not None else None,
                "league": t.get("league"),
                "match": f"{t.get('home')} v {t.get('away')}" if t.get("home") else None,
                "pick": _pick(t),
                "odds": b.get("odds"),
                "chance": round(b["p"], 4) if b.get("p") is not None else None,
                "kelly": round(f, 4) if f is not None else None,
                "fraction": round(applied, 4),
                "capped": reason is None and KELLY_FRACTION * f > KELLY_CAP,
                "bankroll_before": round(e["bankroll_before"], 2),
                "stake": e["stake"],
                "status": t.get("status"),
                "push": bool(t.get("push")),
                "profit": round(e["profit"], 2) if e["profit"] is not None else None,
                "bankroll_after": round(e["bankroll_after"], 2)
                if e["bankroll_after"] is not None
                else None,
                "reason": reason,
                "flat_profit": round(FLAT_STAKE * ret, 2) if ret is not None else None,
            }
        )
    # Keep data.json small: flags only when set, a reason only with no stake.
    for r in rows:
        for k in ("capped", "push", "reason"):
            if not r[k]:
                del r[k]
    return rows
