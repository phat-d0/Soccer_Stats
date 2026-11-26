"""ESPN as a player-data fallback for Understat (lead, 26 Nov 2026; scoping, free, no key).

Understat has answered "We will come back soon" since 2 Nov 2026; goalscorer B, its paper
track and any shots or assists model read Understat's player match files. ESPN's match
summary (`/summary?event=`, already used for lineups and corners) lists both rosters.
This module asks what it holds per player and how far back.

`fields` (CLI `espn-players --mode fields`): per league (E0, SP1, D1, I1, F1) and season
2021/22-2026/27, `PER_SEASON` matches spread through football-data's season, each found
on ESPN's scoreboard for its day (both names, kickoff within 3 h, as espn_results does) and
its summary fetched once. Per summary: roster players, the starter flag, substitution
flags and times (`subbedIn`/`subbedOut`, the key events' clock), each player's stat names
and whether goals, assists, shots, shots on target and cards are there, and whether the
player stats add up to the match's goals. Nothing is cached or saved beyond the run's
`--out` folder (the artifact); requests are paced (`PAUSE`) and stop on `RUN_SECONDS`.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.espn_news import BASE, SLUGS

LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
SEASONS = (2021, 2022, 2023, 2024, 2025, 2026)
PER_SEASON = 6
PAUSE = 0.4
TIMEOUT = 20
RUN_SECONDS = 1500
KICKOFF_TOLERANCE = pd.Timedelta(hours=3)
# The per-player stats a goals/assists/shots model needs, by ESPN's usual names.
WANT = {
    "goals": ("totalGoals", "goals"),
    "assists": ("goalAssists", "assists"),
    "shots": ("totalShots", "shots"),
    "on_target": ("shotsOnTarget",),
    "yellow": ("yellowCards",),
    "red": ("redCards",),
    "sub_in": ("subIns",),
}


def sample(kickoffs: pd.DataFrame, n: int = PER_SEASON) -> pd.DataFrame:
    """n matches spread evenly through a season's played matches (kickoff order)."""
    k = kickoffs.sort_values("kickoff").reset_index(drop=True)
    if len(k) <= n:
        return k
    idx = sorted({int(round(i * (len(k) - 1) / (n - 1))) for i in range(n)})
    return k.iloc[idx].reset_index(drop=True)


def _get(get: Callable, url: str, params: dict, stats: dict) -> dict | None:
    stats["requests"] = stats.get("requests", 0) + 1
    try:
        r = get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        stats["errors"] = stats.get("errors", 0) + 1
        stats["last_error"] = type(exc).__name__
        return None
    time.sleep(PAUSE)
    if r.status_code != 200:
        stats["errors"] = stats.get("errors", 0) + 1
        stats["last_error"] = f"HTTP {r.status_code}"
        return None
    try:
        return r.json()
    except ValueError:
        return None


def find_event(board: dict | None, home: str, away: str, kickoff: pd.Timestamp) -> dict | None:
    """The scoreboard event for this fixture: both names (football-data spelling via
    espn_news.team_name) and kickoff within 3 h."""
    from soccer_stats.espn_news import team_name

    for e in (board or {}).get("events", []) or []:
        comp = ((e.get("competitions") or [{}])[0]) or {}
        teams = {
            c.get("homeAway"): (c.get("team") or {}).get("displayName")
            for c in comp.get("competitors", [])
        }
        known = {home, away}
        h, a = team_name(teams.get("home") or "", known), team_name(teams.get("away") or "", known)
        if (h, a) != (home, away):
            continue
        if abs(pd.Timestamp(e.get("date")).tz_convert("UTC") - kickoff) <= KICKOFF_TOLERANCE:
            return e
    return None


def _stats(p: dict) -> dict[str, float]:
    out = {}
    for s in p.get("stats") or []:
        if isinstance(s, dict) and s.get("name") is not None:
            try:
                out[s["name"]] = float(s.get("value"))
            except (TypeError, ValueError):
                out[s["name"]] = None
    return out


def read_summary(body: dict | None) -> dict:
    """What one summary holds per player (see the module docstring)."""
    body = body if isinstance(body, dict) else {}
    players, names = [], Counter()
    sides = body.get("rosters") or []
    for side in sides:
        for p in side.get("roster") or []:
            st = _stats(p)
            names.update(st)
            players.append(
                {
                    "name": (p.get("athlete") or {}).get("displayName"),
                    "athlete_id": (p.get("athlete") or {}).get("id"),
                    "starter": p.get("starter"),
                    "subbed_in": p.get("subbedIn"),
                    "subbed_out": p.get("subbedOut"),
                    "position": (p.get("position") or {}).get("abbreviation"),
                    "stats": st,
                }
            )
    subs = [
        e
        for e in body.get("keyEvents") or []
        if "substitution" in str((e.get("type") or {}).get("text", "")).lower()
    ]
    have = {k: any(n in names for n in v) for k, v in WANT.items()}
    goal_col = next((n for n in WANT["goals"] if n in names), None)
    player_goals = sum((p["stats"].get(goal_col) or 0) for p in players) if goal_col else None
    comp = (((body.get("header") or {}).get("competitions") or [{}])[0]) or {}
    score = sum(
        float(c.get("score") or 0) for c in comp.get("competitors", []) if isinstance(c, dict)
    )
    return {
        "teams": len(sides),
        "players": len(players),
        "starters": sum(bool(p["starter"]) for p in players),
        "with_stats": sum(bool(p["stats"]) for p in players),
        "starter_flag": any(p["starter"] is not None for p in players),
        "sub_flags": any(p["subbed_in"] is not None for p in players),
        "sub_events": len(subs),
        "sub_clock_example": ((subs[0].get("clock") or {}).get("displayValue") if subs else None),
        "stat_names": sorted(names),
        "have": have,
        "player_goals": player_goals,
        "score_goals": score,
        "example": players[0] if players else None,
    }


def fields(
    leagues=LEAGUES,
    seasons=SEASONS,
    per_season: int = PER_SEASON,
    get: Callable = requests.get,
    kickoffs: Callable | None = None,
    out_dir: Path | None = None,
    seconds: float = RUN_SECONDS,
) -> dict:
    """The fields check (module docstring). `kickoffs(league, season)` gives football-data's
    played matches (home, away, kickoff); default `data.season_fixtures`."""
    kickoffs = kickoffs or fd_matches
    stats: dict = {}
    end = time.monotonic() + seconds
    res: dict = {"leagues": {}, "stats": stats}
    for lg in leagues:
        slug = SLUGS[lg]
        res["leagues"][lg] = {}
        for season in seasons:
            if time.monotonic() > end:
                res["stopped"] = f"time budget {seconds:g} s used"
                break
            try:
                fx = sample(kickoffs(lg, season), per_season)
            except Exception as exc:  # noqa: BLE001  (a missing season is reported, not fatal)
                res["leagues"][lg][season] = {"error": type(exc).__name__}
                continue
            got = []
            for m in fx.to_dict("records"):
                k = pd.Timestamp(m["kickoff"])
                board = _get(
                    get, BASE.format(slug=slug) + "/scoreboard", {"dates": f"{k:%Y%m%d}"}, stats
                )
                e = find_event(board, m["home"], m["away"], k)
                if e is None:
                    got.append({"match": f"{m['home']} v {m['away']}", "found": False})
                    continue
                body = _get(get, BASE.format(slug=slug) + "/summary", {"event": e["id"]}, stats)
                info = read_summary(body)
                info.update(match=f"{m['home']} v {m['away']}", found=True, event_id=e["id"])
                if out_dir is not None and body is not None:
                    path = Path(out_dir) / lg / f"{season}_{e['id']}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(body))
                got.append(info)
            res["leagues"][lg][season] = {"matches": got}
    return res


def fd_matches(league: str, season: int) -> pd.DataFrame:
    """football-data's played matches for a season: home, away, kickoff (UTC)."""
    from soccer_stats.data import load_matches, season_kickoffs

    m = load_matches([league], [season]).reset_index(drop=True)
    k = season_kickoffs(league, season)
    if len(k) == len(m):
        m = m.assign(kickoff=k.to_numpy())
    else:  # no times: 15:00 UK on the match date
        m = m.assign(
            kickoff=pd.to_datetime(m["date"]).dt.tz_localize("UTC") + pd.Timedelta(hours=15)
        )
    return m[["home", "away", "kickoff"]]


def summary_lines(res: dict) -> list[str]:
    lines = [f"ESPN players fields: {res['stats']}"]
    for lg, by in res["leagues"].items():
        for season, r in by.items():
            if "error" in r:
                lines.append(f"{lg} {season}: football-data error {r['error']}")
                continue
            ms = r["matches"]
            found = [m for m in ms if m.get("found")]
            if not found:
                lines.append(f"{lg} {season}: 0 of {len(ms)} found on ESPN")
                continue
            have = Counter()
            for m in found:
                have.update(k for k, v in m["have"].items() if v)
            adds = sum(
                m["player_goals"] == m["score_goals"]
                for m in found
                if m["player_goals"] is not None
            )
            names = sorted(set().union(*(m["stat_names"] for m in found)))
            n_pl = sum(m["players"] for m in found)
            n_st = sum(m["with_stats"] for m in found)
            starter = sum(m["starter_flag"] for m in found)
            subf = sum(m["sub_flags"] for m in found)
            subs = sum(m["sub_events"] for m in found)
            lines.append(
                f"{lg} {season}: {len(found)}/{len(ms)} found; players {n_pl // len(found)} "
                f"a match, with stats {n_st}/{n_pl}; starter flag {starter}, sub flags {subf}, "
                f"sub events {subs} (e.g. {found[0]['sub_clock_example']}); "
                f"has {dict(have)}; player goals add up in {adds}/{len(found)}"
            )
            lines.append(f"{lg} {season} stat names: {', '.join(names)}")
    return lines
