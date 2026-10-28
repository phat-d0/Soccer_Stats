"""In-play price probe (comebacks phase 2, step 1; owner-approved 24 Oct, 1 credit).

Asks one question: does The Odds API's live `/odds` endpoint return h2h prices for matches
already in play, and how fresh are they? One free events list (cost 0) checks the balance
and which events have started; then, only if at least one has and the balance stays above
the shared key's 3,000 reserve, one `/odds` call (h2h, up to ten named books = one region,
1 credit). ESPN's free scoreboard gives each live match's score and clock beside its prices,
so we can see whether the price moves with the game. Cost from `x-requests-last`; the key is
never printed. Descriptive only: nothing trades. Every answer bought is kept under
data/raw/inplay_probe/<league>/ (the paid cache `inplay-probe-`, archived for good:
docs/caches.md), and a historical snapshot already held is never bought again.

    python -m soccer_stats.lab.inplay_probe --league E2 --cap 1 --json out/inplay.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.espn_news import BASE as ESPN_BASE
from soccer_stats.espn_news import SLUGS
from soccer_stats.odds_feed import SPORTS, URL

EVENTS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
BOOKS = (
    "pinnacle,draftkings,fanduel,betfair_ex_uk,williamhill,"
    "betmgm,paddypower,skybet,unibet_uk,betway"
)
RESERVE = 3000
COST = 1  # one market, up to ten named books: one region
CACHE = "inplay_probe"  # under RAW_DIR: the paid cache inplay-probe-


def body_path(raw_dir: Path, league: str, kind: str, at: datetime) -> Path:
    """Where a bought answer is kept: <raw>/inplay_probe/<league>/<kind>_<UTC stamp>.json
    (kind "live" stamped with the call's time, "hist" with the snapshot asked for)."""
    return Path(raw_dir) / CACHE / league / f"{kind}_{at:%Y%m%dT%H%M%SZ}.json"


def keep(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _int(h, k):
    try:
        return int(float(h.get(k)))
    except (TypeError, ValueError):
        return None


def _minute(display: str) -> int | None:
    """ESPN's goal clock ("34'", "45'+2'") as the minute it counts in (45 for 45'+2)."""
    digits = ""
    for ch in str(display or ""):
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def score_at(goals: list[tuple[str, int]], minute: int | None) -> str | None:
    """'h-a' after `minute` from (side, minute) goal events; None without a minute."""
    if minute is None:
        return None
    h = sum(1 for side, m in goals if side == "home" and m <= minute)
    a = sum(1 for side, m in goals if side == "away" and m <= minute)
    return f"{h}-{a}"


def match_minute(elapsed: float) -> int:
    """The match minute about `elapsed` minutes after kickoff: the clock stops at 45 over
    half-time (taken as minutes 47-64 after kickoff), then runs 17 minutes behind."""
    if elapsed <= 47:
        return max(0, int(elapsed))
    if elapsed <= 64:
        return 45
    return min(90, int(elapsed - 17))


def espn_live(league: str, get=requests.get, date: str | None = None) -> list[dict]:
    """ESPN's scoreboard (today, or `date` YYYYMMDD): home, away, score, clock and state
    per match, plus each goal's side and minute (free)."""
    try:
        params = {"dates": date} if date else None
        r = get(ESPN_BASE.format(slug=SLUGS[league]) + "/scoreboard", params=params, timeout=15)
        body = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001  the scores are context, not the question
        return []
    out = []
    for e in body.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        st = (e.get("status") or {}).get("type") or {}
        ids = {str(((sides.get(k) or {}).get("team") or {}).get("id")): k for k in ("home", "away")}
        goals = [
            (
                ids.get(str((d.get("team") or {}).get("id"))),
                _minute((d.get("clock") or {}).get("displayValue")),
            )
            for d in comp.get("details") or []
            if d.get("scoringPlay")
        ]
        out.append(
            {
                "home": ((sides.get("home") or {}).get("team") or {}).get("displayName"),
                "away": ((sides.get("away") or {}).get("team") or {}).get("displayName"),
                "score": "-".join(str((sides.get(k) or {}).get("score")) for k in ("home", "away")),
                "clock": (e.get("status") or {}).get("displayClock"),
                "state": st.get("state"),
                "detail": st.get("shortDetail"),
                "goals": [(side, m) for side, m in goals if side and m is not None],
            }
        )
    return out


def _words(name: str) -> set[str]:
    return {w for w in str(name).lower().replace(".", " ").split() if len(w) > 3}


def match_espn(home: str, away: str, live: list[dict]) -> dict | None:
    for m in live:
        if _words(home) & _words(m["home"] or "") and _words(away) & _words(m["away"] or ""):
            return m
    return None


def summarise(body: list, now: datetime, live: list[dict]) -> list[dict]:
    """Per event returned: started or not, minutes since kickoff, ESPN score, and per book
    the three prices, margin and how old the book's last update is."""
    out = []
    for ev in body if isinstance(body, list) else []:
        ko = _ts(ev["commence_time"])
        row = {
            "home": ev.get("home_team"),
            "away": ev.get("away_team"),
            "kickoff": ev["commence_time"],
            "started": ko <= now,
            "minutes_since_kickoff": round((now - ko).total_seconds() / 60),
            "espn": match_espn(ev.get("home_team"), ev.get("away_team"), live),
            "books": [],
        }
        for b in ev.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                p = {o["name"]: o.get("price") for o in m.get("outcomes") or []}
                prices = [p.get(ev.get("home_team")), p.get("Draw"), p.get(ev.get("away_team"))]
                upd = m.get("last_update") or b.get("last_update")
                row["books"].append(
                    {
                        "book": b.get("key"),
                        "home": prices[0],
                        "draw": prices[1],
                        "away": prices[2],
                        "margin": round(sum(1 / x for x in prices) - 1, 4) if all(prices) else None,
                        "last_update": upd,
                        "age_min": round((now - _ts(upd)).total_seconds() / 60, 1) if upd else None,
                    }
                )
        out.append(row)
    return out


def run(league: str, cap: int, api_key: str, get=requests.get, raw_dir: Path = RAW_DIR) -> dict:
    sport = SPORTS[league]
    res = {"league": league, "sport": sport, "cap": cap, "spent": 0, "log": []}
    if not api_key:
        res["stop"] = "no ODDS_API_KEY"
        return res
    r = get(EVENTS_URL.format(sport=sport), params={"apiKey": api_key}, timeout=20)
    left = _int(r.headers, "x-requests-remaining")
    res["log"].append(
        f"events: HTTP {r.status_code}, cost {_int(r.headers, 'x-requests-last')}, "
        f"credits left {left}"
    )
    events = r.json() if r.status_code == 200 else []
    now = _now()
    started = [e for e in events if _ts(e["commence_time"]) <= now]
    res.update(events=len(events), started=len(started), credits_before=left)
    if not started:
        res["stop"] = "no event in play: nothing bought"
        return res
    if cap < COST or (left is not None and left - COST < RESERVE):
        res["stop"] = f"cap {cap} or the {RESERVE}-credit reserve stops the call (left {left})"
        return res
    r = get(
        URL.format(sport=sport),
        params={
            "apiKey": api_key,
            "bookmakers": BOOKS,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
        timeout=20,
    )
    cost = _int(r.headers, "x-requests-last")
    res["spent"] = COST if cost is None else cost
    res["credits_after"] = _int(r.headers, "x-requests-remaining")
    res["log"].append(
        f"odds: HTTP {r.status_code}, cost {cost}, credits left {res['credits_after']}"
    )
    if r.status_code != 200:
        res["stop"] = f"odds call HTTP {r.status_code}"
        return res
    now = _now()
    body = r.json()
    path = body_path(raw_dir, league, "live", now)
    keep(path, body)
    res["kept"] = path.as_posix()
    res["checked_at"] = now.isoformat(timespec="seconds")
    res["matches"] = summarise(body, now, espn_live(league, get))
    inplay = [m for m in res["matches"] if m["started"]]
    priced = [m for m in inplay if m["books"]]
    ages = [b["age_min"] for m in priced for b in m["books"] if b["age_min"] is not None]
    res["verdict"] = (
        f"{len(priced)} of {len(inplay)} started matches returned with h2h prices "
        f"({len(res['matches'])} events in the response); "
        + (f"book updates {min(ages):.1f}-{max(ages):.1f} min old" if ages else "no in-play prices")
    )
    return res


HIST_URL = "https://api.the-odds-api.com/v4/historical/sports/{sport}/odds"
HIST_COST = 10  # historical: 10 credits per region and market


def run_historical(
    league: str, at: str, cap: int, api_key: str, get=requests.get, raw_dir: Path = RAW_DIR
) -> dict:
    """One historical h2h snapshot at `at` (ISO, UTC), when matches were in play: do
    started events come back with prices, and how fresh were they then? A free events
    list reads the balance first; the call needs cap >= 10 and the 3,000 reserve kept.
    A snapshot already kept for `at` is read back instead of bought."""
    sport = SPORTS[league]
    res = {"league": league, "sport": sport, "cap": cap, "spent": 0, "at": at, "log": []}
    path = body_path(raw_dir, league, "hist", _ts(at))
    if path.exists():
        res["log"].append(f"cached: {path.as_posix()} (nothing bought)")
        res["kept"] = path.as_posix()
        return _historical_report(res, json.loads(path.read_text()), league, at, get)
    if not api_key:
        res["stop"] = "no ODDS_API_KEY"
        return res
    r = get(EVENTS_URL.format(sport=sport), params={"apiKey": api_key}, timeout=20)
    left = _int(r.headers, "x-requests-remaining")
    res["log"].append(f"events (balance check): HTTP {r.status_code}, credits left {left}")
    res["credits_before"] = left
    if cap < HIST_COST or (left is not None and left - HIST_COST < RESERVE):
        res["stop"] = f"cap {cap} or the {RESERVE}-credit reserve stops the call (left {left})"
        return res
    r = get(
        HIST_URL.format(sport=sport),
        params={
            "apiKey": api_key,
            "date": at,
            "bookmakers": BOOKS,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
        timeout=30,
    )
    cost = _int(r.headers, "x-requests-last")
    res["spent"] = HIST_COST if cost is None else cost
    res["credits_after"] = _int(r.headers, "x-requests-remaining")
    res["log"].append(
        f"historical odds: HTTP {r.status_code}, cost {cost}, credits left {res['credits_after']}"
    )
    if r.status_code != 200:
        res["stop"] = f"historical call HTTP {r.status_code}"
        return res
    body = r.json()
    keep(path, body)
    res["kept"] = path.as_posix()
    return _historical_report(res, body, league, at, get)


def _historical_report(res: dict, body: dict, league: str, at: str, get) -> dict:
    snap = _ts(body.get("timestamp") or at)
    res["snapshot"] = body.get("timestamp")
    live = espn_live(league, get, date=f"{snap:%Y%m%d}")
    res["matches"] = summarise(body.get("data") or [], snap, live)
    for m in res["matches"]:
        e = m.get("espn")
        if m["started"] and e:
            minute = match_minute(m["minutes_since_kickoff"])
            m["match_minute"] = minute
            m["score_then"] = score_at([tuple(g) for g in e.get("goals") or []], minute)
            m["final"] = e.get("score")
    inplay = [m for m in res["matches"] if m["started"]]
    priced = [m for m in inplay if m["books"]]
    ages = [b["age_min"] for m in priced for b in m["books"] if b["age_min"] is not None]
    res["verdict"] = (
        f"snapshot {res['snapshot']}: {len(priced)} of {len(inplay)} started matches came back "
        f"with h2h prices ({len(res['matches'])} events in the snapshot); "
        + (f"book updates {min(ages):.1f}-{max(ages):.1f} min old" if ages else "no in-play prices")
    )
    return res


def main(argv=None) -> None:
    import os

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="E2")
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--at", help="historical snapshot time (ISO, UTC); blank = live")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    key = os.environ.get("ODDS_API_KEY", "")
    try:
        if a.at:
            res = run_historical(a.league, a.at, a.cap, key)
        else:
            res = run(a.league, a.cap, key)
    except Exception as exc:  # noqa: BLE001  never print a URL (it holds the key)
        res = {
            "league": a.league,
            "cap": a.cap,
            "spent": "unknown",
            "log": [],
            "stop": type(exc).__name__,
        }
    text = json.dumps(res, default=str).replace(key, "***") if key else json.dumps(res, default=str)
    res = json.loads(text)
    print(f"== In-play probe {res['league']}: cap {res['cap']}, spent {res['spent']} ==")
    for line in res["log"]:
        print(" ", line)
    for m in res.get("matches") or []:
        e = m.get("espn") or {}
        print(
            f"{m['home']} v {m['away']}: {'IN PLAY' if m['started'] else 'pre-match'} "
            f"{m['minutes_since_kickoff']} min after kickoff; "
            f"ESPN {e.get('score', '?')} {e.get('detail', '')}"
            + (
                f"; score then {m['score_then']} (minute ~{m['match_minute']})"
                if "score_then" in m
                else ""
            )
        )
        for b in m["books"]:
            print(
                f"    {b['book']:<14} {b['home']} / {b['draw']} / {b['away']}  "
                f"margin {b['margin']}  updated {b['age_min']} min ago"
            )
    print("VERDICT:", res.get("verdict") or res.get("stop"))
    print("INPLAY_JSON", text)
    if a.json:
        with open(a.json, "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()
