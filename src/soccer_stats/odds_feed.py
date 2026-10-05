"""DraftKings odds for upcoming Premier League matches, via The Odds API (the-odds-api.com).

Needs an API key in the ODDS_API_KEY environment variable (a GitHub Actions secret in the
publish workflow). The free plan allows 500 credits a month; each refresh asks for two
markets (match result and over/under) from one bookmaker, which costs 2 credits, so the
response is cached and refreshed at most every ODDS_API_MAX_AGE_HOURS (default 4h) and
never when fewer than MIN_CREDITS_LEFT remain.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR

URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SPORTS = {"E0": "soccer_epl"}
BOOKMAKER = "draftkings"
BOOKMAKER_NAME = "DraftKings"
MIN_CREDITS_LEFT = 20

# The Odds API team name -> football-data team name, where they differ.
TEAM_NAMES = {
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Tottenham Hotspur": "Tottenham",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Wolverhampton Wanderers": "Wolves",
    "Brighton and Hove Albion": "Brighton",
    "West Ham United": "West Ham",
    "AFC Bournemouth": "Bournemouth",
    "Leeds United": "Leeds",
    "Leicester City": "Leicester",
    "Ipswich Town": "Ipswich",
    "Coventry City": "Coventry",
    "Hull City": "Hull",
    "Sheffield United": "Sheffield United",
    "Luton Town": "Luton",
    "Norwich City": "Norwich",
    "West Bromwich Albion": "West Brom",
}


@dataclass
class OddsStatus:
    bookmaker: str = BOOKMAKER_NAME
    fetched_at: str | None = None  # when the cached response was downloaded (UTC ISO)
    credits_left: int | None = None
    error: str | None = None


def _team(name: str, known: set[str] | None = None) -> str:
    name = TEAM_NAMES.get(name, name)
    if known and name not in known:
        # e.g. "Burnley FC" -> "Burnley": accept a unique prefix match.
        hits = [t for t in known if name.startswith(t + " ") or t.startswith(name + " ")]
        if len(hits) == 1:
            return hits[0]
    return name


def fetch_odds(
    league: str = "E0",
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    max_age_hours: float | None = None,
) -> tuple[list[dict] | None, OddsStatus]:
    """Cached DraftKings odds JSON for a league, plus status. Never raises.

    Returns (None, status) when no key is configured or nothing is cached yet and the
    download fails. Error messages never include the API key.
    """
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    max_age = max_age_hours or float(os.environ.get("ODDS_API_MAX_AGE_HOURS", 4))
    path = raw_dir / f"odds_api_{league}_{BOOKMAKER}.json"
    meta_path = path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    status = OddsStatus(fetched_at=meta.get("fetched_at"), credits_left=meta.get("credits_left"))

    if not api_key:
        status.error = "no ODDS_API_KEY configured"
        return None, status

    fresh = path.exists() and time.time() - path.stat().st_mtime < max_age * 3600
    # Credits reset monthly, so a low count only pauses fetching within the same month.
    this_month = pd.Timestamp.now(tz="UTC").strftime("%Y-%m")
    low = (
        status.credits_left is not None
        and status.credits_left < MIN_CREDITS_LEFT
        and (status.fetched_at or "")[:7] == this_month
    )
    if not fresh and not low:
        try:
            resp = requests.get(
                URL.format(sport=SPORTS[league]),
                params={
                    "apiKey": api_key,
                    "bookmakers": BOOKMAKER,
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
                timeout=30,
            )
        except requests.RequestException as exc:  # message could contain the URL
            status.error = f"could not reach The Odds API ({type(exc).__name__})"
        else:
            if resp.ok:
                raw_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(resp.text)
                remaining = resp.headers.get("x-requests-remaining")
                meta = {
                    "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
                    "credits_left": int(float(remaining)) if remaining else None,
                }
                meta_path.write_text(json.dumps(meta))
                status.fetched_at, status.credits_left = meta["fetched_at"], meta["credits_left"]
            else:
                status.error = f"The Odds API returned HTTP {resp.status_code}"
    elif low and not fresh:
        status.error = f"paused: only {status.credits_left} API credits left this month"

    if not path.exists():
        return None, status
    return json.loads(path.read_text()), status


def parse_odds(events: list[dict], known_teams: set[str] | None = None) -> pd.DataFrame:
    """The Odds API events -> one row per match with DraftKings decimal odds."""
    rows = []
    for ev in events:
        book = next((b for b in ev.get("bookmakers", []) if b.get("key") == BOOKMAKER), None)
        if not book:
            continue
        home, away = _team(ev["home_team"], known_teams), _team(ev["away_team"], known_teams)
        row = {
            "kickoff": pd.Timestamp(ev["commence_time"]).tz_convert("UTC"),
            "home": home,
            "away": away,
            "odds_updated": book.get("last_update"),
        }
        for market in book.get("markets", []):
            if market["key"] == "h2h":
                for o in market["outcomes"]:
                    name = _team(o["name"], known_teams)
                    if name == home:
                        row["odds_home"] = o["price"]
                    elif name == away:
                        row["odds_away"] = o["price"]
                    elif o["name"].lower() == "draw":
                        row["odds_draw"] = o["price"]
            elif market["key"] == "totals":
                for o in market["outcomes"]:
                    if o.get("point") == 2.5:
                        row["odds_over25" if o["name"] == "Over" else "odds_under25"] = o["price"]
        rows.append(row)
    cols = [
        "kickoff",
        "home",
        "away",
        "odds_updated",
        "odds_home",
        "odds_draw",
        "odds_away",
        "odds_over25",
        "odds_under25",
    ]
    return pd.DataFrame(rows).reindex(columns=cols)


def apply_odds(fixtures: pd.DataFrame, odds: pd.DataFrame) -> pd.DataFrame:
    """Replace fixture odds with DraftKings odds; matches DraftKings hasn't priced get none."""
    out = fixtures.copy()
    cols = ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]
    keyed = odds.drop_duplicates(["home", "away"]).set_index(["home", "away"])
    for col in cols:
        out[col] = [
            keyed[col].get((h, a), float("nan")) if not keyed.empty else float("nan")
            for h, a in zip(out["home"], out["away"], strict=True)
        ]
    out["odds_updated"] = [
        keyed["odds_updated"].get((h, a)) if not keyed.empty else None
        for h, a in zip(out["home"], out["away"], strict=True)
    ]
    return out
