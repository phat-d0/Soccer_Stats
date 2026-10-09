"""Live team-total prices: two snapshots per match, logged to data-log (docs/totals.md).

Owner-approved on 9 Oct (the "base" plan): FanDuel's `team_totals` (Bovada too in the
Premier League, same `us` call) in E0, SP1, D1, I1 and F1, one look about 24 hours before
kickoff and one close just before it. Data only: no paper trades, nothing in the app.

Per publish run (`run`):

* **Which matches are due.** A fixture card is due a "look" at the first run with its
  kickoff LOOK_WINDOW_HOURS away (30 to 18 hours), and a "close" at the first run within
  CLOSE_MINUTES of kickoff, or earlier inside CLOSE_LATEST_MINUTES when no later scheduled
  run is left before kickoff (odds_feed.publish_runs). Throttled runs land late, so the
  windows are wide; a run that misses a window simply never takes that snapshot.
* **Never twice.** Every call is recorded (`calls`, one row per event and snapshot, kept on
  data-log and in the local cache); a recorded (event, snapshot) is never fetched again,
  even if the book returned nothing.
* **Budget.** No call below MATCHDAY_RESERVE_CREDITS on the freshest balance seen (any
  league's DraftKings meta, or the last team-total call), and none once this month's
  team-total calls reach MONTHLY_CAP credits. The Premier League's match odds and the
  baseball app keep priority. The key is never printed or logged.

`log` appends the run's rows and calls to data-log (odds_log/<code>_team_totals_<YYYY-MM>
.jsonl and odds_log/team_totals_calls_<YYYY-MM>.jsonl), deduplicated. `report` scores the
model's chance and the look price against FanDuel's de-margined close (lab.metrics) once
enough matches have settled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.odds import devig_shin
from soccer_stats.odds_feed import (
    BOOKMAKER,
    MATCHDAY_RESERVE_CREDITS,
    SPORTS,
    TEAM_TOTAL_LEAGUES,
    _team,
    latest_meta,
    publish_runs,
)

URL = "https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds"
MARKET = "team_totals"
BOOKS = {"E0": ("fanduel", "bovada")}  # both "us": one region, one credit per market
DEFAULT_BOOKS = ("fanduel",)
LOOK_WINDOW_HOURS = (18.0, 30.0)
CLOSE_MINUTES = 30
CLOSE_LATEST_MINUTES = 75
MONTHLY_CAP = 450  # credits a calendar month (UTC); the estimate is about 340
COST_PER_CALL = 1  # one market, one region (market probe, run 37889595473)
KICKOFF_TOLERANCE = pd.Timedelta(hours=3)
LOCAL_CALLS = "team_totals_calls.jsonl"  # local copy of the call record (raw-data cache)
MIN_MATCHES = 50  # report: fewer settled matches with both snapshots is "not enough yet"


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _iso(t) -> str:
    return _ts(t).isoformat(timespec="seconds")


# ---------- timing ----------


def due_snapshot(kickoff, now: pd.Timestamp, runs: list[pd.Timestamp] | None = None) -> str | None:
    """ "look", "close" or None for a match at this run.

    `runs` are the scheduled publish times (default: publish_runs around now); they only
    decide whether a close earlier than CLOSE_MINUTES is the last chance.
    """
    k, now = _ts(kickoff), _ts(now)
    left = k - now
    if left <= pd.Timedelta(0):
        return None
    lo, hi = LOOK_WINDOW_HOURS
    if pd.Timedelta(hours=lo) <= left <= pd.Timedelta(hours=hi):
        return "look"
    if left <= pd.Timedelta(minutes=CLOSE_MINUTES):
        return "close"
    if left <= pd.Timedelta(minutes=CLOSE_LATEST_MINUTES):
        if runs is None:
            runs = publish_runs(now, k)
        later = [t for t in runs if now < t < k - pd.Timedelta(minutes=5)]
        return None if later else "close"
    return None


# ---------- state: the call record ----------


def _read_jsonl(paths) -> list[dict]:
    out = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load_calls(*dirs) -> list[dict]:
    """Every recorded team-total call in these folders (data-log's odds_log, a local copy)."""
    paths = []
    for d in dirs:
        if d is None:
            continue
        d = Path(d)
        paths += sorted(d.glob("team_totals_calls_*.jsonl")) + [d / LOCAL_CALLS]
    seen, out = set(), []
    for c in _read_jsonl(paths):
        key = (c.get("event_id"), c.get("snapshot"))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def month_spend(calls: list[dict], now: pd.Timestamp) -> int:
    """Credits the recorded team-total calls used this calendar month (UTC)."""
    month = _ts(now).strftime("%Y-%m")
    return sum(
        int(c.get("cost") or 0)
        for c in calls
        if c.get("fetched_at") and _ts(c["fetched_at"]).strftime("%Y-%m") == month
    )


def freshest_credits(raw_dir: Path, calls: list[dict]) -> int | None:
    """The most recent balance seen on the shared key: any league's DraftKings meta or
    the last team-total call, whichever is newer."""
    best = latest_meta(raw_dir)
    when, credits = best.get("fetched_at"), best.get("credits_left")
    for c in calls:
        if c.get("credits_left") is not None and c.get("fetched_at"):
            if when is None or _ts(c["fetched_at"]) > _ts(when):
                when, credits = c["fetched_at"], c["credits_left"]
    return credits


# ---------- events and cards ----------


def event_index(raw_dir: Path, league: str, known: set[str]) -> list[dict]:
    """{id, home, away, kickoff} from the league's cached DraftKings body (the same event
    ids serve every book), with names mapped to the cards' spellings."""
    path = Path(raw_dir) / f"odds_api_{league}_{BOOKMAKER}.json"
    try:
        events = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for ev in events or []:
        if not ev.get("id") or not ev.get("commence_time"):
            continue
        out.append(
            {
                "id": ev["id"],
                "home": _team(ev.get("home_team", ""), known),
                "away": _team(ev.get("away_team", ""), known),
                "kickoff": _ts(ev["commence_time"]),
            }
        )
    return out


def match_event(card: dict, events: list[dict]) -> str | None:
    """The event id for a card: same teams near its kickoff, else the one event near its
    kickoff sharing one team."""
    k = _ts(card["kickoff"])
    near = [e for e in events if abs(e["kickoff"] - k) <= KICKOFF_TOLERANCE]
    both = [e for e in near if e["home"] == card["home"] and e["away"] == card["away"]]
    if len(both) == 1:
        return both[0]["id"]
    one = [e for e in near if e["home"] == card["home"] or e["away"] == card["away"]]
    return one[0]["id"] if len(one) == 1 else None


def model_over(card: dict, side: str, line: float) -> float | None:
    """P(team goals > line) from the card's score matrix (`goals_cdf`, cumulative)."""
    cdf = (card.get("goals_cdf") or {}).get(side)
    k = int(np.floor(line))
    if not cdf or k < 0 or k >= len(cdf) or cdf[k] is None:
        return None
    return round(1.0 - float(cdf[k]), 4)


def rows_from_body(
    body: dict | None, card: dict, event_id: str, snapshot: str, downloaded: pd.Timestamp
) -> list[dict]:
    """One row per book, team and line with both sides priced."""
    k = _ts(card["kickoff"])
    known = {card["home"], card["away"]}
    pairs: dict[tuple, dict] = {}
    for b in (body or {}).get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            if m.get("key") != MARKET:
                continue
            updated = m.get("last_update") or b.get("last_update")
            for o in m.get("outcomes", []) or []:
                side = str(o.get("name", "")).lower()
                team = _team(str(o.get("description", "")), known)
                if side not in ("over", "under") or team not in known:
                    continue
                if o.get("point") is None or o.get("price") is None:
                    continue
                key = (b.get("key"), team, float(o["point"]))
                pairs.setdefault(key, {"last_update": updated})[side] = float(o["price"])
    rows = []
    for (book, team, line), q in sorted(pairs.items()):
        if "over" not in q or "under" not in q or min(q["over"], q["under"]) <= 1:
            continue
        fair = devig_shin(np.array([q["over"], q["under"]]))
        quoted = _ts(q["last_update"]) if q.get("last_update") else downloaded
        side = "home" if team == card["home"] else "away"
        rows.append(
            {
                "league": card.get("league"),
                "home": card["home"],
                "away": card["away"],
                "kickoff": _iso(k),
                "event_id": event_id,
                "snapshot": snapshot,
                "book": book,
                "team": team,
                "side": side,
                "line": line,
                "over": q["over"],
                "under": q["under"],
                "fair_over": round(float(fair[0]), 6),
                "fair_under": round(float(fair[1]), 6),
                "margin": round(1 / q["over"] + 1 / q["under"] - 1, 6),
                "fetched_at": _iso(quoted),
                "time_source": "last_update" if q.get("last_update") else "download",
                "downloaded_at": _iso(downloaded),
                "minutes_before": round((k - downloaded) / pd.Timedelta(minutes=1), 1),
                "p_model_over": model_over(card, side, line),
            }
        )
    return rows


# ---------- one publish run ----------


def run(
    cards: list[dict],
    out_dir: Path,
    state_dirs=(),
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get=requests.get,
) -> dict:
    """Fetch every due snapshot within budget; write the rows and calls to `out_dir`
    (rows.jsonl, calls.jsonl) and the calls to the local record. Never raises."""
    now = _ts(now or pd.Timestamp.now(tz="UTC"))
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    raw_dir = Path(raw_dir)
    calls = load_calls(*state_dirs, raw_dir)
    done = {(c.get("event_id"), c.get("snapshot")) for c in calls}
    spent = month_spend(calls, now)
    credits = freshest_credits(raw_dir, calls)
    summary = {"calls": 0, "credits": 0, "rows": 0, "due": 0, "month_spent": spent, "stop": None}
    if not api_key:
        summary["stop"] = "no ODDS_API_KEY"
        return summary
    runs = publish_runs(now, now + pd.Timedelta(hours=2))
    by_league: dict[str, list[dict]] = {}
    for c in cards:
        if c.get("league") in TEAM_TOTAL_LEAGUES and c.get("kickoff"):
            by_league.setdefault(c["league"], []).append(c)
    new_rows, new_calls = [], []
    for league, lcards in by_league.items():
        due = [(c, s) for c in lcards if (s := due_snapshot(c["kickoff"], now, runs))]
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
            books = ",".join(BOOKS.get(league, DEFAULT_BOOKS))
            try:
                resp = get(
                    URL.format(sport=SPORTS[league], event_id=eid),
                    params={
                        "apiKey": api_key,
                        "bookmakers": books,
                        "markets": MARKET,
                        "oddsFormat": "decimal",
                        "dateFormat": "iso",
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:  # message could contain the URL
                summary["stop"] = f"could not reach The Odds API ({type(exc).__name__})"
                break
            cost = resp.headers.get("x-requests-last")
            left = resp.headers.get("x-requests-remaining")
            cost = int(float(cost)) if cost else (COST_PER_CALL if resp.ok else 0)
            if left:
                credits = int(float(left))
            spent += cost
            rows = []
            if resp.ok:
                try:
                    rows = rows_from_body(resp.json(), card, eid, snap, now)
                except ValueError:
                    rows = []
            call = {
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
                "books": books,
                "rows": len(rows),
            }
            done.add((eid, snap))
            new_calls.append(call)
            new_rows += rows
        if summary["stop"]:
            break
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
        f"Team totals: {s.get('calls', 0)} calls, {s.get('credits', 0)} credits, "
        f"{s.get('rows', 0)} rows; month {s.get('month_spent', 0)} of {MONTHLY_CAP} credits"
        + (f", credits left {s['credits_left']}" if s.get("credits_left") is not None else "")
        + (f" (stopped: {s['stop']})" if s.get("stop") else "")
    )


def _append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "a") as f:
        f.write("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")


# ---------- data-log ----------


def _row_key(r: dict) -> tuple:
    return (
        r.get("event_id"),
        r.get("snapshot"),
        r.get("book"),
        r.get("team"),
        r.get("line"),
        r.get("over"),
        r.get("under"),
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
        month = _ts(r["downloaded_at"]).strftime("%Y-%m")
        by_file.setdefault(d / f"{r['league']}_team_totals_{month}.jsonl", []).append(r)
    for path, new in by_file.items():
        seen = {_row_key(x) for x in _read_jsonl([path])}
        keep = []
        for r in new:
            if _row_key(r) not in seen:
                seen.add(_row_key(r))
                keep.append(r)
        d.mkdir(parents=True, exist_ok=True)
        _append(path, keep)
        n_rows += len(keep)
    done = {(c.get("event_id"), c.get("snapshot")) for c in load_calls(d)}
    by_month: dict[Path, list[dict]] = {}
    for c in calls:
        if (c.get("event_id"), c.get("snapshot")) in done:
            continue
        done.add((c.get("event_id"), c.get("snapshot")))
        month = _ts(c["fetched_at"]).strftime("%Y-%m")
        by_month.setdefault(d / f"team_totals_calls_{month}.jsonl", []).append(c)
    for path, new in by_month.items():
        d.mkdir(parents=True, exist_ok=True)
        _append(path, new)
        n_calls += len(new)
    return n_rows, n_calls


def load_rows(log_root: Path) -> pd.DataFrame:
    """Every logged team-total row (all leagues and months)."""
    paths = sorted((Path(log_root) / "odds_log").glob("*_team_totals_*.jsonl"))
    return pd.DataFrame(_read_jsonl(paths))


# ---------- analysis (run later, no key) ----------


def pairs(rows: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """Look and close for the same match, book, team and line, with the team's goals.

    `results` has league, home, away, date and FTHG/FTAG-style `home_goals`/`away_goals`.
    """
    if rows.empty:
        return pd.DataFrame()
    key = ["league", "home", "away", "kickoff", "book", "team", "side", "line"]
    look = rows[rows["snapshot"] == "look"].drop_duplicates(key, keep="first")
    close = rows[rows["snapshot"] == "close"].drop_duplicates(key, keep="last")
    both = look.merge(
        close[key + ["fair_over", "fair_under", "over", "under", "minutes_before"]],
        on=key,
        suffixes=("", "_close"),
    )
    if both.empty or results.empty:
        return pd.DataFrame()
    res = results.copy()
    res["day"] = pd.to_datetime(res["date"]).dt.strftime("%Y-%m-%d")
    both["day"] = pd.to_datetime(both["kickoff"], utc=True).dt.strftime("%Y-%m-%d")
    m = both.merge(
        res[["league", "home", "away", "day", "home_goals", "away_goals"]],
        on=["league", "home", "away", "day"],
    )
    goals = np.where(m["side"] == "home", m["home_goals"], m["away_goals"])
    m["y"] = (goals <= m["line"]).astype(int)  # 0 = over won, 1 = under won
    return m


def report(rows: pd.DataFrame, results: pd.DataFrame, min_matches: int = MIN_MATCHES) -> dict:
    """The model and the look price vs FanDuel's de-margined close (lab.metrics), plus
    calibration. Refuses to score fewer than `min_matches` settled matches."""
    from soccer_stats.lab import metrics

    p = pairs(rows, results)
    matches = (
        int(p[["league", "home", "away", "kickoff"]].drop_duplicates().shape[0]) if len(p) else 0
    )
    out = {"rows": int(len(rows)), "pairs": int(len(p)), "matches": matches}
    if matches < min_matches:
        out["note"] = f"not enough data yet: {matches} settled matches with both snapshots"
        return out
    p = p[p["book"] == "fanduel"].dropna(subset=["p_model_over"])
    groups = (p["league"] + "|" + p["home"] + "|" + p["kickoff"]).to_numpy()
    model = np.column_stack([p["p_model_over"], 1 - p["p_model_over"]])
    look = p[["fair_over", "fair_under"]].to_numpy()
    odds = p[["over", "under"]].to_numpy()
    close = p[["fair_over_close", "fair_under_close"]].to_numpy()
    y = p["y"].to_numpy()
    out["fanduel_rows"] = int(len(p))
    out["model"] = metrics.evaluate(model, y, look, odds, close, groups)
    out["model_vs_close"] = metrics.paired_gain(
        metrics.log_loss_rows(close, y), metrics.log_loss_rows(model, y), groups
    )
    for name, chances in (("model", model), ("close", close)):
        t = metrics.calibration_table(chances, y)
        out[f"calibration_{name}"] = t.reset_index(names="bin").to_dict("records")
    out["median_close_minutes"] = float(p["minutes_before_close"].median())
    return out
