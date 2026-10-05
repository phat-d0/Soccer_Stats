"""Load historical results and odds from football-data.co.uk.

Each season/league CSV has results plus odds from several bookmakers. The ones we
care most about are Pinnacle opening (PSH/PSD/PSA) and closing (PSCH/PSCD/PSCA)
prices: Pinnacle's closing line is the sharpest public benchmark, so beating it
is the best evidence of a real edge.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
RAW_DIR = Path(
    os.environ.get("SOCCER_STATS_DATA_DIR", Path(__file__).resolve().parents[2] / "data" / "raw")
)

LEAGUES = {
    "E0": "England Premier League",
    "E1": "England Championship",
    "SP1": "Spain La Liga",
    "D1": "Germany Bundesliga",
    "I1": "Italy Serie A",
    "F1": "France Ligue 1",
    "N1": "Netherlands Eredivisie",
    "P1": "Portugal Primeira Liga",
}

# Source column -> our column. Missing source columns are filled with NaN.
COLUMNS = {
    "Date": "date",
    "HomeTeam": "home",
    "AwayTeam": "away",
    "FTHG": "home_goals",
    "FTAG": "away_goals",
    "PSH": "odds_home",
    "PSD": "odds_draw",
    "PSA": "odds_away",
    "PSCH": "close_home",
    "PSCD": "close_draw",
    "PSCA": "close_away",
    "AvgH": "avg_home",
    "AvgD": "avg_draw",
    "AvgA": "avg_away",
    "MaxH": "max_home",
    "MaxD": "max_draw",
    "MaxA": "max_away",
    "P>2.5": "odds_over25",
    "P<2.5": "odds_under25",
    "PC>2.5": "close_over25",
    "PC<2.5": "close_under25",
}


def season_code(start_year: int) -> str:
    """2023 -> '2324'."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_season(today: date | None = None) -> int:
    """Start year of the season in progress (seasons roll over in July)."""
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def _fetch(
    url: str,
    path: Path,
    max_age_hours: float | None,
    headers: dict | None = None,
    stale_ok: bool = False,
) -> Path:
    """Download `url` to `path` unless a cached copy is fresh enough.

    max_age_hours=None means a cached file never expires (finished seasons). With
    stale_ok, a failed refresh falls back to the cached copy instead of raising.
    """
    if path.exists() and (
        max_age_hours is None or time.time() - path.stat().st_mtime < max_age_hours * 3600
    ):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        if stale_ok and path.exists():  # source down: a stale copy beats nothing
            return path
        raise
    path.write_bytes(resp.content)
    return path


def download(
    league: str,
    start_year: int,
    raw_dir: Path = RAW_DIR,
    force: bool = False,
    max_age_hours: float = 12,
) -> Path:
    """Download one league-season CSV into the raw cache, returning its path.

    The in-progress season is re-downloaded once the cache is `max_age_hours` old;
    past seasons are cached forever.
    """
    code = season_code(start_year)
    live = start_year >= current_season()
    age = 0 if force else (max_age_hours if live else None)
    return _fetch(
        BASE_URL.format(season=code, league=league),
        raw_dir / f"{league}_{code}.csv",
        age,
        stale_ok=True,  # results can't go stale in a harmful way: a refit just lags
    )


# Odds columns fall back to the market average, then Bet365, when a file has no
# Pinnacle price (e.g. fixtures posted before Pinnacle's line is in the file).
FALLBACKS = {
    "odds_home": ["AvgH", "B365H"],
    "odds_draw": ["AvgD", "B365D"],
    "odds_away": ["AvgA", "B365A"],
    "close_home": ["AvgCH", "B365CH"],
    "close_draw": ["AvgCD", "B365CD"],
    "close_away": ["AvgCA", "B365CA"],
    "odds_over25": ["Avg>2.5", "B365>2.5"],
    "odds_under25": ["Avg<2.5", "B365<2.5"],
    "close_over25": ["AvgC>2.5", "B365C>2.5"],
    "close_under25": ["AvgC<2.5", "B365C<2.5"],
}


def _extract(raw: pd.DataFrame) -> pd.DataFrame:
    """Pick our columns out of a football-data frame in one go (missing -> NaN)."""
    nan = pd.Series(float("nan"), index=raw.index)
    cols = {}
    for src, dst in COLUMNS.items():
        col = raw[src] if src in raw.columns else nan
        for alt in FALLBACKS.get(dst, []):
            if alt in raw.columns:
                col = col.fillna(raw[alt]) if col is not nan else raw[alt]
        cols[dst] = col
    return pd.DataFrame(cols, index=raw.index)


def load_fixtures(
    leagues: Iterable[str], raw_dir: Path = RAW_DIR, max_age_hours: float = 6
) -> pd.DataFrame:
    """Upcoming fixtures (with current odds where posted) for the given leagues."""
    path = _fetch(FIXTURES_URL, raw_dir / "fixtures.csv", max_age_hours)
    raw = pd.read_csv(path, encoding="utf-8-sig", on_bad_lines="skip")
    raw = raw[raw["Div"].isin(list(leagues))]
    df = _extract(raw)
    df["league"] = raw["Div"]
    df["kickoff"] = pd.to_datetime(
        raw["Date"] + " " + raw.get("Time", pd.Series("00:00", index=raw.index)).fillna("00:00"),
        dayfirst=True,
        format="mixed",
    )
    keep = [
        "league",
        "kickoff",
        *(c for c in COLUMNS.values() if c not in ("home_goals", "away_goals")),
    ]
    return df[keep].drop(columns="date").sort_values("kickoff").reset_index(drop=True)


def normalize(
    raw: pd.DataFrame, league: str | None = None, season: str | None = None
) -> pd.DataFrame:
    """Rename and type the columns we use; drop unplayed/blank rows."""
    df = _extract(raw).dropna(subset=["home", "away", "home_goals", "away_goals"])
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, format="mixed")
    df[["home_goals", "away_goals"]] = df[["home_goals", "away_goals"]].astype(int)
    df.insert(0, "league", league)
    df.insert(1, "season", season)
    return df.sort_values("date").reset_index(drop=True)


def load_csv(path: Path, league: str | None = None, season: str | None = None) -> pd.DataFrame:
    # Some older files have trailing junk columns / latin-1 characters.
    raw = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
    return normalize(raw, league=league, season=season)


def load_matches(
    leagues: Iterable[str], start_years: Iterable[int], raw_dir: Path = RAW_DIR
) -> pd.DataFrame:
    """Download (if needed) and concatenate matches for the given leagues and seasons."""
    frames = []
    for league in leagues:
        for year in start_years:
            path = download(league, year, raw_dir=raw_dir)
            frames.append(load_csv(path, league=league, season=season_code(year)))
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)
