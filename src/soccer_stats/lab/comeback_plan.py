"""Phase 2 planner for the favourite comeback test (lead brief, 24 Oct; `docs/lab.md`
"Favourites coming back from behind"). Free: football-data only, no key, no Odds API call.

A kickoff slot is one league's matches that kicked off together. One historical snapshot
(`/v4/historical/sports/{sport}/odds`, h2h, ten books = one region, `HIST_COST` credits)
requested `SNAPSHOT_AFTER` minutes after the slot's kickoff catches every match in it at
half-time. football-data already gives each match's half-time score, so only slots holding a
favourite (Pinnacle's close, phase 1's bands) that trails at half-time need buying. This
lists those slots per league and season, costs them, and sizes the designs in docs/lab.md.
It reads which favourites trailed at half-time (to choose what to buy), never how the
matches ended.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.cup_odds import HIST_COST
from soccer_stats.data import RAW_DIR, download
from soccer_stats.lab import comebacks as cb
from soccer_stats.leagues import LEAGUES

YEARS = (2022, 2023, 2024, 2025)  # 5-minute historical snapshots from late 2022
# The archive returns the latest snapshot at or before the requested time, 5 minutes apart,
# so asking for kickoff + 55 min lands at +50 to +55: inside the half-time break (about +47
# to +63), after any first-half stoppage time.
SNAPSHOT_AFTER = 55
UK = "Europe/London"  # football-data's kickoff times are UK local time
TOP5 = ("E0", "SP1", "D1", "I1", "F1")
# Power for the design sized to phase 1's gap: a trailing favourite wins about 20% of the
# time; detect a 3.8-point gap (phase 1's half-time gap after the second-half correction)
# two-sided at 5% with 80% power.
BASE_WIN = 0.20
GAP = 0.038
Z_ALPHA, Z_POWER = 1.959964, 0.841621


def matches_needed(p: float = BASE_WIN, gap: float = GAP) -> int:
    """Matches for a two-sided 5% test of realized - implied win rate at 80% power."""
    return int(np.ceil(((Z_ALPHA + Z_POWER) ** 2) * p * (1 - p) / gap**2))


def load_season(league: str, year: int, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """football-data rows with the UTC kickoff (Date + Time); matches without a time drop."""
    raw = pd.read_csv(download(league, year, raw_dir), encoding="latin-1", on_bad_lines="skip")
    df = cb.parse_fd(raw, league, year)
    played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).reset_index(drop=True)
    times = played["Time"] if "Time" in played else pd.Series([None] * len(played))
    local = pd.to_datetime(
        played["Date"].astype(str) + " " + times.astype(str),
        dayfirst=True,
        format="mixed",
        errors="coerce",
    )
    df["kickoff"] = (
        local.dt.tz_localize(UK, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC").values
    )
    return df


def trailing(df: pd.DataFrame) -> pd.DataFrame:
    """Favourites (Pinnacle's close, >= 45%) behind at half-time, with their band."""
    fav = cb.favourites(df).dropna(subset=["hthg", "htag"])
    fh = np.where(fav["fav_home"], fav["hthg"], fav["htag"])
    dh = np.where(fav["fav_home"], fav["htag"], fav["hthg"])
    fav["deficit"] = dh - fh
    return fav[fav["deficit"] > 0].reset_index(drop=True)


def season_plan(league: str, year: int, raw_dir: Path = RAW_DIR) -> dict:
    df = load_season(league, year, raw_dir)
    pin = cb._ok(df["ph"], df["pd"], df["pa"])
    t = trailing(df)
    t = t[t["kickoff"].notna()]
    slots = t.groupby("kickoff").size()
    by_band = {
        cb.band_name(lo, hi): int(((t["p_fav"] >= lo) & (t["p_fav"] < hi)).sum())
        for lo, hi in cb.BANDS
    }
    return {
        "league": league,
        "season": f"{year}/{str(year + 1)[-2:]}",
        "matches": int(len(df)),
        "pinnacle_close": round(float(pin.mean()), 3) if len(df) else 0.0,
        "kickoff_slots": int(df["kickoff"].nunique()),
        "trailing_favourites": int(len(t)),
        "one_goal_down": int((t["deficit"] == 1).sum()),
        "by_band": by_band,
        "slots_to_buy": int(len(slots)),
        "credits": int(len(slots)) * HIST_COST,
        "snapshot_times": [
            (k + pd.Timedelta(minutes=SNAPSHOT_AFTER)).strftime("%Y-%m-%dT%H:%MZ")
            for k in slots.index
        ],
    }


def plan(leagues=cb.LEAGUES_B, years=YEARS, raw_dir: Path = RAW_DIR) -> list[dict]:
    rows = []
    for lg in leagues:
        for y in years:
            try:
                rows.append(season_plan(lg, y, raw_dir))
            except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                rows.append({"league": lg, "season": f"{y}/{str(y + 1)[-2:]}", "error": str(exc)})
    return rows


def design(rows: list[dict], name: str, leagues, seasons) -> dict:
    keep = [
        r
        for r in rows
        if "error" not in r and r["league"] in leagues and r["season"][:4] in map(str, seasons)
    ]
    return {
        "name": name,
        "leagues": list(leagues),
        "seasons": [f"{y}/{str(y + 1)[-2:]}" for y in seasons],
        "slots": sum(r["slots_to_buy"] for r in keep),
        "credits": sum(r["credits"] for r in keep),
        "trailing_favourites": sum(r["trailing_favourites"] for r in keep),
        "one_goal_down": sum(r["one_goal_down"] for r in keep),
    }


def designs(rows: list[dict]) -> list[dict]:
    out = [
        design(rows, "A: Premier League, four seasons", ("E0",), YEARS),
        design(rows, "B: top five leagues, 2024/25", TOP5, (2024,)),
    ]
    need = matches_needed()
    c = design(rows, "C: all eight leagues, 2022/23-2024/25", cb.LEAGUES_B, (2022, 2023, 2024))
    c["matches_needed"] = need
    c["reaches_need"] = c["trailing_favourites"] >= need
    out.append(c)
    out.append(design(rows, "D: all eight leagues, four seasons", cb.LEAGUES_B, YEARS))
    return out


def report(rows: list[dict], ds: list[dict]) -> str:
    lines = [
        f"== Comeback phase 2 plan: one snapshot per slot at kickoff + {SNAPSHOT_AFTER} min, "
        f"{HIST_COST} credits each ==",
        "  league season  matches pinnacle% slots trailing (1 down) slots_to_buy credits",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"  {r['league']:4s} {r['season']}  error: {r['error'][:80]}")
            continue
        lines.append(
            f"  {r['league']:4s} {r['season']}  {r['matches']:5d}   {r['pinnacle_close']:6.0%} "
            f"{r['kickoff_slots']:5d} {r['trailing_favourites']:5d} ({r['one_goal_down']:4d}) "
            f"{r['slots_to_buy']:5d} {r['credits']:7d}"
        )
    lines.append("Designs:")
    for d in ds:
        lines.append(
            f"  {d['name']}: {d['slots']} snapshots, {d['credits']} credits, "
            f"{d['trailing_favourites']} trailing favourites ({d['one_goal_down']} one down)"
        )
        if "matches_needed" in d:
            lines.append(
                f"    needs {d['matches_needed']} to detect a {GAP:.1%} gap "
                f"(80% power, 5% two-sided): {'reached' if d['reaches_need'] else 'short'}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default=",".join(cb.LEAGUES_B))
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    leagues = [x.strip() for x in args.leagues.split(",") if x.strip() in LEAGUES]
    rows = plan(leagues)
    ds = designs(rows)
    print(report(rows, ds))
    compact = {"rows": [{k: v for k, v in r.items() if k != "snapshot_times"} for r in rows]}
    print("COMEBACK_PLAN_JSON " + json.dumps({**compact, "designs": ds}))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"rows": rows, "designs": ds}, indent=1))


if __name__ == "__main__":
    main()
