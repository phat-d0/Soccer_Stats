"""Half-time in-play snapshots of every live match (lead brief 2, item 2; owner, 28 Oct).

An in-play dataset, from now on, for the comeback, cash-out and heavy-favourite
questions: twice a match, at about kickoff + 55 min (half-time) and + 75 min, one live
`/odds` call per league (h2h at ten books = one region, 1 credit) records every in-play
match's prices beside ESPN's score and clock at that moment.

* When: closes.yml dispatches inplay.yml at each kickoff cluster's first kickoff + 58 and
  + 78 minutes (close_runs.plan, kinds "inplay55" and "inplay75"). The run decides what is due
  (`due`): a league is due when one of its kickoffs (from data-log's DraftKings log, or for
  the odds-only competitions their free events files, inplay_events) is within a kind's
  window and that (league, kind, kickoff) isn't in the call record yet. An odds-only
  competition's kind pauses for EMPTY_PAUSE_DAYS after EMPTY_PAUSE empty calls (`paused`).
* Cost: a free events list first (0 credits) shows whether anything has started and reads
  the balance; then one `/odds` call per due league, only if the balance stays above the
  shared RESERVE and the month's spend under MONTHLY_CAP. Cost from `x-requests-last`.
* Log: odds_log/<code>_inplay_<YYYY-MM>.jsonl, one row per started match and book: league,
  home, away (football-data names), kickoff, kind (the nearest snapshot kind),
  minutes_since_kickoff, checked_at, book, prices, margin, last_update, age_min, and ESPN's
  score, clock, state and goals then. Call record odds_log/inplay_calls_<YYYY-MM>.jsonl
  (league, kinds, kickoffs covered, fetched_at, cost, credits_left, events, rows), read
  before every call so nothing is fetched twice. Descriptive data: nothing trades on it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats import inplay_events as ie
from soccer_stats.odds_feed import BOOKMAKERS, SPORTS, URL, _team  # the same ten books

EVENTS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
KINDS = {"inplay55": 55, "inplay75": 75}  # minutes after kickoff
# A kickoff is due for a kind while this many minutes have passed (from, up to): no overlap,
# so each call has one kind per kickoff. A cluster's call lands 8 minutes before to 6 after
# the target for its kickoffs (close_runs.INPLAY), with room for a slow start.
WINDOWS = {"inplay55": (45, 65), "inplay75": (65, 90)}
RESERVE = 3000
# A guard against a runaway loop: the nine live competitions need about 580 a month and the
# odds-only competitions (inplay_events, lead-approved 28 Oct) about 1,100 more.
MONTHLY_CAP = 3000
# Odds-only competitions (inplay_events.COMPETITIONS) may go unpriced at our ten books: a
# league-kind whose last EMPTY_PAUSE answered calls logged no rows, over 2+ kickoff days,
# makes no call for EMPTY_PAUSE_DAYS after the last one (corners_live's rule).
EMPTY_PAUSE = 6
EMPTY_PAUSE_DAYS = 7
COST = 1
DK_LOG = re.compile(r"^([A-Z0-9]+)_\d{4}-\d{2}\.jsonl$")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _int(headers, key) -> int | None:
    try:
        return int(float(headers.get(key)))
    except (TypeError, ValueError):
        return None


def _jsonl(paths) -> list[dict]:
    rows = []
    for p in paths:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _dk_logs(log_dir: Path, league: str = "*") -> list[Path]:
    """data-log's DraftKings logs, <code>_<YYYY-MM>.jsonl (not books_, corners_, ...)."""
    return [p for p in sorted(Path(log_dir).glob(f"{league}_*.jsonl")) if DK_LOG.match(p.name)]


def sport_of(league: str) -> str | None:
    """The Odds API sport key: a registry league's, else an odds-only competition's."""
    return SPORTS.get(league) or ie.sport_of(league)


def kickoffs(log_dir: Path) -> dict[str, list[pd.Timestamp]]:
    """Each league's logged kickoffs: data-log's DraftKings log, plus the odds-only
    competitions' events files (inplay_events)."""
    out: dict[str, set] = {k: set(v) for k, v in ie.kickoffs(log_dir).items()}
    for p in _dk_logs(log_dir):
        code = DK_LOG.match(p.name).group(1)
        for r in _jsonl([p]):
            if r.get("kickoff"):
                try:
                    out.setdefault(r.get("league") or code, set()).add(_ts(r["kickoff"]))
                except (TypeError, ValueError):
                    continue
    return {k: sorted(v) for k, v in out.items()}


def known_teams(log_dir: Path, league: str) -> set[str]:
    rows = _jsonl(_dk_logs(log_dir, league))
    return {r[s] for r in rows for s in ("home", "away") if r.get(s)}


def calls(log_dir: Path) -> list[dict]:
    return _jsonl(sorted(Path(log_dir).glob("inplay_calls_*.jsonl")))


def done_keys(records: list[dict]) -> set[tuple[str, str, str]]:
    """(league, kind, kickoff ISO) already snapshotted (or tried) in the call record."""
    out = set()
    for c in records:
        for kind in c.get("kinds") or []:
            for k in c.get("kickoffs") or []:
                out.add((c["league"], kind, _ts(k).isoformat()))
    return out


def kind_of(elapsed: float) -> str | None:
    """The snapshot kind whose window holds `elapsed` minutes since kickoff, or None."""
    for kind, (lo, hi) in WINDOWS.items():
        if lo <= elapsed < hi:
            return kind
    return None


def nearest_kind(elapsed: float) -> str:
    return min(KINDS, key=lambda k: abs(KINDS[k] - elapsed))


def due(ks_by: dict[str, list], records: list[dict], now: pd.Timestamp) -> dict[str, dict]:
    """{league: {kinds, kickoffs}} for each league with a kickoff inside a kind's window
    whose (league, kind, kickoff) isn't in the call record yet."""
    done = done_keys(records)
    out: dict[str, dict] = {}
    for lg, ks in ks_by.items():
        for k in ks:
            kind = kind_of((now - _ts(k)) / pd.Timedelta(minutes=1))
            if kind and (lg, kind, _ts(k).isoformat()) not in done:
                d = out.setdefault(lg, {"kinds": set(), "kickoffs": set()})
                d["kinds"].add(kind)
                d["kickoffs"].add(_ts(k))
    return {
        lg: {"kinds": sorted(d["kinds"]), "kickoffs": sorted(d["kickoffs"])}
        for lg, d in out.items()
    }


def paused(records: list[dict], league: str, kind: str, now: pd.Timestamp):
    """Until when an odds-only competition's kind is paused (EMPTY_PAUSE answered calls in a
    row with no rows, over 2+ kickoff days, the last within EMPTY_PAUSE_DAYS), else None."""
    if league not in ie.COMPETITIONS:
        return None
    answered = sorted(
        (
            c
            for c in records
            if c.get("league") == league
            and kind in (c.get("kinds") or [])
            and c.get("status") == 200
        ),
        key=lambda c: str(c.get("fetched_at")),
    )[-EMPTY_PAUSE:]
    if len(answered) < EMPTY_PAUSE or any(c.get("rows") for c in answered):
        return None
    days = {str(k)[:10] for c in answered for k in c.get("kickoffs") or []}
    if len(days) < 2:
        return None
    until = _ts(answered[-1]["fetched_at"]) + pd.Timedelta(days=EMPTY_PAUSE_DAYS)
    return until if now < until else None


def _espn_live(league: str) -> list[dict]:
    """ESPN's live scoreboard for a league or an odds-only competition (free)."""
    from soccer_stats.lab.inplay_probe import espn_live

    return espn_live(league, slug=ie.slug_of(league))


def month_spent(records: list[dict], now: pd.Timestamp) -> int:
    m = now.strftime("%Y-%m")
    return sum(
        int(c.get("cost") or 0) for c in records if str(c.get("fetched_at", "")).startswith(m)
    )


def rows_from(body: list, league: str, now: pd.Timestamp, known: set[str], live: list[dict]):
    """One row per started event and book with all three h2h prices."""
    from soccer_stats.lab.inplay_probe import match_espn

    out = []
    # The odds-only competitions keep the feed's own names (no football-data to map to).
    name = (lambda n: n) if league in ie.COMPETITIONS else (lambda n: _team(n, known))
    for ev in body if isinstance(body, list) else []:
        ko = _ts(ev["commence_time"])
        if ko > now:
            continue
        elapsed = (now - ko) / pd.Timedelta(minutes=1)
        espn = match_espn(ev.get("home_team"), ev.get("away_team"), live) or {}
        for b in ev.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                p = {o.get("name"): o.get("price") for o in m.get("outcomes") or []}
                prices = {
                    "home": p.get(ev.get("home_team")),
                    "draw": p.get("Draw"),
                    "away": p.get(ev.get("away_team")),
                }
                if not all(isinstance(v, (int, float)) and v > 1 for v in prices.values()):
                    continue
                upd = m.get("last_update") or b.get("last_update")
                out.append(
                    {
                        "league": league,
                        "home": name(ev["home_team"]),
                        "away": name(ev["away_team"]),
                        "kickoff": ko.isoformat(timespec="seconds"),
                        "kind": nearest_kind(elapsed),
                        "minutes_since_kickoff": round(elapsed, 1),
                        "checked_at": now.isoformat(timespec="seconds"),
                        "book": b.get("key"),
                        "prices": prices,
                        "margin": round(sum(1 / v for v in prices.values()) - 1, 4),
                        "last_update": upd,
                        "age_min": round((now - _ts(upd)) / pd.Timedelta(minutes=1), 1)
                        if upd
                        else None,
                        "espn_score": espn.get("score"),
                        "espn_clock": espn.get("clock"),
                        "espn_state": espn.get("state"),
                        "espn_goals": espn.get("goals"),
                    }
                )
    return out


def run(
    log_dir: Path,
    api_key: str,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    espn: Callable | None = None,
    out_dir: Path | None = None,
) -> dict:
    """Snapshot every due league once, reading kickoffs and call records from `log_dir`
    (data-log's odds_log); new rows and call records are appended to `out_dir` (default
    `log_dir`), the same file names, so a workflow can add them to data-log again after a
    failed push without calling twice. Returns {due, calls, credits, rows, credits_left,
    stop, log}."""
    now = _ts(now) if now is not None else pd.Timestamp.now(tz="UTC")
    log_dir = Path(log_dir)
    records = calls(log_dir)
    todo = due(kickoffs(log_dir), records, now)
    res = {"due": {k: v["kinds"] for k, v in todo.items()}, "calls": 0, "credits": 0, "rows": 0}
    res["log"] = []
    if not todo:
        res["stop"] = "nothing due"
        return res
    if not api_key:
        res["stop"] = "no ODDS_API_KEY (only the default branch snapshots)"
        return res
    spent = month_spent(records, now)
    rows_out: list[dict] = []
    calls_out: list[dict] = []
    for lg, d in todo.items():
        sport = sport_of(lg)
        if sport is None:
            continue
        kinds = [k for k in d["kinds"] if paused(records, lg, k, now) is None]
        if not kinds:
            res.setdefault("paused", []).append(lg)
            res["log"].append(f"{lg}: paused (no prices in the last {EMPTY_PAUSE} calls)")
            continue
        d = {**d, "kinds": kinds}
        try:  # free: is anything in play, and the balance
            r = get(EVENTS_URL.format(sport=sport), params={"apiKey": api_key}, timeout=20)
        except requests.RequestException as exc:
            res["log"].append(f"{lg}: events list failed ({type(exc).__name__})")
            continue
        left = _int(r.headers, "x-requests-remaining")
        events = r.json() if r.status_code == 200 else []
        events = events if isinstance(events, list) else []
        started = [e for e in events if e.get("commence_time") and _ts(e["commence_time"]) <= now]
        if not started:
            res["log"].append(f"{lg}: nothing in play in the events list")
            continue
        if left is not None and left - COST < RESERVE:
            res["stop"] = f"the {RESERVE}-credit reserve (left {left})"
            break
        if spent + COST > MONTHLY_CAP:
            res["stop"] = f"monthly cap: {spent} of {MONTHLY_CAP}"
            break
        try:
            r = get(
                URL.format(sport=sport),
                params={
                    "apiKey": api_key,
                    "bookmakers": ",".join(BOOKMAKERS),
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
                timeout=20,
            )
        except requests.RequestException as exc:  # the message could hold the key
            res["log"].append(f"{lg}: odds call failed ({type(exc).__name__})")
            continue
        cost = _int(r.headers, "x-requests-last")
        cost = COST if cost is None else cost
        spent += cost
        res["credits"] += cost
        res["credits_left"] = _int(r.headers, "x-requests-remaining")
        body = r.json() if r.status_code == 200 else []
        try:  # the call is paid: a parsing failure must still leave its call record
            live = (espn or _espn_live)(lg)
            rows = rows_from(body, lg, now, known_teams(log_dir, lg), live)
        except Exception as exc:  # noqa: BLE001
            rows = []
            res["log"].append(f"{lg}: rows failed ({type(exc).__name__})")
        rows_out += rows
        calls_out.append(
            {
                "league": lg,
                "kinds": d["kinds"],
                "kickoffs": [k.isoformat() for k in d["kickoffs"]],
                "fetched_at": now.isoformat(timespec="seconds"),
                "status": r.status_code,
                "cost": cost,
                "credits_left": res.get("credits_left"),
                "events": len(body) if isinstance(body, list) else 0,
                "rows": len(rows),
            }
        )
        res["calls"] += 1
        res["log"].append(
            f"{lg} {'+'.join(d['kinds'])}: HTTP {r.status_code}, cost {cost}, {len(rows)} rows, "
            f"credits left {res.get('credits_left')}"
        )
    append(Path(out_dir) if out_dir else log_dir, rows_out, calls_out, now)
    res["rows"] = len(rows_out)
    return res


def append(log_dir: Path, rows: list[dict], records: list[dict], now: pd.Timestamp) -> None:
    month = now.strftime("%Y-%m")
    log_dir.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for r in rows:
        by_file.setdefault(log_dir / f"{r['league']}_inplay_{month}.jsonl", []).append(
            json.dumps(r, separators=(",", ":"))
        )
    for c in records:
        by_file.setdefault(log_dir / f"inplay_calls_{month}.jsonl", []).append(
            json.dumps(c, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


def summary_line(res: dict) -> str:
    return (
        f"In-play snapshots: {res['calls']} calls, {res['credits']} credits, {res['rows']} rows"
        + (f"; due {res['due']}" if res.get("due") else "")
        + (f"; credits left {res['credits_left']}" if res.get("credits_left") is not None else "")
        + (f"; paused {', '.join(res['paused'])}" if res.get("paused") else "")
        + (f"; stopped: {res['stop']}" if res.get("stop") else "")
    )
