"""Historical corner prices probe: do The Odds API's historical endpoints carry Pinnacle's
team-corner prices (`alternate_team_totals_corners`, the market logged live since round
14), and what would a priced corners backtest cost? (docs/totals.md, "Historical corner
prices probe"; owner-approved 50-credit cap, 11 Oct.)

Four probe points, one match each: E0 2025/26 (a Saturday in February 2026), E0 2023/24
(March 2024, to see how far back the market goes), I1 2025/26 and F1 2024/25. At each:
one historical events call (about 1 credit) 24 hours before a Saturday kickoff taken
from the cached football-data file (a fixed fallback date without one), then one
historical event-odds call for the match nearest that kickoff at its kickoff minus 24
hours, `markets=alternate_team_totals_corners&bookmakers=pinnacle` (about 10 credits:
one market, one book = one region; an empty snapshot costs less). If the cap leaves room
afterwards, one more odds call on the E0 2025/26 match 15 minutes before kickoff (does a
close exist?).

`player_goal_odds.CappedBudget` skips any call whose maximum cost would pass the cap or
leave under 3,000 credits on the shared key; the cost counted is `x-requests-last`.
`cap` 0 is a dry run: no calls at all, only the plan and the projection at the nominal
cost. Bodies are cached under `data/raw/corner_history/` (a rerun costs nothing). The
key is never printed, logged or written: requests carry it only in `params`, failures
log the exception type, and any API message is redacted before it is kept.

`projection` prices the backtest a positive answer would lead to (E0, I1, F1 x 2023/24,
2024/25, 2025/26): matches from the cached football-data files (else 380, or 306 for
Ligue 1), times the measured cost per odds call, for (a) one look snapshot and (b) look
plus close, plus one events call per distinct kickoff date and snapshot.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.corners_live import BOOK, MARKET
from soccer_stats.data import RAW_DIR, season_code
from soccer_stats.edge.market_probe import pairs, quotes
from soccer_stats.odds_feed import SPORTS
from soccer_stats.player_goal_odds import CappedBudget
from soccer_stats.player_odds import BASE, RESERVE_CREDITS

CACHE = "corner_history"  # under RAW_DIR
REGIONS = "eu"  # Pinnacle's region; `bookmakers=` takes precedence on the API
EVENTS_ESTIMATE = 1  # historical events list: 1 credit (0 when empty)
ODDS_ESTIMATE = 10  # historical event odds: 10 x markets x regions (one market, one book)
LOOK_HOURS = 24.0
CLOSE_MINUTES = 15
MATCH_WINDOW = pd.Timedelta(hours=3)  # an event this close to the target kickoff is "it"
PROJECT_LEAGUES = ("E0", "I1", "F1")  # where corners model (f) passed bake-off 2
PROJECT_SEASONS = (2023, 2024, 2025)
STANDARD_MATCHES = {"E0": 380, "I1": 380, "F1": 306}  # F1: 18 clubs from 2023/24
DATES_PER_MATCH = 0.3  # distinct kickoff dates per match when no file is cached (rough)


@dataclass(frozen=True)
class ProbePoint:
    key: str
    league: str
    season: int  # start year
    year: int  # calendar year and month of the Saturday probed
    month: int
    fallback: str  # a Saturday kickoff (UTC) used when the season's file isn't cached


POINTS = (
    ProbePoint("E0_2526", "E0", 2025, 2026, 2, "2026-02-07T15:00:00Z"),
    ProbePoint("E0_2324", "E0", 2023, 2024, 3, "2024-03-09T15:00:00Z"),
    ProbePoint("I1_2526", "I1", 2025, 2026, 2, "2026-02-07T17:00:00Z"),
    ProbePoint("F1_2425", "F1", 2024, 2025, 3, "2025-03-08T16:00:00Z"),
)
CLOSE_POINT = "E0_2526"


def season_label(start: int) -> str:
    return f"{start}/{(start + 1) % 100:02d}"


def _iso(t: pd.Timestamp) -> str:
    return pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str, api_key: str) -> str:
    """Remove the key from any text that might be kept (API messages, bodies)."""
    return text.replace(api_key, "***") if api_key else text


# ---------- cached football-data files (file reads only, never a download) ----------


def season_file(league: str, start: int, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / f"{league}_{season_code(start)}.csv"


def cached_kickoffs(league: str, start: int, raw_dir: Path = RAW_DIR) -> pd.Series | None:
    """Kickoff times (UTC) of the season's matches in its cached football-data file (Time
    is UK local; a missing time counts as 15:00), or None when the file isn't cached."""
    path = season_file(league, start, raw_dir)
    if not path.exists():
        return None
    raw = pd.read_csv(path, encoding="latin-1")
    if "Date" not in raw:
        return None
    raw = raw.dropna(subset=["Date"])
    if "HomeTeam" in raw:
        raw = raw.dropna(subset=["HomeTeam"])
    times = raw["Time"].fillna("15:00") if "Time" in raw else pd.Series("15:00", index=raw.index)
    local = pd.to_datetime(
        raw["Date"].astype(str) + " " + times.astype(str), dayfirst=True, format="mixed"
    )
    return (
        local.dt.tz_localize("Europe/London", ambiguous="NaT", nonexistent="shift_forward")
        .dt.tz_convert("UTC")
        .dropna()
        .sort_values()
        .reset_index(drop=True)
    )


def target_kickoff(point: ProbePoint, raw_dir: Path = RAW_DIR) -> tuple[pd.Timestamp, str]:
    """The first Saturday kickoff of the point's month in the cached file, else the
    fallback date. Returns (kickoff, source)."""
    ks = cached_kickoffs(point.league, point.season, raw_dir)
    if ks is not None:
        hit = ks[(ks.dt.year == point.year) & (ks.dt.month == point.month) & (ks.dt.dayofweek == 5)]
        if len(hit):
            return pd.Timestamp(hit.iloc[0]), "football-data"
    return pd.Timestamp(point.fallback), "fallback"


def pick_event(events: list[dict], target: pd.Timestamp, snapshot: pd.Timestamp) -> dict | None:
    """The listed event nearest the target kickoff (within 3 hours), else the first one
    kicking off 12-36 hours after the snapshot."""
    evs = [e for e in events or [] if e.get("id") and e.get("commence_time")]
    if not evs:
        return None
    k = [pd.Timestamp(e["commence_time"]) for e in evs]
    near = sorted((abs(t - target), i) for i, t in enumerate(k) if abs(t - target) <= MATCH_WINDOW)
    if near:
        return evs[near[0][1]]
    later = sorted(
        (t, i)
        for i, t in enumerate(k)
        if snapshot + pd.Timedelta(hours=12) <= t <= snapshot + pd.Timedelta(hours=36)
    )
    return evs[later[0][1]] if later else None


# ---------- body parsing ----------


def corner_quotes(body: dict | None) -> dict:
    """Pinnacle's team-corner quotes in one historical event-odds body (`data` holds the
    event): per team the lines, how many are two-sided, and the margins 1/over + 1/under
    - 1 on the two-sided ones."""
    ev = (body or {}).get("data", body) if isinstance(body, dict) else {}
    p = pairs(quotes(ev if isinstance(ev, dict) else {}))
    p = p[(p["book"] == BOOK) & (p["market"] == MARKET)] if len(p) else p
    teams: dict[str, dict] = {}
    margins: list[float] = []
    for team, x in p.groupby("subject") if len(p) else []:
        two = x[x["over"].notna() & x["under"].notna()]
        teams[team or "(match)"] = {
            "lines": sorted(float(v) for v in x["line"].dropna()),
            "two_sided": int(len(two)),
            "one_sided": int(len(x) - len(two)),
            "margins": [round(float(m), 4) for m in two["margin"]],
        }
        margins += [float(m) for m in two["margin"]]
    n = sum(t["two_sided"] + t["one_sided"] for t in teams.values())
    books = [b.get("key") for b in (ev or {}).get("bookmakers", []) or []] if ev else []
    return {
        "quoted": n > 0,
        "teams": teams,
        "quotes": n,
        "both_sides": n > 0 and all(t["one_sided"] == 0 for t in teams.values()),
        "margin_median": round(float(pd.Series(margins).median()), 4) if margins else None,
        "margin_min": round(min(margins), 4) if margins else None,
        "margin_max": round(max(margins), 4) if margins else None,
        "books_returned": books,
    }


# ---------- calls ----------


def _call(get, url, params, budget, estimate, label, api_key) -> tuple[dict | None, dict]:
    """One capped call. Returns (body or None, info {status, cost, message, skipped})."""
    if not budget.allows(estimate):
        why = (
            f"would leave under the reserve ({budget.left} left)"
            if budget.left is not None and budget.left - estimate < RESERVE_CREDITS
            else f"would pass the {budget.cap}-credit cap"
        )
        budget.log.append(f"{label}: skipped, {why}")
        return None, {"skipped": True, "cost": 0, "status": None, "message": why}
    try:
        r = get(url, params=params, timeout=60)
    except requests.RequestException as exc:  # the message would hold the URL and key
        budget.log.append(f"{label}: could not reach The Odds API ({type(exc).__name__})")
        return None, {"skipped": False, "cost": 0, "status": None, "message": type(exc).__name__}
    before = budget.spent
    budget.charge(r, estimate, label)
    info = {"skipped": False, "cost": budget.spent - before, "status": r.status_code}
    if not r.ok:
        try:
            msg = str((r.json() or {}).get("message", ""))
        except (ValueError, AttributeError):
            msg = ""
        info["message"] = redact(msg, api_key)[:300]
        if info["message"]:
            budget.log.append(f"{label}: {info['message']}")
        return None, info
    try:
        return r.json(), info
    except ValueError:
        info["message"] = "body was not JSON"
        return None, info


def _cached_call(path: Path, call: Callable[[], tuple[dict | None, dict]], requested: str, log):
    """A cached body (0 credits) or a new call whose body is cached with its cost."""
    if path.exists():
        saved = json.loads(path.read_text())
        log.append(f"{path.name}: cached, 0 credits")
        return saved["body"], {
            "skipped": False,
            "cost": 0,
            "cost_when_fetched": saved.get("cost"),
            "status": saved.get("status"),
            "cached": True,
        }
    body, info = call()
    if body is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "requested": requested,
                    "status": info.get("status"),
                    "cost": info.get("cost"),
                    "body": body,
                }
            )
        )
        info["cost_when_fetched"] = info.get("cost")
    info["cached"] = False
    return body, info


def _odds_snapshot(get, budget, api_key, sport, league, event, kind, when, cache_dir) -> dict:
    at = _iso(when)
    label = f"{league} odds {event['home_team']} v {event['away_team']} {kind} at {at}"
    path = cache_dir / f"{league}_{event['id']}_{kind}_{BOOK}_corners.json"
    body, info = _cached_call(
        path,
        lambda: _call(
            get,
            f"{BASE}/historical/sports/{sport}/events/{event['id']}/odds",
            {
                "apiKey": api_key,
                "regions": REGIONS,
                "bookmakers": BOOK,
                "markets": MARKET,
                "oddsFormat": "decimal",
                "date": at,
            },
            budget,
            ODDS_ESTIMATE,
            label,
            api_key,
        ),
        at,
        budget.log,
    )
    out = {"kind": kind, "requested": at, **info}
    if body is None:
        out.update({"timestamp": None, "quoted": False})
        return out
    out["timestamp"] = body.get("timestamp") if isinstance(body, dict) else None
    out.update(corner_quotes(body))
    return out


def run(
    cap: int,
    points: tuple[ProbePoint, ...] = POINTS,
    api_key: str | None = None,
    get: Callable = requests.get,
    raw_dir: Path = RAW_DIR,
    cache_dir: Path | None = None,
    close: bool = True,
) -> dict:
    """The probe, never past `cap` credits. `cap` 0 (or no key) = dry run: no calls."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    cache_dir = cache_dir if cache_dir is not None else raw_dir / CACHE
    budget = CappedBudget(cap)
    dry = cap <= 0 or not api_key
    res: dict = {
        "cap": cap,
        "dry_run": dry,
        "market": MARKET,
        "book": BOOK,
        "points": [],
        "log": budget.log,
        "left_before": None,
    }
    if cap <= 0:
        budget.log.append("Dry run (cap 0): no calls made")
    elif not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
    for pt in points:
        target, source = target_kickoff(pt, raw_dir)
        snap = target - pd.Timedelta(hours=LOOK_HOURS)
        r: dict = {
            "key": pt.key,
            "league": pt.league,
            "season": season_label(pt.season),
            "target_kickoff": _iso(target),
            "target_source": source,
            "events_date": _iso(snap),
            "event": None,
        }
        res["points"].append(r)
        if dry:
            r["note"] = "dry run: not called"
            continue
        sport = SPORTS[pt.league]
        idx, info = _cached_call(
            cache_dir / f"{pt.league}_events_{snap.strftime('%Y%m%dT%H%MZ')}.json",
            lambda sport=sport, snap=snap, pt=pt: _call(
                get,
                f"{BASE}/historical/sports/{sport}/events",
                {"apiKey": api_key, "date": _iso(snap)},
                budget,
                EVENTS_ESTIMATE,
                f"{pt.key} events at {_iso(snap)}",
                api_key,
            ),
            _iso(snap),
            budget.log,
        )
        r["events_call"] = {k: v for k, v in info.items() if k != "message"} | (
            {"message": info["message"]} if info.get("message") else {}
        )
        listed = (idx or {}).get("data", []) if isinstance(idx, dict) else []
        r["events_snapshot"] = idx.get("timestamp") if isinstance(idx, dict) else None
        r["events_listed"] = len(listed or [])
        ev = pick_event(listed, target, snap)
        if ev is None:
            r["note"] = "no event found" if idx is not None else "events call skipped or failed"
            continue
        r["event"] = {
            "id": ev["id"],
            "home": ev.get("home_team"),
            "away": ev.get("away_team"),
            "commence_time": ev["commence_time"],
        }
        k = pd.Timestamp(ev["commence_time"])
        r["look"] = _odds_snapshot(
            get,
            budget,
            api_key,
            sport,
            pt.league,
            ev,
            "look",
            k - pd.Timedelta(hours=LOOK_HOURS),
            cache_dir,
        )
    if close and not dry:
        r = next((p for p in res["points"] if p["key"] == CLOSE_POINT and p["event"]), None)
        if r is not None:
            ev = {
                "id": r["event"]["id"],
                "home_team": r["event"]["home"],
                "away_team": r["event"]["away"],
            }
            k = pd.Timestamp(r["event"]["commence_time"])
            r["close"] = _odds_snapshot(
                get,
                budget,
                api_key,
                SPORTS[r["league"]],
                r["league"],
                ev,
                "close",
                k - pd.Timedelta(minutes=CLOSE_MINUTES),
                cache_dir,
            )
    if budget.left is not None:  # the balance before = the last balance read + spend
        res["left_before"] = budget.left + budget.spent
    measured = measured_cost(res["points"])
    res["projection"] = projection(measured, raw_dir)
    res["verdict"] = verdict(res["points"], dry)
    return {**res, "spent": budget.spent, "left": budget.left}


# ---------- cost and verdict ----------


def measured_cost(points: list[dict]) -> int | None:
    """The cost of an odds call that returned Pinnacle's team-corner quotes (the largest,
    to stay on the safe side), counted from x-requests-last when it was fetched."""
    costs = [
        s.get("cost_when_fetched")
        for p in points
        for s in (p.get("look"), p.get("close"))
        if s and s.get("quoted") and s.get("cost_when_fetched") is not None
    ]
    return max(costs) if costs else None


def projection(
    cost_per_call: int | None,
    raw_dir: Path = RAW_DIR,
    leagues: tuple[str, ...] = PROJECT_LEAGUES,
    seasons: tuple[int, ...] = PROJECT_SEASONS,
    events_cost: int = EVENTS_ESTIMATE,
) -> dict:
    """Credits for the priced corners backtest: per league and season, (a) one look
    snapshot a match and (b) look plus close, plus one events call per distinct kickoff
    date and snapshot. Without a measured cost the nominal ODDS_ESTIMATE is used."""
    c = cost_per_call if cost_per_call is not None else ODDS_ESTIMATE
    out: dict = {
        "cost_per_odds_call": c,
        "cost_source": "measured" if cost_per_call is not None else "assumed (nominal 10)",
        "events_cost": events_cost,
        "leagues": {},
        "by_season": {},
        "all": {"matches": 0, "look": 0, "look_close": 0},
    }
    for lg in leagues:
        rows, tot = {}, {"matches": 0, "look": 0, "look_close": 0}
        for s in seasons:
            ks = cached_kickoffs(lg, s, raw_dir)
            if ks is not None and len(ks):
                n, dates, src = len(ks), int(ks.dt.date.nunique()), "football-data"
            else:
                n = STANDARD_MATCHES[lg]
                dates, src = math.ceil(n * DATES_PER_MATCH), "standard"
            look = n * c + dates * events_cost
            both = 2 * n * c + 2 * dates * events_cost
            rows[season_label(s)] = {
                "matches": n,
                "matches_source": src,
                "kickoff_dates": dates,
                "look": look,
                "look_close": both,
            }
            for key, v in (("matches", n), ("look", look), ("look_close", both)):
                tot[key] += v
                out["all"][key] += v
                bs = out["by_season"].setdefault(
                    season_label(s), {"matches": 0, "look": 0, "look_close": 0}
                )
                bs[key] += v
        out["leagues"][lg] = {"seasons": rows, "total": tot}
    return out


def verdict(points: list[dict], dry: bool) -> dict:
    """available (every point quoted) / partly available (which seasons) / not available."""
    if dry:
        return {"verdict": "not tested", "text": "Dry run: no calls made, nothing tested."}
    tried = [p for p in points if p.get("look") and not p["look"].get("skipped")]
    if not tried:
        return {
            "verdict": "not tested",
            "text": "No odds call was made (skipped by the cap or reserve, or no event found).",
        }
    yes = [f"{p['league']} {p['season']}" for p in tried if p["look"].get("quoted")]
    no = [f"{p['league']} {p['season']}" for p in tried if not p["look"].get("quoted")]
    untried = [f"{p['league']} {p['season']}" for p in points if p not in tried]
    tail = f" Not tried: {', '.join(untried)}." if untried else ""
    if yes and not no:
        v = "available" if not untried else "partly available"
        text = f"Pinnacle team-corner prices came back at every point tried: {', '.join(yes)}."
    elif yes:
        v = "partly available"
        text = f"Quoted: {', '.join(yes)}. Not quoted: {', '.join(no)}."
    else:
        v = "not available"
        text = f"No Pinnacle team-corner prices at any point tried: {', '.join(no)}."
    close = next((p["close"] for p in points if p.get("close")), None)
    if close is not None and not close.get("skipped"):
        text += " A close (15 min before kickoff) " + (
            "came back." if close.get("quoted") else "did not come back."
        )
    elif close is not None:
        text += " The close call was skipped (cap)."
    return {"verdict": v, "text": text + tail, "quoted": yes, "not_quoted": no}


# ---------- report ----------


def _snap_line(s: dict) -> str:
    if s.get("skipped"):
        return f"  {s['kind']}: skipped ({s.get('message', '')})"
    head = (
        f"  {s['kind']} requested {s['requested']}, snapshot {s.get('timestamp') or '-'}, "
        f"HTTP {s.get('status')}, credits {s.get('cost', 0)}"
        + (f" (cached; {s.get('cost_when_fetched')} when fetched)" if s.get("cached") else "")
    )
    if not s.get("quoted"):
        books = s.get("books_returned")
        return (
            head
            + f": no Pinnacle team-corner quotes (books returned: {books or 'none'})"
            + (f"; {s['message']}" if s.get("message") else "")
        )
    teams = "; ".join(
        f"{t} lines {v['lines']} ({v['two_sided']} two-sided)" for t, v in s["teams"].items()
    )
    m = s["margin_median"]
    return (
        head
        + f": quoted, both sides {'yes' if s['both_sides'] else 'no'}, "
        + (f"margin {m:.1%} ({s['margin_min']:.1%}-{s['margin_max']:.1%})" if m is not None else "")
        + f"\n    {teams}"
    )


def report(res: dict) -> str:
    lines = [
        f"== Historical corner prices probe ({res['market']} at {res['book']}): cap "
        f"{res['cap']}, spent {res['spent']}, credits before {res['left_before']}, after "
        f"{res['left']}{' (dry run)' if res['dry_run'] else ''} ==",
    ]
    for p in res["points"]:
        lines.append(
            f"{p['key']} ({p['league']} {p['season']}): target kickoff {p['target_kickoff']} "
            f"({p['target_source']}), events at {p['events_date']}"
        )
        if "events_call" in p:
            ec = p["events_call"]
            lines.append(
                f"  events: snapshot {p.get('events_snapshot') or '-'}, "
                f"{p.get('events_listed', 0)} listed, HTTP {ec.get('status')}, "
                f"credits {ec.get('cost', 0)}" + (" (cached)" if ec.get("cached") else "")
            )
        ev = p.get("event")
        lines.append(
            f"  event: {ev['home']} v {ev['away']} ({ev['commence_time']})"
            if ev
            else f"  event: none ({p.get('note', '')})"
        )
        for kind in ("look", "close"):
            if p.get(kind):
                lines.append(_snap_line(p[kind]))
    pr = res["projection"]
    lines.append(
        f"Projected backtest cost (odds call {pr['cost_per_odds_call']} credits, "
        f"{pr['cost_source']}; events call {pr['events_cost']}):"
    )
    for lg, x in pr["leagues"].items():
        for s, v in x["seasons"].items():
            lines.append(
                f"  {lg} {s}: {v['matches']} matches ({v['matches_source']}), "
                f"{v['kickoff_dates']} kickoff dates -> look {v['look']:,}, "
                f"look + close {v['look_close']:,}"
            )
        t = x["total"]
        lines.append(f"  {lg} total: look {t['look']:,}, look + close {t['look_close']:,}")
    for s, v in pr["by_season"].items():
        lines.append(f"  {s} (three leagues): look {v['look']:,}, look + close {v['look_close']:,}")
    a = pr["all"]
    lines.append(
        f"  All three leagues, three seasons ({a['matches']} matches): look {a['look']:,}, "
        f"look + close {a['look_close']:,}"
    )
    lines.append(f"Verdict: {res['verdict']['verdict']}. {res['verdict']['text']}")
    lines += ["Log:", *[f"  {x}" for x in res["log"]]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="soccer-stats corner-history-probe")
    ap.add_argument("--cap", type=int, default=0, help="most credits to spend (0 = dry run)")
    ap.add_argument("--json", help="write the results as JSON")
    args = ap.parse_args(argv)
    cli(args.cap, args.json)


def cli(cap: int, json_path: str | None = None, **kw) -> dict:
    key = os.environ.get("ODDS_API_KEY", "")
    res = run(cap, api_key=key, **kw)
    text = redact(json.dumps(res, default=str), key)
    print(redact(report(res), key))
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(text)
    print("CORNER_HISTORY_JSON " + text)
    return res


if __name__ == "__main__":
    main()
