"""DraftKings' historical pre-match odds: the coverage probe and the backfill estimate
(lead brief 2, item 3; owner, 28 Oct).

Goal: replay Blend Lean and Model 12% at DraftKings' own prices (today's backtests use
Pinnacle's early price as a stand-in), for every live domestic league over the seasons the
backtests use (2019/20-2024/25).

* Probe (`probe`, capped; the lead approved 600 credits): one historical bulk h2h snapshot
  per league and season, asking ten books (BOOKMAKERS, one region, about 10 credits), at
  probe_time (a Saturday morning in November, before the day's kickoffs). Walking back from
  the newest season, a league stops at its first season where DraftKings prices nothing.
  CappedBudget keeps the shared 3,000 reserve. Each body is kept under odds_history (the
  snapshot naming the backfill uses), so it is never bought twice. Reads no results.
* Estimate (`estimate`, no key): per league and season, the distinct snapshots a backfill
  needs (odds_history.plan_snapshots on football-data's kickoffs, kickoffs within 15 min
  sharing) at LOOKS (a 48 h look) plus the close, at COST_PER_SNAPSHOT (h2h only), for the
  seasons the probe found DraftKings in.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.edge.props import _get
from soccer_stats.odds_feed import BOOKMAKER, SPORTS
from soccer_stats.odds_history import compact_events, plan_snapshots, snapshot_path
from soccer_stats.player_goal_odds import CappedBudget

URL = "https://api.the-odds-api.com/v4/historical/sports/{sport}/odds"
# The ten books asked (one region): DraftKings, Pinnacle and the US books, the same list as
# the live h2h call's (odds_feed.BOOKMAKERS, PR #125).
BOOKMAKERS = (
    "draftkings",
    "pinnacle",
    "fanduel",
    "betmgm",
    "williamhill_us",
    "betrivers",
    "fanatics",
    "espnbet",
    "bovada",
    "betonlineag",
)
LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
SEASONS = (2024, 2023, 2022, 2021, 2020, 2019)  # start years, newest first
COST_PER_SNAPSHOT = 10  # one market (h2h), up to ten books = one region
LOOKS = (48.0,)  # the replay's early look; the close is always planned too


def probe_time(start_year: int) -> pd.Timestamp:
    """The probe's request time for a season: the first Saturday on or after 1 November,
    09:00 UTC (a matchday morning: that weekend's matches are listed and priced)."""
    d = pd.Timestamp(f"{start_year}-11-01 09:00", tz="UTC")
    return d + pd.Timedelta(days=(5 - d.dayofweek) % 7)


def book_counts(events: list[dict]) -> dict[str, int]:
    """How many events each book prices (h2h) in a snapshot's events."""
    out: dict[str, int] = {}
    for ev in events or []:
        for b in ev.get("bookmakers") or []:
            if any(m.get("key") == "h2h" for m in b.get("markets") or []):
                out[b["key"]] = out.get(b["key"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def probe(
    cap: int,
    leagues=LEAGUES,
    seasons=SEASONS,
    api_key: str = "",
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
) -> dict:
    """One snapshot per league and season, newest first, stopping a league at its first
    season without DraftKings; within `cap` credits and the 3,000 reserve. Returns {cap,
    spent, credits_left, leagues: {code: [{season, at, events, books, draftkings, cached}]},
    covered: {code: [start years with DraftKings]}, log}."""
    budget = CappedBudget(cap)
    out: dict = {"cap": cap, "leagues": {}, "covered": {}}
    for code in leagues:
        rows = out["leagues"].setdefault(code, [])
        out["covered"][code] = []
        for year in seasons:
            at = probe_time(year)
            path = snapshot_path(code, at, raw_dir)
            cached = path.exists()
            if cached:
                events = json.loads(path.read_text()).get("data", [])
            else:
                params = {
                    "apiKey": api_key,
                    "bookmakers": ",".join(BOOKMAKERS),
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                    "date": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                label = f"{code} {year}/{(year + 1) % 100:02d}"
                body = _get(
                    get, URL.format(sport=SPORTS[code]), params, budget, COST_PER_SNAPSHOT, label
                )
                if body is None and budget.log and "skipped" in budget.log[-1]:
                    out["stopped"] = budget.log[-1]
                    break
                events = (body or {}).get("data") or []
                if body is None:  # an error answer: logged, never cached (a rerun retries)
                    rows.append(
                        {
                            "season": label.split(" ", 1)[1],
                            "start_year": year,
                            "at": at.isoformat(),
                            "error": budget.log[-1] if budget.log else None,
                        }
                    )
                    break
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "requested": at.isoformat(),
                            "timestamp": (body or {}).get("timestamp"),
                            "data": compact_events(events),
                            "probe": True,
                        },
                        separators=(",", ":"),
                    )
                )
            counts = book_counts(events)
            rows.append(
                {
                    "season": f"{year}/{(year + 1) % 100:02d}",
                    "start_year": year,
                    "at": at.isoformat(),
                    "events": len(events),
                    "books": counts,
                    "draftkings": counts.get(BOOKMAKER, 0),
                    "cached": cached,
                }
            )
            if counts.get(BOOKMAKER, 0) == 0:
                break  # walking back: no DraftKings in this season, so none earlier
            out["covered"][code].append(year)
        if out.get("stopped"):
            break
    out.update(spent=budget.spent, credits_left=budget.left, log=budget.log)
    return out


def estimate(
    kickoffs_by: dict[tuple[str, int], list[pd.Timestamp]],
    looks=LOOKS,
    cost: int = COST_PER_SNAPSHOT,
) -> dict:
    """Per (league, start year) the distinct snapshots a backfill needs (looks plus the
    close, kickoffs within 15 minutes sharing them) and their credits; plus totals per
    league and overall. `kickoffs_by` maps (league, start year) to its played kickoffs."""
    rows = []
    for (code, year), ks in sorted(kickoffs_by.items()):
        plan = plan_snapshots(ks, looks_hours=looks)
        snaps = int(plan["at"].nunique()) if len(plan) else 0
        rows.append(
            {
                "league": code,
                "season": f"{year}/{(year + 1) % 100:02d}",
                "kickoffs": len(set(ks)),
                "snapshots": snaps,
                "credits": snaps * cost,
            }
        )
    by_league: dict[str, dict] = {}
    for r in rows:
        b = by_league.setdefault(r["league"], {"seasons": 0, "snapshots": 0, "credits": 0})
        b["seasons"] += 1
        b["snapshots"] += r["snapshots"]
        b["credits"] += r["credits"]
    return {
        "looks_hours": list(looks),
        "cost_per_snapshot": cost,
        "rows": rows,
        "by_league": by_league,
        "total": {
            "snapshots": sum(r["snapshots"] for r in rows),
            "credits": sum(r["credits"] for r in rows),
        },
    }
