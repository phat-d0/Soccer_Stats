"""Fast paper settlement from ESPN's free scoreboard and match summaries (18 Oct 2026).

football-data posts a weekend's results a day or two later (data-log's ledgers: match
trades settled a median 41 h after kickoff, corner trades 37 h), while ESPN's scoreboard
says a match is final within minutes. Like the Champions League (cup_model.cup_results),
domestic match trades now settle from ESPN as soon as a match is final, and corner trades
from the summary's corner counts (`wonCorners`); football-data stays the fallback and the
later check (paper.update_ledger / update_corner_ledger).

No Odds API key or credits: ESPN's site API is free and unofficial, so every call is
defensive and bounded (RUN_SECONDS, MAX_FAILURES, MAX_REQUESTS), and any failure only
means those trades wait for football-data. Only leagues and days with an open trade past
kickoff are asked (`fetch(todo, ...)`, from paper.awaiting_results).

Matching never guesses: an ESPN event settles a trade only when both of its team names map
(espn_news.team_name: ESPN_NAMES, odds_feed.TEAM_NAMES, then a unique accent-free or
whole-word match against the league's clubs) to exactly the trade's home and away, its
kickoff is within KICKOFF_TOLERANCE of the trade's, and no other event that day fits or
lands on the same club. Anything else stays open and is listed in the report.

Caching: a scoreboard day whose every event is final, or any day two or more days past,
is cached for good under data/raw/espn_results/; a summary is cached once it has both
corner counts on a final match. Other days are asked again on each run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.espn_news import BASE, SLUGS, team_name

# Domestic leagues only: the Champions League already settles from ESPN (cup_model).
DOMESTIC = {code: slug for code, slug in SLUGS.items() if not slug.startswith("uefa.")}
KICKOFF_TOLERANCE = pd.Timedelta(hours=3)
# Ask about a trade from this long after kickoff (90 minutes, half time and stoppages).
AFTER_KICKOFF = pd.Timedelta(minutes=105)
LOOKBACK = pd.Timedelta(days=14)  # paper.VOID_AFTER_DAYS: older open trades void anyway
# A status ESPN marks completed that is not a played 90 minutes: never settle on it.
NOT_PLAYED = ("POSTPONE", "ABANDON", "CANCEL", "SUSPEND", "FORFEIT", "DELAY")
TIMEOUT = 10
RETRIES = 1
PAUSE = 0.2
RUN_SECONDS = 60  # wall clock for one paper step's ESPN calls
MAX_FAILURES = 3  # failed requests (after retries) before the run stops asking
MAX_REQUESTS = 80
CACHE = "espn_results"
COLUMNS = [
    "league",
    "season",
    "date",
    "kickoff",
    "home",
    "away",
    "home_goals",
    "away_goals",
    "home_corners",
    "away_corners",
    "event_id",
    "result_source",
    "close_home",
    "close_draw",
    "close_away",
]


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


class Budget:
    """One run's ESPN allowance: seconds, failed requests and requests in all."""

    def __init__(
        self,
        seconds: float = RUN_SECONDS,
        failures: int = MAX_FAILURES,
        requests_max: int = MAX_REQUESTS,
        clock: Callable = time.monotonic,
    ):
        self.seconds, self.max_failures, self.max_requests = seconds, failures, requests_max
        self.clock = clock
        self.start = clock()
        self.requests = self.failures = 0
        self.why: str | None = None

    def spent(self) -> bool:
        if self.why:
            return True
        if self.failures >= self.max_failures:
            self.why = f"{self.failures} failed ESPN requests"
        elif self.requests >= self.max_requests:
            self.why = f"{self.max_requests} ESPN requests"
        elif self.clock() - self.start > self.seconds:
            self.why = f"ESPN time budget ({self.seconds:g} s) used up"
        return self.why is not None


def _get_json(url: str, params: dict, get: Callable, budget: Budget) -> dict | None:
    """GET with a short timeout and one retry; None on any failure or once the budget is
    spent (then no call is made)."""
    for attempt in range(RETRIES + 1):
        if budget.spent():
            return None
        budget.requests += 1
        try:
            r = get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                body = r.json()
                return body if isinstance(body, dict) else None
            if r.status_code < 500:
                return None  # 4xx: retrying won't help
        except (requests.RequestException, ValueError):
            pass
        if attempt < RETRIES:
            time.sleep(PAUSE)
    budget.failures += 1
    return None


def _cache(raw_dir: Path, name: str) -> Path:
    return Path(raw_dir) / CACHE / name


def _read(path: Path) -> dict | None:
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


def _raw_events(body: dict | None) -> list[dict]:
    ev = body.get("events") if isinstance(body, dict) else None
    return [e for e in ev if isinstance(e, dict)] if isinstance(ev, list) else []


def status_name(e: dict) -> str:
    st = e.get("status") if isinstance(e.get("status"), dict) else {}
    typ = st.get("type") if isinstance(st.get("type"), dict) else {}
    return str(typ.get("name") or "")


def parse(body: dict | None) -> list[dict]:
    """cups.parse_events plus `final`: ESPN says completed, the status is not a
    postponed/abandoned/cancelled one, and the 90-minute score is known."""
    from soccer_stats.cups import parse_events

    names = {str(e.get("id")): status_name(e).upper() for e in _raw_events(body)}
    out = []
    for e in parse_events(body):
        name = names.get(e["id"], "")
        played = not any(w in name for w in NOT_PLAYED)
        out.append({**e, "status": name, "final": e["completed"] and played and bool(e["score90"])})
    return out


def scoreboard(
    slug: str, day: str, raw_dir: Path, get: Callable, budget: Budget, now: pd.Timestamp
) -> dict | None:
    """One day's scoreboard (YYYYMMDD; ESPN takes one date per call). Cached for good once
    every event on it is final (or the day is two days past); else asked each run."""
    path = _cache(raw_dir, f"{slug}_{day}.json")
    cached = _read(path)
    if cached is not None:
        return cached
    body = _get_json(BASE.format(slug=slug) + "/scoreboard", {"dates": day}, get, budget)
    if body is None:
        return None
    events = parse(body)
    old = pd.Timestamp(day, tz="UTC") < now.normalize() - pd.Timedelta(days=1)
    if old or (events and all(e["final"] for e in events)):
        _write(path, body)
    return body


def summary_corners(body: dict | None, event: dict) -> tuple[int, int] | None:
    """(home, away) corner counts from a summary's boxscore (`wonCorners`), matching each
    team by ESPN id, else display name, else homeAway; None unless both are whole numbers."""
    teams = ((body or {}).get("boxscore") or {}).get("teams") if isinstance(body, dict) else None
    got: dict[str, int] = {}
    for t in teams if isinstance(teams, list) else []:
        if not isinstance(t, dict):
            continue
        team = t.get("team") if isinstance(t.get("team"), dict) else {}
        tid, name = str(team.get("id") or ""), team.get("displayName")
        if tid and tid == event.get("home_id"):
            side = "home"
        elif tid and tid == event.get("away_id"):
            side = "away"
        elif name and name == event.get("home"):
            side = "home"
        elif name and name == event.get("away"):
            side = "away"
        else:
            side = t.get("homeAway") if t.get("homeAway") in ("home", "away") else None
        if side is None or side in got:
            return None  # a team we can't place, or two on one side: don't guess
        for s in t.get("statistics") or []:
            if isinstance(s, dict) and s.get("name") == "wonCorners":
                try:
                    v = float(s.get("displayValue", s.get("value")))
                except (TypeError, ValueError):
                    continue
                if v >= 0 and v == int(v):
                    got[side] = int(v)
    if "home" in got and "away" in got:
        return got["home"], got["away"]
    return None


def summary(slug: str, event: dict, raw_dir: Path, get: Callable, budget: Budget):
    """The final match's corner counts, from a cached summary or one call."""
    path = _cache(raw_dir, f"{slug}_summary_{event['id']}.json")
    cached = _read(path)
    if cached is not None and summary_corners(cached, event):
        return summary_corners(cached, event)
    body = _get_json(BASE.format(slug=slug) + "/summary", {"event": event["id"]}, get, budget)
    corners = summary_corners(body, event)
    if corners is not None:
        _write(path, body)
    return corners


def _matches(todo: list[dict]) -> dict[tuple, dict]:
    """One entry per match (league, home, away, kickoff), with `corners` when a corner
    trade on it is open."""
    out: dict[tuple, dict] = {}
    for t in todo:
        k = _ts(t["kickoff"])
        key = (t["league"], t["home"], t["away"], k)
        m = out.setdefault(
            key,
            {
                "league": t["league"],
                "season": t["season"],
                "home": t["home"],
                "away": t["away"],
                "kickoff": k,
                "corners": False,
            },
        )
        m["corners"] = m["corners"] or t.get("kind") == "corners"
    return out


def match_events(match: dict, events: list[dict], known: set[str]) -> tuple[str, dict | None]:
    """("matched", event) when exactly one event fits the match; else ("ambiguous" |
    "unmatched", None). `events` are one league's parsed events around the kickoff."""
    mapped = [(team_name(e["home"], known), team_name(e["away"], known), e) for e in events]
    by_club: dict[str, set[str]] = {}
    for mh, ma, e in mapped:
        by_club.setdefault(mh, set()).add(e["home"])
        by_club.setdefault(ma, set()).add(e["away"])
    near = [
        e
        for mh, ma, e in mapped
        if (mh, ma) == (match["home"], match["away"])
        and abs(e["kickoff"] - match["kickoff"]) <= KICKOFF_TOLERANCE
    ]
    clash = any(len(by_club.get(c, ())) > 1 for c in (match["home"], match["away"]))
    if len(near) > 1 or (near and clash):
        return "ambiguous", None
    if not near:
        return "unmatched", None
    return "matched", near[0]


def _days(k: pd.Timestamp) -> list[str]:
    """The kickoff's UTC day, and the day before for a kickoff before 06:00 UTC (ESPN's
    dates follow US Eastern time; European kickoffs fall on the same day there)."""
    days = [f"{k:%Y%m%d}"]
    if k.hour < 6:
        days.insert(0, f"{k - pd.Timedelta(days=1):%Y%m%d}")
    return days


def fetch(
    todo: list[dict],
    known: dict[str, set[str]] | None = None,
    now: pd.Timestamp | None = None,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    budget: Budget | None = None,
) -> tuple[pd.DataFrame, dict]:
    """ESPN results for the trades in `todo` (paper.awaiting_results: kind, league,
    season, home, away, kickoff). Returns (rows, report): one row per final, matched match
    in the shape paper's settlement reads (our names, the trade's season, 90-minute goals,
    corner counts where a corner trade needs them, close_* NaN, result_source "espn"); the
    report counts requests and lists the matches that stayed open and why. Never raises
    on ESPN trouble: those matches simply have no row."""
    now = now or pd.Timestamp.now(tz="UTC")
    budget = budget or Budget()
    known = known or {}
    matches = [
        m
        for m in _matches(todo).values()
        if m["league"] in DOMESTIC
        and m["kickoff"] + AFTER_KICKOFF <= now
        and now - m["kickoff"] <= LOOKBACK
    ]
    report = {
        "matches": len(matches),
        "settled": 0,
        "corners": 0,
        "pending": [],
        "unmatched": [],
        "ambiguous": [],
        "no_corners": [],
        "no_answer": [],
    }
    rows = []
    days_by_league: dict[str, set[str]] = {}
    for m in matches:
        days_by_league.setdefault(m["league"], set()).update(_days(m["kickoff"]))
    events_by_league: dict[str, list[dict]] = {}
    answered: dict[str, set[str]] = {}
    for lg in sorted(days_by_league):
        events: dict[str, dict] = {}
        for day in sorted(days_by_league[lg]):
            body = scoreboard(DOMESTIC[lg], day, raw_dir, get, budget, now)
            if body is not None:
                answered.setdefault(lg, set()).add(day)
                for e in parse(body):
                    events[e["id"]] = e
        events_by_league[lg] = list(events.values())
    for m in sorted(matches, key=lambda x: (x["kickoff"], x["league"], x["home"])):
        label = f"{m['league']} {m['home']} v {m['away']} ({m['kickoff']:%d %b %H:%M})"
        if not set(_days(m["kickoff"])) & answered.get(m["league"], set()):
            report["no_answer"].append(label)
            continue
        clubs = set(known.get(m["league"], set())) | {
            x["home"] for x in matches if x["league"] == m["league"]
        }
        clubs |= {x["away"] for x in matches if x["league"] == m["league"]}
        how, e = match_events(m, events_by_league.get(m["league"], []), clubs)
        if how != "matched":
            report[how].append(label)
            continue
        if not e["final"]:
            report["pending"].append(f"{label}: {e['status'] or 'not final'}")
            continue
        corners = None
        if m["corners"]:
            corners = summary(DOMESTIC[m["league"]], e, raw_dir, get, budget)
            if corners is None:
                report["no_corners"].append(label)
            else:
                report["corners"] += 1
        report["settled"] += 1
        rows.append(
            {
                "league": m["league"],
                "season": m["season"],
                "date": e["kickoff"].tz_convert(None).normalize(),
                "kickoff": e["kickoff"],
                "home": m["home"],
                "away": m["away"],
                "home_goals": int(e["score90"][0]),
                "away_goals": int(e["score90"][1]),
                "home_corners": corners[0] if corners else np.nan,
                "away_corners": corners[1] if corners else np.nan,
                "event_id": e["id"],
                "result_source": "espn",
                "close_home": np.nan,
                "close_draw": np.nan,
                "close_away": np.nan,
            }
        )
    report.update(requests=budget.requests, failures=budget.failures, stopped=budget.why)
    return pd.DataFrame(rows, columns=COLUMNS), report


def describe(report: dict) -> list[str]:
    """Log lines for the paper step."""
    lines = [
        f"ESPN results: {report['settled']} of {report['matches']} matches final "
        f"({report['corners']} with corner counts), {report['requests']} requests "
        f"({report['failures']} failed)"
        + (f"; stopped: {report['stopped']}" if report.get("stopped") else "")
    ]
    for key, text in (
        ("unmatched", "no ESPN match (football-data settles it)"),
        ("ambiguous", "ambiguous ESPN match (left open)"),
        ("no_corners", "final, no ESPN corner counts (football-data settles corners)"),
        ("no_answer", "ESPN didn't answer"),
        ("pending", "not final yet"),
    ):
        if report.get(key):
            lines.append(f"  {text}: " + "; ".join(report[key]))
    return lines
