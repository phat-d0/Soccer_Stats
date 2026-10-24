"""In-play price probe (comebacks phase 2, step 1; owner-approved 24 Oct, 1 credit).

Asks one question: does The Odds API's live `/odds` endpoint return h2h prices for matches
already in play, and how fresh are they? One free events list (cost 0) checks the balance
and which events have started; then, only if at least one has and the balance stays above
the shared key's 3,000 reserve, one `/odds` call (h2h, up to ten named books = one region,
1 credit). ESPN's free scoreboard gives each live match's score and clock beside its prices,
so we can see whether the price moves with the game. Cost from `x-requests-last`; the key is
never printed. Descriptive only: nothing trades.

    python -m soccer_stats.lab.inplay_probe --league E2 --cap 1 --json out/inplay.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

import requests

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


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _int(h, k):
    try:
        return int(float(h.get(k)))
    except (TypeError, ValueError):
        return None


def espn_live(league: str, get=requests.get) -> list[dict]:
    """Today's ESPN scoreboard: home, away, score, clock and state per match (free)."""
    try:
        r = get(ESPN_BASE.format(slug=SLUGS[league]) + "/scoreboard", timeout=15)
        body = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001  the scores are context, not the question
        return []
    out = []
    for e in body.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        st = (e.get("status") or {}).get("type") or {}
        out.append(
            {
                "home": ((sides.get("home") or {}).get("team") or {}).get("displayName"),
                "away": ((sides.get("away") or {}).get("team") or {}).get("displayName"),
                "score": "-".join(str((sides.get(k) or {}).get("score")) for k in ("home", "away")),
                "clock": (e.get("status") or {}).get("displayClock"),
                "state": st.get("state"),
                "detail": st.get("shortDetail"),
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


def run(league: str, cap: int, api_key: str, get=requests.get) -> dict:
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
    res["checked_at"] = now.isoformat(timespec="seconds")
    res["matches"] = summarise(r.json(), now, espn_live(league, get))
    inplay = [m for m in res["matches"] if m["started"]]
    priced = [m for m in inplay if m["books"]]
    ages = [b["age_min"] for m in priced for b in m["books"] if b["age_min"] is not None]
    res["verdict"] = (
        f"{len(priced)} of {len(inplay)} started matches returned with h2h prices "
        f"({len(res['matches'])} events in the response); "
        + (f"book updates {min(ages):.1f}-{max(ages):.1f} min old" if ages else "no in-play prices")
    )
    return res


def main(argv=None) -> None:
    import os

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="E2")
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    key = os.environ.get("ODDS_API_KEY", "")
    try:
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
