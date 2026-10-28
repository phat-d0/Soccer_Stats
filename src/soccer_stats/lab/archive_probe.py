"""In-play archive probes (owner-approved 28 Oct; the cap lifted the same day): how far back
does the historical odds archive keep in-play h2h prices, league by league?

One historical bulk h2h snapshot (`HIST_COST` credits, ten books = one region) per
league-season, requested at kickoff + `SNAPSHOT_AFTER` minutes of the weekend slot with the
most matches of that league-season (football-data's kickoffs, so free to plan). "Priced"
is design C's guard (`comeback_fetch.snapshot_priced`: a started event freshly quoted by
DraftKings or Betfair). Each league walks back from its newest unknown season and stops at
the first unpriced one, since archive coverage only grows over time.

Spending rules (as `comeback_fetch`): `CappedBudget` with the 3,000-credit reserve on the
freshest balance, cost from `x-requests-last`, the first call budgeted at `FIRST_ESTIMATE`,
any HTTP or network error stops the run, a cached body is never bought again. Sport keys
are checked first against the free `/v4/sports?all=true` list; an unknown key is skipped.
cap 0 (or no key) is a dry run that prints the plan. The key is redacted everywhere.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.cup_odds import BASE, HIST_COST, URL, _call, balance, iso, redact, stamp
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab.comeback_plan import SNAPSHOT_AFTER, load_season
from soccer_stats.lab.heavy_favourites import OTHER
from soccer_stats.leagues import LEAGUES
from soccer_stats.player_goal_odds import CappedBudget

CACHE = "archive_probe"  # under RAW_DIR; the Actions cache `archive-probe-*` holds it
FIRST_ESTIMATE = 20
# Walk-back chains, newest season first (lead brief, 28 Oct). E0, F1 and D1 have none:
# their 2022/23 was already unpriced in design C's guard.
CHAINS = {
    **{lg: (2021, 2020, 2019) for lg in ("SP1", "I1", "E1", "E2", "E3")},
    **{lg: (2022, 2021, 2020, 2019) for lg in OTHER},
}


def sport_of(league: str) -> str:
    return LEAGUES[league].odds_sport if league in LEAGUES else OTHER[league][1]


def pick_slot(league: str, year: int, raw_dir: Path = RAW_DIR) -> dict | None:
    """The weekend kickoff with the most matches of a league-season (ties: the earliest),
    between October and April so it sits mid-season."""
    df = load_season(league, year, raw_dir)
    ko = df["kickoff"].dropna()
    ko = ko[ko.dt.dayofweek.isin([5, 6]) & ko.dt.month.isin([10, 11, 12, 1, 2, 3, 4])]
    if ko.empty:
        return None
    counts = ko.value_counts()
    best = counts[counts == counts.max()].index.min()
    return {
        "league": league,
        "season": f"{year}/{str(year + 1)[-2:]}",
        "sport": sport_of(league),
        "kickoff": best.strftime("%Y-%m-%dT%H:%MZ"),
        "request_at": (best + pd.Timedelta(minutes=SNAPSHOT_AFTER)).strftime("%Y-%m-%dT%H:%MZ"),
        "matches_in_slot": int(counts.max()),
    }


def books_seen(body: dict) -> dict:
    """Started events and, per book, how many of them it quoted freshly (after kickoff and
    within `cf.FRESH_MINUTES` of the snapshot)."""
    snap = cf._utc(body.get("timestamp"))
    started, books = 0, {}
    for ev in body.get("data") or []:
        ko = cf._utc(ev.get("commence_time"))
        if snap is None or ko is None or ko > snap:
            continue
        started += 1
        for b in ev.get("bookmakers") or []:
            for m in b.get("markets") or []:
                upd = cf._utc(m.get("last_update") or b.get("last_update"))
                fresh = upd is not None and upd > ko
                if m.get("key") == "h2h" and fresh:
                    if (snap - upd).total_seconds() <= cf.FRESH_MINUTES * 60:
                        books[b["key"]] = books.get(b["key"], 0) + 1
    return {"started": started, "books": dict(sorted(books.items()))}


def body_path(cache_dir: Path, slot: dict) -> Path:
    return Path(cache_dir) / f"{slot['sport']}_{stamp(slot['request_at'])}.json"


def known_sports(get: Callable, api_key: str) -> set[str] | None:
    """Every sport key the API lists (free call), or None if it can't be read."""
    try:
        r = get(f"{BASE}/sports", params={"apiKey": api_key, "all": "true"}, timeout=30)
        return {s["key"] for s in r.json()} if r.ok else None
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def run(
    cap: int = 0,
    api_key: str | None = None,
    cache_dir: Path | None = None,
    chains: dict | None = None,
    get: Callable = requests.get,
    raw_dir: Path = RAW_DIR,
) -> dict:
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    cache_dir = Path(cache_dir) if cache_dir is not None else Path(raw_dir) / CACHE
    chains = CHAINS if chains is None else chains
    budget = CappedBudget(max(int(cap), 0))
    res: dict = {"cap": int(cap), "probes": [], "spent": 0, "stopped": None, "log": budget.log}
    dry = cap <= 0 or not api_key
    res["dry_run"] = dry
    sports = None
    if not dry:
        budget.left = balance(get, api_key)
        res["left_before"] = budget.left
        sports = known_sports(get, api_key)
    for league, years in chains.items():
        for year in years:
            try:
                slot = pick_slot(league, year, raw_dir)
            except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                res["probes"].append(
                    {"league": league, "season": f"{year}", "error": str(exc)[:120]}
                )
                break
            if slot is None:
                res["probes"].append({"league": league, "season": f"{year}", "error": "no slot"})
                break
            if sports is not None and slot["sport"] not in sports:
                res["probes"].append({**slot, "error": "sport key not listed by the API"})
                break
            path = body_path(cache_dir, slot)
            if path.exists():
                body, info = json.loads(path.read_text()), {"cost": 0, "cached": True}
            elif dry:
                res["probes"].append({**slot, "dry_run": True})
                break  # a dry run can't know whether to walk further back
            else:
                params = {
                    "apiKey": api_key,
                    "date": iso(pd.Timestamp(slot["request_at"])),
                    "bookmakers": ",".join(cf.BOOKS),
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                }
                estimate = FIRST_ESTIMATE if res["spent"] == 0 else HIST_COST
                label = f"{league} {slot['season']} {slot['request_at']}"
                body, info = _call(get, URL.format(sport=slot["sport"]), params, budget,
                                   estimate, label, api_key)  # fmt: skip
                res["spent"] = budget.spent
                if body is None:
                    res["stopped"] = redact(info.get("message") or "stopped", api_key)
                    res["probes"].append({**slot, "error": res["stopped"]})
                    break
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(body))
                if (info.get("cost") or 0) > cf.MAX_CALL_COST:
                    res["stopped"] = f"a call cost {info['cost']} credits"
            seen = books_seen(body)
            priced = cf.snapshot_priced(body)
            res["probes"].append(
                {
                    **slot,
                    "snapshot": body.get("timestamp"),
                    "events": len(body.get("data") or []),
                    **seen,
                    "priced": priced,
                    "cost": info.get("cost", 0),
                    "cached": bool(info.get("cached")),
                }
            )
            if not priced or res["stopped"]:
                break
        if res["stopped"]:
            break
    res["left_after"] = budget.left
    res["log"] = [redact(line, api_key) for line in budget.log]
    return res


def report(res: dict) -> str:
    lines = [
        f"== In-play archive probes: cap {res['cap']}, spent {res['spent']}, credits left "
        f"{res.get('left_before')} -> {res.get('left_after')} =="
    ]
    for p in res["probes"]:
        if "error" in p:
            lines.append(f"  {p['league']} {p['season']}: {p['error']}")
        elif p.get("dry_run"):
            lines.append(f"  {p['league']} {p['season']}: would buy {p['request_at']} "
                         f"({p['matches_in_slot']} matches kick off together)")  # fmt: skip
        else:
            lines.append(
                f"  {p['league']} {p['season']} at {p['request_at']}: "
                f"{'PRICED' if p['priced'] else 'not priced'}; {p['events']} events, "
                f"{p['started']} started; fresh h2h by {p['books'] or 'none'}; cost {p['cost']}"
            )
    if res.get("stopped"):
        lines.append(f"  stopped: {res['stopped']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cap", type=int, default=0, help="most credits to spend (0 = dry run)")
    p.add_argument("--leagues", default="", help="comma list (default: every chain)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    want = [x.strip() for x in args.leagues.split(",") if x.strip()]
    chains = {k: v for k, v in CHAINS.items() if not want or k in want}
    res = run(cap=args.cap, chains=chains)
    print(report(res))
    print("ARCHIVE_PROBE_JSON " + json.dumps({k: v for k, v in res.items() if k != "log"}))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
