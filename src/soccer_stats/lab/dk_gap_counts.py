"""DraftKings vs Pinnacle's fair price: counts only, for the draft pre-registration.

docs/lab.md "DraftKings vs Pinnacle's fair price (DRAFT)". The rule under design: forget
the model, and bet DraftKings' 1X2 side when its price beats Pinnacle's Shin-fair chance in
the same snapshot by at least T (gap = DraftKings' decimal x Pinnacle's fair chance - 1).

This reads only the bought ten-book history's look snapshots (kickoff - 48 h): prices and
their last_update times. It never reads a result, a close price or a CLV: it counts how many
matches each (T, freshness) pair would bet, per league and season, and how stale the quotes
are, so T and the freshness rule can be fixed before any outcome is read. 0 credits, no key.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from soccer_stats import trades as tr
from soccer_stats.data import RAW_DIR
from soccer_stats.odds import devig_shin

H2H = ("home", "draw", "away")
LOOK = pd.Timedelta(hours=48)
LOOK_AFTER = pd.Timedelta(minutes=5)  # a snapshot taken just after the look time still counts
LOOK_BEFORE = pd.Timedelta(hours=6)
CLOSE_WITHIN = pd.Timedelta(minutes=60)
TS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)
FRESH = (15, 30, 60, 180, None)  # minutes; None = no freshness rule
DK, PIN = "draftkings", "pinnacle"


def _minutes(ts: pd.Timestamp, upd) -> float:
    if upd is None:
        return np.nan
    return (ts - pd.Timestamp(upd)).total_seconds() / 60.0


def look_rows(snaps: pd.DataFrame) -> pd.DataFrame:
    """One row per match with a look snapshot: league-agnostic. `snaps` = load_books rows.
    Columns: kickoff, season, home, away, has_dk, has_pin, dk_age, pin_age, upd_gap (minutes,
    DraftKings' update minus Pinnacle's), gap (the best side's), side, has_close."""
    out = []
    if snaps.empty:
        return pd.DataFrame()
    for (ko, h, a), g in snaps.groupby(["kickoff", "home", "away"], sort=False):
        g = g.sort_values("snapshot_ts")
        at = ko - LOOK
        look = g[(g["snapshot_ts"] <= at + LOOK_AFTER) & (g["snapshot_ts"] >= at - LOOK_BEFORE)]
        close = g[(g["snapshot_ts"] < ko) & (g["snapshot_ts"] >= ko - CLOSE_WITHIN)]
        row = {"kickoff": ko, "season": tr.season_label(ko), "home": h, "away": a,
               "has_look": not look.empty, "has_close": not close.empty}  # fmt: skip
        if not look.empty:
            snap = look.iloc[-1]
            books, ts = snap["books"] or {}, pd.Timestamp(snap["snapshot_ts"])
            dk, pin = books.get(DK), books.get(PIN)
            row.update({"has_dk": dk is not None, "has_pin": pin is not None})
            row.update(gap_fields(dk, pin, ts))
        out.append(row)
    cols = ["kickoff", "season", "home", "away", "has_look", "has_close", "has_dk", "has_pin",
            "gap", "side", "dk_age", "pin_age", "upd_gap"]  # fmt: skip
    return pd.DataFrame(out).reindex(columns=cols)


def gap_fields(dk: dict | None, pin: dict | None, ts: pd.Timestamp) -> dict:
    """The best side's gap and both quotes' ages at the snapshot (prices only)."""
    if not dk or not pin:
        return {"gap": np.nan, "side": None, "dk_age": np.nan, "pin_age": np.nan,
                "upd_gap": np.nan}  # fmt: skip
    fair = devig_shin([float(pin[m]) for m in H2H])
    gaps = {m: float(dk[m]) * float(f) - 1 for m, f in zip(H2H, fair, strict=True)}
    side = max(gaps, key=gaps.get)
    dk_age, pin_age = _minutes(ts, dk.get("last_update")), _minutes(ts, pin.get("last_update"))
    return {"gap": gaps[side], "side": side, "dk_age": dk_age, "pin_age": pin_age,
            "upd_gap": pin_age - dk_age}  # fmt: skip


def fresh(rows: pd.DataFrame, minutes: float | None) -> np.ndarray:
    if minutes is None:
        return np.ones(len(rows), dtype=bool)
    return ((rows["dk_age"] <= minutes) & (rows["pin_age"] <= minutes)).to_numpy()


def counts(rows: pd.DataFrame) -> dict:
    """Matches, coverage, quote ages and bets per (T, freshness), for one group of rows."""
    if rows.empty:
        return {"matches": 0}

    def flag(col):
        return rows[col].fillna(False).astype(bool) if col in rows else pd.Series(False, rows.index)

    both = rows[flag("has_dk") & flag("has_pin")]
    q = (0.5, 0.9, 0.99)

    def quant(col):
        v = both[col].dropna().to_numpy()
        return [round(float(x), 1) for x in np.quantile(v, q)] if len(v) else None

    bets = {}
    for n in FRESH:
        ok = both[fresh(both, n)]
        bets[str(n) if n is not None else "any"] = {
            f"{t:.2f}": int((ok["gap"] >= t).sum()) for t in TS
        }
    sides = both[both["gap"] >= TS[1]]["side"].value_counts().to_dict()
    return {
        "matches": int(len(rows)),
        "look": int(rows["has_look"].sum()),
        "close": int(rows["has_close"].sum()),
        "both_priced": int(len(both)),
        "dk_age_min_q50_q90_q99": quant("dk_age"),
        "pin_age_min_q50_q90_q99": quant("pin_age"),
        "gap_q50_q90_q99": [round(float(x), 4) for x in np.quantile(both["gap"], q)]
        if len(both)
        else None,
        "bets": bets,
        "sides_at_1pct": {k: int(v) for k, v in sides.items()},
    }


def report(by_league: dict[str, pd.DataFrame]) -> dict:
    out: dict = {"leagues": {}, "seasons": {}, "ts": list(TS), "fresh_minutes": list(FRESH)}
    allr = []
    for code, rows in by_league.items():
        if rows.empty:
            out["leagues"][code] = {"matches": 0}
            continue
        rows = rows.assign(league=code)
        allr.append(rows)
        out["leagues"][code] = {
            "all": counts(rows),
            "by_season": {s: counts(g) for s, g in rows.groupby("season")},
        }
    if allr:
        pooled = pd.concat(allr, ignore_index=True)
        out["all"] = counts(pooled)
        out["seasons"] = {s: counts(g) for s, g in pooled.groupby("season")}
    return out


def main(argv: list[str] | None = None) -> None:
    from soccer_stats import dk_price_backtest as dpb

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default="", help="comma list (blank = every league bought)")
    args = p.parse_args(argv)
    codes = [c for c in args.leagues.split(",") if c] or dpb.bought_leagues(RAW_DIR)
    by = {}
    for code in codes:
        try:
            by[code] = look_rows(dpb.load_books(code))
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            print(f"{code}: {type(exc).__name__}: {exc}"[:200])
            by[code] = pd.DataFrame()
        print(f"{code}: {len(by[code])} matches", flush=True)
    res = report(by)
    for s, c in sorted(res.get("seasons", {}).items()):
        b = c.get("bets", {})
        print(f"  {s}: matches {c['matches']}, both priced {c.get('both_priced')}, "
              f"bets any-fresh {b.get('any')}, 30 min {b.get('30')}")  # fmt: skip
    print("DK_GAP_COUNTS_JSON " + json.dumps(res, default=str))


if __name__ == "__main__":
    main()
