"""Priced goalscorer test, step 0: who prices anytime goalscorer in the five leagues.

Lead-approved 1 Nov 2026, cap 250 credits, from the default branch (docs/player_props.md
§14 and the scope sent with it). It answers, before the test is pre-registered (§15):

- **Live:** per league, the free events list, then upcoming matches in kickoff order, all
  five regions, market `player_goal_scorer_anytime` (1 credit per region that answers, so
  at most 5 a call; an empty answer costs 0) until one match is priced or `LIVE_TRIES`.
- **History:** per league and season in `SEASONS` (2025/26, the development season, and
  2026/27, the forward window), one historical events list on a Saturday (1 credit) and the
  first match kicking off that day at two snapshots: the planned entry (kickoff − 50 min,
  after lineups) and close (kickoff − 2 min). One `bookmakers=` list of up to ten books
  (one region, 10 credits a snapshot): the books found live in that league, topped up with
  the books seen pricing the Premier League (§6).
- **Names:** every priced name is matched to the two clubs' Understat squads (the
  league-season player lists for the snapshot's season and the one before).

Nothing here reads a result: no goal, start or model chance is joined. Every body is cached
under `data/raw/player_odds_history/goalscorer_probe/` (the paid `player-odds-history-`
cache), so a rerun buys nothing it already holds. Every call is skipped if it could pass the
cap or leave fewer than 3,000 credits on the shared key; the cost counted is each answer's
`x-requests-last`. The key is never printed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.edge.props import _get
from soccer_stats.odds_feed import SPORTS, _team
from soccer_stats.player_data import match_in_fixture
from soccer_stats.player_goal_odds import MARKET, REGIONS, CappedBudget, book_summary, prices
from soccer_stats.player_odds import BASE

LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
SEASONS = (2025, 2026)
# A Saturday in each season with all five leagues playing (first Saturday of November, or
# the last one before today for the season in progress).
PROBE_DAY = {2025: "2025-11-01", 2026: "2026-10-31"}
SNAPSHOTS = {"entry": pd.Timedelta(minutes=50), "close": pd.Timedelta(minutes=2)}
LIVE_TRIES = 6
MAX_BOOKS = 10
HIST_COST = 10  # one market, one bookmakers= list
# Seen pricing Premier League anytime goalscorer (docs/player_props.md §6), most players first.
EPL_BOOKS = (
    "betrivers",
    "bovada",
    "espnbet",
    "williamhill_us",
    "betmgm",
    "fanatics",
    "mybookieag",
    "betparx",
    "unibet",
    "ballybet",
)
PROBE_DIR = "goalscorer_probe"


def probe_dir(raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / "player_odds_history" / PROBE_DIR


def books_for(found: list[str], extra: tuple[str, ...] = EPL_BOOKS) -> list[str]:
    """The historical bookmakers= list: books found live first, then the EPL books."""
    return (list(dict.fromkeys(found)) + [b for b in extra if b not in found])[:MAX_BOOKS]


def first_match(events: list[dict], day: str) -> dict | None:
    """The first event kicking off on `day` (UTC) at or after 11:00."""
    lo = pd.Timestamp(f"{day}T11:00:00Z")
    hi = lo.normalize() + pd.Timedelta(days=1)
    on = [e for e in events if lo <= pd.Timestamp(e["commence_time"]) < hi]
    return min(on, key=lambda e: (e["commence_time"], e["id"])) if on else None


def snapshot_time(kickoff: str, kind: str) -> str:
    t = pd.Timestamp(kickoff).tz_convert("UTC") - SNAPSHOTS[kind]
    return t.floor("min").strftime("%Y-%m-%dT%H:%M:%SZ")


def rosters_from(season_files: list[dict]) -> dict[str, dict[str, str]]:
    """team -> {Understat player id: name} from Understat league-season files (`players`);
    a player listed at several clubs ("A,B") goes into each."""
    from soccer_stats.xg import TEAM_NAMES as XG_TEAM_NAMES

    out: dict[str, dict[str, str]] = {}
    for data in season_files:
        players = data.get("players", []) if isinstance(data, dict) else []
        for p in players:
            for team in (p.get("team_title") or "").split(","):
                team = XG_TEAM_NAMES.get(team.strip(), team.strip())
                if team and p.get("player_name"):
                    out.setdefault(team, {})[str(p.get("id"))] = p["player_name"]
    return out


def name_match(p: pd.DataFrame, home: str, away: str, rosters: dict) -> dict:
    """How many priced names land on one of the two clubs' Understat players."""
    names = list(dict.fromkeys(p["player"])) if not p.empty else []
    known = set(rosters)
    h, a = _team(home or "", known), _team(away or "", known)
    clubs = {t: rosters[t] for t in (h, a) if t in rosters}
    found, missed = match_in_fixture(names, clubs) if clubs else ({}, names)
    return {
        "names": len(names),
        "matched": len(found),
        "share": round(len(found) / len(names), 3) if names else None,
        "clubs_found": sorted(clubs),
        "unmatched": missed[:12],
    }


def _cached(path: Path, fetch: Callable[[], dict | None], log: list[str], label: str):
    if path.exists():
        log.append(f"{label}: cached, 0 credits")
        return json.loads(path.read_text())
    body = fetch()
    if body is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
    return body


def run(
    cap: int,
    leagues: tuple[str, ...] = LEAGUES,
    seasons: tuple[int, ...] = SEASONS,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    raw_dir: Path = RAW_DIR,
    rosters: Callable[[str, int], dict] | None = None,
) -> dict:
    """The probe, never past `cap` credits. `rosters(league, season)` gives the Understat
    squads for name matching (default: the cached league-season files)."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    rosters = rosters or understat_rosters
    budget = CappedBudget(cap)
    out = {"cap": cap, "leagues": {}, "log": budget.log}
    if not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
        return {**out, "spent": 0, "left": None}
    base = probe_dir(raw_dir)
    stamp = now.strftime("%Y%m%dT%H%M")
    for lg in leagues:
        sport = SPORTS[lg]
        res = {"live": None, "history": {}}
        try:
            current = rosters(lg, max(seasons))
        except Exception as exc:  # noqa: BLE001  (names are a side check)
            current, res["roster_error"] = {}, type(exc).__name__
        # Live: all five regions, until one match is priced.
        evs = _get(
            get, f"{BASE}/sports/{sport}/events", {"apiKey": api_key}, budget, 0, f"{lg} events"
        )
        upcoming = sorted(
            [e for e in evs or [] if pd.Timestamp(e["commence_time"]) > now],
            key=lambda e: e["commence_time"],
        )
        tried = []
        for e in upcoming[:LIVE_TRIES]:
            label = f"{lg} live {e['home_team']} v {e['away_team']} ({e['commence_time']})"
            body = _get(
                get,
                f"{BASE}/sports/{sport}/events/{e['id']}/odds",
                {
                    "apiKey": api_key,
                    "regions": ",".join(REGIONS),
                    "markets": MARKET,
                    "oddsFormat": "decimal",
                },
                budget,
                len(REGIONS),
                label,
            )
            if body is None:
                continue
            path = base / lg / f"live_{stamp}_{e['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body))
            p = prices(body)
            tried.append(f"{e['home_team']} v {e['away_team']}")
            if not p.empty:
                res["live"] = {
                    "event": f"{e['home_team']} v {e['away_team']}",
                    "kickoff": e["commence_time"],
                    "books": book_summary(p),
                    "names": name_match(p, e["home_team"], e["away_team"], current),
                }
                break
        res["live_tried"] = tried
        found = [b["book"] for b in (res["live"] or {}).get("books", [])]
        books = books_for(found)
        res["books_requested"] = books
        # History: one Saturday match per season, entry and close snapshots.
        for season in seasons:
            day = PROBE_DAY[season]
            ev_path = base / lg / f"events_{day}.json"
            ev_body = _cached(
                ev_path,
                lambda sport=sport, day=day, lg=lg: _get(
                    get,
                    f"{BASE}/historical/sports/{sport}/events",
                    {"apiKey": api_key, "date": f"{day}T10:00:00Z"},
                    budget,
                    1,
                    f"{lg} events {day}",
                ),
                budget.log,
                f"{lg} events {day}",
            )
            data = (ev_body or {}).get("data", ev_body) if isinstance(ev_body, dict) else ev_body
            ev = first_match(data or [], day)
            row = {"day": day, "event": None}
            if ev is None:
                res["history"][str(season)] = row
                continue
            row.update(event=f"{ev['home_team']} v {ev['away_team']}", kickoff=ev["commence_time"])
            try:
                squads = rosters(lg, season)
            except Exception as exc:  # noqa: BLE001
                squads, row["roster_error"] = {}, type(exc).__name__
            for kind in SNAPSHOTS:
                at = snapshot_time(ev["commence_time"], kind)
                label = f"{lg} {season} {kind} {row['event']} at {at}"
                body = _cached(
                    base / lg / f"{ev['id']}_{kind}.json",
                    lambda sport=sport, ev=ev, at=at, label=label, books=books: _get(
                        get,
                        f"{BASE}/historical/sports/{sport}/events/{ev['id']}/odds",
                        {
                            "apiKey": api_key,
                            "bookmakers": ",".join(books),
                            "markets": MARKET,
                            "oddsFormat": "decimal",
                            "date": at,
                        },
                        budget,
                        HIST_COST,
                        label,
                    ),
                    budget.log,
                    label,
                )
                if body is None:
                    row[kind] = None
                    continue
                p = prices(body)
                row[kind] = {
                    "at": at,
                    "books": book_summary(p),
                    "names": name_match(p, ev["home_team"], ev["away_team"], squads),
                }
            res["history"][str(season)] = row
        out["leagues"][lg] = res
    return {**out, "spent": budget.spent, "left": budget.left}


def understat_rosters(league: str, season: int, raw_dir: Path = RAW_DIR) -> dict:
    """Squads from Understat's league-season files for `season` and the season before."""
    from soccer_stats.xg import fetch_season

    files = []
    for y in (season - 1, season):
        try:
            files.append(json.loads(fetch_season(league, y, raw_dir).read_text()))
        except Exception:  # noqa: BLE001  (a missing season leaves its players out)
            continue
    return rosters_from(files)


def summary_lines(res: dict) -> list[str]:
    """Plain lines for the job log: per league, live and per season, the books and names."""

    def books(snap: dict | None) -> str:
        if not snap:
            return "no answer"
        b = snap["books"]
        if not b:
            return "no book priced it"
        two = sum(x["two_sided"] for x in b)
        n = snap["names"]
        return (
            ", ".join(f"{x['book']} {x['players']}" for x in b)
            + f" | two-sided lines {two} | names matched {n['matched']}/{n['names']}"
        )

    lines = [f"Goalscorer probe: spent {res.get('spent')} of {res.get('cap')} credits"]
    for lg, r in res.get("leagues", {}).items():
        live = r.get("live")
        tried = len(r.get("live_tried", []))
        lines.append(
            f"{lg} live: "
            + (f"{live['event']} ({live['kickoff']}): {books(live)}" if live else "none priced")
            + f" [{tried} tried]"
        )
        for season, h in r.get("history", {}).items():
            if not h.get("event"):
                lines.append(f"{lg} {season}: no match found on {h.get('day')}")
                continue
            for kind in SNAPSHOTS:
                lines.append(f"{lg} {season} {kind} ({h['event']}): {books(h.get(kind))}")
    return lines
