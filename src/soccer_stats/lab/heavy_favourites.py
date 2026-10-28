"""Scoping a priced test of heavy favourites (75%+) trailing at half-time (lead brief, 26 Oct).
Free: football-data only, no key, no Odds API call, nothing bought.

The idea came from design C's 75%+ band (22 bets, +51%), picked after the results were seen,
so it needs data no earlier test used. This counts, per league and season, the favourites
priced 75%+ before kickoff (Shin-fair, Pinnacle's close, else the market average's close)
that trailed at half-time, the kickoff slots holding them and the credits at one historical
snapshot per slot (`HIST_COST`), for:
- (a) the eight live leagues in 2025/26 and 2026/27 to date (B and C used 2022/23-2024/25);
- (b) other football-data leagues with an Odds API sport key, 2022/23-2025/26 (the in-play
  archive looked unpriced before mid-2022, design C's guard).
It reads which favourites trailed at half-time (to choose what to buy), never how they ended,
and sizes the sample needed for a gap of `GAPS` points.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.cup_odds import HIST_COST
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab.comeback_plan import SNAPSHOT_AFTER, Z_ALPHA, Z_POWER, load_season
from soccer_stats.leagues import LEAGUES

MIN_FAV = 0.75
EIGHT = cb.LEAGUES_B
EIGHT_YEARS = (2025, 2026)  # 2026/27 to date
# football-data's main-format leagues outside the live eight that carry half-time scores,
# with their Odds API sport keys (to confirm with the free /sports list before any purchase).
OTHER = {
    "N1": ("Eredivisie", "soccer_netherlands_eredivisie"),
    "P1": ("Primeira Liga", "soccer_portugal_primeira_liga"),
    "SC0": ("Scottish Premiership", "soccer_spl"),
    "B1": ("Belgian Pro League", "soccer_belgium_first_div"),
    "T1": ("Turkish Super Lig", "soccer_turkey_super_league"),
    "G1": ("Greek Super League", "soccer_greece_super_league"),
    "D2": ("2. Bundesliga", "soccer_germany_bundesliga2"),
    "I2": ("Serie B", "soccer_italy_serie_b"),
    "SP2": ("Segunda Division", "soccer_spain_segunda_division"),
    "F2": ("Ligue 2", "soccer_france_ligue_two"),
}
OTHER_YEARS = (2022, 2023, 2024, 2025)
# Design C's 75%+ band: the market's fair chance averaged 0.457, so a gap is measured from there.
BASE_IMPLIED = 0.46
GAPS = (0.12, 0.06)
USABLE = 0.81  # design C: 732 DraftKings bets from 902 trailing favourites


def season_name(year: int) -> str:
    return f"{year}/{str(year + 1)[-2:]}"


def fair_chances(df: pd.DataFrame) -> pd.DataFrame:
    """Shin-fair home/away chances from Pinnacle's close, else the market average's close;
    `source` says which ("pinnacle", "average", or None when neither is complete)."""
    df = df.copy()
    pin = cb._ok(df["ph"], df["pd"], df["pa"])
    avg = cb._ok(df["ah"], df["ad"], df["aa"])
    fh = np.full(len(df), np.nan)
    fa = np.full(len(df), np.nan)
    for i in range(len(df)):
        cols = ("ph", "pd", "pa") if pin[i] else ("ah", "ad", "aa") if avg[i] else None
        if cols:
            f = cb._fair(df.loc[df.index[i], list(cols)].to_numpy(float))
            fh[i], fa[i] = f[0], f[2]
    df["fh"], df["fa"] = fh, fa
    df["source"] = np.where(pin, "pinnacle", np.where(avg, "average", None))
    df["fav_home"] = df["fh"] >= df["fa"]
    df["p_fav"] = np.where(df["fav_home"], df["fh"], df["fa"])
    return df


def season_scope(league: str, year: int, raw_dir: Path = RAW_DIR) -> dict:
    df = fair_chances(load_season(league, year, raw_dir))
    heavy = df[df["p_fav"] >= MIN_FAV].dropna(subset=["hthg", "htag"])
    fh = np.where(heavy["fav_home"], heavy["hthg"], heavy["htag"])
    dh = np.where(heavy["fav_home"], heavy["htag"], heavy["hthg"])
    deficit = dh - fh
    trail = heavy[deficit > 0]
    timed = trail[trail["kickoff"].notna()]
    slots = int(timed["kickoff"].nunique())
    return {
        "league": league,
        "season": season_name(year),
        "matches": int(len(df)),
        "priced_pinnacle": int((df["source"] == "pinnacle").sum()),
        "priced_average": int((df["source"] == "average").sum()),
        "unpriced": int(df["source"].isna().sum()),
        "heavy": int(len(heavy)),
        "heavy_from_average": int((heavy["source"] == "average").sum()),
        "trailing": int(len(trail)),
        "trailing_from_average": int((trail["source"] == "average").sum()),
        "one_down": int((deficit[deficit > 0] == 1).sum()),
        "no_kickoff_time": int(len(trail) - len(timed)),
        "slots": slots,
        "credits": slots * HIST_COST,
        "snapshot_times": sorted(
            (k + pd.Timedelta(minutes=SNAPSHOT_AFTER)).strftime("%Y-%m-%dT%H:%MZ")
            for k in timed["kickoff"].unique()
        ),
    }


def scope(groups: dict | None = None, raw_dir: Path = RAW_DIR) -> list[dict]:
    groups = groups or {"a": (EIGHT, EIGHT_YEARS), "b": (tuple(OTHER), OTHER_YEARS)}
    rows = []
    for group, (leagues, years) in groups.items():
        for lg in leagues:
            for y in years:
                try:
                    r = season_scope(lg, y, raw_dir)
                except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                    r = {"league": lg, "season": season_name(y), "error": str(exc)[:120]}
                r["group"] = group
                r["sport"] = LEAGUES[lg].odds_sport if lg in LEAGUES else OTHER[lg][1]
                rows.append(r)
    return rows


# The frozen design-H sample (lead brief, 28 Oct): every league-season known to be priced in
# play (the archive probes of 28 Oct), none used by B or C.
SAMPLE = {
    **{lg: (2025, 2026) for lg in ("E0", "D1", "F1", "E1", "E2", "E3")},
    "SP1": (2020, 2021, 2025, 2026),
    "I1": (2021, 2025, 2026),
    **{lg: (2021, 2022, 2023, 2024, 2025, 2026) for lg in ("D2", "SP2")},
    **{
        lg: (2022, 2023, 2024, 2025, 2026)
        for lg in ("N1", "P1", "SC0", "B1", "T1", "G1", "I2", "F2")
    },
}


def slot_list(league: str, year: int, raw_dir: Path = RAW_DIR) -> tuple[list[dict], dict]:
    """One league-season's slots: heavy favourites trailing at half-time grouped by kickoff,
    with the snapshot time to request. Half-time only, never how the match ended."""
    df = fair_chances(load_season(league, year, raw_dir))
    heavy = df[df["p_fav"] >= MIN_FAV].dropna(subset=["hthg", "htag"])
    fh = np.where(heavy["fav_home"], heavy["hthg"], heavy["htag"])
    dh = np.where(heavy["fav_home"], heavy["htag"], heavy["hthg"])
    trail = heavy[(dh - fh) > 0].sort_values(["kickoff", "home"])
    counts = {"trailing": int(len(trail)), "no_kickoff_time": int(trail["kickoff"].isna().sum())}
    sport = LEAGUES[league].odds_sport if league in LEAGUES else OTHER[league][1]
    out = []
    for ko, g in trail[trail["kickoff"].notna()].groupby("kickoff"):
        out.append(
            {
                "league": league,
                "sport": sport,
                "kickoff": ko.strftime("%Y-%m-%dT%H:%MZ"),
                "request_at": (ko + pd.Timedelta(minutes=SNAPSHOT_AFTER)).strftime(
                    "%Y-%m-%dT%H:%MZ"
                ),
                "matches": [
                    {
                        "home": r.home,
                        "away": r.away,
                        "favourite": "home" if r.fav_home else "away",
                        "p_fav": round(float(r.p_fav), 4),
                        "ht": [int(r.hthg), int(r.htag)],
                        "price": r.source,
                    }
                    for r in g.itertuples()
                ],
            }
        )
    return out, counts


def freeze(sample: dict | None = None, raw_dir: Path = RAW_DIR, used=None) -> dict:
    """Design H's slot list. `used` = (league, kickoff) pairs already bought by B or C."""
    from soccer_stats.lab import comeback_fetch as cf

    sample = SAMPLE if sample is None else sample
    if used is None:
        used = {
            (s["league"], s["kickoff"]) for d in ("B", "C") for s in cf.load_slots(cf.slots_path(d))
        }
    slots, per = [], {}
    for lg, years in sample.items():
        for y in years:
            key = f"{lg} {season_name(y)}"
            try:
                got, counts = slot_list(lg, y, raw_dir)
            except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                per[key] = {"error": str(exc)[:120]}
                continue
            kept = [s for s in got if (s["league"], s["kickoff"]) not in used]
            counts.update(slots=len(kept), matches=sum(len(s["matches"]) for s in kept),
                          dropped_as_used=len(got) - len(kept))  # fmt: skip
            per[key] = counts
            slots += kept
    slots.sort(key=lambda s: s["request_at"])
    return {
        "design": "H",
        "rule": f"favourite >= {MIN_FAV:.0%} (Pinnacle's close, else the average's) trailing at "
        f"half-time; one snapshot per slot at kickoff + {SNAPSHOT_AFTER} min",
        "slots": slots,
        "per_league_season": per,
        "totals": {
            "slots": len(slots),
            "matches": sum(len(s["matches"]) for s in slots),
            "credits": len(slots) * HIST_COST,
        },
    }


def needed(gap: float, p: float = BASE_IMPLIED, power: float | None = Z_POWER) -> int:
    """Bets for a two-sided 95% range on realized - implied to exclude 0: at 80% power, or
    with `power=None` the size whose expected range just touches 0 (a coin flip)."""
    z = Z_ALPHA + (power or 0.0)
    return int(np.ceil(z**2 * p * (1 - p) / gap**2))


def totals(rows: list[dict]) -> dict:
    out = {}
    for group in sorted({r["group"] for r in rows}):
        ok = [r for r in rows if r["group"] == group and "error" not in r]
        keys = ("matches", "heavy", "heavy_from_average", "trailing", "trailing_from_average")
        t = {k: sum(r[k] for r in ok) for k in (*keys, "one_down", "slots", "credits")}
        t["usable_estimate"] = int(round(t["trailing"] * USABLE))
        out[group] = t
    return out


def power() -> dict:
    return {
        f"{round(g * 100)}pts": {"power80": needed(g), "power50": needed(g, power=None)}
        for g in GAPS
    }


def report(rows: list[dict], tot: dict, pw: dict) -> str:
    lines = [
        f"== Heavy favourites (>= {MIN_FAV:.0%}) trailing at half-time: free scope, one "
        f"snapshot per slot at kickoff + {SNAPSHOT_AFTER} min, {HIST_COST} credits each ==",
        "  grp league season matches pin avg none heavy(avg) trailing(avg) 1down slots credits",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"  {r['group']}   {r['league']:4s} {r['season']}  error: {r['error']}")
            continue
        lines.append(
            f"  {r['group']}   {r['league']:4s} {r['season']} {r['matches']:6d} "
            f"{r['priced_pinnacle']:4d} {r['priced_average']:4d} {r['unpriced']:4d} "
            f"{r['heavy']:4d}({r['heavy_from_average']:3d}) {r['trailing']:4d}"
            f"({r['trailing_from_average']:3d}) {r['one_down']:4d} {r['slots']:4d} "
            f"{r['credits']:6d}"
        )
    for g, t in tot.items():
        lines.append(f"Group {g}: {json.dumps(t)}")
    lines.append(f"Bets needed (implied {BASE_IMPLIED:.0%}): {json.dumps(pw)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--json", default="")
    p.add_argument("--slots-out", default="", help="write design H's frozen slot list here")
    args = p.parse_args(argv)
    if args.slots_out:
        frozen = freeze()
        Path(args.slots_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.slots_out).write_text(json.dumps(frozen, indent=1))
        for k, v in frozen["per_league_season"].items():
            print(f"  {k}: {json.dumps(v)}")
        print("Totals: " + json.dumps(frozen["totals"]))
        print("COMEBACK_H_SLOTS_JSON " + json.dumps(frozen))
        return
    rows = scope()
    tot, pw = totals(rows), power()
    print(report(rows, tot, pw))
    compact = [{k: v for k, v in r.items() if k != "snapshot_times"} for r in rows]
    print("HEAVY_FAVOURITES_JSON " + json.dumps({"rows": compact, "totals": tot, "power": pw}))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        out = {"rows": rows, "totals": tot, "power": pw}
        Path(args.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
