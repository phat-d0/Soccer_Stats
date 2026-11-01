"""Priced goalscorer test (docs/player_props.md §16): the historical anytime-goalscorer prices.

Two designs, each a fixed set of matches from football-data's kickoffs:
- `dev`: step 3, the 2025/26 development season (sets the edge threshold T only).
- `forward`: step 1, 2026/27 matches from 2026-10-10 that have kicked off (the judged window's
  history; the live log takes over from its start).

Per league (E0, SP1, D1, I1, F1) and UTC match day: one historical events list (1 credit),
taken 3 hours before that day's first football-data kickoff. Then, per event kicking off that
day, two snapshots of `player_goal_scorer_anytime` with §16's ten-book list (one region, 10
credits each): the entry (kickoff − 50 min) and the close (kickoff − 2 min).

Spend control (the same rules as every paid path here):
- `player_goal_odds.CappedBudget`: a call is skipped if it could pass the cap or leave under
  3,000 credits on the shared key (the balance is read first from the free sports list).
- The cost counted is each answer's `x-requests-last`. A call costing more than
  `MAX_CALL_COST` stops the run. A network failure, 401, 429 or 5xx stops it too. A 404 or
  422 for one event (not listed then) is recorded and the run goes on.
- `cap` 0, or no key, is a dry run: no call at all, only the plan from football-data's
  kickoffs and what the cache already holds.
- `minutes` is a time budget; the run resumes where it stopped.

Never paying twice: every body is cached under
`data/raw/player_odds_history/goalscorer_test/<league>/` (`events_<YYYYMMDD>.json`,
`<event id>_entry.json`, `<event id>_close.json`) inside the paid `player-odds-history-`
cache. A recorded answer (status 200, 404 or 422 in `calls.jsonl`) is never asked again.
No result is read here. The key travels only in request params and is redacted from
every message kept.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.cup_odds import _call, balance, iso, redact
from soccer_stats.data import RAW_DIR, season_kickoffs
from soccer_stats.odds_feed import SPORTS
from soccer_stats.player_goal_odds import MARKET, CappedBudget
from soccer_stats.player_odds import BASE

LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
# §16's list: nine bookmakers (the judged best price) and the Betfair exchange (logged only).
BOOKS = (
    "draftkings",
    "fanduel",
    "betmgm",
    "betrivers",
    "williamhill_us",
    "bovada",
    "espnbet",
    "onexbet",
    "williamhill",
    "betfair_ex_uk",
)
JUDGED_BOOKS = BOOKS[:9]
SNAPSHOTS = {"entry": pd.Timedelta(minutes=50), "close": pd.Timedelta(minutes=2)}
LIST_LEAD = pd.Timedelta(hours=3)  # the day's events list, this long before its first kickoff
HIST_COST = 10
LIST_COST = 1
MAX_CALL_COST = 10
KEEP_GOING = (404, 422)  # one event not listed at that moment: recorded, the run goes on
DESIGNS = {
    "dev": {"season": 2025, "since": None, "note": "2025/26 development (sets T only)"},
    "forward": {
        "season": 2026,
        "since": "2026-10-10",
        "note": "2026/27 from 10 Oct (the judged window's history)",
    },
}
CACHE = "goalscorer_test"
SETTLED_AFTER = pd.Timedelta(hours=3)  # forward: only matches kicked off this long ago


def cache_dir(raw_dir: Path = RAW_DIR) -> Path:
    return Path(raw_dir) / "player_odds_history" / CACHE


def read_calls(base: Path) -> list[dict]:
    path = base / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _append_call(base: Path, row: dict) -> None:
    base.mkdir(parents=True, exist_ok=True)
    with open(base / "calls.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def match_days(kickoffs: pd.Series, since=None, until=None) -> list[dict]:
    """One row per UTC match day: {day, first, matches}, oldest first."""
    k = pd.to_datetime(pd.Series(list(kickoffs)), utc=True)
    if since is not None:
        k = k[k >= pd.Timestamp(since, tz="UTC")]
    if until is not None:
        k = k[k <= pd.Timestamp(until)]
    out = []
    for day, g in k.groupby(k.dt.strftime("%Y-%m-%d")):
        out.append({"day": day, "first": g.min(), "matches": int(len(g))})
    return sorted(out, key=lambda d: d["day"])


def plan(
    design: str = "dev",
    leagues: tuple[str, ...] = LEAGUES,
    now: pd.Timestamp | None = None,
    kickoffs: Callable[[str, int], pd.Series] = season_kickoffs,
) -> list[dict]:
    """Per league and match day: {league, sport, day, first, matches, list_at}."""
    d = DESIGNS[design]
    now = now or pd.Timestamp.now(tz="UTC")
    until = now - SETTLED_AFTER if design == "forward" else None
    out = []
    for lg in leagues:
        for row in match_days(kickoffs(lg, d["season"]), d["since"], until):
            out.append(
                {"league": lg, "sport": SPORTS[lg], **row, "list_at": row["first"] - LIST_LEAD}
            )
    return sorted(out, key=lambda r: (r["first"], r["league"]))


def day_events(body: dict | None, day: str) -> list[dict]:
    """The listed events kicking off on `day` (UTC)."""
    data = (body or {}).get("data", body) if isinstance(body, dict) else body
    return sorted(
        (
            e
            for e in data or []
            if isinstance(e, dict)
            and str(pd.Timestamp(e.get("commence_time")).tz_convert("UTC").date()) == day
        ),
        key=lambda e: (e["commence_time"], e["id"]),
    )


def snapshot_at(kickoff: str, kind: str) -> str:
    return iso(pd.Timestamp(kickoff) - SNAPSHOTS[kind])


def priced(body: dict | None) -> bool:
    ev = (body or {}).get("data", body) if isinstance(body, dict) else {}
    return any(
        m.get("key") == MARKET and m.get("outcomes")
        for b in (ev or {}).get("bookmakers", []) or []
        for m in b.get("markets", []) or []
    )


def run(
    design: str = "dev",
    cap: int = 0,
    api_key: str | None = None,
    raw_dir: Path = RAW_DIR,
    leagues: tuple[str, ...] = LEAGUES,
    get: Callable = requests.get,
    minutes: float | None = None,
    now: pd.Timestamp | None = None,
    kickoffs: Callable[[str, int], pd.Series] = season_kickoffs,
) -> dict:
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    base = cache_dir(raw_dir)
    now = now or pd.Timestamp.now(tz="UTC")
    until = now - SETTLED_AFTER if design == "forward" else None
    days = plan(design, leagues, now, kickoffs)
    done = {
        c["key"]
        for c in read_calls(base)
        if c.get("status") == 200 or c.get("status") in KEEP_GOING
    }
    budget = CappedBudget(max(int(cap), 0))
    res: dict = {
        "design": design,
        "note": DESIGNS[design]["note"],
        "cap": int(cap),
        "books": list(BOOKS),
        "days": len(days),
        "matches_football_data": sum(d["matches"] for d in days),
        "spent": 0,
        "bought": {"lists": 0, "snapshots": 0},
        "stopped": None,
        "log": budget.log,
    }
    held_lists = sum(
        (base / d["league"] / f"events_{d['day'].replace('-', '')}.json").exists() for d in days
    )
    res["estimate"] = (len(days) - held_lists) * LIST_COST + 2 * HIST_COST * res[
        "matches_football_data"
    ]
    dry = cap <= 0 or not api_key
    res["dry_run"] = dry
    if dry:
        budget.log.append(
            f"Dry run (cap 0 or no key): no Odds API calls; {len(days)} match days "
            f"({held_lists} lists held), {res['matches_football_data']} matches, about "
            f"{res['estimate']} credits before what the cache holds"
        )
    else:
        budget.left = balance(get, api_key)
        res["left_before"] = budget.left
        budget.log.append(f"Credits left before the run: {budget.left}")
    end = time.monotonic() + minutes * 60 if minutes else None

    def stop_now(info: dict, key: str) -> bool:
        if info.get("skipped"):
            res["stopped"] = redact(info.get("message", "skipped"), api_key)
            return True
        if info.get("stop") and info.get("status") not in KEEP_GOING:
            res["stopped"] = info.get("message") or f"HTTP {info.get('status')}"
            return True
        if (info.get("cost") or 0) > MAX_CALL_COST:
            res["stopped"] = f"a call cost {info['cost']} credits (> {MAX_CALL_COST}) at {key}"
            return True
        return False

    def fetch(path: Path, key: str, url: str, params: dict, estimate: int, label: str):
        """(body or None, stop?) for one capped, cached call."""
        if path.exists():
            return json.loads(path.read_text()), False
        if key in done or dry:
            return None, False
        if end is not None and time.monotonic() > end:
            res["stopped"] = f"time budget of {minutes:g} minutes used; run again to resume"
            res["out_of_time"] = True
            return None, True
        body, info = _call(
            get, url, {"apiKey": api_key, **params}, budget, estimate, label, api_key
        )
        if info.get("skipped"):
            stop_now(info, key)
            return None, True
        _append_call(base, {"key": key, "label": label, "status": info.get("status"),
                            "cost": info.get("cost"), "fetched_at": iso(pd.Timestamp.now(tz="UTC")),
                            "message": redact(info.get("message", ""), api_key)})  # fmt: skip
        if body is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body))
        res["spent"] = budget.spent
        return body, stop_now(info, key)

    per: dict[str, dict] = {
        lg: {
            "days": 0,
            "matches_football_data": 0,
            "events": 0,
            "entry": 0,
            "close": 0,
            "entry_priced": 0,
            "close_priced": 0,
        }  # fmt: skip
        for lg in leagues
    }
    stopped = False
    for d in days:
        lg, s = d["league"], per[d["league"]]
        s["days"] += 1
        s["matches_football_data"] += d["matches"]
        if stopped:
            continue
        stem = d["day"].replace("-", "")
        key = f"{lg}|events|{stem}"
        list_path = base / lg / f"events_{stem}.json"
        had_list = list_path.exists()
        body, stopped = fetch(
            list_path,
            key,
            f"{BASE}/historical/sports/{d['sport']}/events",
            {"date": iso(d["list_at"]), "dateFormat": "iso"},
            LIST_COST,
            f"{lg} events {d['day']}",
        )
        if body is not None:
            res["bought"]["lists"] += int(not had_list)
        evs = day_events(body, d["day"])
        if until is not None:
            evs = [e for e in evs if pd.Timestamp(e["commence_time"]) <= until]
        s["events"] += len(evs)
        for e in evs:
            for kind in SNAPSHOTS:
                if stopped:
                    break
                path = base / lg / f"{e['id']}_{kind}.json"
                had = path.exists()
                key = f"{lg}|{e['id']}|{kind}"
                at = snapshot_at(e["commence_time"], kind)
                body, stopped = fetch(
                    path,
                    key,
                    f"{BASE}/historical/sports/{d['sport']}/events/{e['id']}/odds",
                    {
                        "bookmakers": ",".join(BOOKS),
                        "markets": MARKET,
                        "oddsFormat": "decimal",
                        "dateFormat": "iso",
                        "date": at,
                    },  # fmt: skip
                    HIST_COST,
                    f"{lg} {kind} {e['home_team']} v {e['away_team']} at {at}",
                )
                if body is not None:
                    s[kind] += 1
                    s[f"{kind}_priced"] += int(priced(body))
                    res["bought"]["snapshots"] += int(not had)
            if stopped:
                break
        if stopped and res["stopped"]:
            budget.log.append(f"Stopping: {res['stopped']}")
    res["leagues"] = per
    res["left_after"] = budget.left
    res["spent"] = budget.spent
    res["log"] = [redact(x, api_key) for x in budget.log]
    return res


def summary_lines(res: dict) -> list[str]:
    head = (
        f"Goalscorer backfill ({res['design']}: {res['note']}): {res['days']} match days, "
        f"{res['matches_football_data']} football-data matches, cap {res['cap']}"
    )
    lines = [head]
    if res.get("dry_run"):
        lines.append(f"  dry run: about {res['estimate']} credits before what the cache holds")
    else:
        lines.append(
            f"  bought {res['bought']['lists']} lists and {res['bought']['snapshots']} snapshots, "
            f"spent {res['spent']} credits; credits left {res.get('left_before')} -> "
            f"{res.get('left_after')}"
        )
    if res.get("stopped"):
        lines.append(f"  stopped: {res['stopped']}")
    for lg, s in res.get("leagues", {}).items():
        lines.append(
            f"  {lg}: {s['matches_football_data']} matches on {s['days']} days, {s['events']} "
            f"events listed; entry held {s['entry']} (priced {s['entry_priced']}), close held "
            f"{s['close']} (priced {s['close_priced']})"
        )
    return lines
