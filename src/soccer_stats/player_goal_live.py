"""Priced goalscorer test (docs/player_props.md §16), step 2: the live anytime-goalscorer log.

On each default-branch publish (after ESPN's team news is on the cards), for every E0, SP1,
D1, I1 and F1 fixture:
- **entry:** the first run with the kickoff 20–75 min away once ESPN shows both lineups
  confirmed, or at 20–30 min if they still aren't;
- **close:** the first run with the kickoff 0–20 min away (closes.yml dispatches a publish
  15 min before each kickoff cluster).

Each snapshot is one live event-odds call: `player_goal_scorer_anytime`, §16's ten books
(one `bookmakers=` list, 1 credit). Event ids come from the league's cached DraftKings body
(`team_totals.event_index`). An (event, snapshot) in the call record is never fetched again.
Before each call: the freshest balance minus 1 stays ≥ 3,000, and this month's goalscorer
calls stay ≤ `MONTHLY_CAP`. A call that isn't answered 2xx (a 502, say) doesn't count as done:
the next run inside the same window asks again, at most `MAX_ATTEMPTS` times per (event,
snapshot) so a billing error can't loop; every attempt keeps its call record and cost
(lead, 7 Nov 2026). Logging stops after `LIVE_UNTIL` (the judged window's last day)
until the lead decides. An error part-way keeps the calls already made and logs only the
error type, never a URL or the key. No result is read here.

Rows (one per event, snapshot, book and player, Yes and No prices) and calls go to a pending
folder; `log` appends them to data-log `odds_log/<code>_goalscorer_<YYYY-MM>.jsonl` and
`odds_log/goalscorer_calls_<YYYY-MM>.jsonl`, deduplicated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.odds_feed import MATCHDAY_RESERVE_CREDITS, SPORTS
from soccer_stats.player_goal_backfill import BOOKS
from soccer_stats.player_goal_odds import MARKET, prices
from soccer_stats.team_totals import (
    _append,
    _header_int,
    _iso,
    _read_jsonl,
    _ts,
    event_index,
    freshest_credits,
    match_event,
)

URL = "https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds"
LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
ENTRY_WINDOW = (20, 75)  # minutes before kickoff
ENTRY_LATEST = 30  # take the entry by now even without confirmed lineups
CLOSE_WINDOW = (0, 20)
COST_PER_CALL = 1  # one market, one bookmakers= list
MONTHLY_CAP = 3000  # §16 step 2
LIVE_UNTIL = pd.Timestamp("2027-02-01T00:00:00Z")  # §16: the judged window ends 31 Jan 2027
LOCAL_CALLS = "goalscorer_calls.jsonl"
MAX_ATTEMPTS = 3  # per (event, snapshot), answered or not (lead, 7 Nov 2026)


def lineups_confirmed(card: dict) -> bool:
    lineup = ((card.get("team_news") or {}).get("lineup")) or {}
    return bool(lineup.get("confirmed"))


def due_snapshot(card: dict, now: pd.Timestamp) -> str | None:
    """ "entry", "close" or None for a fixture at this run."""
    left = (_ts(card["kickoff"]) - _ts(now)).total_seconds() / 60
    if CLOSE_WINDOW[0] < left <= CLOSE_WINDOW[1]:
        return "close"
    if ENTRY_WINDOW[0] < left <= ENTRY_WINDOW[1] and (
        lineups_confirmed(card) or left <= ENTRY_LATEST
    ):
        return "entry"
    return None


def load_calls(*dirs) -> list[dict]:
    """Every recorded goalscorer call in these folders (data-log's odds_log, a local copy)."""
    paths = []
    for d in dirs:
        if d is None:
            continue
        d = Path(d)
        paths += sorted(d.glob("goalscorer_calls_*.jsonl")) + [d / LOCAL_CALLS]
    seen, out = set(), []
    for c in _read_jsonl(paths):
        key = _call_key(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _call_key(c: dict) -> tuple:
    """One attempt: a retried snapshot keeps every attempt's record."""
    return (c.get("event_id"), c.get("snapshot"), c.get("fetched_at"))


def answered(c: dict) -> bool:
    try:
        return 200 <= int(c.get("status")) < 300
    except (TypeError, ValueError):
        return False


def done_keys(calls: list[dict]) -> set[tuple]:
    """(event, snapshot) pairs never to ask again: answered 2xx, or tried MAX_ATTEMPTS times."""
    tries: dict[tuple, int] = {}
    done = set()
    for c in calls:
        key = (c.get("event_id"), c.get("snapshot"))
        tries[key] = tries.get(key, 0) + 1
        if answered(c) or tries[key] >= MAX_ATTEMPTS:
            done.add(key)
    return done


def month_spend(calls: list[dict], now: pd.Timestamp) -> int:
    month = _ts(now).strftime("%Y-%m")
    return sum(
        int(c.get("cost") or 0)
        for c in calls
        if c.get("fetched_at") and _ts(c["fetched_at"]).strftime("%Y-%m") == month
    )


def rows_from_body(body: dict, card: dict, eid: str, snap: str, now: pd.Timestamp) -> list[dict]:
    p = prices(body)
    k = _ts(card["kickoff"])
    lineup = ((card.get("team_news") or {}).get("lineup")) or {}
    out = []
    for r in p.itertuples(index=False):
        out.append(
            {
                "league": card["league"],
                "event_id": eid,
                "home": card["home"],
                "away": card["away"],
                "kickoff": _iso(k),
                "snapshot": snap,
                "book": r.book,
                "player": r.player,
                "yes": None if pd.isna(r.yes) else float(r.yes),
                "no": None if pd.isna(r.no) else float(r.no),
                "fetched_at": _iso(now),
                "minutes_before": round((k - _ts(now)).total_seconds() / 60, 1),
                "lineup_confirmed": bool(lineup.get("confirmed")),
                "first_confirmed_at": lineup.get("first_confirmed_at"),
            }
        )
    return out


def run(
    cards: list[dict],
    out_dir: Path,
    state_dirs=(),
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get=requests.get,
) -> dict:
    """Fetch every due snapshot within budget; write rows.jsonl and calls.jsonl to
    `out_dir` and the calls to the local record. Never raises."""
    now = _ts(now or pd.Timestamp.now(tz="UTC"))
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    raw_dir = Path(raw_dir)
    calls = load_calls(*state_dirs, raw_dir)
    done = done_keys(calls)
    spent = month_spend(calls, now)
    credits = freshest_credits(raw_dir, calls)
    summary = {"calls": 0, "credits": 0, "rows": 0, "due": 0, "month_spent": spent, "stop": None}
    if now >= LIVE_UNTIL:
        summary["stop"] = f"the judged window ended ({LIVE_UNTIL.date()}); the lead decides"
        return summary
    if not api_key:
        summary["stop"] = "no ODDS_API_KEY"
        return summary
    by_league: dict[str, list[dict]] = {}
    for c in cards:
        if c.get("league") in LEAGUES and c.get("kickoff"):
            by_league.setdefault(c["league"], []).append(c)
    new_rows, new_calls = [], []
    try:  # a crash part-way still writes the calls already made (below)
        for league, lcards in by_league.items():
            due = [(c, s) for c in lcards if (s := due_snapshot(c, now))]
            if not due:
                continue
            known = {c["home"] for c in lcards} | {c["away"] for c in lcards}
            events = event_index(raw_dir, league, known)
            for card, snap in due:
                eid = match_event(card, events)
                if eid is None or (eid, snap) in done:
                    continue
                summary["due"] += 1
                if credits is not None and credits - COST_PER_CALL < MATCHDAY_RESERVE_CREDITS:
                    summary["stop"] = (
                        f"reserve: {credits} credits left (keeping {MATCHDAY_RESERVE_CREDITS})"
                    )
                    break
                if spent + COST_PER_CALL > MONTHLY_CAP:
                    summary["stop"] = f"monthly cap: {spent} of {MONTHLY_CAP} credits used"
                    break
                try:
                    resp = get(
                        URL.format(sport=SPORTS[league], event_id=eid),
                        params={
                            "apiKey": api_key,
                            "bookmakers": ",".join(BOOKS),
                            "markets": MARKET,
                            "oddsFormat": "decimal",
                            "dateFormat": "iso",
                        },
                        timeout=30,
                    )
                except requests.RequestException as exc:  # the message could hold the URL
                    summary["stop"] = f"could not reach The Odds API ({type(exc).__name__})"
                    break
                cost = _header_int(resp, "x-requests-last")
                left = _header_int(resp, "x-requests-remaining")
                if cost is None:
                    cost = COST_PER_CALL if resp.ok else 0
                if left is not None:
                    credits = left
                spent += cost
                rows = []
                if resp.ok:
                    try:
                        rows = rows_from_body(resp.json(), card, eid, snap, now)
                    except Exception:  # noqa: BLE001  odd body: keep the call record, no rows
                        rows = []
                new_calls.append(
                    {
                        "league": league,
                        "event_id": eid,
                        "home": card["home"],
                        "away": card["away"],
                        "kickoff": _iso(card["kickoff"]),
                        "snapshot": snap,
                        "fetched_at": _iso(now),
                        "status": resp.status_code,
                        "cost": cost,
                        "credits_left": credits,
                        "lineup_confirmed": lineups_confirmed(card),
                        "rows": len(rows),
                    }
                )
                done.add((eid, snap))  # one attempt per run; a non-2xx is retried next run
                new_rows += rows
            if summary["stop"]:
                break
    except Exception as exc:  # noqa: BLE001  type only: a message could hold the URL
        summary["stop"] = f"error ({type(exc).__name__})"
    summary.update(
        calls=len(new_calls),
        credits=sum(c["cost"] for c in new_calls),
        rows=len(new_rows),
        month_spent=spent,
        credits_left=credits,
    )
    if new_calls:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        _append(out_dir / "rows.jsonl", new_rows)
        _append(out_dir / "calls.jsonl", new_calls)
        raw_dir.mkdir(parents=True, exist_ok=True)
        _append(raw_dir / LOCAL_CALLS, new_calls)
    return summary


def summary_line(s: dict) -> str:
    return (
        f"Goalscorer prices: {s.get('calls', 0)} calls, {s.get('credits', 0)} credits, "
        f"{s.get('rows', 0)} rows; month {s.get('month_spent', 0)} of {MONTHLY_CAP} credits"
        + (f", credits left {s['credits_left']}" if s.get("credits_left") is not None else "")
        + (f" (stopped: {s['stop']})" if s.get("stop") else "")
    )


def _row_key(r: dict) -> tuple:
    return (
        r.get("event_id"),
        r.get("snapshot"),
        r.get("book"),
        r.get("player"),
        r.get("fetched_at"),
    )


def log(pending: Path, log_root: Path) -> tuple[int, int]:
    """Append the run's rows and calls to data-log's odds_log, deduplicated.
    Returns (rows written, calls written)."""
    pending, d = Path(pending), Path(log_root) / "odds_log"
    rows = _read_jsonl([pending / "rows.jsonl"])
    calls = _read_jsonl([pending / "calls.jsonl"])
    n_rows = n_calls = 0
    by_file: dict[Path, list[dict]] = {}
    for r in rows:
        month = _ts(r["fetched_at"]).strftime("%Y-%m")
        by_file.setdefault(d / f"{r['league']}_goalscorer_{month}.jsonl", []).append(r)
    for path, new in by_file.items():
        seen = {_row_key(x) for x in _read_jsonl([path])}
        keep = [r for r in new if _row_key(r) not in seen]
        d.mkdir(parents=True, exist_ok=True)
        _append(path, keep)
        n_rows += len(keep)
    done = {_call_key(c) for c in load_calls(d)}
    by_month: dict[Path, list[dict]] = {}
    for c in calls:
        key = _call_key(c)
        if key in done:
            continue
        done.add(key)
        month = _ts(c["fetched_at"]).strftime("%Y-%m")
        by_month.setdefault(d / f"goalscorer_calls_{month}.jsonl", []).append(c)
    for path, new in by_month.items():
        d.mkdir(parents=True, exist_ok=True)
        _append(path, new)
        n_calls += len(new)
    return n_rows, n_calls


def load_rows(log_root: Path) -> pd.DataFrame:
    """Every logged live goalscorer row (all leagues and months)."""
    d = Path(log_root) / "odds_log"
    rows = _read_jsonl(sorted(d.glob("*_goalscorer_*.jsonl")))
    return pd.DataFrame(rows)
