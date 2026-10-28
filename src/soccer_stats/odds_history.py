"""Historical DraftKings odds from The Odds API, for the DraftKings backtest.

    soccer-stats backfill-odds --seasons 2025 --dry-run
    soccer-stats backfill-odds --seasons 2025 --max-credits 5000

The historical endpoint (paid plans only) returns the closest snapshot at or before a
requested time, covering every listed match. For each kickoff we need three snapshots:
the two looks (48 and 3 hours before kickoff by default) and the close (just before
kickoff). Kickoffs within 15 minutes of each other share snapshots.

Safety rules:
* --dry-run prints the plan and its cost without calling the API.
* Stops before --max-credits would be passed, and before credits left would fall
  below --keep-credits (so a backfill can't starve the live refreshes).
* Each snapshot is saved to disk under its requested timestamp and never fetched twice.
* The key is read from ODDS_API_KEY only and never appears in output or errors.
* Run by hand only (locally, or the "Backfill DraftKings odds" workflow), never in
  the scheduled build.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.odds_feed import BOOKMAKER, BOOKMAKERS, SPORTS, parse_odds
from soccer_stats.trades import season_label

URL = "https://api.the-odds-api.com/v4/historical/sports/{sport}/odds"
LOOKS_HOURS = (48.0, 3.0)  # backtest look times, hours before kickoff
CLOSE_MINUTES = 1  # the close: the last snapshot this long before kickoff
GROUP_MINUTES = 15  # kickoffs this close together share snapshots
COST_PER_SNAPSHOT = 20  # 10 credits per region per market; two markets
KEEP_CREDITS = 1500  # leave about a month of hourly live refreshes untouched
# The multi-league backfill (lead brief 2, 28 Oct; docs/leagues.md "DraftKings history"):
# h2h only at the live call's ten books (one region, 10 credits a snapshot), a 48 h look
# plus the close, and a guard per league-season: when its first GUARD_FIRST snapshots in
# time order (cached or bought) hold no DraftKings price, the rest of that season is
# skipped, as in comeback design C.
TEN_BOOKS = BOOKMAKERS
TEN_BOOKS_COST = 10
GUARD_FIRST = 3
STALE_HOURS = 6.0  # a price last updated longer than this before a look is missing


def history_dir(league: str, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / "odds_history" / league


def snapshot_path(league: str, at: pd.Timestamp, raw_dir: Path = RAW_DIR) -> Path:
    return history_dir(league, raw_dir) / f"{pd.Timestamp(at).strftime('%Y%m%dT%H%MZ')}.json"


def plan_snapshots(
    kickoffs: Iterable[pd.Timestamp],
    looks_hours: Iterable[float] = LOOKS_HOURS,
    group_minutes: int = GROUP_MINUTES,
) -> pd.DataFrame:
    """Snapshot request times for a set of kickoffs.

    Returns one row per (kickoff, kind) with the request time `at`; kind is
    "look48" / "look3" / ... or "close". Kickoffs within `group_minutes` of the first
    kickoff of their group use that first kickoff's times, which are earlier and so
    never look ahead.
    """
    ks = sorted({pd.Timestamp(k).tz_convert("UTC") for k in kickoffs})
    rows, anchor = [], None
    for k in ks:
        if anchor is None or k - anchor > pd.Timedelta(minutes=group_minutes):
            anchor = k
        for h in looks_hours:
            at = (anchor - pd.Timedelta(hours=h)).floor("5min")
            rows.append({"kickoff": k, "kind": f"look{h:g}", "at": at})
        at = (anchor - pd.Timedelta(minutes=CLOSE_MINUTES)).floor("5min")
        rows.append({"kickoff": k, "kind": "close", "at": at})
    return pd.DataFrame(rows, columns=["kickoff", "kind", "at"])


@dataclass
class BackfillReport:
    planned: int = 0  # distinct snapshots needed
    cached: int = 0  # already on disk
    fetched: int = 0  # downloaded this run
    credits_used: int = 0
    credits_left: int | None = None
    estimated_credits: int = 0  # for the snapshots still missing
    stopped: str | None = None  # why it stopped early, if it did
    errors: list[str] = field(default_factory=list)
    # per season (football-data code, e.g. "2425"): snapshots seen, events in them, and
    # events DraftKings / Pinnacle priced; filled for every snapshot on disk after the run
    by_season: dict[str, dict] = field(default_factory=dict)
    guarded: dict[str, str] = field(default_factory=dict)  # season -> why it was skipped
    skipped: int = 0  # snapshots not bought because their season was guarded

    def lines(self) -> list[str]:
        out = [
            f"Snapshots needed: {self.planned} ({self.cached} cached, "
            f"{self.planned - self.cached} to fetch, about "
            f"{self.estimated_credits} credits)",
            f"Fetched {self.fetched}, credits used {self.credits_used}"
            + (f", credits left {self.credits_left}" if self.credits_left is not None else ""),
        ]
        for season, b in sorted(self.by_season.items()):
            ev = b["events"] or 1
            out.append(
                f"  {season[:2]}/{season[2:]}: {b['snapshots']} snapshots, {b['events']} events, "
                f"DraftKings {b['draftkings']} ({b['draftkings'] / ev:.0%}), "
                f"Pinnacle {b['pinnacle']} ({b['pinnacle'] / ev:.0%})"
                + (f"; guarded: {self.guarded[season]}" if season in self.guarded else "")
            )
        if self.skipped:
            out.append(f"Skipped {self.skipped} snapshots in guarded seasons")
        if self.stopped:
            out.append(f"Stopped: {self.stopped}")
        out += [f"Error: {e}" for e in self.errors[:5]]
        return out

    def to_json(self) -> dict:
        return {
            "planned": self.planned,
            "cached": self.cached,
            "fetched": self.fetched,
            "credits_used": self.credits_used,
            "credits_left": self.credits_left,
            "estimated_credits": self.estimated_credits,
            "stopped": self.stopped,
            "errors": self.errors[:5],
            "by_season": self.by_season,
            "guarded": self.guarded,
            "skipped": self.skipped,
        }


def snapshot_counts(snap: dict) -> dict:
    """Events in a saved snapshot, and how many DraftKings and Pinnacle priced."""
    data = snap.get("data") or []
    keys = [{b.get("key") for b in ev.get("bookmakers") or []} for ev in data]
    return {
        "events": len(data),
        "draftkings": sum(BOOKMAKER in k for k in keys),
        "pinnacle": sum("pinnacle" in k for k in keys),
    }


def backfill(
    snapshot_times: Iterable[pd.Timestamp],
    league: str = "E0",
    *,
    max_credits: int,
    keep_credits: int = KEEP_CREDITS,
    dry_run: bool = False,
    api_key: str | None = None,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    books: tuple[str, ...] = (BOOKMAKER,),
    markets: str = "h2h,totals",
    cost_per_snapshot: int = COST_PER_SNAPSHOT,
    guard_first: int = 0,
) -> BackfillReport:
    """Download the snapshots that aren't cached yet, within the credit limits.

    `books`/`markets`/`cost_per_snapshot`: the original E0 run asked DraftKings alone for
    h2h and totals (20 credits); the multi-league backfill asks TEN_BOOKS for h2h
    (TEN_BOOKS_COST). With `guard_first`, a season whose first `guard_first` snapshots
    hold no DraftKings price buys nothing more."""
    times = sorted({pd.Timestamp(t) for t in snapshot_times})
    todo = [t for t in times if not snapshot_path(league, t, raw_dir).exists()]
    rep = BackfillReport(planned=len(times), cached=len(times) - len(todo))
    rep.estimated_credits = len(todo) * cost_per_snapshot
    if dry_run or not todo:
        _tally(rep, times, league, raw_dir)
        return rep

    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        rep.stopped = "no ODDS_API_KEY configured"
        return rep

    # Budget each call at the larger of the planned cost and the last call's real one
    # (an empty snapshot bills less, which must not loosen the cap).
    cost = cost_per_snapshot
    seen: dict[str, list[int]] = {}  # season -> DraftKings-priced events per snapshot
    for t in times:
        season = season_label(t)
        if season in rep.guarded:
            rep.skipped += t in todo
            continue
        path = snapshot_path(league, t, raw_dir)
        if path.exists():  # cached: counts toward the guard, costs nothing
            dk = snapshot_counts(json.loads(path.read_text()))["draftkings"]
            _guard(rep, seen, season, dk, guard_first)
            continue
        if rep.credits_used + cost > max_credits:
            rep.stopped = f"--max-credits {max_credits} reached"
            break
        if rep.credits_left is not None and rep.credits_left - cost < keep_credits:
            rep.stopped = f"keeping {keep_credits} credits for live refreshes"
            break
        try:
            resp = get(
                URL.format(sport=SPORTS[league]),
                params={
                    "apiKey": api_key,
                    "bookmakers": ",".join(books),
                    "markets": markets,
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                    "date": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=60,
            )
        except requests.RequestException as exc:  # message could contain the URL
            rep.errors.append(f"could not reach The Odds API ({type(exc).__name__})")
            rep.stopped = "network error"
            break
        if not resp.ok:
            rep.errors.append(f"The Odds API returned HTTP {resp.status_code}")
            rep.stopped = "API error (the historical endpoint needs a paid plan)"
            break
        remaining, last = (
            resp.headers.get("x-requests-remaining"),
            resp.headers.get("x-requests-last"),
        )
        spent = int(float(last)) if last else cost_per_snapshot
        cost = max(cost_per_snapshot, spent)
        rep.credits_used += spent
        rep.credits_left = int(float(remaining)) if remaining else rep.credits_left
        body = resp.json()
        snap = {
            "requested": t.isoformat(),
            "timestamp": body.get("timestamp"),
            "data": compact_events(body.get("data", []), books),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap, separators=(",", ":")))
        rep.fetched += 1
        _guard(rep, seen, season, snapshot_counts(snap)["draftkings"], guard_first)
    _tally(rep, times, league, raw_dir)
    return rep


def _guard(rep: BackfillReport, seen: dict, season: str, dk: int, first: int) -> None:
    """Design C's empty-data guard: the first `first` snapshots of a season (in time
    order) with no DraftKings price stop that season."""
    if not first:
        return
    got = seen.setdefault(season, [])
    got.append(dk)
    if len(got) == first and not any(got):
        rep.guarded[season] = f"no DraftKings price in its first {first} snapshots"


def _tally(rep: BackfillReport, times, league: str, raw_dir: Path) -> None:
    """Per-season counts over every planned snapshot on disk (cached or just bought)."""
    for t in times:
        path = snapshot_path(league, t, raw_dir)
        if not path.exists():
            continue
        c = snapshot_counts(json.loads(path.read_text()))
        b = rep.by_season.setdefault(
            season_label(t), {"snapshots": 0, "events": 0, "draftkings": 0, "pinnacle": 0}
        )
        b["snapshots"] += 1
        for k in ("events", "draftkings", "pinnacle"):
            b[k] += c[k]


def compact_events(events: list[dict], keep: Iterable[str] = (BOOKMAKER,)) -> list[dict]:
    """Keep only what the backtest needs: teams, kickoff and the asked books' markets
    (DraftKings alone for the original E0 run, TEN_BOOKS for the multi-league one)."""
    keep = set(keep)
    out = []
    for ev in events:
        books = [b for b in ev.get("bookmakers", []) if b.get("key") in keep]
        out.append(
            {
                "commence_time": ev.get("commence_time"),
                "home_team": ev.get("home_team"),
                "away_team": ev.get("away_team"),
                "bookmakers": books,
            }
        )
    return out


def load_history(
    league: str = "E0", known_teams: set[str] | None = None, raw_dir: Path = RAW_DIR
) -> pd.DataFrame:
    """Every cached snapshot as rows: snapshot time, kickoff, teams and DraftKings odds.

    `at` is the requested time; `snapshot_ts` is the snapshot the API returned (at or
    before `at`).
    """
    frames = []
    for path in sorted(history_dir(league, raw_dir).glob("*.json")):
        snap = json.loads(path.read_text())
        df = parse_odds(snap.get("data", []), known_teams)
        if df.empty:
            continue
        df.insert(0, "at", pd.Timestamp(snap["requested"]))
        df.insert(1, "snapshot_ts", pd.Timestamp(snap.get("timestamp") or snap["requested"]))
        frames.append(df)
    if not frames:
        return pd.DataFrame(
            columns=["at", "snapshot_ts", "kickoff", "home", "away", "odds_updated"]
        )
    out = pd.concat(frames, ignore_index=True)
    out["odds_updated"] = pd.to_datetime(out["odds_updated"], utc=True)
    return out.sort_values(["at", "kickoff"]).reset_index(drop=True)


def price_at(
    history: pd.DataFrame,
    home: str,
    away: str,
    kickoff: pd.Timestamp,
    at: pd.Timestamp,
    stale_hours: float | None = STALE_HOURS,
) -> dict | None:
    """DraftKings odds for one match from the latest snapshot at or before `at`.

    None when no snapshot has the match, or its price was last updated more than
    STALE_HOURS before `at`. Only snapshots taken at or before `at` are considered, so
    later data can never leak in.
    """
    h = history[
        (history["home"] == home)
        & (history["away"] == away)
        & (history["snapshot_ts"] <= at)
        & ((history["kickoff"] - kickoff).abs() <= pd.Timedelta(hours=48))
    ]
    if h.empty:
        return None
    row = h.iloc[-1]
    upd = row["odds_updated"]
    if stale_hours is not None and pd.notna(upd) and at - upd > pd.Timedelta(hours=stale_hours):
        return None
    return row.to_dict()


def coverage(matches: pd.DataFrame, history: pd.DataFrame) -> list[dict]:
    """Per season: matches with a DraftKings price out of matches played."""
    if matches.empty:
        return []
    priced = (
        {
            (h, a, season_label(k))
            for h, a, k in zip(history["home"], history["away"], history["kickoff"], strict=True)
        }
        if not history.empty
        else set()
    )
    out = []
    for season, g in matches.groupby("season", sort=True):
        have = sum((h, a, str(season)) in priced for h, a in zip(g["home"], g["away"], strict=True))
        out.append(
            {"season": str(season), "matches": len(g), "priced": have, "share": have / len(g)}
        )
    return out
