"""Both live match rules replayed at DraftKings' own historical prices (lead, 28 Oct; 0 credits).

The method was written into docs/leagues.md ("Backtests at DraftKings' prices: method")
before this ran. In short:

* Candidates: every match of a league's seasons that the Pinnacle backtests score
  (lean_backtest.coverage) and the bought DraftKings history covers, where DraftKings priced
  the full 1X2 in the 48 h look snapshot (quote updated at most STALE_HOURS before it).
* Entry: DraftKings' quoted price at the look (break-even 1/decimal). Model 12% =
  trades.best_pick at 12% on the raw walk-forward model chance (edge12_filters' baseline);
  Blend Lean = trades.lean_pick on the blend (lean_backtest.walk_forward: fits on Pinnacle's
  close beside the model, every 28 days on earlier matches, the block set by the look time)
  fed DraftKings' Shin-fair look price, reported inside the live odds range and at every
  price.
* CLV: entry x Pinnacle's Shin-fair close from the same close snapshot - 1 (no fallback; a
  bet without it counts in clv_missing), and against DraftKings' own close (descriptive).
* The same rules on the same matches at football-data's Pinnacle early price, beside it.

The saved file (data-log backtest/dk_price_backtest.json) holds one entry per league, each run
adding or replacing its league's; `all` pools whichever leagues are in.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import bands as bd
from soccer_stats import edge12_backtest as eb
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import edge12_filters as ef
from soccer_stats.odds import devig_shin
from soccer_stats.odds_feed import BOOKMAKER, _team, book_prices
from soccer_stats.odds_history import history_dir

H2H = mc.H2H
LOOK = pd.Timedelta(hours=48)
STALE_HOURS = 6.0
SHARP = "pinnacle"
NAME = "dk_price_backtest.json"
KIND = "dk_price_backtest"
NOTE = (
    "Blend Lean and Model 12% exactly as live, entered at DraftKings' own price 48 hours "
    "before kickoff from the bought ten-book history, CLV against Pinnacle's margin-free "
    "close in the same snapshot. Beside it, the same rules on the same matches at "
    "football-data's Pinnacle early price. Method fixed in docs/leagues.md before the run."
)


def load_books(league: str, known: set[str] | None = None, raw_dir: Path = RAW_DIR):
    """Every cached snapshot as rows: at (requested), snapshot_ts, kickoff, home, away and
    `books` = {book: {home, draw, away, last_update}} (football-data names)."""
    rows = []
    for path in sorted(history_dir(league, raw_dir).glob("*.json")):
        snap = json.loads(path.read_text())
        at = pd.Timestamp(snap["requested"])
        ts = pd.Timestamp(snap.get("timestamp") or snap["requested"])
        for ev in snap.get("data") or []:
            books = book_prices(ev, known)
            if not books:
                continue
            rows.append(
                {
                    "at": at,
                    "snapshot_ts": ts,
                    "kickoff": pd.Timestamp(ev["commence_time"]).tz_convert("UTC"),
                    "home": _team(ev["home_team"], known),
                    "away": _team(ev["away_team"], known),
                    "books": books,
                }
            )
    cols = ["at", "snapshot_ts", "kickoff", "home", "away", "books"]
    return pd.DataFrame(rows, columns=cols)


def quote(snaps: pd.DataFrame, at: pd.Timestamp, book: str) -> dict | None:
    """`book`'s 1X2 in the latest snapshot taken at or before `at` that holds it, when the
    quote was updated at most STALE_HOURS before `at`; None otherwise. `snaps` is one
    match's rows, sorted by snapshot_ts."""
    h = snaps[snaps["snapshot_ts"] <= at]
    for books in reversed(list(h["books"])):
        q = (books or {}).get(book)
        if not q:
            continue
        upd = q.get("last_update")
        if upd is not None and at - pd.Timestamp(upd) > pd.Timedelta(hours=STALE_HOURS):
            return None
        return {m: float(q[m]) for m in H2H}
    return None


def candidates(
    merged: pd.DataFrame, hist: pd.DataFrame, seasons: set[str]
) -> tuple[pd.DataFrame, dict]:
    """lean_backtest.target_rows' shape at DraftKings' prices: one row per predicted match in
    `seasons` whose DraftKings 1X2 was in the 48 h look. odds_<side> = DraftKings' look
    (quoted), mkt_<side> its Shin-fair price, close_<side> Pinnacle's close in the same close
    snapshot, dk_close_<side> DraftKings' close, kickoff, day (the look time). Plus
    `coverage` per season: matches, look, close (DraftKings) and pinnacle_close counts."""
    by_pair = {k: g.sort_values("snapshot_ts") for k, g in hist.groupby(["home", "away"])}
    rows, cov = [], {}
    pool = merged[merged["season"].astype(str).isin(seasons)] if len(merged) else merged
    for r in pool.to_dict("records"):
        season = str(r["season"])
        c = cov.setdefault(
            season, {"matches": 0, "look": 0, "close": 0, "pinnacle_close": 0, "candidates": 0}
        )
        c["matches"] += 1
        if any(pd.isna(r.get(f"p_{m}")) for m in H2H):
            continue
        g = by_pair.get((r["home"], r["away"]))
        if g is None:
            continue
        day = pd.Timestamp(r["date"]).date()
        near = g[(g["kickoff"].dt.date - day).abs() <= pd.Timedelta(days=1)]
        if near.empty:
            continue
        kickoff = near["kickoff"].mode().iloc[0]
        look_at, close_at = kickoff - LOOK, kickoff
        dk_look = quote(near, look_at, BOOKMAKER)
        dk_close = quote(near, close_at, BOOKMAKER)
        pin_close = quote(near, close_at, SHARP)
        c["look"] += dk_look is not None
        c["close"] += dk_close is not None
        c["pinnacle_close"] += pin_close is not None
        if dk_look is None:
            continue
        c["candidates"] += 1
        fair = devig_shin(np.array([dk_look[m] for m in H2H], dtype=float))
        row = {
            "date": kickoff.tz_convert(None),
            "kickoff": kickoff,
            "day": look_at.tz_convert(None),
            "season": season,
            "home": r["home"],
            "away": r["away"],
            "home_goals": r["home_goals"],
            "away_goals": r["away_goals"],
            "y": int(mc.outcome_index([r["home_goals"]], [r["away_goals"]], "h2h")[0]),
        }
        for i, m in enumerate(H2H):
            row[f"p_{m}"] = float(r[f"p_{m}"])
            row[f"odds_{m}"] = dk_look[m]
            row[f"mkt_{m}"] = float(fair[i])
            row[f"close_{m}"] = pin_close[m] if pin_close else np.nan
            row[f"dk_close_{m}"] = dk_close[m] if dk_close else np.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values("date").reset_index(drop=True)
    return df, cov


def _with_dk_clv(b: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    """Each bet's CLV against DraftKings' own close (`clv_dk`, descriptive)."""
    out = b.copy()
    out["clv_dk"] = np.nan
    if not len(b) or target.empty:
        return out
    t = target.assign(_d=pd.to_datetime(target["date"]).dt.date.astype(str))
    t = t.drop_duplicates(["_d", "home", "away"]).set_index(["_d", "home", "away"])
    for i, r in out.iterrows():
        k = (str(r["date"]), r["home"], r["away"])
        if k in t.index:
            close = {m: t.at[k, f"dk_close_{m}"] for m in H2H}
            close = {m: float(v) for m, v in close.items() if pd.notna(v)}
            v = tr.clv(float(r["odds"]), r["market"], close)
            out.at[i, "clv_dk"] = np.nan if v is None else v
    return out


def _extra(b: pd.DataFrame) -> dict:
    """CLV vs DraftKings' close (mean, range), bets without Pinnacle's close, odds bands."""
    col = b["clv_dk"] if "clv_dk" in b else pd.Series(dtype=float)
    dk = pd.to_numeric(col, errors="coerce").dropna().to_numpy(dtype=float)
    pin = pd.to_numeric(b["clv"], errors="coerce") if len(b) else pd.Series(dtype=float)
    return {
        "clv_dk": float(dk.mean()) if len(dk) else None,
        "clv_dk_ci95": lb._boot_mean(dk, seed=2),
        "clv_dk_bets": len(dk),
        "clv_missing": int(pin.isna().sum()) if len(b) else 0,
        "by_odds_band": bd.by_odds_band(b) if len(b) else [],
    }


def verdict(m: dict) -> dict:
    """The pass rule fixed in the method: the 97.5% end of the CLV range above 0."""
    ci = m.get("clv_ci95")
    return {
        "passes": bool(ci and ci[0] > 0),
        "rule": "pooled CLV vs Pinnacle's close: the 95% range wholly above 0",
    }


def lean_block(b: pd.DataFrame) -> dict:
    """The live rule (inside the odds range) and the old rule (every price)."""
    inside = b[[tr.in_lean_range(o) for o in b["odds"]]] if len(b) else b
    live, old = lb.summary(inside), lb.summary(b)
    return {
        "live_rule": {**live, **_extra(inside), "odds_range": list(tr.LEAN_ODDS_RANGE)},
        "all_odds": {**old, **_extra(b)},
    }


def edge12_block(b: pd.DataFrame) -> dict:
    return {**eb.summary(b), **_extra(b)}


def league_report(league: str, preds: pd.DataFrame, pins: pd.DataFrame, hist: pd.DataFrame):
    """One league at DraftKings' prices and, on the same matches, at Pinnacle's early
    price. Returns (report, {rule: bets frame at DraftKings})."""
    merged = lb.with_pinnacle(preds, pins)
    cov = lb.coverage(pins)
    scored_by_pin = {c["season"] for c in cov if c["included"]}
    bought = {lb_season(k) for k in hist["kickoff"]} if len(hist) else set()
    seasons = scored_by_pin & bought
    target, dk_cov = candidates(merged, hist, seasons)
    train = mc.training_rows(merged, "h2h") if not merged.empty else merged
    if target.empty:
        lean_b = lb.bets(pd.DataFrame(), league)
        e12_b = ef.bets(pd.DataFrame(), league, "baseline")
        fits = []
    else:
        scored, fits = lb.walk_forward(train, target)
        lean_b = _with_dk_clv(lb.bets(scored, league), target)
        e12_b = _with_dk_clv(ef.bets(target, league, "baseline"), target)
    # The same matches at Pinnacle's early price (football-data), the same rules.
    pin_t = lb.target_rows(merged)
    if len(pin_t) and len(target):
        keys = set(zip(target["season"].astype(str), target["home"], target["away"], strict=True))
        pin_t = pin_t[
            [
                (str(s), h, a) in keys
                for s, h, a in zip(pin_t["season"], pin_t["home"], pin_t["away"], strict=True)
            ]
        ]
        pin_scored, _ = lb.walk_forward(train, pin_t)
        pin_lean, pin_e12 = lb.bets(pin_scored, league), ef.bets(pin_t, league, "baseline")
    else:
        pin_lean, pin_e12 = (
            lb.bets(pd.DataFrame(), league),
            ef.bets(pd.DataFrame(), league, "baseline"),
        )
    rep = {
        "kind": KIND,
        "seasons": sorted(lb.season_label(s) for s in seasons),
        "seasons_scored_by_pinnacle": sorted(lb.season_label(s) for s in scored_by_pin),
        "coverage": [
            {"season": s, "label": lb.season_label(s), **c} for s, c in sorted(dk_cov.items())
        ],
        "candidates": int(len(target)),
        "fits": len(fits),
        "lean": lean_block(lean_b),
        "edge12": edge12_block(e12_b),
        "pinnacle_same_matches": {
            "candidates": int(len(pin_t)),
            "lean": lean_block(pin_lean),
            "edge12": edge12_block(pin_e12),
        },
    }
    return rep, {"lean": lean_b, "edge12": e12_b}


def lb_season(kickoff: pd.Timestamp) -> str:
    """football-data's season code ('2425') for a kickoff."""
    return tr.season_label(pd.Timestamp(kickoff))


def pooled(bets_by_league: dict[str, dict[str, pd.DataFrame]]) -> dict:
    """`all`: every league's bets together, with the verdict per rule."""
    frames = {k: [b[k] for b in bets_by_league.values() if len(b[k])] for k in ("lean", "edge12")}
    lean = (
        pd.concat(frames["lean"], ignore_index=True)
        if frames["lean"]
        else lb.bets(pd.DataFrame(), "")
    )
    e12 = (
        pd.concat(frames["edge12"], ignore_index=True)
        if frames["edge12"]
        else ef.bets(pd.DataFrame(), "", "baseline")
    )
    lb_ = lean_block(lean)
    e12_ = edge12_block(e12)
    return {
        "leagues": sorted(bets_by_league),
        "lean": {
            **lb_,
            "verdict": {
                "live_rule": verdict(lb_["live_rule"]["lean"]),
                "all_odds": verdict(lb_["all_odds"]["lean"]),
            },
        },
        "edge12": {**e12_, "verdict": verdict(e12_["edge12"])},
    }


def merge_file(old: dict | None, league: str, rep: dict, bets: dict[str, pd.DataFrame], now=None):
    """The saved file: `leagues[league]` replaced (with its bets as records so a later run
    can pool them), `all` recomputed over every league in the file."""
    now = now or pd.Timestamp.now(tz="UTC")
    out = dict(old or {})
    leagues = dict(out.get("leagues") or {})
    leagues[league] = {
        **rep,
        "bets": {k: json.loads(b.to_json(orient="records")) for k, b in bets.items()},
    }
    every = {
        code: {k: pd.DataFrame(v["bets"].get(k) or []) for k in ("lean", "edge12")}
        for code, v in leagues.items()
        if isinstance(v.get("bets"), dict)
    }
    for code, b in every.items():  # metrics need these columns even when empty
        if b["lean"].empty:
            b["lean"] = lb.bets(pd.DataFrame(), code)
        if b["edge12"].empty:
            b["edge12"] = ef.bets(pd.DataFrame(), code, "baseline")
    out.update(
        {
            "generated_at": now.isoformat(timespec="seconds"),
            "kind": KIND,
            "title": "Blend Lean and Model 12% at DraftKings' own prices",
            "note": NOTE,
            "method": "docs/leagues.md, 'Backtests at DraftKings' prices: method'",
            "leagues": leagues,
            "all": pooled(every),
        }
    )
    return out


def headline(rep: dict) -> list[str]:
    """Plain lines for the job log."""
    out = []
    for c in rep.get("coverage") or []:
        n = c["matches"] or 1
        out.append(
            f"  {c['label']}: {c['matches']} matches, DraftKings at the look {c['look'] / n:.0%}, "
            f"at the close {c['close'] / n:.0%}, Pinnacle close {c['pinnacle_close'] / n:.0%}"
        )

    def line(name, m):
        if not m or not m.get("bets"):
            return f"  {name}: 0 bets"
        roi_ci = m.get("roi_ci95") or [None, None]
        clv_ci = m.get("clv_ci95") or [None, None]
        f = lambda v: "n/a" if v is None else f"{v:+.1%}"  # noqa: E731
        return (
            f"  {name}: {m['bets']} bets, won {m['win_rate']:.1%} vs {m['breakeven']:.1%} "
            f"needed, ROI {f(m['roi'])} ({f(roi_ci[0])} to {f(roi_ci[1])}), CLV vs Pinnacle "
            f"{f(m.get('clv'))} ({f(clv_ci[0])} to {f(clv_ci[1])})"
        )

    for src, label in (
        (rep, "DraftKings"),
        (rep.get("pinnacle_same_matches") or {}, "Pinnacle early"),
    ):
        out.append(f" {label}:")
        out.append(line("Blend Lean (live rule)", src["lean"]["live_rule"]["lean"]))
        out.append(line("Blend Lean (all odds)", src["lean"]["all_odds"]["lean"]))
        out.append(line("Model 12%", src["edge12"]["edge12"]))
    return out
