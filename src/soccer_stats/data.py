"""Load historical results and odds from football-data.co.uk.

Each season/league CSV has results plus odds from several bookmakers. The ones we
care most about are Pinnacle opening (PSH/PSD/PSA) and closing (PSCH/PSCD/PSCA)
prices: Pinnacle's closing line is the sharpest public benchmark, so beating it
is the best evidence of a real edge.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

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


def download(league: str, start_year: int, raw_dir: Path = RAW_DIR, force: bool = False) -> Path:
    """Download one league-season CSV into the raw cache, returning its path."""
    code = season_code(start_year)
    path = raw_dir / f"{league}_{code}.csv"
    if path.exists() and not force:
        return path
    raw_dir.mkdir(parents=True, exist_ok=True)
    resp = requests.get(BASE_URL.format(season=code, league=league), timeout=30)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def normalize(
    raw: pd.DataFrame, league: str | None = None, season: str | None = None
) -> pd.DataFrame:
    """Rename and type the columns we use; drop unplayed/blank rows."""
    df = raw.rename(columns=COLUMNS)
    for col in COLUMNS.values():
        if col not in df.columns:
            df[col] = float("nan")
    df = df[list(COLUMNS.values())].dropna(subset=["home", "away", "home_goals", "away_goals"])
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
