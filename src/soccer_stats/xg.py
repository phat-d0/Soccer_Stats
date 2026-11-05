"""Per-match expected goals (xG) from Understat, merged onto football-data matches.

Understat covers EPL, La Liga, Bundesliga, Serie A, Ligue 1 and RFPL from 2014/15.
xG measures the quality of chances each side created, so it is a less noisy signal of
team strength than goals: a team that wins 1-0 while conceding 2.5 xG got lucky.
"""

from __future__ import annotations

import json
import re
import time
import warnings
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from soccer_stats.data import RAW_DIR, STALE, _fetch, current_season

URL = "https://understat.com/getLeagueData/{league}/{season}"
# Understat's AJAX endpoint only answers requests that look like they come from its site.
HEADERS = {"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"}

# football-data league code -> Understat league name
LEAGUES = {
    "E0": "EPL",
    "SP1": "La_Liga",
    "D1": "Bundesliga",
    "I1": "Serie_A",
    "F1": "Ligue_1",
}

# Understat team name -> football-data team name, where they differ.
TEAM_NAMES = {
    # England
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Wolverhampton Wanderers": "Wolves",
    "West Bromwich Albion": "West Brom",
    "Queens Park Rangers": "QPR",
    # Spain
    "Athletic Club": "Ath Bilbao",
    "Atletico Madrid": "Ath Madrid",
    "Real Betis": "Betis",
    "Celta Vigo": "Celta",
    "Espanyol": "Espanol",
    "Real Sociedad": "Sociedad",
    "Rayo Vallecano": "Vallecano",
    "Deportivo La Coruna": "La Coruna",
    "Real Valladolid": "Valladolid",
    "SD Huesca": "Huesca",
    "Sporting Gijon": "Sp Gijon",
    # Germany
    "Borussia Dortmund": "Dortmund",
    "Borussia M.Gladbach": "M'gladbach",
    "Bayer Leverkusen": "Leverkusen",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "FC Cologne": "FC Koln",
    "Hertha Berlin": "Hertha",
    "Mainz 05": "Mainz",
    "RasenBallsport Leipzig": "RB Leipzig",
    "VfB Stuttgart": "Stuttgart",
    "Fortuna Duesseldorf": "Fortuna Dusseldorf",
    "Arminia Bielefeld": "Bielefeld",
    "Greuther Fuerth": "Greuther Furth",
    "St. Pauli": "St Pauli",
    "FC Heidenheim": "Heidenheim",
    "Hamburger SV": "Hamburg",
    "Hannover 96": "Hannover",
    "Nuernberg": "Nurnberg",
    # Italy
    "AC Milan": "Milan",
    "Parma Calcio 1913": "Parma",
    "SPAL 2013": "Spal",
    # France
    "Paris Saint Germain": "Paris SG",
    "Saint-Etienne": "St Etienne",
    "Clermont Foot": "Clermont",
}


def check_league_json(body: bytes) -> None:
    """Raise unless `body` is Understat league data: JSON with a non-empty `dates` list.

    Understat has answered with an HTML page (status 200) since 2 Nov 2026; caching that
    page in place of the season's JSON is what turned xG off.
    """
    if body.lstrip()[:1] == b"<":
        raise ValueError(f"an HTML page ({len(body):,} bytes), not JSON")
    data = json.loads(body)
    dates = data.get("dates") if isinstance(data, dict) else data
    if not isinstance(dates, list) or not dates:
        raise ValueError("JSON without any matches")


def check_match_json(body: bytes) -> None:
    """Raise unless `body` is one Understat match's JSON (a dict, not an HTML page)."""
    if body.lstrip()[:1] == b"<":
        raise ValueError(f"an HTML page ({len(body):,} bytes), not JSON")
    if not isinstance(json.loads(body), dict):
        raise ValueError("JSON that isn't a match")


def fetch_season(league: str, start_year: int, raw_dir: Path = RAW_DIR) -> Path:
    """Download (and cache) Understat's league-season JSON. Live season refreshes every 12h.

    A bad answer (HTML, empty) is never cached: the last good file is kept and used,
    and a cached file that isn't good league JSON is fetched again.
    """
    us_league = LEAGUES[league]
    return _fetch(
        URL.format(league=us_league, season=start_year),
        raw_dir / f"understat_{us_league}_{start_year}.json",
        max_age_hours=12 if start_year >= current_season() else None,
        headers=HEADERS,
        stale_ok=True,
        validate=check_league_json,
    )


def parse_season(data: dict | list) -> pd.DataFrame:
    """Understat league JSON -> one row per played match with goals and xG."""
    dates = data.get("dates", []) if isinstance(data, dict) else data
    rows = [
        {
            "datetime": d["datetime"],
            "home": TEAM_NAMES.get(d["h"]["title"], d["h"]["title"]),
            "away": TEAM_NAMES.get(d["a"]["title"], d["a"]["title"]),
            "us_home_goals": int(d["goals"]["h"]),
            "us_away_goals": int(d["goals"]["a"]),
            "home_xg": float(d["xG"]["h"]),
            "away_xg": float(d["xG"]["a"]),
        }
        for d in dates
        if d.get("isResult") and d.get("xG", {}).get("h") is not None
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["datetime"] = pd.to_datetime(df["datetime"])
    return df


def parse_schedule(data: dict | list, include_played: bool = False) -> pd.DataFrame:
    """Understat league JSON -> unplayed fixtures with UTC kick-off times.

    Understat publishes the whole season's schedule, so this covers fixtures weeks
    ahead (football-data's fixtures file only lists about the next week). With
    include_played, played matches are kept too (for planning historical odds).
    """
    dates = data.get("dates", []) if isinstance(data, dict) else data
    rows = [
        {
            "kickoff": d["datetime"],
            "home": TEAM_NAMES.get(d["h"]["title"], d["h"]["title"]),
            "away": TEAM_NAMES.get(d["a"]["title"], d["a"]["title"]),
        }
        for d in dates
        if include_played or not d.get("isResult")
    ]
    df = pd.DataFrame(rows, columns=["kickoff", "home", "away"])
    # Understat times are UTC (e.g. a 20:00 BST kick-off is listed as 19:00).
    df["kickoff"] = pd.to_datetime(df["kickoff"]).dt.tz_localize("UTC")
    return df.sort_values("kickoff").reset_index(drop=True)


def load_schedule(
    league: str, start_year: int, raw_dir: Path = RAW_DIR, include_played: bool = False
) -> pd.DataFrame:
    data = json.loads(fetch_season(league, start_year, raw_dir).read_text())
    return parse_schedule(data, include_played)


def load_xg(
    leagues: Iterable[str], start_years: Iterable[int], raw_dir: Path = RAW_DIR
) -> pd.DataFrame:
    frames = []
    for league in leagues:
        for year in start_years:
            df = parse_season(json.loads(fetch_season(league, year, raw_dir).read_text()))
            df.insert(0, "league", league)
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def attach_xg(matches: pd.DataFrame, xg: pd.DataFrame, max_day_gap: int = 2) -> pd.DataFrame:
    """Add home_xg / away_xg to football-data matches (NaN where no xG match is found).

    Matches are paired on home team, away team and a kick-off date within `max_day_gap`
    days (time zones and late kick-offs can shift the calendar date).
    """
    out = matches.drop(columns=["home_xg", "away_xg"], errors="ignore").copy()
    out["home_xg"] = float("nan")
    out["away_xg"] = float("nan")
    if xg.empty:
        return out

    key = xg.assign(xg_date=xg["datetime"].dt.normalize())
    merged = out.reset_index().merge(key, on=["home", "away"], how="inner", suffixes=("", "_us"))
    merged = merged[(merged["date"] - merged["xg_date"]).abs().dt.days <= max_day_gap]
    merged = merged.drop_duplicates("index")
    out.loc[merged["index"], "home_xg"] = merged["home_xg_us"].to_numpy()
    out.loc[merged["index"], "away_xg"] = merged["away_xg_us"].to_numpy()

    # Team names that never matched are almost always a naming mismatch: surface them.
    unmatched = set(xg["home"]) - set(matches["home"])
    if unmatched and len(merged) < len(xg) * 0.95:
        warnings.warn(
            f"Understat teams with no football-data match (add to TEAM_NAMES): {sorted(unmatched)}",
            stacklevel=2,
        )
    return out


def with_xg(matches: pd.DataFrame, raw_dir: Path = RAW_DIR) -> tuple[pd.DataFrame, str | None]:
    """Attach xG for every league/season in `matches`. Never raises.

    Each league-season loads on its own, so one bad file (Understat down, a maintenance
    page) costs only that season: the model then uses goals for those matches and xG for
    the rest. Returns (matches with home_xg/away_xg, error message or None); the message
    names every season without xG and every season served from an older cached file.
    """
    leagues = [lg for lg in matches["league"].dropna().unique() if lg in LEAGUES]
    years = sorted({2000 + int(s[:2]) for s in matches["season"].dropna().unique()})
    frames, missing = [], []
    start = len(STALE)
    for league in leagues:
        for year in years:
            try:
                frames.append(load_xg([league], [year], raw_dir))
            except Exception as exc:  # network down, site changed, maintenance page
                missing.append(f"{league} {year}/{(year + 1) % 100:02d} ({exc})")
    xg = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    notes = []
    if missing:
        notes.append("Could not load xG from Understat for " + "; ".join(missing))
    stale = STALE[start:]
    if stale:
        notes.append("Understat refresh failed, using the last good file for " + "; ".join(stale))
    return attach_xg(matches, xg), (". ".join(notes) or None)


def repair_from(old_dir: Path, raw_dir: Path = RAW_DIR) -> list[dict]:
    """Copy good Understat league files from an older raw cache over bad current ones.

    For each `understat_<league>_<year>.json` in `old_dir`: copied (mtime kept) only when
    the old file is good league JSON and the current one is missing or bad, so a good
    current file is never replaced. One dict per file: name, action (copied, kept_current,
    old_bad), matches in the file used.
    """
    import shutil

    out = []
    for old in sorted(Path(old_dir).glob("understat_*_*.json")):
        cur = Path(raw_dir) / old.name
        row: dict = {"file": old.name}
        if not _good(old):
            row["action"] = "old_bad"
        elif cur.exists() and _good(cur):
            row["action"] = "kept_current"
        else:
            shutil.copy2(old, cur)
            row["action"] = "copied"
        used = cur if cur.exists() else old
        if _good(used):
            row["matches"] = len(json.loads(used.read_bytes())["dates"])
        out.append(row)
    return out


def _good(path: Path) -> bool:
    try:
        check_league_json(path.read_bytes())
    except Exception:
        return False
    return True


def probe(raw_dir: Path = RAW_DIR, seasons: Iterable[int] | None = None) -> list[dict]:
    """Diagnose Understat (no key, 0 credits): what the endpoints answer and what is cached.

    For each league and season, one raw request to the AJAX endpoint (status, content type,
    size, the first bytes, whether it parses as JSON with matches) and the same for the
    cached file. Also asks the public league page, to tell a block from a moved endpoint.
    """
    import requests

    years = list(seasons) if seasons is not None else [current_season(), current_season() - 1]
    out = []

    def describe(body: bytes) -> dict:
        info: dict = {"bytes": len(body), "head": body[:200].decode("utf-8", "replace")}
        try:
            data = json.loads(body)
        except ValueError as exc:
            info["json"] = f"not JSON ({exc})"
            return info
        dates = data.get("dates", []) if isinstance(data, dict) else data
        info["json"] = "ok"
        info["keys"] = sorted(data)[:10] if isinstance(data, dict) else "list"
        info["matches"] = len(dates) if isinstance(dates, list) else None
        return info

    for league, us in LEAGUES.items():
        for year in years:
            row: dict = {"league": league, "season": year}
            url = URL.format(league=us, season=year)
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                row["live"] = {
                    "status": r.status_code,
                    "type": r.headers.get("content-type"),
                    "server": r.headers.get("server"),
                    **describe(r.content),
                }
            except requests.RequestException as exc:
                row["live"] = {"error": type(exc).__name__, "detail": str(exc)[:200]}
            cached = raw_dir / f"understat_{us}_{year}.json"
            if cached.exists():
                age = (time.time() - cached.stat().st_mtime) / 3600
                row["cached"] = {"age_hours": round(age, 1), **describe(cached.read_bytes())}
            else:
                row["cached"] = None
            out.append(row)
    # The same URL with other headers, to tell a header-based block from an IP block.
    chrome = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
    url = URL.format(league="EPL", season=years[0])
    variants = {
        "chrome_ajax": {**HEADERS, "User-Agent": chrome, "Referer": "https://understat.com/"},
        "plain": {},
    }
    for name, hdrs in variants.items():
        try:
            r = requests.get(url, headers=hdrs, timeout=30)
            out.append({"variant": name, "status": r.status_code, **describe(r.content)})
        except requests.RequestException as exc:
            out.append({"variant": name, "error": type(exc).__name__})
    for page in (f"https://understat.com/league/EPL/{years[0]}", "https://understat.com/"):
        try:
            r = requests.get(page, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
            body = r.text
            out.append(
                {
                    "page": page,
                    "status": r.status_code,
                    "bytes": len(body),
                    "has_datesData": "datesData" in body,
                    "getLeagueData": "getLeagueData" in body,
                    "head": body[:200],
                    "text": re.sub(
                        r"\s+",
                        " ",
                        re.sub(
                            r"<[^>]+>", " ", re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", body)
                        ),
                    )[:1500],
                    "tail": body[-600:],
                    "scripts": sorted(set(re.findall(r'src="([^"]+\.js[^"]*)"', body)))[:15],
                }
            )
        except requests.RequestException as exc:
            out.append({"page": page, "error": type(exc).__name__})
    return out
