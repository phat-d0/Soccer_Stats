"""The three UEFA cups: a cost estimate only (owner's request, 12 Oct 2026).

Nothing here fetches odds, spends credits or touches the live build. The cups are
registered off in leagues.py (UCL, UEL, UECL, `cup=True`); this module prices switching
them on (docs/leagues.md, "European cups: cost estimate"):

* **Calendar.** football-data has no cup files, so the fixtures come from ESPN's free,
  unofficial site API (the scoreboard, one date per call, as espn_news does). A season's
  dates come from the scoreboard's own `leagues[0].calendar` (the days ESPN lists
  matches), seeded by one call on the 1st of each month; without a calendar it asks every
  day. Past days are cached for good under `data/raw/cups/`. ESPN keeps the qualifying
  rounds under their own slugs (`*_qual`), which match The Odds API's split (the main
  sport keys cover the league phase and knockouts).
* **Live odds** (`live_estimate`): odds_feed.estimate_credits per month on that calendar
  under the "matchday" rule at 1 credit a refresh, plus how many refreshes and matches
  fall on Tuesday to Thursday.
* **Snapshots** (`snapshot_estimate`): the base plan (a look 24 h out plus a close) at 1
  credit per market per snapshot, the estimator team totals and corners use.
* **Historical prices** (`historical_options`): The Odds API's historical bulk odds cost
  HIST_COST credits per region per market per snapshot, and one snapshot covers every
  listed match at that moment, so a close costs one snapshot per distinct kickoff time.
* **Team coverage** (`team_coverage`): which cup teams football-data's domestic files
  (main divisions and its "extra leagues") would let us rate.
* **Corner counts** (`summary_corners`): whether ESPN's cup summaries carry corners.
"""

from __future__ import annotations

import difflib
import io
import json
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}"
CUPS = {"UCL": "uefa.champions", "UEL": "uefa.europa", "UECL": "uefa.europa.conf"}
QUAL = {code: slug + "_qual" for code, slug in CUPS.items()}  # qualifying rounds
SEASONS = (2024, 2025)  # the 36-team league-phase format began in 2024/25
HIST_COST = 10  # historical bulk odds: credits per region per market per snapshot
LOOK_HOURS = 24
MIDWEEK = (1, 2, 3)  # Tuesday, Wednesday, Thursday (Monday = 0)
TIMEOUT = 15
RETRIES = 2
PAUSE = 0.1
MAX_FAILURES = 25  # failed requests (after retries) before a calendar stops asking ESPN
CACHE = "cups"

# Our six domestic fits, then football-data's other main divisions and its "extra
# leagues" (results, Pinnacle's and the market's closing 1X2, fewer columns).
FITTED = ("E0", "SP1", "D1", "I1", "F1", "E1")
FD_MAIN = FITTED + (
    "E2",
    "E3",
    "EC",
    "SC0",
    "SC1",
    "SC2",
    "SC3",
    "D2",
    "I2",
    "SP2",
    "F2",
    "N1",
    "B1",
    "P1",
    "T1",
    "G1",
)
FD_EXTRA = (
    "ARG",
    "AUT",
    "BRA",
    "CHN",
    "DNK",
    "FIN",
    "IRL",
    "JPN",
    "MEX",
    "NOR",
    "POL",
    "ROU",
    "RUS",
    "SWE",
    "SWZ",
    "USA",
)
FD_MAIN_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
FD_EXTRA_URL = "https://www.football-data.co.uk/new/{code}.csv"
COUNTRY = {
    "E0": "England",
    "E1": "England",
    "E2": "England",
    "E3": "England",
    "EC": "England",
    "SC0": "Scotland",
    "SC1": "Scotland",
    "SC2": "Scotland",
    "SC3": "Scotland",
    "SP1": "Spain",
    "SP2": "Spain",
    "D1": "Germany",
    "D2": "Germany",
    "I1": "Italy",
    "I2": "Italy",
    "F1": "France",
    "F2": "France",
    "N1": "Netherlands",
    "B1": "Belgium",
    "P1": "Portugal",
    "T1": "Turkey",
    "G1": "Greece",
    "AUT": "Austria",
    "SWZ": "Switzerland",
    "DNK": "Denmark",
    "NOR": "Norway",
    "SWE": "Sweden",
    "POL": "Poland",
    "ROU": "Romania",
    "FIN": "Finland",
    "IRL": "Ireland",
    "RUS": "Russia",
}
# Words that carry no identity in club names ("FC Porto" = "Porto").
STOP = {
    "fc",
    "cf",
    "sc",
    "ac",
    "afc",
    "fk",
    "sk",
    "nk",
    "bk",
    "cd",
    "ud",
    "sv",
    "as",
    "ss",
    "rc",
    "kv",
    "krc",
    "hnk",
    "gnk",
    "pfc",
    "jk",
    "if",
    "ik",
    "ff",
    "club",
    "de",
    "the",
    "calcio",
    "1",
}
# ESPN spellings -> football-data's, where neither the name nor the fuzzy match finds it.
# Each entry was checked against the football-data file named in the comment.
ALIASES: dict[str, str] = {
    "Internazionale": "Inter",  # I1
    "Paris Saint-Germain": "Paris SG",  # F1
    "Sporting CP": "Sp Lisbon",  # P1
    "SC Braga": "Sp Braga",  # P1
    "Olympiacos": "Olympiakos",  # G1
    "PAOK Salonika": "PAOK",  # G1
    "Union St.-Gilloise": "St. Gilloise",  # B1
    "Royale Union Saint-Gilloise": "St. Gilloise",  # B1
    "Bayer Leverkusen": "Leverkusen",  # D1
    "Eintracht Frankfurt": "Ein Frankfurt",  # D1
    "VfB Stuttgart": "Stuttgart",  # D1
    "SC Freiburg": "Freiburg",  # D1
    "Atlético Madrid": "Ath Madrid",  # SP1
    "Athletic Club": "Ath Bilbao",  # SP1
    "Celta Vigo": "Celta",  # SP1
    "Real Betis": "Betis",  # SP1
    "Nottingham Forest": "Nott'm Forest",  # E0
    "Brighton & Hove Albion": "Brighton",  # E0
    "Lyon": "Lyon",  # F1
    "Olympique Lyonnais": "Lyon",  # F1
    "Olympique de Marseille": "Marseille",  # F1
    "AS Monaco": "Monaco",  # F1
    "Lille": "Lille",  # F1
    "Feyenoord Rotterdam": "Feyenoord",  # N1
    "AZ Alkmaar": "AZ Alkmaar",  # N1
    "Go Ahead Eagles": "Go Ahead Eagles",  # N1
    "Bodo/Glimt": "Bodo/Glimt",  # NOR
    "FC Copenhagen": "FC Copenhagen",  # DNK
    "Red Bull Salzburg": "Salzburg",  # AUT
    "Sturm Graz": "Sturm Graz",  # AUT
    "Young Boys": "Young Boys",  # SWZ
    "FC Basel": "Basel",  # SWZ
    "Fenerbahce": "Fenerbahce",  # T1
    "Rapid Vienna": "Rapid Vienna",  # AUT
}
# Fuzzy matches seen to be wrong (a different club with a similar name).
DENY: set[tuple[str, str]] = {
    ("Red Star Belgrade", "Red Star"),  # F2's Red Star (Paris), not Crvena zvezda
}


# ---------- ESPN calendar ----------


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _get_json(url: str, params: dict, get: Callable, stats: dict) -> dict | None:
    """GET with a short timeout and a small retry; None on any failure, and None without
    a call once MAX_FAILURES requests have failed."""
    for attempt in range(RETRIES + 1):
        if stats.get("failures", 0) >= MAX_FAILURES:
            stats["stopped"] = True
            return None
        stats["requests"] = stats.get("requests", 0) + 1
        try:
            r = get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code < 500:
                return None  # 4xx (an unknown slug or date): retrying won't help
        except (requests.RequestException, ValueError):
            pass
        if attempt < RETRIES:
            time.sleep(PAUSE * (attempt + 1))
    stats["failures"] = stats.get("failures", 0) + 1
    return None


def _cache_path(raw_dir: Path, slug: str, day: str) -> Path:
    return Path(raw_dir) / CACHE / "espn" / f"{slug}_{day}.json"


def scoreboard_day(
    slug: str, day: str, raw_dir: Path, get: Callable, stats: dict, today: pd.Timestamp
) -> dict | None:
    """ESPN's scoreboard for one date (YYYYMMDD). Days at least two days past are final
    and cached for good; nearer days are always asked again."""
    path = _cache_path(raw_dir, slug, day)
    final = pd.Timestamp(day, tz="UTC") < today.normalize() - pd.Timedelta(days=1)
    if final and path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    body = _get_json(ESPN.format(slug=slug) + "/scoreboard", {"dates": day}, get, stats)
    if body is not None and final:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
    return body


def has_calendar(body: dict | None) -> bool:
    """True when the scoreboard carries a `leagues[0].calendar` list (even an empty one:
    a slug with no matches that season)."""
    leagues = body.get("leagues") if isinstance(body, dict) else None
    ok = isinstance(leagues, list) and leagues and isinstance(leagues[0], dict)
    return bool(ok) and isinstance(leagues[0].get("calendar"), list)


def calendar_days(body: dict | None) -> list[str]:
    """The days (YYYYMMDD) ESPN's scoreboard lists in `leagues[0].calendar` (soccer:
    ISO date strings, the days with matches); [] when absent or in another shape."""
    leagues = (body or {}).get("leagues") if isinstance(body, dict) else None
    if not isinstance(leagues, list) or not leagues or not isinstance(leagues[0], dict):
        return []
    out = []
    for c in leagues[0].get("calendar") or []:
        text = c if isinstance(c, str) else None
        if text and re.match(r"\d{4}-\d{2}-\d{2}", text):
            out.append(text[:10].replace("-", ""))
    return out


def parse_events(body: dict | None) -> list[dict]:
    """{id, kickoff, home, away, home_id, away_id, completed, stage} per event."""
    out = []
    events = (body or {}).get("events") if isinstance(body, dict) else None
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        comps = e.get("competitions") or [{}]
        comp = comps[0] if isinstance(comps[0], dict) else {}
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or [] if isinstance(c, dict)}
        h, a = (sides.get(s, {}).get("team") or {} for s in ("home", "away"))
        if not (e.get("id") and e.get("date") and h.get("displayName") and a.get("displayName")):
            continue
        status = ((e.get("status") or {}).get("type") or {}).get("completed")
        out.append(
            {
                "id": str(e["id"]),
                "kickoff": _ts(e["date"]),
                "home": h["displayName"],
                "away": a["displayName"],
                "home_id": str(h.get("id", "")),
                "away_id": str(a.get("id", "")),
                "completed": bool(status),
                "stage": (e.get("season") or {}).get("slug"),
            }
        )
    return out


def _all_days(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    return [
        f"{d:%Y%m%d}" for d in pd.date_range(start.normalize(), end, freq="D", inclusive="left")
    ]


def season_calendar(
    slug: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    every_day: bool = False,
    today: pd.Timestamp | None = None,
) -> dict:
    """Every ESPN event for `slug` kicking off in [start, end), deduplicated by id.

    Seeds one call on the 1st of each month, then asks each day ESPN's calendar lists
    within the window; with `every_day` (or when no response carries a calendar) it asks
    every day instead. Returns {slug, events, requests, failures, mode, days}."""
    today = today or pd.Timestamp.now(tz="UTC")
    stats: dict = {}
    window = set(_all_days(start, end))
    if every_day:
        todo, mode = sorted(window), "every day"
    else:
        seeds = [f"{d:%Y%m%d}" for d in pd.date_range(start, end, freq="MS", inclusive="left")]
        todo, mode = sorted(set(seeds) or {min(window)}), "calendar"
    done: set[str] = set()
    events: dict[str, dict] = {}
    listed = answered = False
    while todo:
        day = todo.pop(0)
        if day in done:
            continue
        done.add(day)
        body = scoreboard_day(slug, day, raw_dir, get, stats, today)
        answered = answered or body is not None
        for e in parse_events(body):
            events[e["id"]] = e
        if mode == "calendar":
            cal = [d for d in calendar_days(body) if d in window]
            listed = listed or has_calendar(body)
            todo = sorted((set(todo) | set(cal)) - done)
        if not todo and mode == "calendar" and not listed:
            if not answered:  # an unknown slug (every seed 4xx or failed): nothing to ask
                mode = "no response"
                break
            mode = "every day (no calendar)"  # answers but no calendar: ask every day
            todo = sorted(window - done)
    inside = [e for e in events.values() if start <= e["kickoff"] < end]
    return {
        "slug": slug,
        "events": sorted(inside, key=lambda e: (e["kickoff"], e["id"])),
        "requests": stats.get("requests", 0),
        "failures": stats.get("failures", 0),
        "mode": mode,
        "days": len(done),
    }


def season_window(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """1 July of the start year to 1 July of the next (UTC)."""
    return pd.Timestamp(f"{year}-07-01", tz="UTC"), pd.Timestamp(f"{year + 1}-07-01", tz="UTC")


# ---------- estimates (pure: calendars in, numbers out) ----------


def _months(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return list(pd.date_range(start, end, freq="MS", inclusive="left"))


def live_estimate(code: str, kickoffs, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Live DraftKings h2h refreshes per month under the "matchday" rule (1 credit a
    refresh), with the Tuesday-Thursday share of refreshes and matches. `typical` is the
    median month among those with matches."""
    from soccer_stats.odds_feed import estimate_credits

    ks = sorted(_ts(k) for k in kickoffs)
    rows = []
    for m in _months(start, end):
        nxt = m + pd.offsets.MonthBegin(1)
        times: list = []
        e = estimate_credits(code, ks, m, nxt, call_times=times)
        month_ks = [k for k in ks if m <= k < nxt]
        rows.append(
            {
                "month": f"{m:%Y-%m}",
                "matches": e["matches"],
                "matches_midweek": sum(k.weekday() in MIDWEEK for k in month_ks),
                "calls": e["calls"],
                "calls_midweek": sum(t.weekday() in MIDWEEK for t in times),
                "credits": e["credits"],
                "call_days": dict(Counter(f"{t:%a}" for t in times)),
            }
        )
    busy = sorted(r["credits"] for r in rows if r["matches"])
    total = {k: sum(r[k] for r in rows) for k in ("matches", "matches_midweek", "calls")}
    total["calls_midweek"] = sum(r["calls_midweek"] for r in rows)
    total["credits"] = sum(r["credits"] for r in rows)
    return {
        "months": rows,
        "season": total,
        "typical": float(pd.Series(busy).median()) if busy else 0.0,
        "months_with_matches": len(busy),
    }


def snapshot_estimate(code: str, kickoffs, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Credits per month for the base snapshot plan (a look 24 h out plus the close),
    1 credit per market per snapshot: Pinnacle team corners (corners_live.estimate) and
    FanDuel team totals alone (odds_feed.estimate_snapshot_credits, 1 market). The live
    team-total call also asks for the match total (2 markets), which doubles that."""
    from soccer_stats import corners_live
    from soccer_stats.odds_feed import estimate_snapshot_credits

    ks = sorted(_ts(k) for k in kickoffs)
    rows = []
    for m in _months(start, end):
        nxt = m + pd.offsets.MonthBegin(1)
        c = corners_live.estimate({code: ks}, m, nxt)[code]
        t = estimate_snapshot_credits(code, ks, m, nxt, "base", markets=1)
        rows.append(
            {
                "month": f"{m:%Y-%m}",
                "matches": c["matches"],
                "corners": c["credits"],
                "team_totals": t["credits"],
                "team_totals_with_match_total": 2 * t["credits"],
            }
        )
    keys = ("matches", "corners", "team_totals", "team_totals_with_match_total")
    busy = [r for r in rows if r["matches"]]
    return {
        "months": rows,
        "season": {k: sum(r[k] for r in rows) for k in keys},
        "typical": {
            k: float(pd.Series([r[k] for r in busy]).median()) if busy else 0.0 for k in keys[1:]
        },
    }


def kickoff_slots(kickoffs) -> int:
    """Distinct kickoff times: one historical bulk snapshot per slot prices every match
    starting then."""
    return len({_ts(k) for k in kickoffs})


def historical_cost(kickoffs) -> dict:
    """One cup-season's historical h2h cost, HIST_COST per region per snapshot:
    (a) Pinnacle close (eu), (b) DraftKings close (us), (c) both books, close plus a
    look LOOK_HOURS before (look times deduplicated, as each is its own snapshot)."""
    ks = {_ts(k) for k in kickoffs}
    slots = len(ks)
    looks = len({k - pd.Timedelta(hours=LOOK_HOURS) for k in ks})
    return {
        "matches": len(kickoffs),
        "slots": slots,
        "a_pinnacle_close": slots * HIST_COST,
        "b_draftkings_close": slots * HIST_COST,
        "c_both_close_and_look": (slots + looks) * HIST_COST * 2,
    }


def historical_options(by_season: dict[int, list]) -> dict:
    """historical_cost per season, the latest season alone ("1 season") and every season
    given together ("2 seasons" for 2024/25 and 2025/26)."""
    per = {str(y): historical_cost(ks) for y, ks in sorted(by_season.items())}
    keys = ("matches", "slots", "a_pinnacle_close", "b_draftkings_close", "c_both_close_and_look")
    latest = per[max(per)] if per else dict.fromkeys(keys, 0)
    return {
        "seasons": per,
        "one_season": {k: latest[k] for k in keys},
        "all_seasons": {k: sum(p[k] for p in per.values()) for k in keys},
    }


# ---------- team coverage on football-data ----------


def norm(name: str) -> str:
    """Lower case, no accents or punctuation, without filler words ("FC", "SK", ...)."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in STOP)


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(Path(path).read_bytes()), encoding="latin-1", on_bad_lines="skip")


def fd_teams(raw_dir: Path = RAW_DIR, seasons=SEASONS, fetch: Callable | None = None) -> dict:
    """{code: set of team names} from football-data for the seasons given: main
    divisions' season files (HomeTeam/AwayTeam), and the extra leagues' all-seasons
    files (Home/Away, rows whose Season mentions one of the years). Files that fail are
    {code: {"error": ...}} in the second return value."""
    from soccer_stats.data import _fetch

    fetch = fetch or (lambda url, path: _fetch(url, path, max_age_hours=None))
    base = Path(raw_dir) / CACHE / "football_data"
    teams: dict[str, set] = {}
    errors: dict[str, str] = {}
    for code in FD_MAIN:
        for y in seasons:
            season = f"{y % 100:02d}{(y + 1) % 100:02d}"
            try:
                df = _read_csv(
                    fetch(
                        FD_MAIN_URL.format(season=season, code=code), base / f"{code}_{season}.csv"
                    )
                )
                names = set(df.get("HomeTeam", pd.Series(dtype=str)).dropna())
                names |= set(df.get("AwayTeam", pd.Series(dtype=str)).dropna())
                teams.setdefault(code, set()).update(names)
            except Exception as exc:  # noqa: BLE001  not posted, or unreachable
                errors[f"{code} {season}"] = type(exc).__name__
    years = tuple(str(y) for y in seasons) + tuple(str(y + 1) for y in seasons)
    for code in FD_EXTRA:
        try:
            df = _read_csv(fetch(FD_EXTRA_URL.format(code=code), base / f"{code}.csv"))
            season = df.get("Season", pd.Series("", index=df.index)).astype(str)
            keep = df[season.apply(lambda s: any(y in s for y in years))]
            names = set(keep.get("Home", pd.Series(dtype=str)).dropna())
            names |= set(keep.get("Away", pd.Series(dtype=str)).dropna())
            teams[code] = names
        except Exception as exc:  # noqa: BLE001
            errors[code] = type(exc).__name__
    return {"teams": teams, "errors": errors}


def match_team(name: str, teams: dict[str, set]) -> dict | None:
    """Football-data's club for an ESPN name: {league, name, how} or None.

    In order: our maps (ALIASES, espn_news.ESPN_NAMES, odds_feed.TEAM_NAMES) and the
    exact normalized name; one club whose words contain the other's (or the reverse);
    a unique close spelling (difflib ratio >= 0.88). DENY drops pairs seen to be wrong."""
    from soccer_stats.espn_news import ESPN_NAMES
    from soccer_stats.odds_feed import TEAM_NAMES

    index: dict[str, list[tuple[str, str]]] = {}
    for code, names in teams.items():
        for n in names:
            index.setdefault(norm(n), []).append((code, n))

    def pick(cands, how):
        cands = [c for c in cands if (name, c[1]) not in DENY]
        # A club in two files (promoted or relegated across the two seasons) is one club:
        # prefer a fitted league, then a main division.
        if not cands or len({c[1] for c in cands}) > 1:
            return None
        rank = {c: (c not in FITTED, c not in FD_MAIN) for c in teams}
        code, n = min(cands, key=lambda c: rank[c[0]])
        return {"league": code, "name": n, "how": how}

    for mapped in (ALIASES.get(name), ESPN_NAMES.get(name), TEAM_NAMES.get(name), name):
        if mapped and norm(mapped) in index:
            got = pick(index[norm(mapped)], "name" if mapped == name else "alias")
            if got:
                return got
    key = norm(name)
    words = set(key.split())
    if not words:
        return None

    def contains(k: str) -> bool:  # one name's words inside the other's, half or more
        other = set(k.split())
        small, big = sorted((other, words), key=len)
        return bool(small) and small <= big and 2 * len(small) >= len(big)

    subset = [c for k, cs in index.items() if contains(k) for c in cs]
    got = pick(subset, "words")
    if got:
        return got
    close = difflib.get_close_matches(key, list(index), n=2, cutoff=0.88)
    if len(close) == 1:
        return pick(index[close[0]], "spelling")
    return None


def cup_teams(events: list[dict]) -> list[str]:
    """Distinct team names in a calendar (by ESPN id when present)."""
    seen: dict[str, str] = {}
    for e in events:
        for side in ("home", "away"):
            seen.setdefault(e.get(f"{side}_id") or e[side], e[side])
    return sorted(seen.values())


def team_coverage(teams_by_cup: dict[str, list[str]], fd: dict[str, set]) -> dict:
    """Per cup and overall: how many teams football-data rates, split into our six fitted
    leagues, its other main divisions and its extra leagues, with the unmatched names."""

    def tier(code: str) -> str:
        return "fitted" if code in FITTED else "main" if code in FD_MAIN else "extra"

    out: dict = {"cups": {}}
    everyone: dict[str, dict | None] = {}
    for cup, names in teams_by_cup.items():
        rows = {n: match_team(n, fd) for n in names}
        everyone.update(rows)
        out["cups"][cup] = _coverage(rows, tier)
    out["all"] = _coverage(everyone, tier)
    return out


def _coverage(rows: dict[str, dict | None], tier: Callable) -> dict:
    n = len(rows)
    counts = Counter(tier(r["league"]) for r in rows.values() if r)
    by_league = Counter(r["league"] for r in rows.values() if r)
    rated = sum(counts.values())
    return {
        "teams": n,
        "fitted": counts.get("fitted", 0),
        "main": counts.get("main", 0),
        "extra": counts.get("extra", 0),
        "rated": rated,
        "share": round(rated / n, 3) if n else None,
        "share_main": round((counts.get("fitted", 0) + counts.get("main", 0)) / n, 3)
        if n
        else None,
        "by_league": dict(by_league.most_common()),
        "matches": {k: v for k, v in sorted(rows.items()) if v},
        "unmatched": sorted(k for k, v in rows.items() if not v),
    }


# ---------- corner counts in ESPN's summaries ----------


def summary_corners(slug: str, event_id: str, get: Callable = requests.get) -> dict:
    """Whether a match summary's boxscore carries corners: the stat names found, and the
    corner counts per team when there."""
    stats: dict = {}
    body = _get_json(ESPN.format(slug=slug) + "/summary", {"event": event_id}, get, stats)
    teams = ((body or {}).get("boxscore") or {}).get("teams") or []
    names: list[str] = []
    corners: dict[str, str] = {}
    for t in teams if isinstance(teams, list) else []:
        if not isinstance(t, dict):
            continue
        team = (t.get("team") or {}).get("displayName", "?")
        for s in t.get("statistics") or []:
            if not isinstance(s, dict):
                continue
            nm = str(s.get("name", ""))
            if nm and nm not in names:
                names.append(nm)
            if "corner" in nm.lower():
                corners[team] = s.get("displayValue")
    return {"event": event_id, "ok": body is not None, "stat_names": names, "corners": corners}


# ---------- the whole estimate ----------


def run(
    seasons=SEASONS,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    every_day: bool = False,
    teams: bool = True,
    fd_fetch: Callable | None = None,
    today: pd.Timestamp | None = None,
) -> dict:
    """Calendars for every cup and season (main and qualifying slugs), then the live,
    snapshot and historical estimates, ESPN's corner check and football-data coverage."""
    out: dict = {"seasons": list(seasons), "cups": {}}
    latest = max(seasons)
    for code, slug in CUPS.items():
        cup: dict = {"slug": slug, "seasons": {}}
        hist: dict[int, list] = {}
        for y in seasons:
            start, end = season_window(y)
            cal = season_calendar(slug, start, end, raw_dir, get, every_day, today)
            ks = [e["kickoff"] for e in cal["events"]]
            hist[y] = ks
            q = season_calendar(QUAL[code], start, end, raw_dir, get, every_day, today)
            qks = [e["kickoff"] for e in q["events"]]
            cup["seasons"][str(y)] = {
                "calendar": {k: cal[k] for k in ("requests", "failures", "mode", "days")},
                "matches": len(ks),
                "midweek_matches": sum(k.weekday() in MIDWEEK for k in ks),
                "kickoff_days": dict(Counter(f"{k:%a}" for k in ks)),
                "slots": kickoff_slots(ks),
                "first": f"{min(ks):%Y-%m-%d}" if ks else None,
                "last": f"{max(ks):%Y-%m-%d}" if ks else None,
                "live": live_estimate(code, ks, start, end),
                "snapshots": snapshot_estimate(code, ks, start, end),
                "qualifying": {
                    "slug": QUAL[code],
                    "matches": len(qks),
                    "slots": kickoff_slots(qks),
                    "live": live_estimate(code, qks, start, end)["season"],
                },
            }
            if y == latest:
                cup["teams"] = cup_teams(cal["events"])
                cup["qualifying_teams"] = cup_teams(q["events"])
                done = [e for e in cal["events"] if e["completed"]]
                cup["corner_check"] = [summary_corners(slug, e["id"], get) for e in done[-3:]]
                cup["events_latest"] = [
                    {**e, "kickoff": e["kickoff"].isoformat()} for e in cal["events"]
                ]
        cup["historical"] = historical_options(hist)
        out["cups"][code] = cup
    out["total"] = _totals(out["cups"], latest)
    if teams:
        fd = fd_teams(raw_dir, fetch=fd_fetch)
        out["football_data_errors"] = fd["errors"]
        out["coverage"] = team_coverage(
            {c: v.get("teams", []) for c, v in out["cups"].items()}, fd["teams"]
        )
        qual_only = {
            c: [t for t in v.get("qualifying_teams", []) if t not in v.get("teams", [])]
            for c, v in out["cups"].items()
        }
        out["coverage_qualifying_only"] = team_coverage(qual_only, fd["teams"])
    return out


def _totals(cups: dict, latest: int) -> dict:
    """The three cups together for the latest season: live credits per month, snapshot
    credits and the historical options."""
    months: dict[str, dict] = {}
    for c in cups.values():
        s = c["seasons"][str(latest)]
        for r, sr in zip(s["live"]["months"], s["snapshots"]["months"], strict=True):
            m = months.setdefault(
                r["month"], {"matches": 0, "credits": 0, "calls_midweek": 0, "corners": 0}
            )
            m["matches"] += r["matches"]
            m["credits"] += r["credits"]
            m["calls_midweek"] += r["calls_midweek"]
            m["corners"] += sr["corners"]
    busy = sorted(m["credits"] for m in months.values() if m["matches"])
    hist_keys = ("slots", "a_pinnacle_close", "b_draftkings_close", "c_both_close_and_look")
    return {
        "months": months,
        "season_credits": sum(m["credits"] for m in months.values()),
        "typical": float(pd.Series(busy).median()) if busy else 0.0,
        "historical_one_season": {
            k: sum(c["historical"]["one_season"][k] for c in cups.values()) for k in hist_keys
        },
        "historical_all_seasons": {
            k: sum(c["historical"]["all_seasons"][k] for c in cups.values()) for k in hist_keys
        },
    }


def report(res: dict) -> str:
    """The estimate in plain lines for the job log."""
    latest = str(max(res["seasons"]))
    lines = ["== European cups: cost estimate (no key, 0 credits) =="]
    for code, c in res["cups"].items():
        lines.append(f"{code} ({c['slug']}):")
        for y, s in c["seasons"].items():
            cal, live, snap = s["calendar"], s["live"], s["snapshots"]
            lines.append(
                f"  {y}/{int(y) % 100 + 1:02d}: {s['matches']} matches ({s['first']} to "
                f"{s['last']}), Tue-Thu {s['midweek_matches']}, kickoff days "
                f"{s['kickoff_days']}, slots {s['slots']}; ESPN {cal['requests']} requests, "
                f"{cal['failures']} failed, {cal['days']} days ({cal['mode']})"
            )
            lines.append(
                f"    live h2h: season {live['season']['credits']} credits, typical month "
                f"{live['typical']:.0f}, refreshes Tue-Thu {live['season']['calls_midweek']} of "
                f"{live['season']['calls']}"
            )
            for r in live["months"]:
                if r["matches"] or r["calls"]:
                    lines.append(
                        f"      {r['month']}: {r['matches']} matches, {r['calls']} refreshes "
                        f"({r['calls_midweek']} Tue-Thu) = {r['credits']} credits "
                        f"{r['call_days']}"
                    )
            lines.append(
                f"    snapshots (look + close, 1 credit/market): corners season "
                f"{snap['season']['corners']}, typical {snap['typical']['corners']:.0f}/month; "
                f"team totals {snap['season']['team_totals']} (x2 with the match total)"
            )
            q = s["qualifying"]
            lines.append(
                f"    qualifying ({q['slug']}): {q['matches']} matches, slots {q['slots']}, live "
                f"{q['live']['credits']} credits"
            )
        h = c["historical"]
        for label, k in (("1 season", "one_season"), ("2 seasons", "all_seasons")):
            v = h[k]
            lines.append(
                f"  historical {label}: {v['matches']} matches, {v['slots']} slots; (a) Pinnacle "
                f"close {v['a_pinnacle_close']}, (b) DraftKings close {v['b_draftkings_close']}, "
                f"(c) both, close + 24 h look {v['c_both_close_and_look']}"
            )
        for cc in c.get("corner_check", []):
            lines.append(
                f"  ESPN summary {cc['event']}: corners {cc['corners'] or 'none'}; stats "
                f"{cc['stat_names'][:12]}"
            )
    t = res["total"]
    lines.append(
        f"All three ({latest}/{int(latest) % 100 + 1:02d}): live season {t['season_credits']} "
        f"credits, typical month {t['typical']:.0f}"
    )
    for m, r in t["months"].items():
        if r["matches"]:
            lines.append(
                f"  {m}: {r['matches']} matches, {r['credits']} credits "
                f"({r['calls_midweek']} refreshes Tue-Thu), corners snapshots {r['corners']}"
            )
    lines.append(f"  historical 1 season: {t['historical_one_season']}")
    lines.append(f"  historical 2 seasons: {t['historical_all_seasons']}")
    for key, label in (
        ("coverage", "main competition"),
        ("coverage_qualifying_only", "qualifying only"),
    ):
        cov = res.get(key)
        if not cov:
            continue
        lines.append(
            f"Team coverage on football-data, {label} {latest}/{int(latest) % 100 + 1:02d}:"
        )
        for cup, v in [*cov["cups"].items(), ("all", cov["all"])]:
            lines.append(
                f"  {cup}: {v['rated']} of {v['teams']} rated ({v['share']}); our six "
                f"{v['fitted']}, other main {v['main']}, extra {v['extra']}; by league "
                f"{v['by_league']}"
            )
            if cup == "all":
                lines.append(f"    unmatched: {v['unmatched']}")
                lines.append(
                    "    matched: "
                    + "; ".join(
                        f"{k} -> {m['league']} {m['name']} ({m['how']})"
                        for k, m in v["matches"].items()
                    )
                )
    if res.get("football_data_errors"):
        lines.append(f"football-data files unavailable: {res['football_data_errors']}")
    return "\n".join(lines)


def summary(res: dict) -> dict:
    """The compact CUPS_JSON line: everything but the event lists and team matches."""
    out = json.loads(json.dumps(res, default=str))
    for c in out["cups"].values():
        c.pop("events_latest", None)
    for key in ("coverage", "coverage_qualifying_only"):
        for v in [*(out.get(key) or {}).get("cups", {}).values(), (out.get(key) or {}).get("all")]:
            if v:
                v.pop("matches", None)
    return out
