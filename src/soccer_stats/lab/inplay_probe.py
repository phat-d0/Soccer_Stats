"""Phase 2 probe of the favourite comeback test (owner, 24 Oct; `docs/lab.md` "Favourites
coming back from behind"): do The Odds API's historical snapshots carry in-play prices?

One historical bulk snapshot (`/v4/historical/sports/{sport}/odds`, h2h, up to ten books in
one call = one region, about 10 credits) taken while matches were in play. For every event
in it, the report says whether it had already kicked off at the snapshot, which books quoted
it and how fresh each quote was (`last_update` against the snapshot time). Started events
with fresh quotes = in-play prices exist; started events missing, or quotes frozen at
kickoff = they don't. Nothing else is read or kept; the key is redacted from every message.

The default moment is the Premier League's 2024/25 final day: ten matches kicked off together
at 15:00 UTC on 25 May 2025, so 15:55 UTC catches all ten at half-time (event B's moment).
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.cup_odds import HIST_COST, URL, _call, balance, iso, redact
from soccer_stats.player_goal_odds import CappedBudget

SPORT = "soccer_epl"
AT = "2025-05-25T15:55:00Z"
# Ten books in one call cost one region; a mix of US and European books, Pinnacle first.
BOOKS = (
    "pinnacle",
    "draftkings",
    "fanduel",
    "betmgm",
    "betfair_ex_eu",
    "williamhill",
    "unibet_eu",
    "bovada",
    "marathonbet",
    "sport888",
)
FRESH_MINUTES = 15  # a quote updated this close to the snapshot counts as live


def events_report(body: dict, at: pd.Timestamp) -> dict:
    """Per event: kicked off or not, minutes since kickoff, and per book its h2h prices and
    how many minutes before the snapshot the quote was last updated."""
    data = body.get("data") or []
    out = []
    for ev in data:
        ko = pd.Timestamp(ev.get("commence_time"))
        started = ko <= at
        books = {}
        for b in ev.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                upd = m.get("last_update") or b.get("last_update")
                age = (at - pd.Timestamp(upd)).total_seconds() / 60 if upd else None
                books[b.get("key")] = {
                    "prices": {o.get("name"): o.get("price") for o in m.get("outcomes") or []},
                    "last_update": upd,
                    "minutes_old": None if age is None else round(age, 1),
                    "after_kickoff": bool(upd) and pd.Timestamp(upd) > ko,
                }
        out.append(
            {
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
                "commence_time": ev.get("commence_time"),
                "started": started,
                "minutes_since_kickoff": round((at - ko).total_seconds() / 60, 1)
                if started
                else None,
                "books": books,
            }
        )
    started = [e for e in out if e["started"]]
    live = [
        e
        for e in started
        if any(
            b["after_kickoff"]
            and b["minutes_old"] is not None
            and b["minutes_old"] <= FRESH_MINUTES
            for b in e["books"].values()
        )
    ]
    live_books = sorted(
        {
            k
            for e in started
            for k, b in e["books"].items()
            if b["after_kickoff"]
            and b["minutes_old"] is not None
            and b["minutes_old"] <= FRESH_MINUTES
        }
    )
    if not started:
        verdict = "no started events in the snapshot: historical snapshots drop matches at kickoff"
    elif live:
        verdict = f"in-play prices present: {len(live)} of {len(started)} started events"
    else:
        verdict = "started events listed but no quote updated after kickoff: no in-play prices"
    return {
        "snapshot": body.get("timestamp"),
        "previous": body.get("previous_timestamp"),
        "next": body.get("next_timestamp"),
        "events": len(out),
        "started": len(started),
        "started_with_fresh_quotes": len(live),
        "books_quoting_in_play": live_books,
        "verdict": verdict,
        "detail": out,
    }


def run(
    at: str = AT,
    sport: str = SPORT,
    books=BOOKS,
    cap: int = 0,
    api_key: str | None = None,
    get: Callable = requests.get,
) -> dict:
    """One capped historical snapshot. cap 0 or no key = a dry run (no call)."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    when = pd.Timestamp(at)
    when = when.tz_localize("UTC") if when.tzinfo is None else when.tz_convert("UTC")
    budget = CappedBudget(max(int(cap), 0))
    res: dict = {"sport": sport, "at": iso(when), "books": list(books), "cap": int(cap)}
    if cap <= 0 or not api_key:
        res["dry_run"] = True
        res["log"] = ["Dry run (cap 0 or no key): no Odds API calls"]
        res["estimate"] = HIST_COST
        return res
    res["dry_run"] = False
    budget.left = balance(get, api_key)
    res["left_before"] = budget.left
    params = {
        "apiKey": api_key,
        "date": iso(when),
        "bookmakers": ",".join(books),
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    body, info = _call(get, URL.format(sport=sport), params, budget, HIST_COST, "snapshot", api_key)
    res["call"] = {k: v for k, v in info.items() if k != "stop"}
    res["spent"] = budget.spent
    res["left_after"] = budget.left
    res["log"] = [redact(line, api_key) for line in budget.log]
    if body is not None:
        res.update(events_report(body, when))
    return res


def report(res: dict) -> str:
    lines = [
        f"== In-play probe: {res['sport']} at {res['at']} (cap {res['cap']}) ==",
        *[f"  {line}" for line in res.get("log", [])],
    ]
    if res.get("dry_run"):
        lines.append(f"  dry run; one snapshot would cost about {res.get('estimate')} credits")
        return "\n".join(lines)
    lines.append(
        f"  spent {res.get('spent')} credits; left {res.get('left_before')} -> "
        f"{res.get('left_after')}"
    )
    if "verdict" not in res:
        lines.append(f"  no snapshot: {res.get('call')}")
        return "\n".join(lines)
    lines.append(
        f"  snapshot {res['snapshot']} (previous {res['previous']}, next {res['next']}): "
        f"{res['events']} events, {res['started']} started, "
        f"{res['started_with_fresh_quotes']} with quotes updated after kickoff "
        f"(<= {FRESH_MINUTES} min old)"
    )
    lines.append(f"  books quoting in play: {', '.join(res['books_quoting_in_play']) or 'none'}")
    for e in res["detail"]:
        if not e["started"]:
            continue
        lines.append(
            f"  {e['home']} v {e['away']} (+{e['minutes_since_kickoff']} min): "
            + (
                "; ".join(
                    f"{k} {b['prices']} {b['minutes_old']} min old"
                    for k, b in sorted(e["books"].items())
                )
                or "no books"
            )
        )
    lines.append(f"VERDICT: {res['verdict']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--at", default=AT, help="snapshot time (ISO, UTC)")
    p.add_argument("--sport", default=SPORT)
    p.add_argument("--cap", type=int, default=0, help="most credits to spend (0 = dry run)")
    p.add_argument("--json", default="", help="write the result here")
    args = p.parse_args(argv)
    res = run(at=args.at or AT, sport=args.sport, cap=args.cap)
    print(report(res))
    compact = {k: v for k, v in res.items() if k != "detail"}
    print("INPLAY_PROBE_JSON " + json.dumps(compact, default=str))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
