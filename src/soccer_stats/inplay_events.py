"""Odds-only in-play competitions and their free events lists (lead, 28 Oct; owner-approved).

Research's 75%+ heavy-favourite idea needs in-play prices beyond the nine live competitions,
going forward. The half-time snapshots (inplay_log) cover these too, odds only: no model, no
trades, no cards in the app, the feed's own team names.

* `COMPETITIONS`: the six domestic cups and the Europa and Conference Leagues (heavy_more's
  CUPS), the ten football-data leagues the archive prices in play beyond our eight
  (heavy_favourites.OTHER), and heavy_more's extra leagues (EXTRA) less Romania, which The
  Odds API doesn't list (`NOT_ON_THE_API`): 33 in all.
* We fetch none of their pre-match odds, so their kickoffs come from The Odds API's events
  list, which is free (0 credits: `x-requests-last` 0, checked on every call; a call that
  bills stops the step). `fetch` asks the free sports list (`all=true`: every key, with
  `active`) which keys are in season, warns in its log line about any configured key the
  API doesn't list at all (recorded as status "unknown", `res["unknown"]`), then
  each due competition's events list, at most every EVENTS_EVERY, and appends new
  (event, kickoff) rows to data-log odds_log/<code>_events_<YYYY-MM>.jsonl (league, sport,
  event_id, home, away, kickoff, fetched_at) plus a record per competition in
  odds_log/inplay_events_calls_<YYYY-MM>.jsonl (league, sport, fetched_at, status, events,
  new, cost). close_runs plans in-play runs (never close or settle runs) from these
  kickoffs, and inplay_log snapshots them like any league.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
EVENTS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
EVENTS_EVERY = pd.Timedelta(hours=3)  # one events list per competition at most this often
EVENTS_RE = re.compile(r"^([A-Z0-9]+)_events_\d{4}-\d{2}\.jsonl$")
RECORD_RE = re.compile(r"^inplay_events_calls_\d{4}-\d{2}\.jsonl$")
KEEP_AFTER = pd.Timedelta(hours=3)  # kickoffs further back than this aren't logged

# code: (name, Odds API sport key, ESPN slug for the score and clock)
DOMESTIC_CUPS = {
    "FAC": ("FA Cup", "soccer_fa_cup", "eng.fa"),
    "EFLC": ("League Cup", "soccer_england_efl_cup", "eng.league_cup"),
    "CDR": ("Copa del Rey", "soccer_spain_copa_del_rey", "esp.copa_del_rey"),
    "DFBP": ("DFB-Pokal", "soccer_germany_dfb_pokal", "ger.dfb_pokal"),
    "CI": ("Coppa Italia", "soccer_italy_coppa_italia", "ita.coppa_italia"),
    "CDF": ("Coupe de France", "soccer_france_coupe_de_france", "fra.coupe_de_france"),
}
UEFA_CUPS = {
    "UEL": ("Europa League", "soccer_uefa_europa_league", "uefa.europa"),
    "UECL": ("Conference League", "soccer_uefa_europa_conference_league", "uefa.europa.conf"),
}
FD_EXTRA = {
    "N1": ("Eredivisie", "soccer_netherlands_eredivisie", "ned.1"),
    "P1": ("Primeira Liga", "soccer_portugal_primeira_liga", "por.1"),
    "SC0": ("Scottish Premiership", "soccer_spl", "sco.1"),
    "B1": ("Belgian Pro League", "soccer_belgium_first_div", "bel.1"),
    "T1": ("Turkish Super Lig", "soccer_turkey_super_league", "tur.1"),
    "G1": ("Greek Super League", "soccer_greece_super_league", "gre.1"),
    "D2": ("2. Bundesliga", "soccer_germany_bundesliga2", "ger.2"),
    "I2": ("Serie B", "soccer_italy_serie_b", "ita.2"),
    "SP2": ("Segunda Division", "soccer_spain_segunda_division", "esp.2"),
    "F2": ("Ligue 2", "soccer_france_ligue_two", "fra.2"),
}
# heavy_more's sixteen less Romania: The Odds API has no Romanian league ("Unknown sport" for
# soccer_romania_liga_1, 28 Oct), so there is nothing to snapshot.
NOT_ON_THE_API = {"ROU": ("Romania", "soccer_romania_liga_1", "rou.1")}
MORE_EXTRA = {
    "AUT": ("Austria", "soccer_austria_bundesliga", "aut.1"),
    "SWZ": ("Switzerland", "soccer_switzerland_superleague", "sui.1"),
    "DNK": ("Denmark", "soccer_denmark_superliga", "den.1"),
    "NOR": ("Norway", "soccer_norway_eliteserien", "nor.1"),
    "SWE": ("Sweden", "soccer_sweden_allsvenskan", "swe.1"),
    "POL": ("Poland", "soccer_poland_ekstraklasa", "pol.1"),
    "FIN": ("Finland", "soccer_finland_veikkausliiga", "fin.1"),
    "IRL": ("Ireland", "soccer_league_of_ireland", "irl.1"),
    "RUS": ("Russia", "soccer_russia_premier_league", "rus.1"),
    "JPN": ("Japan", "soccer_japan_j_league", "jpn.1"),
    "CHN": ("China", "soccer_china_superleague", "chn.1"),
    "USA": ("USA", "soccer_usa_mls", "usa.1"),
    "MEX": ("Mexico", "soccer_mexico_ligamx", "mex.1"),
    "BRA": ("Brazil", "soccer_brazil_campeonato", "bra.1"),
    "ARG": ("Argentina", "soccer_argentina_primera_division", "arg.1"),
}
GROUPS = {
    "domestic_cups": DOMESTIC_CUPS,
    "uefa_cups": UEFA_CUPS,
    "fd_extra": FD_EXTRA,
    "more_extra": MORE_EXTRA,
}
COMPETITIONS = {**DOMESTIC_CUPS, **UEFA_CUPS, **FD_EXTRA, **MORE_EXTRA}


def sport_of(code: str) -> str | None:
    c = COMPETITIONS.get(code)
    return c[1] if c else None


def slug_of(code: str) -> str | None:
    c = COMPETITIONS.get(code)
    return c[2] if c else None


def group_of(code: str) -> str | None:
    return next((g for g, d in GROUPS.items() if code in d), None)


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _int(headers, key) -> int | None:
    try:
        return int(float(headers.get(key)))
    except (TypeError, ValueError):
        return None


def _jsonl(paths) -> list[dict]:
    rows = []
    for p in paths:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def event_files(log_dir: Path) -> list[Path]:
    d = Path(log_dir)
    return [p for p in sorted(d.glob("*_events_*.jsonl")) if EVENTS_RE.match(p.name)]


def records(log_dir: Path) -> list[dict]:
    d = Path(log_dir)
    return _jsonl(
        p for p in sorted(d.glob("inplay_events_calls_*.jsonl")) if RECORD_RE.match(p.name)
    )


def kickoffs(log_dir: Path) -> dict[str, list[pd.Timestamp]]:
    """Each odds-only competition's logged kickoffs (its events files)."""
    out: dict[str, set] = {}
    for p in event_files(log_dir):
        code = EVENTS_RE.match(p.name).group(1)
        for r in _jsonl([p]):
            try:
                out.setdefault(r.get("league") or code, set()).add(_ts(r["kickoff"]))
            except (KeyError, TypeError, ValueError):
                continue
    return {k: sorted(v) for k, v in out.items()}


def due(recs: list[dict], now: pd.Timestamp, every: pd.Timedelta = EVENTS_EVERY) -> list[str]:
    """Competitions whose last events list (answered or found out of season) is older than
    `every`, or that have none."""
    last: dict[str, pd.Timestamp] = {}
    for r in recs:
        try:
            t = _ts(r["fetched_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if r.get("league") and (r["league"] not in last or t > last[r["league"]]):
            last[r["league"]] = t
    return [c for c in COMPETITIONS if c not in last or now - last[c] >= every]


def fetch(
    log_dir: Path,
    api_key: str,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    out_dir: Path | None = None,
) -> dict:
    """Refresh the due competitions' events lists (free) and append new kickoffs. Reads the
    record and events files from `log_dir`, appends to `out_dir` (default `log_dir`).
    Returns {due, asked, inactive, events, new, credits, stop, log}."""
    now = _ts(now) if now is not None else pd.Timestamp.now(tz="UTC")
    log_dir = Path(log_dir)
    todo = due(records(log_dir), now)
    res = {"due": len(todo), "asked": 0, "inactive": 0, "events": 0, "new": 0, "credits": 0}
    res["log"] = []
    if not todo:
        res["stop"] = "nothing due"
        return res
    if not api_key:
        res["stop"] = "no ODDS_API_KEY (only the default branch logs events)"
        return res
    stamp = now.isoformat(timespec="seconds")
    try:
        r = get(SPORTS_URL, params={"apiKey": api_key, "all": "true"}, timeout=20)
    except requests.RequestException as exc:  # the message could hold the key
        res["stop"] = f"sports list failed ({type(exc).__name__})"
        return res
    cost = _int(r.headers, "x-requests-last") or 0
    res["credits"] += cost
    if r.status_code != 200 or cost > 0:
        res["stop"] = f"sports list: HTTP {r.status_code}, cost {cost}"
        return res
    body = r.json()
    listed = [x for x in body if isinstance(x, dict)] if isinstance(body, list) else []
    known = {x.get("key") for x in listed}
    active = {x.get("key") for x in listed if x.get("active", True)}
    # A configured key the API doesn't list at all is a registry mistake, not an off-season.
    unknown = sorted(c for c in COMPETITIONS if sport_of(c) not in known) if known else []
    if unknown:
        res["unknown"] = unknown
        res["log"].append(
            "WARNING sport keys The Odds API doesn't list: "
            + ", ".join(f"{c} {sport_of(c)}" for c in unknown)
        )
    seen = {
        (x.get("league"), x.get("event_id"), x.get("kickoff")) for x in _jsonl(event_files(log_dir))
    }
    rows: list[dict] = []
    recs: list[dict] = []
    for code in todo:
        sport = sport_of(code)
        rec = {"league": code, "sport": sport, "fetched_at": stamp}
        if sport not in active:
            status = "unknown" if code in res.get("unknown", ()) else "inactive"
            recs.append({**rec, "status": status, "events": 0, "new": 0, "cost": 0})
            res["inactive"] += status == "inactive"
            continue
        try:
            r = get(
                EVENTS_URL.format(sport=sport),
                params={"apiKey": api_key, "dateFormat": "iso"},
                timeout=20,
            )
        except requests.RequestException as exc:
            res["log"].append(f"{code}: events list failed ({type(exc).__name__})")
            continue
        cost = _int(r.headers, "x-requests-last") or 0
        res["credits"] += cost
        res["asked"] += 1
        events = r.json() if r.status_code == 200 else []
        events = events if isinstance(events, list) else []
        new = 0
        for ev in events:
            try:
                ko = _ts(ev["commence_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if ko < now - KEEP_AFTER:
                continue
            key = (code, ev.get("id"), ko.isoformat(timespec="seconds"))
            if key in seen:
                continue
            seen.add(key)
            new += 1
            rows.append(
                {
                    "league": code,
                    "sport": sport,
                    "event_id": ev.get("id"),
                    "home": ev.get("home_team"),
                    "away": ev.get("away_team"),
                    "kickoff": key[2],
                    "fetched_at": stamp,
                }
            )
        res["events"] += len(events)
        res["new"] += new
        recs.append(
            {**rec, "status": r.status_code, "events": len(events), "new": new, "cost": cost}
        )
        if cost > 0:  # never expected: the events list is free
            res["stop"] = f"{code}: the events list cost {cost} credits; stopped"
            break
    append(Path(out_dir) if out_dir else log_dir, rows, recs, now)
    return res


def append(log_dir: Path, rows: list[dict], recs: list[dict], now: pd.Timestamp) -> None:
    month = now.strftime("%Y-%m")
    if not rows and not recs:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for r in rows:
        m = _ts(r["kickoff"]).strftime("%Y-%m")
        by_file.setdefault(log_dir / f"{r['league']}_events_{m}.jsonl", []).append(
            json.dumps(r, separators=(",", ":"))
        )
    for c in recs:
        by_file.setdefault(log_dir / f"inplay_events_calls_{month}.jsonl", []).append(
            json.dumps(c, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


def summary_line(res: dict) -> str:
    return (
        f"In-play events lists: {res['asked']} asked, {res['inactive']} out of season, "
        f"{res['events']} events, {res['new']} new kickoffs, {res['credits']} credits"
        + (f"; stopped: {res['stop']}" if res.get("stop") else "")
        + ("; " + "; ".join(res["log"]) if res.get("log") else "")
    )
