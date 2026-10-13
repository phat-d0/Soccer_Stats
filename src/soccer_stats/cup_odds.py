"""Historical closing prices for a UEFA cup (owner decision, 13 Oct 2026: two seasons of
Champions League closes, cap 1,200 credits; docs/leagues.md "Champions League:
historical closes download").

    soccer-stats backfill-cup-odds --cup UCL --seasons 2024,2025 --cap 0      # dry run
    soccer-stats backfill-cup-odds --cup UCL --seasons 2024,2025 --cap 1200 \
        --state <data-log copies> --pending out/cup_odds
    soccer-stats log-cup-odds --cup UCL --pending out/cup_odds --log-dir <data-log>

What it buys: The Odds API's historical bulk odds (`/v4/historical/sports/{sport}/odds`,
`markets=h2h`, `bookmakers=pinnacle,draftkings`, decimal) once per kickoff slot of ESPN's
calendar (cups.season_calendar, main slug only: league phase and knockouts, never the
qualifying rounds), at the slot's kickoff minus CLOSE_MINUTES floored to the API's
5-minute steps. One bulk snapshot prices every match listed at that moment, so it costs
HIST_COST (10) credits per market per region, and up to ten named books count as one
region: about 10 credits a slot.

Spend control:
* `player_goal_odds.CappedBudget`: a call is skipped when its largest possible cost would
  pass the cap or leave under 3,000 credits on the shared key (the balance is read first
  from the free `/v4/sports` call); the cost counted is `x-requests-last` on every call.
* The first paid call with both books is budgeted at 20 (one region per book, the worst
  case). If it really costs twice HIST_COST, DraftKings is dropped for the rest of the run
  and the log says so; otherwise later calls are budgeted at the measured cost.
* `cap` 0 (or no key) is a dry run: no Odds API call at all, only the plan (snapshots,
  matches per snapshot, the cost estimate). Cached bodies are still read (0 credits).
* Any HTTP error or network failure stops further calls (the error type only is kept;
  an API message is redacted before it is kept).

Never paying twice:
* Every body is cached under `data/raw/cup_odds/<sport>_<YYYYmmddTHHMM>Z.json` (keyed by
  sport and requested snapshot time) and reused before any call.
* The durable copy is data-log's `odds_history/<cup>_closes.jsonl` (one row per match and
  book) and `odds_history/<cup>_close_calls.jsonl` (one row per snapshot call). The job
  reads both first (`state`): a slot whose close call is recorded with status 200, or
  whose matches all hold a Pinnacle row, is never requested again; a slot retried once is
  never retried again.
* A second pass asks each slot's `previous_timestamp` once (the snapshot 5 minutes
  earlier) when some of its matches still have no Pinnacle price, only after every slot's
  first snapshot and only while the cap allows.

The key travels only in request params. It is never printed, logged, cached or written:
`redact` runs on every message, the report and the JSON.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats import cups
from soccer_stats.data import RAW_DIR
from soccer_stats.leagues import LEAGUES
from soccer_stats.player_goal_odds import CappedBudget
from soccer_stats.player_odds import BASE, RESERVE_CREDITS

CACHE = "cup_odds"  # under RAW_DIR
LOG_DIR = "odds_history"  # on data-log
BOOKS = ("pinnacle", "draftkings")
PRIMARY_BOOK = "pinnacle"
MARKET = "h2h"
HIST_COST = cups.HIST_COST  # credits per market per region per snapshot
CLOSE_MINUTES = 5  # the close: the last 5-minute snapshot at least this long before kickoff
STEP = "5min"  # The Odds API keeps historical snapshots at 5-minute steps
MATCH_HOURS = 3  # an odds event this close to an ESPN kickoff can be that match
SLOT_MINUTES = 15  # an odds event this close to a slot kickoff that pairs with nothing
STRONG = ("alias", "name", "words")  # name matches good enough to pair on one team
SPELLING = 0.88  # a close spelling (difflib ratio), as cups.match_team uses
URL = BASE + "/historical/sports/{sport}/odds"

# The Odds API's spellings -> ESPN's, where the names don't normalize to each other.
# An alias is authoritative: an Odds API name with one never pairs with another ESPN name
# (so "Inter Milan" can't pair with ESPN's "AC Milan" on the shared word).
ODDS_TO_ESPN: dict[str, str] = {
    "Inter Milan": "Internazionale",
    "Paris Saint Germain": "Paris Saint-Germain",
    "Sporting Lisbon": "Sporting CP",
    "Athletic Bilbao": "Athletic Club",
    "FC Copenhagen": "F.C. København",
    "Copenhagen": "F.C. København",
    "Union Saint-Gilloise": "Union St.-Gilloise",
    "Union Saint Gilloise": "Union St.-Gilloise",
    "Olympiakos": "Olympiacos",
    "Olympiakos Piraeus": "Olympiacos",
    "Crvena Zvezda": "Red Star Belgrade",
    "FK Crvena Zvezda": "Red Star Belgrade",
    "Stade Brestois 29": "Brest",
    "Feyenoord": "Feyenoord Rotterdam",
    "Ajax": "Ajax Amsterdam",
    "Bodø/Glimt": "Bodo/Glimt",
    "Bodo Glimt": "Bodo/Glimt",
    "Qarabağ FK": "Qarabag",
    "Qarabag FK": "Qarabag",
    "GNK Dinamo Zagreb": "Dinamo Zagreb",
    "SK Slavia Praha": "Slavia Prague",
    "AC Sparta Praha": "Sparta Prague",
}


def iso(t) -> str:
    return pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(t) -> str:
    return pd.Timestamp(t).tz_convert("UTC").strftime("%Y%m%dT%H%MZ")


def redact(text: str, api_key: str) -> str:
    """Remove the key from any text that might be kept."""
    return str(text).replace(api_key, "***") if api_key else str(text)


def season_label(year: int) -> str:
    return f"{year}/{(year + 1) % 100:02d}"


# ---------- the plan ----------


def request_time(kickoff) -> pd.Timestamp:
    """The close snapshot for a kickoff: CLOSE_MINUTES before it, floored to 5 minutes."""
    return (cups._ts(kickoff) - pd.Timedelta(minutes=CLOSE_MINUTES)).floor(STEP)


def plan(events_by_season: dict[int, list[dict]]) -> list[dict]:
    """One snapshot per kickoff slot (kickoffs sharing a request time share it), oldest
    first: {requested, season, kickoffs, events}."""
    snaps: dict[pd.Timestamp, dict] = {}
    for y, events in sorted(events_by_season.items()):
        for e in events:
            t = request_time(e["kickoff"])
            s = snaps.setdefault(t, {"requested": t, "season": y, "kickoffs": set(), "events": []})
            s["kickoffs"].add(cups._ts(e["kickoff"]))
            s["events"].append(e)
    out = []
    for t in sorted(snaps):
        s = snaps[t]
        s["kickoffs"] = sorted(s["kickoffs"])
        s["events"] = sorted(s["events"], key=lambda e: (e["kickoff"], e["id"]))
        out.append(s)
    return out


# ---------- team names ----------


def same_team(odds_name: str, espn_name: str) -> str | None:
    """How an Odds API name matches an ESPN name: "alias", "name" (equal once normalized:
    no accents, punctuation or filler words like FC), "words" (one name's words inside the
    other's, half or more), "spelling" (difflib ratio >= SPELLING) or None."""
    if not odds_name or not espn_name:
        return None
    alias = ODDS_TO_ESPN.get(odds_name)
    if alias:
        return "alias" if alias == espn_name else None
    a, b = cups.norm(odds_name), cups.norm(espn_name)
    if not a or not b:
        return None
    if a == b:
        return "name"
    small, big = sorted((set(a.split()), set(b.split())), key=len)
    if small and small <= big and 2 * len(small) >= len(big):
        return "words"
    if difflib.SequenceMatcher(None, a, b).ratio() >= SPELLING:
        return "spelling"
    return None


def pair_events(espn_events: list[dict], odds_events: list[dict]) -> list[dict]:
    """Pair ESPN matches with Odds API events kicking off within MATCH_HOURS: both teams
    matching (one of them strongly: alias, name or words) first, then one team matching
    strongly; a pair of close spellings alone never pairs; straight
    before home/away swapped; each event used once. Returns [{espn, odds, how, swapped,
    home_how, away_how}]."""
    cands = []
    for i, e in enumerate(espn_events):
        k = cups._ts(e["kickoff"])
        for j, o in enumerate(odds_events):
            try:
                c = pd.Timestamp(o.get("commence_time"))
            except (TypeError, ValueError):
                continue
            if pd.isna(c) or abs(c - k) > pd.Timedelta(hours=MATCH_HOURS):
                continue
            for swapped in (False, True):
                oh, oa = o.get("home_team"), o.get("away_team")
                if swapped:
                    oh, oa = oa, oh
                h, a = same_team(oh, e["home"]), same_team(oa, e["away"])
                if h and a and (h in STRONG or a in STRONG):
                    rank = 0
                elif h in STRONG or a in STRONG:
                    rank = 2
                else:
                    continue
                cands.append((rank + swapped, i, j, swapped, h, a))
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    used_e: set[int] = set()
    used_o: set[int] = set()
    out = []
    for rank, i, j, swapped, h, a in cands:
        if i in used_e or j in used_o:
            continue
        used_e.add(i)
        used_o.add(j)
        out.append(
            {
                "espn": espn_events[i],
                "odds": odds_events[j],
                "how": "both" if rank < 2 else "one",
                "swapped": swapped,
                "home_how": h or "partner",
                "away_how": a or "partner",
            }
        )
    return out


# ---------- prices ----------


def h2h_prices(event: dict, books) -> dict[str, dict]:
    """{book: {"outcomes": {name: decimal}, "last_update"}} for the h2h market of the
    books asked; a book needs all three outcomes (two teams and the draw)."""
    out = {}
    for b in event.get("bookmakers") or []:
        if not isinstance(b, dict) or b.get("key") not in books:
            continue
        for m in b.get("markets") or []:
            if not isinstance(m, dict) or m.get("key") != MARKET:
                continue
            prices = {}
            for o in m.get("outcomes") or []:
                try:
                    prices[str(o["name"])] = float(o["price"])
                except (KeyError, TypeError, ValueError):
                    continue
            names = {event.get("home_team"), event.get("away_team"), "Draw"}
            if names <= set(prices):
                out[b["key"]] = {
                    "outcomes": prices,
                    "last_update": m.get("last_update") or b.get("last_update"),
                }
    return out


def rows_from_body(
    cup: str, snap: dict, body: dict, books, kind: str, have: set[tuple[str, str]]
) -> tuple[list[dict], dict]:
    """Close rows for the snapshot's own matches (one per match and book not in `have`),
    plus {pairs, name_map, unmatched} for the log."""
    events = (body or {}).get("data") if isinstance(body, dict) else None
    events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    taken = pd.Timestamp(body.get("timestamp")) if body.get("timestamp") else None
    pairs = pair_events(snap["events"], events)
    rows, name_map = [], {}
    for p in pairs:
        e, o = p["espn"], p["odds"]
        oh, oa = o.get("home_team"), o.get("away_team")
        if p["swapped"]:
            oh, oa = oa, oh
        name_map[oh] = {"espn": e["home"], "how": p["home_how"]}
        name_map[oa] = {"espn": e["away"], "how": p["away_how"]}
        k = cups._ts(e["kickoff"])
        for book, q in h2h_prices(o, books).items():
            if (str(e["id"]), book) in have:
                continue
            prices = q["outcomes"]
            rows.append(
                {
                    "cup": cup,
                    "season": season_label(snap["season"]),
                    "stage": e.get("stage"),
                    "date": f"{k:%Y-%m-%d}",
                    "kickoff": iso(k),
                    "espn_id": str(e["id"]),
                    "home": e["home"],
                    "away": e["away"],
                    "odds_home": oh,
                    "odds_away": oa,
                    "event_id": o.get("id"),
                    "commence_time": o.get("commence_time"),
                    "book": book,
                    "price_home": prices[oh],
                    "price_draw": prices["Draw"],
                    "price_away": prices[oa],
                    "snapshot": iso(taken) if taken is not None else None,
                    "requested": iso(snap["requested"]),
                    "minutes_before": round((k - taken).total_seconds() / 60, 1)
                    if taken is not None
                    else None,
                    "last_update": q["last_update"],
                    "kind": kind,
                    "match": p["how"],
                    "swapped": p["swapped"],
                }
            )
    paired = {id(p["odds"]) for p in pairs}
    unmatched = []
    for o in events:
        if id(o) in paired:
            continue
        try:
            c = pd.Timestamp(o.get("commence_time"))
        except (TypeError, ValueError):
            continue
        near = any(abs(c - k) <= pd.Timedelta(minutes=SLOT_MINUTES) for k in snap["kickoffs"])
        if near:
            unmatched.append(f"{o.get('home_team')} v {o.get('away_team')} ({o['commence_time']})")
    return rows, {"pairs": len(pairs), "name_map": name_map, "unmatched": unmatched}


# ---------- data-log state ----------


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")


def closes_name(cup: str) -> str:
    return f"{cup}_closes.jsonl"


def calls_name(cup: str) -> str:
    return f"{cup}_close_calls.jsonl"


def _state_file(state: Path | None, name: str) -> Path | None:
    """A data-log file in a flat state copy, or in a data-log checkout's odds_history/."""
    if not state:
        return None
    for p in (Path(state) / name, Path(state) / LOG_DIR / name):
        if p.exists():
            return p
    return None


def load_state(state: Path | None, cup: str) -> tuple[list[dict], list[dict]]:
    """(close rows, call records) already on data-log."""
    rows_p, calls_p = _state_file(state, closes_name(cup)), _state_file(state, calls_name(cup))
    return (_read_jsonl(rows_p) if rows_p else [], _read_jsonl(calls_p) if calls_p else [])


def row_key(r: dict) -> tuple[str, str]:
    return (str(r.get("espn_id")), str(r.get("book")))


def call_key(c: dict) -> tuple[str, str, str, str]:
    """A call record's identity; the status is part of it, so a later success is still
    written after a recorded error."""
    return (str(c.get("sport")), str(c.get("kind")), str(c.get("slot")), str(c.get("status")))


def log(pending: Path, log_root: Path, cup: str) -> tuple[int, int]:
    """Append a run's new rows and calls to data-log's odds_history/, deduplicated (one
    row per match and book, one call per sport, kind and slot). Returns the counts."""
    pending, d = Path(pending), Path(log_root) / LOG_DIR
    rows_path, calls_path = d / closes_name(cup), d / calls_name(cup)
    seen = {row_key(r) for r in _read_jsonl(rows_path)}
    keep = []
    for r in _read_jsonl(pending / "rows.jsonl"):
        if row_key(r) not in seen:
            seen.add(row_key(r))
            keep.append(r)
    _append(rows_path, keep)
    done = {call_key(c) for c in _read_jsonl(calls_path)}
    new_calls = []
    for c in _read_jsonl(pending / "calls.jsonl"):
        if call_key(c) not in done:
            done.add(call_key(c))
            new_calls.append(c)
    _append(calls_path, new_calls)
    return len(keep), len(new_calls)


# ---------- calls ----------


def cache_path(cache_dir: Path, sport: str, at) -> Path:
    return Path(cache_dir) / f"{sport}_{stamp(at)}.json"


def balance(get: Callable, api_key: str) -> int | None:
    """Credits left on the key, from the free `/v4/sports` list (it costs nothing)."""
    try:
        r = get(f"{BASE}/sports", params={"apiKey": api_key}, timeout=30)
        rem = r.headers.get("x-requests-remaining")
        return int(float(rem)) if rem not in (None, "") else None
    except (requests.RequestException, ValueError, AttributeError):
        return None


def _call(get, url, params, budget, estimate, label, api_key) -> tuple[dict | None, dict]:
    """One capped call: (body or None, {status, cost, skipped, message, stop})."""
    if not budget.allows(estimate):
        why = (
            f"would leave under the {RESERVE_CREDITS}-credit reserve ({budget.left} left)"
            if budget.left is not None and budget.left - estimate < RESERVE_CREDITS
            else f"would pass the {budget.cap}-credit cap"
        )
        budget.log.append(f"{label}: skipped, {why}")
        return None, {"skipped": True, "cost": 0, "status": None, "message": why}
    try:
        r = get(url, params=params, timeout=60)
    except requests.RequestException as exc:  # the message would hold the URL and key
        budget.log.append(f"{label}: could not reach The Odds API ({type(exc).__name__})")
        return None, {
            "skipped": False,
            "cost": 0,
            "status": None,
            "stop": True,
            "message": f"could not reach The Odds API ({type(exc).__name__})",
        }
    before = budget.spent
    budget.charge(r, estimate, label)
    info = {"skipped": False, "cost": budget.spent - before, "status": r.status_code}
    if not r.ok:
        try:
            msg = str((r.json() or {}).get("message", ""))
        except (ValueError, AttributeError):
            msg = ""
        info["message"] = redact(msg, api_key)[:300]
        info["stop"] = True
        budget.log.append(f"{label}: HTTP {r.status_code} {info['message']}".rstrip())
        return None, info
    try:
        body = r.json()
    except ValueError:
        info["message"], info["stop"] = "body was not JSON", True
        return None, info
    if not isinstance(body, dict):
        info["message"], info["stop"] = "body was not an object", True
        return None, info
    return body, info


# ---------- the run ----------


def run(
    cup: str = "UCL",
    seasons=cups.SEASONS,
    cap: int = 0,
    api_key: str | None = None,
    state: Path | None = None,
    raw_dir: Path = RAW_DIR,
    cache_dir: Path | None = None,
    odds_get: Callable = requests.get,
    espn_get: Callable = requests.get,
    today: pd.Timestamp | None = None,
    retry: bool = True,
    books=BOOKS,
    calendars: dict[int, list[dict]] | None = None,
) -> dict:
    """ESPN's calendar, the snapshot plan, then each slot's close: held on data-log (skip),
    cached (0 credits), planned (dry run) or bought within the cap. Returns the result
    with `rows` and `calls` (new this run) for `log`."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    sport = LEAGUES[cup].odds_sport
    cache_dir = Path(cache_dir) if cache_dir is not None else Path(raw_dir) / CACHE
    books = list(books)
    dry = cap <= 0 or not api_key
    budget = CappedBudget(max(int(cap), 0))
    res: dict = {
        "cup": cup,
        "sport": sport,
        "seasons": [int(y) for y in seasons],
        "cap": int(cap),
        "dry_run": dry,
        "books": list(books),
        "dropped_draftkings": False,
        "log": budget.log,
    }
    if cap <= 0:
        budget.log.append("Dry run (cap 0): no Odds API calls")
    elif not api_key:
        budget.log.append("No ODDS_API_KEY: no Odds API calls")

    # ESPN's calendar (free): league phase and knockouts only (the main slug).
    if calendars is None:
        calendars, res["calendar"] = {}, {}
        for y in res["seasons"]:
            start, end = cups.season_window(y)
            cal = cups.season_calendar(cups.CUPS[cup], start, end, raw_dir, espn_get, today=today)
            calendars[y] = cal["events"]
            res["calendar"][str(y)] = {k: cal[k] for k in ("requests", "failures", "mode", "days")}
    calendars = {y: list(calendars.get(y, [])) for y in res["seasons"]}
    snaps = plan(calendars)

    held_rows, held_calls = load_state(state, cup)
    have = {row_key(r) for r in held_rows}
    done_calls = {call_key(c)[:3] for c in held_calls if c.get("status") == 200}
    new_rows: list[dict] = []
    new_calls: list[dict] = []
    name_map: dict[str, dict] = {}
    unmatched: list[str] = []

    if not dry:
        budget.left = balance(odds_get, api_key)
        res["left_before"] = budget.left
        budget.log.append(f"Credits left before the run: {budget.left}")
    measured: int | None = None  # cost of a paid call with the current book list
    stopped: str | None = None

    def pin_missing(s: dict) -> bool:
        return any((str(e["id"]), PRIMARY_BOOK) not in have for e in s["events"])

    def fetch(s: dict, at: pd.Timestamp, kind: str) -> tuple[dict | None, dict]:
        nonlocal measured, books, stopped
        path = cache_path(cache_dir, sport, at)
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                return saved["body"], {
                    "source": "cache",
                    "cost": 0,
                    "cost_when_fetched": saved.get("cost"),
                    "status": saved.get("status", 200),
                    "books": saved.get("books"),
                    "fetched_at": saved.get("fetched_at"),
                }
            except (OSError, ValueError, KeyError):
                pass
        if dry:
            return None, {"source": "planned"}
        if stopped:
            return None, {"source": "skipped", "message": stopped}
        estimate = measured if measured else HIST_COST * len(books)
        label = f"{cup} {kind} {iso(at)} ({len(s['events'])} matches)"
        body, info = _call(
            odds_get,
            URL.format(sport=sport),
            {
                "apiKey": api_key,
                "bookmakers": ",".join(books),
                "markets": MARKET,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
                "date": iso(at),
            },
            budget,
            estimate,
            label,
            api_key,
        )
        if info.get("skipped"):
            return None, {"source": "skipped", "message": info.get("message")}
        info.update(
            {
                "source": "api",
                "cost_when_fetched": info.get("cost"),
                "books": list(books),
                "fetched_at": iso(pd.Timestamp.now(tz="UTC")),
            }
        )
        if info.get("stop"):
            stopped = info.get("message") or f"HTTP {info.get('status')}"
            stopped = f"stopped after an error ({stopped})"
        if body is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "sport": sport,
                        "requested": iso(at),
                        "books": list(books),
                        "status": info["status"],
                        "cost": info["cost"],
                        "fetched_at": info["fetched_at"],
                        "body": body,
                    }
                )
            )
            cost = info["cost"]
            if cost and cost > 0:
                if "draftkings" in books and len(books) > 1 and cost >= 2 * HIST_COST:
                    books = [b for b in books if b != "draftkings"]
                    measured = None
                    res["dropped_draftkings"] = True
                    budget.log.append(
                        f"Adding DraftKings doubled the cost (x-requests-last {cost}): "
                        "DraftKings dropped for the rest of the run"
                    )
                elif measured is None or cost > measured:
                    measured = cost
        return body, info

    def take(s: dict, body: dict, info: dict, kind: str, at: pd.Timestamp) -> None:
        rows, notes = rows_from_body(cup, s, body, BOOKS, kind, have)
        for r in rows:
            have.add(row_key(r))
        new_rows.extend(rows)
        name_map.update(notes["name_map"])
        unmatched.extend(notes["unmatched"])
        books_rows = {b: sum(r["book"] == b for r in rows) for b in BOOKS}
        new_calls.append(
            {
                "cup": cup,
                "sport": sport,
                "season": season_label(s["season"]),
                "kind": kind,
                "slot": iso(s["requested"]),
                "requested": iso(at),
                "timestamp": body.get("timestamp"),
                "previous_timestamp": body.get("previous_timestamp"),
                "books": info.get("books"),
                "status": info.get("status", 200),
                "cost": info.get("cost_when_fetched"),
                "fetched_at": info.get("fetched_at"),
                "source": info["source"],
                "events": len(body.get("data") or []),
                "slot_matches": len(s["events"]),
                "paired": notes["pairs"],
                **{f"rows_{b}": n for b, n in books_rows.items()},
            }
        )

    status: list[dict] = []
    for s in snaps:
        key = (sport, "close", iso(s["requested"]))
        st = {"requested": iso(s["requested"]), "season": s["season"], "matches": len(s["events"])}
        status.append(st)
        if key in done_calls or not pin_missing(s):
            st["state"] = "held"
            continue
        body, info = fetch(s, s["requested"], "close")
        st["state"] = info["source"]
        if body is not None:
            take(s, body, info, "close", s["requested"])
        elif info["source"] == "api":
            new_calls.append(
                {
                    "cup": cup,
                    "sport": sport,
                    "season": season_label(s["season"]),
                    "kind": "close",
                    "slot": iso(s["requested"]),
                    "requested": iso(s["requested"]),
                    "status": info.get("status"),
                    "cost": info.get("cost"),
                    "fetched_at": info.get("fetched_at"),
                    "source": "api",
                    "message": info.get("message"),
                }
            )
        st["body"] = body is not None
        s["_body"] = body

    # Second pass: each slot's previous snapshot once, while matches lack Pinnacle.
    retried = 0
    if retry:
        for s, st in zip(snaps, status, strict=True):
            body = s.pop("_body", None)
            if st["state"] in ("held", "planned", "skipped") or body is None:
                continue
            if (sport, "retry", iso(s["requested"])) in done_calls or not pin_missing(s):
                continue
            prev = body.get("previous_timestamp")
            if not prev:
                continue
            at = cups._ts(prev)
            pbody, pinfo = fetch(s, at, "retry")
            if pbody is not None:
                take(s, pbody, pinfo, "retry", at)
                retried += 1
                st["retry"] = pinfo["source"]
            elif pinfo["source"] == "skipped":
                st["retry"] = "skipped"

    all_rows = held_rows + new_rows
    coverage = {}
    for y in res["seasons"]:
        events = calendars.get(y, [])
        ids = {str(e["id"]) for e in events}
        priced = {b: {r["espn_id"] for r in all_rows if r.get("book") == b} & ids for b in BOOKS}
        ys = [st for st in status if st["season"] == y]
        coverage[str(y)] = {
            "label": season_label(y),
            "matches": len(events),
            "slots": len(ys),
            "held": sum(st["state"] == "held" for st in ys),
            "cached": sum(st["state"] == "cache" for st in ys),
            "bought": sum(st["state"] == "api" for st in ys),
            "planned": sum(st["state"] == "planned" for st in ys),
            "skipped": sum(st["state"] == "skipped" for st in ys),
            "pinnacle": len(priced["pinnacle"]),
            "draftkings": len(priced["draftkings"]),
            "estimate_credits": HIST_COST * sum(st["state"] == "planned" for st in ys),
        }
    state_at = {st["requested"]: st["state"] for st in status}
    unpriced = [
        f"{e['home']} v {e['away']} ({iso(e['kickoff'])})"
        for y in res["seasons"]
        for e in calendars.get(y, [])
        if (str(e["id"]), PRIMARY_BOOK) not in have
        and state_at.get(iso(request_time(e["kickoff"]))) in ("api", "cache", "held")
    ]
    res.update(
        {
            "snapshots": status,
            "coverage": coverage,
            "rows": new_rows,
            "calls": new_calls,
            "retried": retried,
            "name_map": dict(sorted(name_map.items())),
            "unmatched": sorted(set(unmatched)),
            "unpriced": unpriced,
            "stopped": stopped,
            "spent": budget.spent,
            "left": budget.left,
            "estimate_credits": sum(c["estimate_credits"] for c in coverage.values()),
        }
    )
    return res


# ---------- output ----------


def summary_line(res: dict) -> str:
    """`Cup odds: N snapshots, C credits, M matches with Pinnacle, D with DraftKings,
    credits left X` (matches counted over every season, data-log's rows included)."""
    snaps = sum(st["state"] in ("api", "cache") for st in res["snapshots"]) + res["retried"]
    pin = sum(c["pinnacle"] for c in res["coverage"].values())
    dk = sum(c["draftkings"] for c in res["coverage"].values())
    return (
        f"Cup odds: {snaps} snapshots, {res['spent']} credits, {pin} matches with Pinnacle, "
        f"{dk} with DraftKings, credits left {'unknown' if res['left'] is None else res['left']}"
    )


def compact(res: dict) -> dict:
    """The CUP_ODDS_JSON line: coverage per season and the run's totals (no rows)."""
    keep = (
        "cup",
        "sport",
        "seasons",
        "cap",
        "dry_run",
        "books",
        "dropped_draftkings",
        "coverage",
        "retried",
        "unmatched",
        "stopped",
        "spent",
        "left",
        "left_before",
        "estimate_credits",
        "calendar",
    )
    out = {k: res.get(k) for k in keep}
    out["rows_new"], out["calls_new"] = len(res["rows"]), len(res["calls"])
    out["unpriced"] = len(res["unpriced"])
    return out


def report(res: dict) -> str:
    lines = [
        f"== {res['cup']} historical closes ({res['sport']}, h2h, books "
        f"{','.join(res['books'])}; cap {res['cap']}{', dry run' if res['dry_run'] else ''}) =="
    ]
    for y, c in res["coverage"].items():
        cal = (res.get("calendar") or {}).get(y, {})
        lines.append(
            f"{c['label']}: {c['matches']} matches on ESPN's calendar, {c['slots']} snapshots "
            f"(held {c['held']}, cached {c['cached']}, bought {c['bought']}, planned "
            f"{c['planned']}, skipped {c['skipped']}); priced Pinnacle {c['pinnacle']}, "
            f"DraftKings {c['draftkings']}"
            + (f"; ESPN {cal.get('requests')} requests ({cal.get('mode')})" if cal else "")
        )
        if c["planned"]:
            lines.append(
                f"  estimate: {c['planned']} snapshots x {HIST_COST} = {c['estimate_credits']} "
                f"credits (x2 if DraftKings counted as a second region)"
            )
    if res["dry_run"]:
        lines.append("Planned snapshots (requested time: matches):")
        for st in res["snapshots"]:
            if st["state"] == "planned":
                lines.append(f"  {st['requested']}: {st['matches']}")
    lines += res["log"]
    if res["name_map"]:
        lines.append(
            "Team names (Odds API -> ESPN): "
            + "; ".join(f"{k} -> {v['espn']} ({v['how']})" for k, v in res["name_map"].items())
        )
    if res["unmatched"]:
        lines.append(f"Unmatched Odds API events near a slot: {res['unmatched']}")
    if res["unpriced"]:
        lines.append(
            f"ESPN matches without a Pinnacle close ({len(res['unpriced'])}): "
            f"{res['unpriced'][:40]}"
        )
    if res.get("stopped"):
        lines.append(f"Stopped: {res['stopped']}")
    lines.append(summary_line(res))
    return "\n".join(lines)


def write_pending(res: dict, pending: Path) -> None:
    """The run's new rows and calls, for `log` (rows.jsonl, calls.jsonl)."""
    pending = Path(pending)
    pending.mkdir(parents=True, exist_ok=True)
    for name, items in (("rows.jsonl", res["rows"]), ("calls.jsonl", res["calls"])):
        path = pending / name
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in items))


def cli(
    cup: str,
    seasons,
    cap: int,
    state: str | None = None,
    pending: str | None = None,
    json_path: str | None = None,
    retry: bool = True,
    **kw,
) -> dict:
    key = os.environ.get("ODDS_API_KEY", "")
    res = run(
        cup, seasons, cap, api_key=key, state=Path(state) if state else None, retry=retry, **kw
    )
    if pending:
        write_pending(res, Path(pending))
    print(redact(report(res), key))
    text = redact(json.dumps(res, default=str), key)
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(text)
    print("CUP_ODDS_JSON " + redact(json.dumps(compact(res), default=str), key))
    return res
