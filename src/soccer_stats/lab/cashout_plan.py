"""The cash-out test's frozen sample (owner via the lead, 28 Oct; approved up to 50,000
credits): every match with a heavy pre-match underdog, in every league-season the in-play
archive prices, bought at two in-play moments whatever the score.

Free planning: football-data only, no key. It reads the pre-match closing price and the
kickoff time, never a half-time or full-time score, so nothing it picks depends on how a
match went.
- Underdog: the side with the longer quoted price at Pinnacle's close, else the market
  average's close (`price` says which). Kept when that price is at least `MIN_DOG` (3.0);
  the test's primary band is `PRIMARY_DOG` (4.0) and up, 3.0-4.0 is reported beside it.
- Snapshots (`SNAPSHOTS`): half-time (kickoff + 55 min) and about the 70th minute (kickoff
  + 85 min), one per league kickoff slot, shared by every match in it. A request already
  held for design B, C or H (same sport and time) costs nothing.
- Excluded: every match bought by B, C or H. B and C are spent; H's outcomes must stay
  unread until it is judged (its amendment), so H's matches never enter this sample.
- Priced league-seasons (`PRICED`): design C's purchase and the archive probes of 28 Oct.
- Over `BUDGET` credits, the oldest seasons are dropped first (lead, 28 Oct), whole
  league-seasons at a time, until the estimate fits.
`--slots-out` prints `CASHOUT_SLOTS_JSON`, the file `comeback_fetch --slots X` buys from.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.cup_odds import HIST_COST
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab.comeback_plan import load_season
from soccer_stats.lab.heavy_favourites import OTHER, season_name
from soccer_stats.leagues import LEAGUES

MIN_DOG = 3.0
PRIMARY_DOG = 4.0
SNAPSHOTS = {"ht": 55, "m70": 85}  # minutes after kickoff
BUDGET = 50_000
# In-play prices in the archive (DraftKings or Betfair quoting started matches): design C's
# guard (E0, F1 and D1 2022/23 unpriced) and the archive probes of 28 Oct (docs/lab.md).
_NOW = (2025, 2026)  # 2025/26 and 2026/27 to date
PRICED = {
    **{lg: (2023, 2024, *_NOW) for lg in ("E0", "F1", "D1")},
    **{lg: (2022, 2023, 2024, *_NOW) for lg in ("E1", "E2", "E3")},
    "SP1": (2020, 2021, 2022, 2023, 2024, *_NOW),
    "I1": (2021, 2022, 2023, 2024, *_NOW),
    **{lg: (2021, 2022, 2023, 2024, *_NOW) for lg in ("D2", "SP2")},
    **{lg: (2022, 2023, 2024, *_NOW) for lg in ("N1", "P1", "SC0", "B1", "T1", "G1", "I2", "F2")},
}


def sport_of(league: str) -> str:
    return LEAGUES[league].odds_sport if league in LEAGUES else OTHER[league][1]


def _stamp(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def underdogs(df: pd.DataFrame) -> pd.DataFrame:
    """Matches whose underdog's quoted closing price is at least MIN_DOG: `dog` (home/away),
    `dog_odds`, `price` ("pinnacle" or "average"). Pinnacle's close when all three of its
    prices are there, else the average's."""
    df = df.copy()
    pin = df[["ph", "pd", "pa"]].notna().all(axis=1) & (df[["ph", "pd", "pa"]] > 1).all(axis=1)
    avg = df[["ah", "ad", "aa"]].notna().all(axis=1) & (df[["ah", "ad", "aa"]] > 1).all(axis=1)
    h = np.where(pin, df["ph"], np.where(avg, df["ah"], np.nan))
    a = np.where(pin, df["pa"], np.where(avg, df["aa"], np.nan))
    df["dog"] = np.where(a >= h, "away", "home")
    df["dog_odds"] = np.fmax(h, a)
    df["price"] = np.where(pin, "pinnacle", np.where(avg, "average", None))
    keep = np.isfinite(df["dog_odds"]) & (df["dog_odds"] >= MIN_DOG) & df["kickoff"].notna()
    return df[keep]


def used_matches(designs=("B", "C", "H")) -> tuple[set, set]:
    """(league, kickoff, home) of every match bought by the designs, and the (sport,
    request_at) of every snapshot they hold."""
    matches, held = set(), set()
    for d in designs:
        for s in cf.load_slots(cf.slots_path(d)):
            held.add((s["sport"], s["request_at"]))
            for m in s.get("matches", []):
                matches.add((s["league"], s["kickoff"], m["home"]))
    return matches, held


def season_slots(league: str, year: int, used: set, raw_dir: Path = RAW_DIR) -> list[dict]:
    """One league-season's kickoff slots holding a heavy underdog not bought before."""
    df = underdogs(load_season(league, year, raw_dir))
    sport = sport_of(league)
    out = []
    for ko, g in df.groupby("kickoff"):
        k = _stamp(ko)
        rows = [r for r in g.sort_values("home").itertuples() if (league, k, r.home) not in used]
        if not rows:
            continue
        out.append(
            {
                "league": league,
                "season": season_name(year),
                "sport": sport,
                "kickoff": k,
                "matches": [
                    {
                        "home": r.home,
                        "away": r.away,
                        "dog": r.dog,
                        "dog_odds": round(float(r.dog_odds), 3),
                        "price": r.price,
                    }
                    for r in rows
                ],  # fmt: skip
            }
        )
    return out


def requests_for(slots: list[dict]) -> list[dict]:
    """The snapshots to buy: one per slot and moment, merged when two slots of a league ask
    for the same time (`comeback_fetch` buys each (sport, request_at) once)."""
    seen: dict[tuple, dict] = {}
    for s in slots:
        ko = pd.Timestamp(s["kickoff"])
        for snap, minutes in SNAPSHOTS.items():
            at = _stamp(ko + pd.Timedelta(minutes=minutes))
            key = (s["sport"], at)
            if key not in seen:
                seen[key] = {"league": s["league"], "sport": s["sport"], "kickoff": s["kickoff"],
                             "request_at": at, "for": []}  # fmt: skip
            seen[key]["for"].append({"kickoff": s["kickoff"], "snapshot": snap})
    return sorted(seen.values(), key=lambda r: r["request_at"])


def plan(priced: dict | None = None, raw_dir: Path = RAW_DIR, used=None, held=None) -> dict:
    priced = PRICED if priced is None else priced
    if used is None or held is None:
        u, h = used_matches()
        used = u if used is None else used
        held = h if held is None else held
    per: dict[str, dict] = {}
    by_key: dict[str, list[dict]] = {}
    for lg, years in priced.items():
        for y in years:
            key = f"{lg} {season_name(y)}"
            try:
                slots = season_slots(lg, y, used, raw_dir)
            except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                per[key] = {"league": lg, "year": y, "error": str(exc)[:120]}
                continue
            reqs = requests_for(slots)
            new = [r for r in reqs if (r["sport"], r["request_at"]) not in held]
            ms = [m for s in slots for m in s["matches"]]
            per[key] = {
                "league": lg,
                "year": y,
                "slots": len(slots),
                "matches": len(ms),
                "primary": sum(m["dog_odds"] >= PRIMARY_DOG for m in ms),
                "average_priced": sum(m["price"] == "average" for m in ms),
                "requests": len(reqs),
                "held": len(reqs) - len(new),
                "credits": len(new) * HIST_COST,
            }
            by_key[key] = slots
    return {"per_league_season": per, "slots_by_key": by_key}


def trim(per: dict, budget: int = BUDGET) -> tuple[list[str], list[str]]:
    """Keep the newest seasons: drop whole league-seasons, oldest season first (then
    dearest first within a season), until the credits fit the budget."""
    keys = [k for k, v in per.items() if "error" not in v]
    total = sum(per[k]["credits"] for k in keys)
    order = sorted(keys, key=lambda k: (per[k]["year"], -per[k]["credits"], k))
    dropped = []
    for k in order:
        if total <= budget:
            break
        total -= per[k]["credits"]
        dropped.append(k)
    return [k for k in keys if k not in dropped], dropped


def freeze(priced: dict | None = None, raw_dir: Path = RAW_DIR, budget: int = BUDGET,
           used=None, held=None) -> dict:  # fmt: skip
    p = plan(priced, raw_dir, used, held)
    per = p["per_league_season"]
    kept, dropped = trim(per, budget)
    slots = sorted((s for k in kept for s in p["slots_by_key"][k]), key=lambda s: s["kickoff"])
    reqs = requests_for(slots)
    _, held_set = used_matches() if held is None else (None, held)
    new = [r for r in reqs if (r["sport"], r["request_at"]) not in held_set]
    ms = [m for s in slots for m in s["matches"]]
    return {
        "design": "X",
        "rule": f"underdog quoted >= {MIN_DOG} at the close (Pinnacle's, else the average's; "
        f"primary band >= {PRIMARY_DOG}); snapshots at kickoff + "
        + " and + ".join(f"{m} min" for m in SNAPSHOTS.values())
        + ", every score state; B, C and H matches excluded",
        "snapshots": SNAPSHOTS,
        "budget": budget,
        "dropped": dropped,
        "per_league_season": per,
        "totals": {
            "league_seasons": len(kept),
            "slots": len(slots),
            "matches": len(ms),
            "primary": sum(m["dog_odds"] >= PRIMARY_DOG for m in ms),
            "requests": len(reqs),
            "held": len(reqs) - len(new),
            "credits": len(new) * HIST_COST,
        },
        "matches": slots,
        "slots": reqs,  # comeback_fetch reads "slots": one entry per snapshot to buy
    }


def report(res: dict) -> str:
    t = res["totals"]
    lines = [
        f"== Cash-out sample: {t['league_seasons']} league-seasons, {t['slots']} slots, "
        f"{t['matches']} matches ({t['primary']} at {PRIMARY_DOG}+), {t['requests']} snapshots "
        f"({t['held']} held), {t['credits']} credits (budget {res['budget']}) =="
    ]
    for k, v in res["per_league_season"].items():
        if "error" in v:
            lines.append(f"  {k}: {v['error']}")
            continue
        mark = "  (dropped)" if k in res["dropped"] else ""
        lines.append(
            f"  {k}: {v['slots']} slots, {v['matches']} matches ({v['primary']} at "
            f"{PRIMARY_DOG}+, {v['average_priced']} average-priced), {v['requests']} snapshots, "
            f"{v['credits']} credits{mark}"
        )
    if res["dropped"]:
        lines.append(f"  dropped to fit the budget (oldest first): {', '.join(res['dropped'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--budget", type=int, default=BUDGET, help="most credits the sample may cost")
    p.add_argument("--slots-out", action="store_true", help="print CASHOUT_SLOTS_JSON")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    res = freeze(budget=args.budget)
    print(report(res))
    summary = {k: v for k, v in res.items() if k not in ("matches", "slots")}
    print("CASHOUT_PLAN_JSON " + json.dumps(summary))
    if args.slots_out:
        print("CASHOUT_SLOTS_JSON " + json.dumps(res))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
