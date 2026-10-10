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
    try:  # a crash part-way still writes the calls already made (below)
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
                cost = _header_int(resp, "x-requests-last")
                left = _header_int(resp, "x-requests-remaining")
                if cost is None:  # unknown: count the worst case of a successful call
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


def _header_int(resp, name: str) -> int | None:
    try:
        v = resp.headers.get(name)
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


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


# ---------- round 13: FanDuel's team totals against the main market (docs/totals.md) ----------

DK_FRESH_HOURS = 6.0  # a DraftKings quote older than this at the FanDuel look is dropped
ANCHORS = {"fanduel_anchor": "fanduel", "pinnacle_anchor": "pinnacle"}  # totals book
MM_CANDIDATES = ("fanduel_anchor", "pinnacle_anchor", "model", "blend")  # the test family
MM_DESCRIPTIVE = ("h2h",)  # scored and printed, never a pass (amendment 1)
MM_THRESHOLDS = (0.02, 0.05, 0.10)
MM_TESTS = len(MM_CANDIDATES) * len(MM_THRESHOLDS)  # 12: 99.583% ranges
MM_MIN_BETS = 30
CONFIRM_MATCHES = 150
MAX_GOALS = 10
_H2H = ("home", "draw", "away")
_KEY8 = ["league", "home", "away", "kickoff", "book", "team", "side", "line"]


def market_kind(rows: pd.DataFrame) -> pd.Series:
    """Each row's market; rows logged before the totals anchor carry none (team totals)."""
    if "market" not in rows:
        return pd.Series("team_totals", index=rows.index)
    return rows["market"].fillna("team_totals")


def _score_probs(lh: float, la: float) -> np.ndarray:
    from scipy.stats import poisson

    k = np.arange(MAX_GOALS + 1)
    m = np.outer(poisson.pmf(k, lh), poisson.pmf(k, la))
    return m / m.sum()


def _total_pmf(m: np.ndarray) -> np.ndarray:
    k = np.arange(m.shape[0])
    tot = (k[:, None] + k[None, :]).ravel()
    return np.bincount(tot, weights=m.ravel())


def _outcomes(m: np.ndarray) -> dict:
    t = _total_pmf(m)
    return {
        "home": float(np.tril(m, -1).sum()),
        "draw": float(np.trace(m)),
        "away": float(np.triu(m, 1).sum()),
        "over25": float(t[3:].sum()),
        "under25": float(t[:3].sum()),
    }


def breakeven_over(total_pmf: np.ndarray, line: float) -> float:
    """The over's break-even chance at `line` (x.5, whole or quarter, half-stake split):
    sum of win over sum of win + lose across the line's halves (a push counts as neither)."""
    parts = [line - 0.25, line + 0.25] if (line * 4) % 2 == 1 else [line]
    goals = np.arange(len(total_pmf))
    win = sum(float(total_pmf[goals > c].sum()) for c in parts)
    lose = sum(float(total_pmf[goals < c].sum()) for c in parts)
    return win / (win + lose)


def implied_means(h2h: dict, totals: dict | None = None) -> tuple[float, float]:
    """Poisson means (home, away) whose score matrix best matches the margin-free main
    market: least squares over home/draw/away, plus over/under 2.5 when given."""
    from scipy.optimize import minimize

    target = {k: float(h2h[k]) for k in _H2H}
    if totals:
        target.update({k: float(totals[k]) for k in ("over25", "under25")})

    def loss(x):
        o = _outcomes(_score_probs(*np.exp(x)))
        return sum((o[k] - v) ** 2 for k, v in target.items())

    lo, hi = np.log(0.05), np.log(6.0)
    res = minimize(loss, np.log([1.4, 1.1]), method="L-BFGS-B", bounds=[(lo, hi), (lo, hi)])
    return float(np.exp(res.x[0])), float(np.exp(res.x[1]))


def anchored_means(h2h: dict, line: float, fair_over: float) -> tuple[float, float]:
    """Means s·T and (1 − s)·T: for each split s the total T matches the totals price's
    fair over at `line` exactly; s is chosen to fit DraftKings' h2h (least squares)."""
    from scipy.optimize import brentq, minimize_scalar

    def total_for(s):
        def gap(t):
            return breakeven_over(_total_pmf(_score_probs(s * t, (1 - s) * t)), line) - fair_over

        lo, hi = 0.1, 9.0
        if gap(lo) > 0:
            return lo
        if gap(hi) < 0:
            return hi
        return brentq(gap, lo, hi, xtol=1e-6)

    def loss(s):
        t = total_for(s)
        o = _outcomes(_score_probs(s * t, (1 - s) * t))
        return sum((o[k] - float(h2h[k])) ** 2 for k in _H2H)

    s = minimize_scalar(loss, bounds=(0.02, 0.98), method="bounded", options={"xatol": 1e-5}).x
    t = total_for(s)
    return float(s * t), float((1 - s) * t)


def over_chance(lam: float, line: float) -> float:
    """P(goals > line) for Poisson goals with mean `lam`."""
    from scipy.stats import poisson

    return float(1.0 - poisson.cdf(np.floor(line), lam))


def load_dk(log_root: Path, leagues=TEAM_TOTAL_LEAGUES) -> pd.DataFrame:
    """Every logged DraftKings main-market row in these leagues, with UTC times."""
    from soccer_stats import odds_log

    frames = [odds_log.load(Path(log_root), lg).assign(league=lg) for lg in leagues]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    dk = pd.concat(frames, ignore_index=True)
    dk["downloaded_at"] = pd.to_datetime(dk["downloaded_at"], utc=True)
    return dk.sort_values("downloaded_at").reset_index(drop=True)


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _anchor(totals: pd.DataFrame, book: str) -> tuple[float, float] | None:
    """(line, fair over) of the book's main total in one call: the line nearest 50/50."""
    t = totals[totals["book"] == book] if len(totals) else totals
    if not len(t):
        return None
    r = t.iloc[int((t["fair_over"] - 0.5).abs().to_numpy().argmin())]
    return float(r["line"]), float(r["fair_over"])


def with_market(rows: pd.DataFrame, dk: pd.DataFrame, fresh_hours: float = DK_FRESH_HOURS):
    """FanDuel team-total look rows (x.5 lines) with each market-derived over chance:
    `p_h2h` (DraftKings h2h alone, plus O/U 2.5 if logged) and, where the same call
    carried a book's main total, `p_fanduel_anchor` / `p_pinnacle_anchor`. A DraftKings
    h2h quote is the last one downloaded at or before the FanDuel download, dropped when
    more than `fresh_hours` older (`dk_status`). Reads no results."""
    kind = market_kind(rows)
    team = rows[kind == "team_totals"]
    totals = rows[kind == "totals"]
    look = team[(team["snapshot"] == "look") & (team["book"] == "fanduel")].copy()
    look = look[(look["line"] % 1) == 0.5]
    out = []
    cache: dict = {}
    for _, r in look.iterrows():
        rec = r.to_dict()
        rec.update(
            p_h2h=np.nan, lam_home=np.nan, lam_away=np.nan, dk_age_hours=np.nan, dk_totals=False
        )
        for name in ANCHORS:
            rec[f"p_{name}"] = np.nan
            rec[f"{name}_line"] = np.nan
        seen = _utc(r["downloaded_at"])
        k = _utc(r["kickoff"])
        q = (
            dk[
                (dk["league"] == r["league"])
                & (dk["home"] == r["home"])
                & (dk["away"] == r["away"])
                & ((dk["kickoff"] - k).abs() <= KICKOFF_TOLERANCE)
                & (dk["downloaded_at"] <= seen)
            ]
            if not dk.empty
            else dk
        )
        h2h = q[q["market"] == "h2h"] if len(q) else q
        if not len(h2h):
            rec["dk_status"] = "no quote"
            out.append(rec)
            continue
        last = h2h.iloc[-1]
        age = (seen - last["downloaded_at"]) / pd.Timedelta(hours=1)
        rec["dk_age_hours"] = round(float(age), 2)
        if age > fresh_hours:
            rec["dk_status"] = "stale"
            out.append(rec)
            continue
        rec["dk_status"] = "ok"
        side_home = r["side"] == "home"
        fresh = q[q["downloaded_at"] >= seen - pd.Timedelta(hours=fresh_hours)]
        tot = fresh[fresh["market"] == "totals"] if len(fresh) else fresh
        dk_tot = tot.iloc[-1]["fair"] if len(tot) else None
        h2h_key = tuple(sorted(last["fair"].items()))
        key = ("h2h", h2h_key, tuple(sorted(dk_tot.items())) if dk_tot else None)
        if key not in cache:
            cache[key] = implied_means(last["fair"], dk_tot)
        lh, la = cache[key]
        rec["dk_totals"] = dk_tot is not None
        rec.update(lam_home=round(lh, 4), lam_away=round(la, 4))
        rec["p_h2h"] = round(over_chance(lh if side_home else la, r["line"]), 6)
        same_call = (
            totals[
                (totals["event_id"] == r["event_id"])
                & (totals["snapshot"] == r["snapshot"])
                & (totals["downloaded_at"] == r["downloaded_at"])
            ]
            if len(totals)
            else totals
        )
        for name, book in ANCHORS.items():
            a = _anchor(same_call, book)
            if a is None:
                continue
            key = (name, h2h_key, a)
            if key not in cache:
                cache[key] = anchored_means(last["fair"], *a)
            lh, la = cache[key]
            rec[f"{name}_line"] = a[0]
            rec[f"p_{name}"] = round(over_chance(lh if side_home else la, r["line"]), 6)
        out.append(rec)
    return pd.DataFrame(out)


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def candidates(m: pd.DataFrame) -> dict[str, np.ndarray]:
    """Each candidate's over chance per row (NaN where it has no input)."""
    b = pd.to_numeric(m["p_model_over"], errors="coerce").to_numpy(float)
    a2 = m["p_pinnacle_anchor"].to_numpy(float)
    c = 1 / (1 + np.exp(-(0.5 * _logit(a2) + 0.5 * _logit(b))))
    c[~(np.isfinite(a2) & np.isfinite(b))] = np.nan
    return {
        "fanduel_anchor": m["p_fanduel_anchor"].to_numpy(float),
        "pinnacle_anchor": a2,
        "model": b,
        "blend": c,
        "h2h": m["p_h2h"].to_numpy(float),
    }


def _line_type(line) -> str:
    if not np.isfinite(line):
        return "none"
    return "half" if line % 1 == 0.5 else ("whole" if line % 1 == 0 else "quarter")


def plumbing(rows: pd.DataFrame, dk: pd.DataFrame) -> dict:
    """Counts only (no results): FanDuel look rows, DraftKings joins and freshness, anchor
    coverage, and how often each market-derived chance beats FanDuel's raw price."""
    m = with_market(rows, dk) if len(rows) else pd.DataFrame()
    out = {"look_rows": int(len(m)), "dk_status": {}, "by_league": {}, "anchors": {}}
    if m.empty:
        return out
    out["dk_status"] = m["dk_status"].value_counts().to_dict()
    out["dk_totals_rows"] = int(m["dk_totals"].sum())
    for name in ANCHORS:
        has = m[f"p_{name}"].notna()
        lines = m.loc[has, f"{name}_line"]
        out["anchors"][name] = {
            "rows": int(has.sum()),
            "line_types": lines.map(_line_type).value_counts().to_dict(),
            "median_line": float(lines.median()) if has.any() else None,
        }
    ok = m[m["dk_status"] == "ok"]
    cols = {"h2h": "p_h2h", **{n: f"p_{n}" for n in ANCHORS}}
    for lg, g in ok.groupby("league"):
        d = {}
        for name, col in cols.items():
            p = g[col]
            x = g[p.notna()]
            p = p.dropna()
            edge = np.maximum(p * x["over"], (1 - p) * x["under"]) - 1
            d[name] = {
                "rows": int(len(x)),
                "beats_raw_price": int((edge > 0).sum()),
                **{f"edge_ge_{t:.0%}": int((edge >= t).sum()) for t in MM_THRESHOLDS},
                "median_gap_vs_fanduel_fair": (
                    round(float((p - x["fair_over"]).abs().median()), 4) if len(x) else None
                ),
            }
        out["by_league"][lg] = d
    if len(ok):
        out["median_dk_age_hours"] = float(ok["dk_age_hours"].median())
    return out


def _matches(p: pd.DataFrame, ok) -> int:
    x = p[ok]
    return int(x[["league", "home", "away", "kickoff"]].drop_duplicates().shape[0])


def market_report(
    rows: pd.DataFrame,
    dk: pd.DataFrame,
    results: pd.DataFrame,
    min_matches: int = MIN_MATCHES,
    after=None,
    rule: tuple[str, float] | None = None,
) -> dict:
    """The round-13 test (amendment 1). Development (rule None): one run, once each anchored
    candidate has `min_matches` settled matches with its anchor; every candidate × threshold
    at 99.583%, the h2h-only chance beside them (descriptive). Confirmation (`rule` and
    `after`): one frozen rule at 95% on matches kicking off after `after`, from
    CONFIRM_MATCHES settled matches carrying that rule's input."""
    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab import metrics

    out = {"plumbing": plumbing(rows, dk)}
    m = with_market(rows, dk) if len(rows) else pd.DataFrame()
    team = rows[market_kind(rows) == "team_totals"] if len(rows) else rows
    p = pairs(team, results) if len(m) else pd.DataFrame()
    if len(p):
        cols = ["p_h2h"] + [f"p_{n}" for n in ANCHORS]
        ok = m.loc[m["dk_status"] == "ok", _KEY8 + cols].drop_duplicates(_KEY8)
        p = p.merge(ok, on=_KEY8)
    if after is not None and len(p):
        p = p[pd.to_datetime(p["kickoff"], utc=True) > pd.Timestamp(after)]
    cands = candidates(p) if len(p) else {}
    counts = {n: (_matches(p, np.isfinite(v)) if len(p) else 0) for n, v in cands.items()}
    out.update(stage="confirmation" if rule else "development", matches=counts)
    if rule:
        need = {rule[0]: CONFIRM_MATCHES}
    else:
        need = {n: min_matches for n in ANCHORS}
    short = {n: f"{counts.get(n, 0)} of {k}" for n, k in need.items() if counts.get(n, 0) < k}
    if short:
        out["note"] = "not enough data yet: settled matches " + ", ".join(
            f"{n} {v}" for n, v in short.items()
        )
        return out
    level = 0.95 if rule else bonferroni_level(MM_TESTS)
    groups = (p["league"] + "|" + p["home"] + "|" + p["kickoff"]).to_numpy()
    y = p["y"].to_numpy()
    look = p[["fair_over", "fair_under"]].to_numpy()
    odds = p[["over", "under"]].to_numpy()
    close = p[["fair_over_close", "fair_under_close"]].to_numpy()
    out.update(level=level, rows=int(len(p)), last_kickoff=str(p["kickoff"].max()))
    if rule:
        tests, names = [rule], [rule[0]]
    else:
        tests = [(c, t) for c in MM_CANDIDATES + MM_DESCRIPTIVE for t in MM_THRESHOLDS]
        names = list(MM_CANDIDATES + MM_DESCRIPTIVE)
    res: dict = {}
    for name in names:
        q = cands[name]
        ok = np.isfinite(q)
        if ok.sum() < 2:
            res[name] = {"rows": int(ok.sum())}
            continue
        two = np.column_stack([q, 1 - q])
        ev = metrics.evaluate(two, y, look, odds, close, groups, level=level)
        ev.pop("bets", None)
        ev["descriptive"] = name in MM_DESCRIPTIVE
        ev["vs_close"] = metrics.paired_gain(
            metrics.log_loss_rows(close[ok], y[ok]),
            metrics.log_loss_rows(two[ok], y[ok]),
            groups[ok],
            level=level,
        )
        ev["calibration"] = (
            metrics.calibration_table(two[ok], y[ok]).reset_index(names="bin").to_dict("records")
        )
        ev["rules"] = {}
        for c, t in tests:
            if c != name:
                continue
            b = metrics.bet_scores(two[ok], y[ok], odds[ok], close[ok], groups[ok], t, level)
            rng = b.get("clv_range")
            passed = bool(rng and rng[0] > 0 and b.get("bets", 0) >= MM_MIN_BETS)
            b["pass"] = passed and name not in MM_DESCRIPTIVE
            ev["rules"][f"{t:.2f}"] = b
        res[name] = ev
    out["candidates"] = res
    out["calibration_fanduel_look"] = (
        metrics.calibration_table(look, y).reset_index(names="bin").to_dict("records")
    )
    passed = [
        (c, float(t), b["clv_range"][0])
        for c, r in res.items()
        for t, b in r.get("rules", {}).items()
        if b["pass"]
    ]
    out["passes"] = [f"{c}:{t:.2f}" for c, t, _ in passed]
    if passed and not rule:
        best = max(passed, key=lambda x: (x[2], -x[1]))
        out["frozen_rule"] = f"{best[0]}:{best[1]:.2f}"
        out["confirm_after"] = out["last_kickoff"]
    return out
