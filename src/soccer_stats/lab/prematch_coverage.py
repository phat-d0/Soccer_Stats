"""Coverage of a bought pre-match cup design (H2P, H2U): counts only, never a result (lead, 28
Oct). Reads the frozen slot file, the fetch's call record and the bought bodies in
`comeback-odds-*`. A pre-match snapshot holds prices, not scores, so nothing here can read an
outcome.

* Snapshots: bought, the cost each call billed, how many held any event, and how many held an
  event kicking off within 30 minutes of the slot.
* Matches: found when an event within 30 minutes of the kickoff has both names close to ESPN's
  (normalised names equal, one inside the other, or a close spelling), home and away either
  way round (finals are at neutral grounds). Then whether Pinnacle and DraftKings quote a full
  1X2. By cup, by season and in total.
"""

from __future__ import annotations

import argparse
import difflib
import json
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from soccer_stats.cups import norm
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks2 as c2

KICKOFF_TOL = 30  # minutes between ESPN's kickoff and the event's
CLOSE_NAME = 0.75


def _prefixes(short: list[str], long: list[str]) -> bool:
    """Each word of the shorter name starts a word of the longer one, in order ("man city",
    "manchester city")."""
    i = 0
    for w in long:
        if i < len(short) and w.startswith(short[i]):
            i += 1
    return i == len(short)


def same_team(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    xs, ys = x.split(), y.split()
    if (len(xs) <= len(ys) and _prefixes(xs, ys)) or (len(ys) <= len(xs) and _prefixes(ys, xs)):
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= CLOSE_NAME


def find(events: list[dict], home: str, away: str, kickoff: pd.Timestamp) -> dict | None:
    for ev in events:
        t = c2._ts(ev.get("commence_time"))
        if t is None or abs((t - kickoff).total_seconds()) > KICKOFF_TOL * 60:
            continue
        h, a = ev.get("home_team", ""), ev.get("away_team", "")
        if (same_team(home, h) and same_team(away, a)) or (
            same_team(home, a) and same_team(away, h)
        ):
            return ev
    return None


def full_1x2(ev: dict, book: str) -> bool:
    q = c2.quote(ev, book)
    return bool(q) and set(q["prices"]) == {"home", "draw", "away"}


def season_of(kickoff: str) -> str:
    t = pd.Timestamp(kickoff)
    y = t.year if t.month >= 7 else t.year - 1
    return f"{y}/{str(y + 1)[-2:]}"


def coverage(plan: dict, cache_dir: Path) -> dict:
    calls = {(c["sport"], c["request_at"]): c for c in cf.read_calls(cache_dir)}
    snaps = Counter()
    costs = Counter()
    groups: dict[tuple, Counter] = defaultdict(Counter)
    bodies: dict[tuple, list] = {}
    for s in plan["slots"]:
        key = (s["sport"], s["request_at"])
        call = calls.get(key)
        if call is not None:
            costs[str(call.get("cost"))] += 1
        path = cf.body_path(cache_dir, s)
        if not path.exists():
            snaps["no_body"] += 1
            continue
        events = json.loads(path.read_text()).get("data") or []
        bodies[key] = events
        snaps["bought"] += 1
        snaps["any_events"] += bool(events)
        ko = pd.Timestamp(s["kickoff"])
        near = [
            e for e in events
            if (t := c2._ts(e.get("commence_time"))) is not None
            and abs((t - ko).total_seconds()) <= KICKOFF_TOL * 60
        ]  # fmt: skip
        snaps["events_at_kickoff"] += bool(near)
    for s in plan["kickoff_slots"]:
        events = bodies.get((s["sport"], s["request_at"]), [])
        ko = pd.Timestamp(s["kickoff"])
        for m in s["matches"]:
            ev = find(events, m["home"], m["away"], ko)
            near = any(
                (t := c2._ts(e.get("commence_time"))) is not None
                and abs((t - ko).total_seconds()) <= KICKOFF_TOL * 60
                for e in events
            )
            pin = bool(ev) and full_1x2(ev, "pinnacle")
            dk = bool(ev) and full_1x2(ev, "draftkings")
            row = {
                "matches": 1,
                "events_at_kickoff": int(near),
                "found": int(bool(ev)),
                "pinnacle": int(pin),
                "draftkings": int(dk),
                "either": int(pin or dk),
            }
            for g in (("all",), ("cup", s["league"]), ("season", season_of(s["kickoff"])),
                      ("cup_season", s["league"], season_of(s["kickoff"]))):  # fmt: skip
                groups[g].update(row)
    out = {
        "design": plan.get("design"),
        "snapshots": dict(snaps),
        "cost_per_call": dict(costs),
        "matches": dict(groups[("all",)]),
        "by_cup": {g[1]: dict(v) for g, v in sorted(groups.items()) if g[0] == "cup"},
        "by_season": {g[1]: dict(v) for g, v in sorted(groups.items()) if g[0] == "season"},
        "by_cup_season": {
            f"{g[1]} {g[2]}": dict(v) for g, v in sorted(groups.items()) if g[0] == "cup_season"
        },
    }
    return out


def report(res: dict) -> str:
    s, m = res["snapshots"], res["matches"]
    lines = [
        f"== Pre-match coverage ({res['design']}) ==",
        f"  snapshots: {s.get('bought', 0)} bought ({s.get('no_body', 0)} missing), "
        f"{s.get('any_events', 0)} with any event, {s.get('events_at_kickoff', 0)} with an "
        f"event at the slot's kickoff; billed per call: {res['cost_per_call']}",
        f"  matches: {m.get('matches', 0)}, an event at kickoff {m.get('events_at_kickoff', 0)}, "
        f"found {m.get('found', 0)}, Pinnacle 1X2 "
        f"{m.get('pinnacle', 0)}, DraftKings 1X2 {m.get('draftkings', 0)}, either "
        f"{m.get('either', 0)}",
    ]
    for name in ("by_cup", "by_season", "by_cup_season"):
        lines.append(f"  {name}:")
        for k, v in res[name].items():
            lines.append(
                f"    {k}: {v['matches']} matches, at kickoff {v['events_at_kickoff']}, found "
                f"{v['found']}, Pinnacle "
                f"{v['pinnacle']}, DraftKings {v['draftkings']}, either {v['either']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--slots", default="H2P", help="design H2P or H2U, or a slot file")
    p.add_argument("--cache-dir", default="", help="the bought bodies (default raw dir)")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    plan = json.loads(cf.slots_path(args.slots).read_text())
    res = coverage(plan, cache)
    print(report(res))
    print("PREMATCH_COVERAGE_JSON " + json.dumps(res))


if __name__ == "__main__":
    main()
