"""FanDuel player shot lines from The Odds API (player_shots, player_shots_on_target).

DraftKings doesn't price Premier League player shots through The Odds API, so player
bets use FanDuel (PLAYER_BOOKMAKER); match bets stay on DraftKings.

Player props come one match at a time from the event-odds endpoint. Live, each upcoming
match within PLAYER_WINDOW_HOURS of kickoff is refreshed at most every REFRESH_HOURS
(hourly in the last 3 hours), and only while the account keeps RESERVE_CREDITS spare,
reading the cost of each call from the response headers. History comes from the
historical event-odds endpoint (from 3 May 2023): one look (3 hours before kickoff)
and the close per match, about 20 credits each.

Live player odds are only fetched once the player model has beaten its baseline (the
gate in backtest/<league>_players.json); see publish.player_gate.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.odds_feed import SPORTS, _team
from soccer_stats.odds_history import BackfillReport

BASE = "https://api.the-odds-api.com/v4"
MARKETS = ("player_shots", "player_shots_on_target")
PLAYER_BOOKMAKER = "fanduel"
PLAYER_BOOKMAKER_NAME = "FanDuel"
PLAYER_WINDOW_HOURS = 30
REFRESH_HOURS, LATE_REFRESH_HOURS, LATE_HOURS = 3.0, 1.0, 3.0
RESERVE_CREDITS = 3000  # leave the match odds plenty
COST_PER_CALL = 20  # historical: 10 per market; live is less, read from headers
LOOK_HOURS = 3.0


def _params(api_key: str, **extra) -> dict:
    return {
        "apiKey": api_key,
        "bookmakers": PLAYER_BOOKMAKER,
        "markets": ",".join(MARKETS),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
        **extra,
    }


def parse_event(event: dict, known_teams: set[str] | None = None) -> pd.DataFrame:
    """One row per priced side: player, market, line, side, odds."""
    rows = []
    home = _team(event.get("home_team", ""), known_teams)
    away = _team(event.get("away_team", ""), known_teams)
    for book in event.get("bookmakers", []):
        if book.get("key") != PLAYER_BOOKMAKER:
            continue
        for m in book.get("markets", []):
            if m.get("key") not in MARKETS:
                continue
            for o in m.get("outcomes", []):
                if o.get("point") is None or not o.get("description"):
                    continue
                rows.append(
                    {
                        "event_id": event.get("id"),
                        "kickoff": pd.Timestamp(event["commence_time"]).tz_convert("UTC"),
                        "home": home,
                        "away": away,
                        "player": o["description"],
                        "market": m["key"],
                        "line": float(o["point"]),
                        "side": o["name"].lower(),
                        "odds": float(o["price"]),
                        "odds_updated": m.get("last_update") or book.get("last_update"),
                    }
                )
    cols = [
        "event_id",
        "kickoff",
        "home",
        "away",
        "player",
        "market",
        "line",
        "side",
        "odds",
        "odds_updated",
    ]
    return pd.DataFrame(rows, columns=cols)


def live_dir(league: str, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / "player_odds" / league


def fetch_live(
    league: str = "E0",
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    credits_left: int | None = None,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
) -> tuple[list[dict], dict]:
    """Cached FanDuel player odds for matches kicking off soon. Never raises.

    Returns (events with player odds, status). `credits_left` is the account balance
    seen by the match-odds refresh, so player calls never eat into its reserve.
    """
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    d = live_dir(league, raw_dir)
    status = {"fetched": 0, "cached": 0, "credits_used": 0, "error": None}
    if not api_key:
        status["error"] = "no ODDS_API_KEY configured"
        return _cached(d, now), status
    try:
        resp = get(f"{BASE}/sports/{SPORTS[league]}/events", params={"apiKey": api_key}, timeout=30)
        events = resp.json() if resp.ok else []
        if not resp.ok:
            status["error"] = f"The Odds API returned HTTP {resp.status_code}"
    except (requests.RequestException, ValueError) as exc:
        status["error"] = f"could not reach The Odds API ({type(exc).__name__})"
        events = []
    left = credits_left
    for ev in events:
        k = pd.Timestamp(ev["commence_time"]).tz_convert("UTC")
        hours = (k - now) / pd.Timedelta(hours=1)
        if not 0 < hours <= PLAYER_WINDOW_HOURS:
            continue
        path = d / f"{ev['id']}.json"
        every = LATE_REFRESH_HOURS if hours <= LATE_HOURS else REFRESH_HOURS
        if path.exists():
            age = (now - pd.Timestamp(json.loads(path.read_text())["fetched_at"])) / pd.Timedelta(
                hours=1
            )
            if age < every:
                status["cached"] += 1
                continue
        if left is not None and left - COST_PER_CALL < RESERVE_CREDITS:
            status["error"] = f"keeping {RESERVE_CREDITS} credits for match odds"
            break
        try:
            r = get(
                f"{BASE}/sports/{SPORTS[league]}/events/{ev['id']}/odds",
                params=_params(api_key),
                timeout=30,
            )
        except requests.RequestException as exc:
            status["error"] = f"could not reach The Odds API ({type(exc).__name__})"
            break
        if not r.ok:
            status["error"] = f"The Odds API returned HTTP {r.status_code}"
            break
        cost = r.headers.get("x-requests-last")
        rem = r.headers.get("x-requests-remaining")
        status["credits_used"] += int(float(cost)) if cost else 0
        left = int(float(rem)) if rem else left
        d.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": now.isoformat(), "event": r.json()}))
        status["fetched"] += 1
    status["credits_left"] = left
    return _cached(d, now), status


def _cached(d: Path, now: pd.Timestamp) -> list[dict]:
    out = []
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        snap = json.loads(path.read_text())
        ev = snap.get("event") or {}
        try:
            k = pd.Timestamp(ev["commence_time"]).tz_convert("UTC")
        except (KeyError, ValueError):
            continue
        if k > now - pd.Timedelta(hours=3):
            out.append({**ev, "fetched_at": snap["fetched_at"]})
    return out


# ---------- history ----------


def hist_path(league: str, event_id: str, kind: str, raw_dir: Path = RAW_DIR) -> Path:
    # The bookmaker is in the name, so snapshots from another book are never reused.
    return raw_dir / "player_odds_history" / league / f"{event_id}_{kind}_{PLAYER_BOOKMAKER}.json"


def events_index_path(league: str, at: pd.Timestamp, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / "player_odds_history" / league / f"events_{at.strftime('%Y%m%dT%H%MZ')}.json"


def backfill(
    kickoffs: Iterable[pd.Timestamp],
    league: str = "E0",
    *,
    max_credits: int,
    keep_credits: int = 1500,
    dry_run: bool = False,
    api_key: str | None = None,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
) -> BackfillReport:
    """Historical player odds: per kickoff time, the event list at the look (1 credit),
    then each match's odds at the look and at the close (about 20 credits each)."""
    all_k = [pd.Timestamp(k).tz_convert("UTC") for k in kickoffs]
    per_slot = Counter(all_k)  # matches per kickoff time
    ks = sorted(per_slot)
    rep = BackfillReport()
    plan = []  # (look time, kickoff)
    for k in ks:
        plan.append(((k - pd.Timedelta(hours=LOOK_HOURS)).floor("5min"), k))
    # Rough count before we know event ids: 2 snapshots per kickoff slot's matches.
    idx_todo = [a for a, _ in plan if not events_index_path(league, a, raw_dir).exists()]
    missing = _missing_snapshots(plan, per_slot, league, raw_dir)
    rep.planned = len(all_k) * 2
    rep.estimated_credits = len(set(idx_todo)) + missing * COST_PER_CALL
    rep.cached = max(rep.planned - missing, 0)
    if dry_run:
        return rep
    rep.cached = 0
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        rep.stopped = "no ODDS_API_KEY configured"
        return rep

    def call(url, params, cost_guess):
        if rep.credits_used + cost_guess > max_credits:
            rep.stopped = f"--max-credits {max_credits} reached"
            return None
        if rep.credits_left is not None and rep.credits_left - cost_guess < keep_credits:
            rep.stopped = f"keeping {keep_credits} credits for live refreshes"
            return None
        try:
            r = get(url, params=params, timeout=60)
        except requests.RequestException as exc:
            rep.errors.append(f"could not reach The Odds API ({type(exc).__name__})")
            rep.stopped = "network error"
            return None
        if not r.ok:
            rep.errors.append(f"The Odds API returned HTTP {r.status_code}")
            rep.stopped = "API error"
            return None
        cost, rem = r.headers.get("x-requests-last"), r.headers.get("x-requests-remaining")
        rep.credits_used += int(float(cost)) if cost else cost_guess
        rep.credits_left = int(float(rem)) if rem else rep.credits_left
        return r.json()

    sport = SPORTS[league]
    for at, k in plan:
        if rep.stopped:
            break
        ip = events_index_path(league, at, raw_dir)
        if ip.exists():
            idx = json.loads(ip.read_text())
        else:
            body = call(
                f"{BASE}/historical/sports/{sport}/events",
                {"apiKey": api_key, "date": at.strftime("%Y-%m-%dT%H:%M:%SZ")},
                1,
            )
            if body is None:
                break
            idx = body.get("data", [])
            ip.parent.mkdir(parents=True, exist_ok=True)
            ip.write_text(json.dumps(idx))
        for ev in idx:
            if pd.Timestamp(ev["commence_time"]).tz_convert("UTC") != k:
                continue
            for kind, when in (("look", at), ("close", k - pd.Timedelta(minutes=1))):
                path = hist_path(league, ev["id"], kind, raw_dir)
                if path.exists():
                    rep.cached += 1
                    continue
                body = call(
                    f"{BASE}/historical/sports/{sport}/events/{ev['id']}/odds",
                    _params(api_key, date=when.strftime("%Y-%m-%dT%H:%M:%SZ")),
                    COST_PER_CALL,
                )
                if body is None:
                    break
                path.write_text(
                    json.dumps(
                        {
                            "requested": when.isoformat(),
                            "timestamp": body.get("timestamp"),
                            "event": body.get("data", {}),
                        }
                    )
                )
                rep.fetched += 1
            if rep.stopped:
                break
    return rep


def _missing_snapshots(plan, per_slot, league, raw_dir) -> int:
    """Snapshots still to fetch: two per match (look and close)."""
    n = 0
    for at, k in plan:
        ip = events_index_path(league, at, raw_dir)
        if not ip.exists():
            n += 2 * per_slot[k]
            continue
        for ev in json.loads(ip.read_text()):
            if pd.Timestamp(ev["commence_time"]).tz_convert("UTC") == k:
                n += sum(
                    not hist_path(league, ev["id"], kind, raw_dir).exists()
                    for kind in ("look", "close")
                )
    return n


def load_history(
    league: str = "E0", known_teams: set[str] | None = None, raw_dir: Path = RAW_DIR
) -> pd.DataFrame:
    """Every cached historical player-odds snapshot as rows, tagged look or close."""
    d = raw_dir / "player_odds_history" / league
    frames = []
    for path in (
        sorted(d.glob(f"*_look_{PLAYER_BOOKMAKER}.json"))
        + sorted(d.glob(f"*_close_{PLAYER_BOOKMAKER}.json"))
        if d.exists()
        else []
    ):
        snap = json.loads(path.read_text())
        df = (
            parse_event(snap.get("event") or {}, known_teams)
            if snap.get("event")
            else pd.DataFrame()
        )
        if df.empty:
            continue
        df["kind"] = "look" if "_look_" in path.stem else "close"
        df["snapshot_ts"] = pd.Timestamp(snap.get("timestamp") or snap["requested"])
        frames.append(df)
    return (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(
            columns=[
                "event_id",
                "kickoff",
                "home",
                "away",
                "player",
                "market",
                "line",
                "side",
                "odds",
                "odds_updated",
                "kind",
                "snapshot_ts",
            ]
        )
    )


def check(
    league: str = "E0",
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    get: Callable = requests.get,
) -> list[str]:
    """Diagnose player-prop coverage: what the cached history holds, and which US
    bookmakers price player shots for one recent and one upcoming match (about 40-80
    credits). Returns report lines; the key never appears in them."""
    out = []
    d = raw_dir / "player_odds_history" / league
    files = (
        sorted(d.glob(f"*_look_{PLAYER_BOOKMAKER}.json"))
        + sorted(d.glob(f"*_close_{PLAYER_BOOKMAKER}.json"))
        if d.exists()
        else []
    )
    books, markets, with_dk, sample = Counter(), Counter(), 0, None
    for p in files:
        ev = json.loads(p.read_text()).get("event") or {}
        bs = ev.get("bookmakers", [])
        for b in bs:
            books[b.get("key")] += 1
            for m in b.get("markets", []):
                markets[m.get("key")] += 1
        if any(b.get("key") == PLAYER_BOOKMAKER and b.get("markets") for b in bs):
            with_dk += 1
        if sample is None and ev.get("id"):
            sample = (ev["id"], json.loads(p.read_text()).get("requested"))
    out.append(
        f"Cached history: {len(files)} snapshots, {with_dk} with {PLAYER_BOOKMAKER_NAME} player markets"
    )
    out.append(
        f"  bookmakers seen: {dict(books) or 'none'}; markets seen: {dict(markets) or 'none'}"
    )
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        return out + ["No ODDS_API_KEY: skipping live checks"]
    sport = SPORTS[league]
    params = {
        "apiKey": api_key,
        "regions": "us,us2,uk,eu",
        "markets": ",".join(MARKETS),
        "oddsFormat": "decimal",
    }

    def describe(label, body):
        ev = body.get("data", body) if isinstance(body, dict) else {}
        rows = []
        for b in ev.get("bookmakers", []):
            ms = {m["key"]: len(m.get("outcomes", [])) for m in b.get("markets", [])}
            rows.append(f"{b['key']} {ms}")
        out.append(
            f"{label}: {ev.get('home_team')} v {ev.get('away_team')} -> "
            + ("; ".join(rows) if rows else "no bookmaker prices player shots")
        )

    try:
        if sample:
            r = get(
                f"{BASE}/historical/sports/{sport}/events/{sample[0]}/odds",
                params={**params, "date": sample[1][:19] + "Z"},
                timeout=60,
            )
            out.append(
                f"Historical call: HTTP {r.status_code}, cost {r.headers.get('x-requests-last')}"
            )
            if r.ok:
                describe("  history (all regions)", r.json())
        r = get(f"{BASE}/sports/{sport}/events", params={"apiKey": api_key}, timeout=30)
        evs = r.json() if r.ok else []
        if evs:
            e = evs[0]
            r = get(f"{BASE}/sports/{sport}/events/{e['id']}/odds", params=params, timeout=30)
            out.append(
                f"Live call: HTTP {r.status_code}, cost {r.headers.get('x-requests-last')}, "
                f"credits left {r.headers.get('x-requests-remaining')}"
            )
            if r.ok:
                describe("  next match (all regions)", r.json())
    except requests.RequestException as exc:
        out.append(f"Could not reach The Odds API ({type(exc).__name__})")
    return out
