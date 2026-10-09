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

Other leagues (leagues.LEAGUES) are fetched only when their `live` flag is on, and then by
the "matchday" policy (policy_floor): only with a match within MATCHDAY_HOURS, every
MATCHDAY_FAR_HOURS until MATCHDAY_NEAR_WINDOW hours before a kickoff, then hourly, then
every 30 minutes in the last two hours. The Premier League keeps its "always" policy.
estimate_credits replays these rules over a fixture calendar to cost a league per month.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.leagues import LEAGUES

URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
SPORTS = {code: lg.odds_sport for code, lg in LEAGUES.items()}
BOOKMAKER = "draftkings"
BOOKMAKER_NAME = "DraftKings"
RESERVE_CREDITS = 20  # never spend below this
# "matchday" leagues stop below this many credits on the shared key (the same reserve the
# player-odds fetches keep), so they pause long before the Premier League or baseball app.
MATCHDAY_RESERVE_CREDITS = 3000
MIN_INTERVAL_HOURS = 1.0  # never refresh more often than this...
KICKOFF_INTERVAL_HOURS = 0.5  # ...except this close to a kickoff
KICKOFF_WINDOW_HOURS = 2.0
DEFAULT_COST = 2  # credits per refresh until the API tells us
MATCHDAY_HOURS = 48.0  # "matchday" leagues: fetch only with a kickoff this close
MATCHDAY_NEAR_WINDOW = 6.0  # ...hourly inside this many hours of it...
MATCHDAY_FAR_HOURS = 3.0  # ...and every this many hours before that

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
    # Spain (DraftKings spellings seen on the first six-league publish, 8 Oct 2026)
    "Athletic Bilbao": "Ath Bilbao",
    "Atlético Madrid": "Ath Madrid",
    "Atletico Madrid": "Ath Madrid",
    "Alavés": "Alaves",
    "CA Osasuna": "Osasuna",
    "Deportivo La Coruña": "La Coruna",
    "Espanyol": "Espanol",
    "Málaga": "Malaga",
    "Rayo Vallecano": "Vallecano",
    "Real Betis": "Betis",
    "Real Racing Club de Santander": "Racing Santander",
    "Real Sociedad": "Sociedad",
    # Germany
    "1. FC Köln": "FC Koln",
    "Bayer Leverkusen": "Leverkusen",
    "Borussia Dortmund": "Dortmund",
    "Borussia Monchengladbach": "M'gladbach",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "FC Schalke 04": "Schalke 04",
    "FSV Mainz 05": "Mainz",
    "Hamburger SV": "Hamburg",
    "SC Freiburg": "Freiburg",
    "SC Paderborn": "Paderborn",
    "TSG Hoffenheim": "Hoffenheim",
    "VfB Stuttgart": "Stuttgart",
    # Italy
    "AC Milan": "Milan",
    "AS Roma": "Roma",
    # France
    "AS Monaco": "Monaco",
    "Paris Saint Germain": "Paris SG",
    "RC Lens": "Lens",
}


@dataclass
class OddsStatus:
    bookmaker: str = BOOKMAKER_NAME
    fetched_at: str | None = None  # when the cached response was downloaded (UTC ISO)
    credits_left: int | None = None
    last_cost: int | None = None
    refresh_hours: float | None = None  # current budgeted refresh interval
    error: str | None = None


def _plain(name: str) -> str:
    """Lower case without accents: "Atlético" -> "atletico"."""
    text = unicodedata.normalize("NFKD", name)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


def _team(name: str, known: set[str] | None = None) -> str:
    name = TEAM_NAMES.get(name, name)
    if known and name not in known:
        plain = _plain(name)
        same = [t for t in known if _plain(t) == plain]
        if len(same) == 1:  # e.g. "Alavés" -> "Alaves"
            return same[0]
        # e.g. "Burnley FC" -> "Burnley", "AS Roma" -> "Roma": accept a unique
        # prefix or suffix match on whole words.
        hits = [
            t
            for t in known
            if plain.startswith(_plain(t) + " ")
            or _plain(t).startswith(plain + " ")
            or plain.endswith(" " + _plain(t))
        ]
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
    share: int = 1,
    reserve: int = RESERVE_CREDITS,
) -> float:
    """Hours between refreshes so the remaining credits last until the next reset.

    `share` is how many live leagues split the credits (each gets an equal part); the
    Premier League always budgets with share 1, so other leagues never slow it down.
    `reserve` is the balance never spent below. Returns inf when nothing more can be
    spent this period.
    """
    if credits_left is None:
        return min_hours  # unknown budget: fetch once to find out
    per_call = max(cost or DEFAULT_COST, 1)
    affordable = (credits_left - reserve) // per_call // max(share, 1)
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


def _kickoffs(events: list[dict] | None) -> list[pd.Timestamp]:
    out = []
    for ev in events or []:
        try:
            out.append(pd.Timestamp(ev["commence_time"]).tz_convert("UTC"))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def policy_floor(league: str, kickoffs, now: pd.Timestamp) -> float | None:
    """Minimum hours between refreshes for a league's policy; None = don't fetch now.

    "always" (the Premier League): as floor_hours. "matchday": nothing unless a kickoff
    is within MATCHDAY_HOURS; then every MATCHDAY_FAR_HOURS, hourly within
    MATCHDAY_NEAR_WINDOW hours, and every 30 minutes within KICKOFF_WINDOW_HOURS.
    """
    ahead = [k - now for k in kickoffs if k >= now]
    lg = LEAGUES.get(league)
    if lg is None or lg.odds_policy == "always":
        near = any(d <= pd.Timedelta(hours=KICKOFF_WINDOW_HOURS) for d in ahead)
        return KICKOFF_INTERVAL_HOURS if near else MIN_INTERVAL_HOURS
    soonest = min(ahead, default=None)
    if soonest is None or soonest > pd.Timedelta(hours=MATCHDAY_HOURS):
        return None
    if soonest <= pd.Timedelta(hours=KICKOFF_WINDOW_HOURS):
        return KICKOFF_INTERVAL_HOURS
    if soonest <= pd.Timedelta(hours=MATCHDAY_NEAR_WINDOW):
        return MIN_INTERVAL_HOURS
    return MATCHDAY_FAR_HOURS


def latest_meta(raw_dir: Path) -> dict:
    """The most recent download's meta across every league's odds cache (empty if none).

    The key is shared, so the freshest credits_left is the best guess at the balance."""
    best: dict = {}
    for path in Path(raw_dir).glob(f"odds_api_*_{BOOKMAKER}.meta.json"):
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if meta.get("fetched_at") and meta.get("fetched_at") > best.get("fetched_at", ""):
            best = meta
    return best


def fetch_odds(
    league: str = "E0",
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    kickoffs=None,
    share: int = 1,
) -> tuple[list[dict] | None, OddsStatus]:
    """Cached DraftKings odds JSON for a league, plus status. Never raises.

    Downloads only when the budgeted refresh interval has passed (see module docstring).
    Returns (None, status) when no key is configured or nothing is cached yet and the
    download fails. Error messages never include the API key.

    A league whose `live` flag is off is never fetched. `kickoffs` (the league's upcoming
    kickoffs from the schedule) let a "matchday" league know a match is near before any
    odds are cached; `share` splits the shared credit budget between the live leagues.
    A "matchday" league budgets from the freshest balance any league has seen and keeps
    MATCHDAY_RESERVE_CREDITS spare, so it pauses first when credits run low.
    """
    lg = LEAGUES.get(league)
    if lg is not None and not lg.live:
        return None, OddsStatus(error=f"{lg.name} odds are off (league not live)")
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
    reserve = RESERVE_CREDITS
    seen = last
    if lg is not None and lg.odds_policy != "always":
        reserve = MATCHDAY_RESERVE_CREDITS
        fresh = latest_meta(raw_dir)
        if fresh and (seen is None or pd.Timestamp(fresh["fetched_at"]) > seen):
            seen, credits = pd.Timestamp(fresh["fetched_at"]), fresh.get("credits_left")
    if seen is not None and seen < next_reset(now, reset_day) - pd.DateOffset(months=1):
        credits = None  # the allowance has reset since the last download we know of
    cached = json.loads(path.read_text()) if path.exists() else None
    floor = policy_floor(league, _kickoffs(cached) + list(kickoffs or []), now)
    if floor is None:  # a "matchday" league with no match soon: keep what's cached
        status.error = f"no {lg.name if lg else league} match within {MATCHDAY_HOURS:g} hours"
        return cached, status
    interval = refresh_interval_hours(
        credits, status.last_cost, now, reset_day, floor, share, reserve
    )
    status.refresh_hours = None if interval == float("inf") else round(interval, 2)
    hours_since = (now - last) / pd.Timedelta(hours=1) if last is not None else None
    # When paused, the Premier League still checks once a day in case the allowance reset
    # on another day; a matchday league waits for its balance to come back in a fresher meta.
    due = last is None or hours_since >= interval
    due = due or (reserve == RESERVE_CREDITS and interval == float("inf") and hours_since >= 24)

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
                    status.credits_left, status.last_cost, now, reset_day, floor, share, reserve
                )
                status.refresh_hours = None if interval == float("inf") else round(interval, 2)
            else:
                status.error = f"The Odds API returned HTTP {resp.status_code}"
    elif interval == float("inf"):
        status.error = (
            f"paused until the allowance resets: {credits} credits left "
            f"(keeping {reserve} in reserve)"
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


# ---------- credit budget estimate ----------

# Scheduled publish runs (publish.yml): every hour at :07, plus :22, :37 and :52 from 10:00
# to 21:59 UTC. GitHub delays or drops some scheduled runs, so the estimate is an upper
# bound for the refresh rules alone.
RUN_MINUTES_ALWAYS = (7,)
RUN_MINUTES_DAYTIME = (22, 37, 52)
DAYTIME_HOURS = range(10, 22)


def publish_runs(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """The scheduled publish times in [start, end)."""
    out = []
    t = start.floor("h")
    while t < end:
        mins = RUN_MINUTES_ALWAYS + (RUN_MINUTES_DAYTIME if t.hour in DAYTIME_HOURS else ())
        out += [t + pd.Timedelta(minutes=m) for m in mins]
        t += pd.Timedelta(hours=1)
    return [r for r in out if start <= r < end]


def estimate_credits(
    league: str,
    kickoffs,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost: int = DEFAULT_COST,
) -> dict:
    """Odds API calls and credits one league would use between start and end.

    Replays the scheduled publish runs against the league's refresh policy
    (policy_floor) with an unlimited budget, so it is what the rules alone would spend:
    each refresh is one call for h2h and totals from one bookmaker (`cost` credits).
    The `live` flag is ignored: this prices switching a league on.
    """
    ks = sorted(pd.Timestamp(k).tz_convert("UTC") for k in kickoffs)
    last = None
    calls = 0
    for t in publish_runs(start, end):
        floor = policy_floor(league, ks, t)
        if floor is None:
            continue
        if last is None or (t - last) >= pd.Timedelta(hours=floor):
            calls += 1
            last = t
    return {
        "league": league,
        "matches": sum(start <= k < end for k in ks),
        "calls": calls,
        "credits": calls * cost,
        "days": round((end - start) / pd.Timedelta(days=1), 1),
    }


# ---------- team-total snapshots: a cost estimate only (docs/totals.md) ----------

# FanDuel quotes goal team totals in the top five leagues (and Bovada in E0, same "us"
# region); the Championship has none on The Odds API (market probe, run 37889595473).
TEAM_TOTAL_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
# Snapshots per match besides the close, as hours before kickoff.
SNAPSHOT_PLANS = {"lean": (), "base": (24.0,), "rich": (24.0, 6.0)}
CLOSE_WINDOW_MINUTES = 30  # a close quoted earlier than this is still logged, but counted
EVENT_REGIONS = 1  # FanDuel and Bovada are both "us": up to ten books cost one region


def snapshot_runs(kickoff: pd.Timestamp, offsets, runs: list[pd.Timestamp]) -> dict:
    """The publish runs that would take a match's snapshots: for each offset (hours
    before kickoff) the first run at or after that time, and for the close the last run
    before kickoff. Runs taken by two snapshots are one call."""
    before = [t for t in runs if t < kickoff]
    out: dict = {}
    for h in offsets:
        due = kickoff - pd.Timedelta(hours=h)
        t = next((t for t in before if t >= due), None)
        if t is not None:
            out[f"{h:g}h"] = t
    if before:
        out["close"] = before[-1]
    return out


def estimate_snapshot_credits(
    league: str,
    kickoffs,
    start: pd.Timestamp,
    end: pd.Timestamp,
    plan: str = "base",
    markets: int = 1,
) -> dict:
    """Credits to snapshot every match kicking off in [start, end) under a snapshot plan.

    One `/events/{id}/odds` call per match per snapshot; each costs `markets` ×
    EVENT_REGIONS credits (`x-requests-last` in the market probe: 1 per market per region;
    the events list that gives the ids is free). Runs follow the publish schedule
    (publish_runs), so the close is the last scheduled run before kickoff; GitHub's
    throttling of scheduled runs makes real closes later and real spend lower.
    """
    ks = sorted(pd.Timestamp(k).tz_convert("UTC") for k in kickoffs)
    month = [k for k in ks if start <= k < end]
    offsets = SNAPSHOT_PLANS[plan]
    pad = pd.Timedelta(hours=max(offsets, default=0) + 2)
    runs = publish_runs(start - pad, end)
    calls, in_window = 0, 0
    for k in month:
        taken = snapshot_runs(k, offsets, runs)
        calls += len(set(taken.values()))
        close = taken.get("close")
        if close is not None and k - close <= pd.Timedelta(minutes=CLOSE_WINDOW_MINUTES):
            in_window += 1
    return {
        "league": league,
        "plan": plan,
        "markets": markets,
        "matches": len(month),
        "calls": calls,
        "credits": calls * markets * EVENT_REGIONS,
        "close_in_window": in_window,
    }


def matches_by(kickoffs, start: pd.Timestamp, weeks: float) -> int:
    """Matches kicking off in the `weeks` from start (how fast priced matches build up)."""
    end = start + pd.Timedelta(weeks=weeks)
    return sum(start <= pd.Timestamp(k).tz_convert("UTC") < end for k in kickoffs)
