"""DraftKings odds for upcoming Premier League matches, via The Odds API (the-odds-api.com).

Needs an API key in the ODDS_API_KEY environment variable (a GitHub Actions secret in the
publish workflow). The free plan allows 500 credits a month; a refresh costs a couple of
credits (two markets, one bookmaker). Phones never call the API: only the publish job
does, and it budgets refreshes so the free allowance always lasts the month:

* After each download we record the credits left and what that call cost (the API's
  x-requests-remaining / x-requests-last headers).
* The refresh interval is the time until the allowance resets divided by the number of
  refreshes still affordable (keeping RESERVE_CREDITS spare), but never more often than
  hourly, or every 30 minutes in the two hours before a kickoff. On the free plan that's
  every 2-3 hours; a paid plan's larger allowance brings it down to the floor. It
  stretches automatically if credits run lower than planned.
* Below the reserve, fetching stops until the reset date, apart from one check a day in
  case the allowance has reset early. The reset day comes from ODDS_API_RESET_DAY
  (default the 1st), and is learned automatically the first time a download shows more
  credits than the previous one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR

URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SPORTS = {"E0": "soccer_epl"}
BOOKMAKER = "draftkings"
BOOKMAKER_NAME = "DraftKings"
RESERVE_CREDITS = 20  # never spend below this
MIN_INTERVAL_HOURS = 1.0  # never refresh more often than this...
KICKOFF_INTERVAL_HOURS = 0.5  # ...except this close to a kickoff
KICKOFF_WINDOW_HOURS = 2.0
DEFAULT_COST = 2  # credits per refresh until the API tells us

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
    last_cost: int | None = None
    refresh_hours: float | None = None  # current budgeted refresh interval
    error: str | None = None


def _team(name: str, known: set[str] | None = None) -> str:
    name = TEAM_NAMES.get(name, name)
    if known and name not in known:
        # e.g. "Burnley FC" -> "Burnley": accept a unique prefix match.
        hits = [t for t in known if name.startswith(t + " ") or t.startswith(name + " ")]
        if len(hits) == 1:
            return hits[0]
    return name


def next_reset(now: pd.Timestamp, reset_day: int = 1) -> pd.Timestamp:
    """Next time the monthly allowance resets (midnight UTC on `reset_day`)."""
    this = now.normalize().replace(day=min(reset_day, 28))
    return this if this > now else (this + pd.offsets.MonthBegin(1)).replace(day=min(reset_day, 28))


def refresh_interval_hours(
    credits_left: int | None,
    cost: int | None,
    now: pd.Timestamp,
    reset_day: int = 1,
    min_hours: float = MIN_INTERVAL_HOURS,
) -> float:
    """Hours between refreshes so the remaining credits last until the next reset.

    Returns inf when nothing more can be spent this period.
    """
    if credits_left is None:
        return min_hours  # unknown budget: fetch once to find out
    affordable = (credits_left - RESERVE_CREDITS) // max(cost or DEFAULT_COST, 1)
    if affordable <= 0:
        return float("inf")
    hours_left = (next_reset(now, reset_day) - now) / pd.Timedelta(hours=1)
    return max(min_hours, hours_left / affordable)


def floor_hours(events: list[dict] | None, now: pd.Timestamp) -> float:
    """Minimum refresh interval: shorter when a cached match kicks off within the window."""
    for ev in events or []:
        try:
            k = pd.Timestamp(ev["commence_time"]).tz_convert("UTC")
        except (KeyError, ValueError, TypeError):
            continue
        if pd.Timedelta(0) <= k - now <= pd.Timedelta(hours=KICKOFF_WINDOW_HOURS):
            return KICKOFF_INTERVAL_HOURS
    return MIN_INTERVAL_HOURS


def fetch_odds(
    league: str = "E0",
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
) -> tuple[list[dict] | None, OddsStatus]:
    """Cached DraftKings odds JSON for a league, plus status. Never raises.

    Downloads only when the budgeted refresh interval has passed (see module docstring).
    Returns (None, status) when no key is configured or nothing is cached yet and the
    download fails. Error messages never include the API key.
    """
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    reset_day = int(os.environ.get("ODDS_API_RESET_DAY", 1))
    now = now or pd.Timestamp.now(tz="UTC")
    path = raw_dir / f"odds_api_{league}_{BOOKMAKER}.json"
    meta_path = path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    reset_day = int(meta.get("reset_day") or reset_day)  # learned beats configured
    status = OddsStatus(
        fetched_at=meta.get("fetched_at"),
        credits_left=meta.get("credits_left"),
        last_cost=meta.get("last_cost"),
    )

    if not api_key:
        status.error = "no ODDS_API_KEY configured"
        return None, status

    last = pd.Timestamp(status.fetched_at) if status.fetched_at and path.exists() else None
    credits = status.credits_left
    if last is not None and last < next_reset(now, reset_day) - pd.DateOffset(months=1):
        credits = None  # the allowance has reset since our last download
    cached = json.loads(path.read_text()) if path.exists() else None
    floor = floor_hours(cached, now)
    interval = refresh_interval_hours(credits, status.last_cost, now, reset_day, floor)
    status.refresh_hours = None if interval == float("inf") else round(interval, 2)
    hours_since = (now - last) / pd.Timedelta(hours=1) if last is not None else None
    # When paused, still check once a day in case the allowance reset on another day.
    due = (
        last is None or hours_since >= interval or (interval == float("inf") and hours_since >= 24)
    )

    if due:
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
                cost = resp.headers.get("x-requests-last")
                learned = meta.get("reset_day")
                old_left = meta.get("credits_left")
                new_left = int(float(remaining)) if remaining else None
                if old_left is not None and new_left is not None and new_left > old_left:
                    learned = now.day  # credits went up: the allowance reset since last time
                meta = {
                    "fetched_at": now.isoformat(timespec="seconds"),
                    "credits_left": new_left,
                    "last_cost": int(float(cost)) if cost else None,
                    "reset_day": learned,
                }
                reset_day = int(learned or reset_day)
                meta_path.write_text(json.dumps(meta))
                status.fetched_at = meta["fetched_at"]
                status.credits_left, status.last_cost = meta["credits_left"], meta["last_cost"]
                interval = refresh_interval_hours(
                    status.credits_left, status.last_cost, now, reset_day, floor
                )
                status.refresh_hours = None if interval == float("inf") else round(interval, 2)
            else:
                status.error = f"The Odds API returned HTTP {resp.status_code}"
    elif interval == float("inf"):
        status.error = (
            f"paused until the allowance resets: {status.credits_left} credits left "
            f"(keeping {RESERVE_CREDITS} in reserve)"
        )

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
