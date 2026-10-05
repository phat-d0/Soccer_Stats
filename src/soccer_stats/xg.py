"""Per-match expected goals (xG) from Understat, merged onto football-data matches.

Understat covers EPL, La Liga, Bundesliga, Serie A, Ligue 1 and RFPL from 2014/15.
xG measures the quality of chances each side created, so it is a less noisy signal of
team strength than goals: a team that wins 1-0 while conceding 2.5 xG got lucky.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from soccer_stats.data import RAW_DIR, _fetch, current_season

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
    # Italy
    "AC Milan": "Milan",
    "Parma Calcio 1913": "Parma",
    "SPAL 2013": "Spal",
    # France
    "Paris Saint Germain": "Paris SG",
    "Saint-Etienne": "St Etienne",
    "Clermont Foot": "Clermont",
}


def fetch_season(league: str, start_year: int, raw_dir: Path = RAW_DIR) -> Path:
    """Download (and cache) Understat's league-season JSON. Live season refreshes every 12h."""
    us_league = LEAGUES[league]
    return _fetch(
        URL.format(league=us_league, season=start_year),
        raw_dir / f"understat_{us_league}_{start_year}.json",
        max_age_hours=12 if start_year >= current_season() else None,
        headers=HEADERS,
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

    Returns (matches with home_xg/away_xg, error message or None). On failure the xG
    columns are all NaN, so models silently fall back to goals.
    """
    leagues = [lg for lg in matches["league"].dropna().unique() if lg in LEAGUES]
    years = sorted({2000 + int(s[:2]) for s in matches["season"].dropna().unique()})
    try:
        xg = load_xg(leagues, years, raw_dir)
    except Exception as exc:  # network down, site changed, etc.
        return attach_xg(matches, pd.DataFrame()), f"Could not load xG from Understat: {exc}"
    return attach_xg(matches, xg), None
