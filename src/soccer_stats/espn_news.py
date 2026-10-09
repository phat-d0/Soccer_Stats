"""Team news from ESPN's free soccer endpoints: injuries and confirmed lineups.

Owner-approved on 9 Oct for all six leagues. Data and display only: nothing here changes
how bets are judged, and it costs no Odds API credits. ESPN's site API is unofficial, so
every call is defensive (short timeouts, a small retry, any failure = no news this run).

Per publish run (`add`):

* **Scoreboard** per league (one call, cached SCOREBOARD_HOURS) lists ESPN's events with
  their ids and teams; each is matched to a fixture card by team names (odds_feed._team
  plus ESPN_NAMES) and kickoff.
* **Summary** per matched event, only inside a window: kickoff within INJURY_HOURS
  (injuries, refreshed every SUMMARY_HOURS), and within LINEUP_MINUTES every run until a
  confirmed XI appears (11 starters on both sides); after that the body is final.
* Each card gains `team_news` (`source`, `fetched_at`, `injuries`, `lineup`; see
  `card_news`). The Premier League's FPL news (`news`) is unchanged.

`log` appends the cards' team news to data-log (team_news/<code>_<YYYY-MM>.jsonl), one
row per match and team whenever it changed, and marks the row where a confirmed XI first
appeared (`first_confirmed`): the lineup announcement time the 16 Nov check needs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.odds_feed import _team

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}"
SLUGS = {"E0": "eng.1", "SP1": "esp.1", "D1": "ger.1", "I1": "ita.1", "F1": "fra.1", "E1": "eng.2"}
# ESPN spellings the shared map (odds_feed.TEAM_NAMES) and its fuzzy match don't cover.
ESPN_NAMES: dict[str, str] = {
    "AFC Bournemouth": "Bournemouth",
    "Brighton & Hove Albion": "Brighton",
    "Wolverhampton Wanderers": "Wolves",
    "Sheffield Wednesday": "Sheffield Weds",
    "Queens Park Rangers": "QPR",
    "West Bromwich Albion": "West Brom",
    "Bayern Munich": "Bayern Munich",
    "Borussia Mönchengladbach": "M'gladbach",
    "Internazionale": "Inter",
    "AC Milan": "Milan",
    "Paris Saint-Germain": "Paris SG",
    "Atlético Madrid": "Ath Madrid",
    "Athletic Club": "Ath Bilbao",
}
INJURY_HOURS = 36
LINEUP_MINUTES = 90
SUMMARY_HOURS = 3.0  # refresh interval for a match's summary before the lineup window
SCOREBOARD_HOURS = 6.0
KICKOFF_TOLERANCE = pd.Timedelta(hours=3)
TIMEOUT = 15
RETRIES = 2
PAUSE = 0.2
MAX_SUMMARIES = 60  # per run, across leagues: keeps a busy weekend's request count modest
CACHE = "espn"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _iso(t) -> str:
    return _ts(t).isoformat(timespec="seconds")


def _get_json(url: str, params: dict, get: Callable, stats: dict) -> dict | None:
    """GET with a short timeout and a small retry; None on any failure."""
    for attempt in range(RETRIES + 1):
        stats["requests"] = stats.get("requests", 0) + 1
        try:
            r = get(url, params=params, timeout=TIMEOUT)
            stats["last_status"] = r.status_code
            if r.status_code == 200:
                return r.json()
            if r.status_code < 500:
                return None  # 4xx: retrying won't help
        except (requests.RequestException, ValueError) as exc:
            stats["last_status"] = type(exc).__name__
        if attempt < RETRIES:
            time.sleep(PAUSE * (attempt + 1))
    stats["failures"] = stats.get("failures", 0) + 1
    return None


def _dicts(x) -> list[dict]:
    """The dicts in a list from ESPN's JSON; anything else (a renamed or odd field) = []."""
    return [v for v in x if isinstance(v, dict)] if isinstance(x, list) else []


def _get(d, *keys):
    """A nested value through dicts only, else None."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def team_name(name: str, known: set[str]) -> str:
    return _team(ESPN_NAMES.get(name, name), known)


# ---------- parsing ----------


def parse_scoreboard(body: dict | None, known: set[str]) -> list[dict]:
    """{id, kickoff, home, away, espn_home, espn_away} per event, names mapped."""
    out = []
    for e in _dicts(_get(body, "events")):
        comp = (_dicts(e.get("competitions")) or [{}])[0]
        sides = {c.get("homeAway"): c for c in _dicts(comp.get("competitors"))}
        h = _get(sides.get("home"), "team", "displayName")
        a = _get(sides.get("away"), "team", "displayName")
        if not (e.get("id") and e.get("date") and h and a):
            continue
        out.append(
            {
                "id": str(e["id"]),
                "kickoff": _ts(e["date"]),
                "home": team_name(h, known),
                "away": team_name(a, known),
                "espn_home": h,
                "espn_away": a,
            }
        )
    return out


def _names(players) -> list[str]:
    return [n for p in players if isinstance(n := _get(p, "athlete", "displayName"), str) and n]


def parse_summary(body: dict | None) -> dict:
    """{lineups: {espn team: {starters, subs}}, injuries: {espn team: [...]}, updated}."""
    body = body if isinstance(body, dict) else {}
    lineups = {}
    for side in _dicts(body.get("rosters")):
        team = _get(side, "team", "displayName")
        roster = _dicts(side.get("roster"))
        if not team or not roster:
            continue
        lineups[team] = {
            "starters": _names(p for p in roster if p.get("starter")),
            "subs": _names(p for p in roster if not p.get("starter")),
        }
    injuries = {}
    for block in _dicts(body.get("injuries")):
        team = _get(block, "team", "displayName")
        if not team:
            continue
        rows = []
        for i in _dicts(block.get("injuries")):
            name = _get(i, "athlete", "displayName")
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "status": i.get("status") or _get(i, "type", "description"),
                    "detail": _get(i, "details", "type") or _get(i, "details", "detail"),
                }
            )
        injuries[team] = rows
    updated = _get(body, "meta", "lastUpdatedAt") or _get(body, "header", "lastUpdated")
    return {"lineups": lineups, "injuries": injuries, "updated": updated}


def confirmed(lineups: dict) -> bool:
    """Both teams list a full starting XI."""
    return len(lineups) == 2 and all(len(v["starters"]) >= 11 for v in lineups.values())


def card_news(event: dict, parsed: dict, fetched_at: str, first_confirmed_at: str | None) -> dict:
    """The compact `team_news` block for a fixture card."""
    by_side = {"home": event["espn_home"], "away": event["espn_away"]}
    lineup_ok = confirmed(parsed["lineups"])
    lineup = {"confirmed": lineup_ok, "fetched_at": fetched_at}
    if parsed["lineups"]:
        for side, espn in by_side.items():
            got = parsed["lineups"].get(espn) or {"starters": [], "subs": []}
            lineup[side] = {"starters": got["starters"], "subs": got["subs"]}
    if first_confirmed_at:
        lineup["first_confirmed_at"] = first_confirmed_at
    return {
        "source": "ESPN",
        "event_id": event["id"],
        "fetched_at": fetched_at,
        "updated": parsed.get("updated"),
        "injuries": {side: parsed["injuries"].get(espn, []) for side, espn in by_side.items()},
        "lineup": lineup,
    }


# ---------- cache ----------


def _cache_dir(raw_dir: Path) -> Path:
    return Path(raw_dir) / CACHE


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _days(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    """YYYYMMDD for each UTC day from start to end (ESPN takes one date per call; a
    range like 20261009-20261011 returns 400)."""
    d0, d1 = start.normalize(), end.normalize()
    return [f"{d:%Y%m%d}" for d in pd.date_range(d0, d1, freq="D")]


def scoreboard(league: str, raw_dir: Path, now: pd.Timestamp, get, stats) -> dict | None:
    """The league's scoreboard for every day from now to INJURY_HOURS ahead (one call per
    day, cached SCOREBOARD_HOURS), merged into one {events: [...]}."""
    path = _cache_dir(raw_dir) / f"{league}_scoreboard.json"
    cached = _read_cache(path)
    if cached and now - _ts(cached["fetched_at"]) < pd.Timedelta(hours=SCOREBOARD_HOURS):
        return cached["body"]
    events, ok = {}, False
    for day in _days(now, now + pd.Timedelta(hours=INJURY_HOURS)):
        url = BASE.format(slug=SLUGS[league]) + "/scoreboard"
        body = _get_json(url, {"dates": day}, get, stats)
        if body is not None:
            ok = True
            for e in _dicts(body.get("events")):
                events.setdefault(str(e.get("id")), e)  # one entry per event
    if not ok:
        return cached["body"] if cached else None
    body = {"events": list(events.values())}
    _write_cache(path, {"fetched_at": _iso(now), "body": body})
    return body


def summary(league, event, raw_dir, now, get, stats) -> dict | None:
    """The event's summary when due (see module docstring), else the cached one.
    Returns {fetched_at, body, first_confirmed_at} or None."""
    path = _cache_dir(raw_dir) / f"{league}_{event['id']}.json"
    cached = _read_cache(path)
    left = event["kickoff"] - now
    in_lineup_window = left <= pd.Timedelta(minutes=LINEUP_MINUTES)
    if cached:
        age = now - _ts(cached["fetched_at"])
        if cached.get("first_confirmed_at") or left <= pd.Timedelta(0):
            return cached  # the XI is final (or the match has started): no more calls
        if not in_lineup_window and age < pd.Timedelta(hours=SUMMARY_HOURS):
            return cached
    if stats.get("summaries", 0) >= MAX_SUMMARIES:
        return cached
    stats["summaries"] = stats.get("summaries", 0) + 1
    body = _get_json(
        BASE.format(slug=SLUGS[league]) + "/summary", {"event": event["id"]}, get, stats
    )
    time.sleep(PAUSE)
    if body is None:
        return cached
    first = (cached or {}).get("first_confirmed_at")
    if not first and confirmed(parse_summary(body)["lineups"]):
        first = _iso(now)
    out = {"fetched_at": _iso(now), "body": body, "first_confirmed_at": first}
    _write_cache(path, out)
    return out


def match_event(card: dict, events: list[dict]) -> dict | None:
    k = _ts(card["kickoff"])
    near = [e for e in events if abs(e["kickoff"] - k) <= KICKOFF_TOLERANCE]
    both = [e for e in near if e["home"] == card["home"] and e["away"] == card["away"]]
    return both[0] if len(both) == 1 else None


# ---------- one publish run ----------


def add(
    cards: list[dict],
    raw_dir: Path = RAW_DIR,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
) -> dict:
    """Attach `team_news` to the cards due it. Never raises; returns a summary."""
    now = _ts(now or pd.Timestamp.now(tz="UTC"))
    stats: dict = {"requests": 0, "failures": 0, "summaries": 0}
    out = {"matches": 0, "confirmed": 0, "unmatched": [], "errors": []}
    by_league: dict[str, list[dict]] = {}
    for c in cards:
        if c.get("league") in SLUGS and c.get("kickoff"):
            k = _ts(c["kickoff"])
            if now < k <= now + pd.Timedelta(hours=INJURY_HOURS):
                by_league.setdefault(c["league"], []).append(c)
    for league, lcards in by_league.items():
        try:
            known = {c["home"] for c in lcards} | {c["away"] for c in lcards}
            events = parse_scoreboard(scoreboard(league, raw_dir, now, get, stats), known)
            in_window = [
                e for e in events if now < e["kickoff"] <= now + pd.Timedelta(hours=INJURY_HOURS)
            ]
            used = set()
            for card in lcards:
                ev = match_event(card, events)
                if ev is None:
                    continue
                used.add(ev["id"])
                s = summary(league, ev, raw_dir, now, get, stats)
                if s is None:
                    continue
                news = card_news(
                    ev, parse_summary(s["body"]), s["fetched_at"], s.get("first_confirmed_at")
                )
                card["team_news"] = news
                out["matches"] += 1
                out["confirmed"] += bool(news["lineup"]["confirmed"])
            out["unmatched"] += [
                f"{league}: {e['espn_home']} v {e['espn_away']}"
                for e in in_window
                if e["id"] not in used
            ]
        except Exception as exc:  # noqa: BLE001  ESPN is unofficial: never stop the build
            out["errors"].append(f"{league}: {type(exc).__name__}")
    out.update(requests=stats["requests"], failures=stats["failures"])
    return out


def summary_line(s: dict) -> str:
    return (
        f"Team news (ESPN): {s['matches']} matches, {s['confirmed']} confirmed lineups, "
        f"{s['requests']} requests ({s['failures']} failed)"
        + (f"; unmatched: {'; '.join(s['unmatched'])}" if s["unmatched"] else "")
        + (f"; errors: {', '.join(s['errors'])}" if s["errors"] else "")
    )


# ---------- data-log ----------


def rows_from_data(data: dict, now: pd.Timestamp) -> list[dict]:
    """One row per card team with ESPN team news."""
    rows = []
    for c in data.get("fixtures") or []:
        tn = c.get("team_news")
        if not tn:
            continue
        lu = tn.get("lineup") or {}
        for side in ("home", "away"):
            got = lu.get(side) or {}
            rows.append(
                {
                    "league": c.get("league"),
                    "home": c["home"],
                    "away": c["away"],
                    "kickoff": _iso(c["kickoff"]),
                    "event_id": tn.get("event_id"),
                    "team": c[side],
                    "side": side,
                    "confirmed": bool(lu.get("confirmed")),
                    "starters": got.get("starters", []),
                    "subs": got.get("subs", []),
                    "injuries": (tn.get("injuries") or {}).get(side, []),
                    "espn_updated": tn.get("updated"),
                    "fetched_at": tn.get("fetched_at"),
                    "first_confirmed_at": lu.get("first_confirmed_at"),
                    "logged_at": _iso(now),
                }
            )
    return rows


def _content(r: dict) -> tuple:
    return (
        r.get("event_id"),
        r.get("team"),
        r.get("confirmed"),
        tuple(r.get("starters") or ()),
        tuple(r.get("subs") or ()),
        json.dumps(r.get("injuries") or [], sort_keys=True),
    )


def log(root: Path, rows: list[dict]) -> int:
    """Append rows whose content changed since the last logged row for that match and
    team; mark the first confirmed XI per match and team. Returns rows written."""
    d = Path(root) / "team_news"
    existing: dict[Path, list[dict]] = {}
    written = 0
    for r in rows:
        month = _ts(r["fetched_at"] or r["logged_at"]).strftime("%Y-%m")
        path = d / f"{r['league']}_{month}.jsonl"
        if path not in existing:
            existing[path] = []
            for p in sorted(d.glob(f"{r['league']}_*.jsonl")):
                for line in p.read_text().splitlines():
                    if line.strip():
                        existing[path].append(json.loads(line))
        same = [
            x
            for x in existing[path]
            if (x.get("event_id"), x.get("team")) == (r["event_id"], r["team"])
        ]
        if same and _content(same[-1]) == _content(r):
            continue
        r = dict(r)
        r["first_confirmed"] = bool(r["confirmed"]) and not any(x.get("confirmed") for x in same)
        d.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        existing[path].append(r)
        written += 1
    return written


# ---------- probe (no key; run in Actions) ----------


def probe(get: Callable = requests.get, back: int = 4, ahead: int = 2) -> dict:
    """Per league: which days' scoreboards answer, ESPN team names, and the shape of one
    finished and one upcoming match's summary (lineups, injuries, update time)."""
    now = pd.Timestamp.now(tz="UTC")
    out = {}
    for league, slug in SLUGS.items():
        stats: dict = {}
        events, statuses = [], {}
        for day in _days(now - pd.Timedelta(days=back), now + pd.Timedelta(days=ahead)):
            body = _get_json(BASE.format(slug=slug) + "/scoreboard", {"dates": day}, get, stats)
            statuses[day] = stats.get("last_status")
            events += parse_scoreboard(body, set())
        info = {"slug": slug, "statuses": statuses, "events": len(events)}
        info["teams"] = sorted({e["espn_home"] for e in events} | {e["espn_away"] for e in events})
        past = [e for e in events if e["kickoff"] < now - pd.Timedelta(hours=3)]
        future = [e for e in events if e["kickoff"] > now]
        for label, pick in (("finished", past[-1:]), ("upcoming", future[:1])):
            if not pick:
                continue
            s = _get_json(BASE.format(slug=slug) + "/summary", {"event": pick[0]["id"]}, get, stats)
            p = parse_summary(s)
            info[label] = {
                "match": f"{pick[0]['espn_home']} v {pick[0]['espn_away']}",
                "starters": {t: len(v["starters"]) for t, v in p["lineups"].items()},
                "subs": {t: len(v["subs"]) for t, v in p["lineups"].items()},
                "starter_sample": next((v["starters"][:3] for v in p["lineups"].values()), []),
                "injuries_key": "injuries" in (s or {}),
                "meta": (s or {}).get("meta"),
                "updated": p["updated"],
            }
        out[league] = info
    return out
