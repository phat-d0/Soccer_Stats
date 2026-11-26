"""Player-prop market probe (lead, 26 Nov 2026; cap 60 credits, default branch only).

A scoping step for player markets beyond anytime goalscorer: shots, shots on target,
assists, cards and any goal-or-assist market The Odds API lists. It answers, before
any test is proposed, who prices them, at which lines, on one or both sides, and with
what margin. Nothing here reads a result.

- **Market keys** (1 credit a call): per league (E0, SP1, D1, I1, F1) the free events
  list, then the event's `/markets` list for upcoming matches in kickoff order until one
  lists any player market (at most `LIVE_TRIES`). It says which books list which keys,
  so a goal-or-assist key is found if one exists.
- **Live prices** (1 credit per market returned, one region): that match's odds for
  the wanted markets found, at up to ten books that list them (one `bookmakers=` list).
- **History** (about 10 credits per market returned): E0's first match on
  `HISTORY_DAY` (a 2025/26 Saturday), one snapshot at kickoff − 50 min, the markets found
  live, so we know whether a development backtest can be bought.
- **Other regions** (E0 only, last): uk, eu and au for the same match, only when no US
  book priced any market on both sides.

Calls run in that order and each is skipped if it could pass the cap or leave fewer than
3,000 credits on the shared key; cost is each answer's `x-requests-last`. Every body is
cached under `data/raw/player_odds_history/market_probe/` (the paid
`player-odds-history-` cache), so a rerun buys nothing it already holds. The key is never
printed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.edge.props import _get
from soccer_stats.odds_feed import SPORTS
from soccer_stats.player_goal_odds import CappedBudget
from soccer_stats.player_odds import BASE

LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
# The markets asked about, in priority order (the cap buys the first ones first).
WANTED = (
    "player_shots",
    "player_shots_on_target",
    "player_assists",
    "player_to_receive_card",
)
# A goal-or-assist market has no documented key; any listed key holding one of these
# words is asked for too.
EXTRA_WORDS = ("assist", "goal_or", "or_assist", "score_or")
SKIP = ("player_goal_scorer_anytime",)  # known (§6, §16): not probed again
LIVE_TRIES = 3
MAX_BOOKS = 10
HIST_COST = 10  # per market returned, one bookmakers= list
HISTORY_DAY = "2025-11-01"
HISTORY_LEAD = pd.Timedelta(minutes=50)
OTHER_REGIONS = ("uk", "eu", "au")
PROBE_DIR = "market_probe"
SIDES = {"over": "over", "yes": "over", "under": "under", "no": "under"}


def probe_dir(raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / "player_odds_history" / PROBE_DIR


def market_keys(body: dict | None) -> dict[str, list[str]]:
    """An event's `/markets` answer as {market key: [books listing it]}."""
    ev = (body or {}).get("data", body) if isinstance(body, dict) else {}
    out: dict[str, list[str]] = {}
    for b in (ev or {}).get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            if m.get("key"):
                out.setdefault(m["key"], []).append(b.get("key"))
    return out


def wanted_markets(keys: dict[str, list[str]]) -> list[str]:
    """The wanted keys that are listed, in priority order, then any other player key
    whose name suggests assists or goal-or-assist."""
    found = [k for k in WANTED if k in keys]
    extra = sorted(
        k
        for k in keys
        if k.startswith("player_")
        and k not in WANTED
        and k not in SKIP
        and any(w in k for w in EXTRA_WORDS)
    )
    return found + extra


def books_listing(keys: dict[str, list[str]], markets: list[str]) -> list[str]:
    """Books listing any of `markets`, most markets first (ties by name), at most ten."""
    count: dict[str, int] = {}
    for m in markets:
        for b in keys.get(m, []):
            count[b] = count.get(b, 0) + 1
    return sorted(count, key=lambda b: (-count[b], b))[:MAX_BOOKS]


def rows(body: dict | None) -> pd.DataFrame:
    """One row per book, market, player, line and side: {market, book, player, point,
    side ('over' for Over/Yes, 'under' for Under/No), odds}."""
    ev = (body or {}).get("data", body) if isinstance(body, dict) else {}
    out = []
    for b in (ev or {}).get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            for o in m.get("outcomes", []) or []:
                if o.get("price") is None:
                    continue
                name, desc = str(o.get("name", "")), o.get("description")
                side = SIDES.get(name.lower())
                player = desc if desc else (None if side else name)
                if not player:
                    continue
                out.append(
                    {
                        "event_id": (ev or {}).get("id"),
                        "market": m.get("key"),
                        "book": b.get("key"),
                        "player": player,
                        "point": o.get("point"),
                        "side": side or "over",
                        "odds": float(o["price"]),
                    }
                )
    cols = ["event_id", "market", "book", "player", "point", "side", "odds"]
    return pd.DataFrame(out, columns=cols)


def summary(r: pd.DataFrame) -> list[dict]:
    """Per market and book: players, lines listed, how many lines are priced on both
    sides and their mean margin (1/over + 1/under − 1), the mean Over/Yes odds, and for a
    market priced on one side only the sum of the Yes chances per event (the book's
    implied count of players who do it, margin included)."""
    out = []
    if r.empty:
        return out
    r = r.assign(point=r["point"].fillna(-1.0))
    for (market, book), x in r.groupby(["market", "book"]):
        wide = x.pivot_table(
            index=["event_id", "player", "point"], columns="side", values="odds", aggfunc="first"
        )
        over = wide["over"] if "over" in wide else pd.Series(dtype=float)
        two_sided = "over" in wide and "under" in wide
        both = wide.dropna(subset=["over", "under"]) if two_sided else wide.iloc[0:0]
        margin = (1 / both["over"] + 1 / both["under"] - 1) if len(both) else pd.Series([])
        lines = sorted({float(p) for p in x["point"] if p != -1.0})
        one = x[x["side"] == "over"]
        yes_sum = (
            one.groupby(["event_id", "point"])["odds"].apply(lambda o: float((1 / o).sum()))
            if len(one)
            else pd.Series([])
        )
        out.append(
            {
                "market": market,
                "book": book,
                "players": int(x["player"].nunique()),
                "lines": lines,
                "priced_lines": int(len(wide)),
                "two_sided": int(len(both)),
                "margin_two_sided": round(float(margin.mean()), 4) if len(margin) else None,
                "avg_over_odds": round(float(over.mean()), 3) if len(over.dropna()) else None,
                "yes_sum_per_event": round(float(yes_sum.mean()), 3) if len(yes_sum) else None,
            }
        )
    return sorted(out, key=lambda s: (s["market"], -s["players"], s["book"]))


def _save(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


def _cached(path: Path, fetch: Callable[[], dict | None], log: list[str], label: str):
    if path.exists():
        log.append(f"{label}: cached, 0 credits")
        return json.loads(path.read_text())
    body = fetch()
    if body is not None:
        _save(path, body)
    return body


def first_match(events: list[dict], day: str) -> dict | None:
    """The first event kicking off on `day` (UTC) at or after 11:00."""
    lo = pd.Timestamp(f"{day}T11:00:00Z")
    hi = lo.normalize() + pd.Timedelta(days=1)
    on = [e for e in events if lo <= pd.Timestamp(e["commence_time"]) < hi]
    return min(on, key=lambda e: (e["commence_time"], e["id"])) if on else None


def run(
    cap: int,
    leagues: tuple[str, ...] = LEAGUES,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    raw_dir: Path = RAW_DIR,
) -> dict:
    """The probe, never past `cap` credits (order: market keys, live prices, E0 history,
    E0 other regions)."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    budget = CappedBudget(cap)
    out = {"cap": cap, "leagues": {}, "history": None, "other_regions": None, "log": budget.log}
    if not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
        return {**out, "spent": 0, "left": None}
    base = probe_dir(raw_dir)
    stamp = now.strftime("%Y%m%dT%H%M")
    live_e0 = None
    for lg in leagues:
        sport = SPORTS[lg]
        res: dict = {"event": None, "keys": {}, "markets": [], "books": [], "summary": []}
        evs = _get(
            get, f"{BASE}/sports/{sport}/events", {"apiKey": api_key}, budget, 0, f"{lg} events"
        )
        upcoming = sorted(
            [e for e in evs or [] if pd.Timestamp(e["commence_time"]) > now],
            key=lambda e: e["commence_time"],
        )
        for e in upcoming[:LIVE_TRIES]:
            label = f"{lg} markets {e['home_team']} v {e['away_team']}"
            body = _get(
                get,
                f"{BASE}/sports/{sport}/events/{e['id']}/markets",
                {"apiKey": api_key, "regions": "us"},
                budget,
                1,
                label,
            )
            if body is None:
                continue
            _save(base / lg / f"markets_{stamp}_{e['id']}.json", body)
            keys = market_keys(body)
            if any(k.startswith("player_") for k in keys):
                res.update(
                    event=f"{e['home_team']} v {e['away_team']}",
                    kickoff=e["commence_time"],
                    event_id=e["id"],
                    keys={k: v for k, v in keys.items() if k.startswith("player_")},
                )
                break
        markets = wanted_markets(res["keys"])
        books = books_listing(res["keys"], markets)
        res.update(markets=markets, books=books)
        if markets and books:
            body = _get(
                get,
                f"{BASE}/sports/{sport}/events/{res['event_id']}/odds",
                {
                    "apiKey": api_key,
                    "bookmakers": ",".join(books),
                    "markets": ",".join(markets),
                    "oddsFormat": "decimal",
                },
                budget,
                len(markets),
                f"{lg} live odds {res['event']}",
            )
            if body is not None:
                _save(base / lg / f"live_{stamp}_{res['event_id']}.json", body)
                res["summary"] = summary(rows(body))
        if lg == "E0":
            live_e0 = res
        out["leagues"][lg] = res

    # History: E0's first match on HISTORY_DAY, kickoff - 50 min, the markets found live.
    if live_e0 and live_e0["markets"]:
        sport = SPORTS["E0"]
        h: dict = {"day": HISTORY_DAY, "event": None}
        ev_body = _cached(
            base / "E0" / f"events_{HISTORY_DAY}.json",
            lambda: _get(
                get,
                f"{BASE}/historical/sports/{sport}/events",
                {"apiKey": api_key, "date": f"{HISTORY_DAY}T10:00:00Z"},
                budget,
                1,
                f"E0 events {HISTORY_DAY}",
            ),
            budget.log,
            f"E0 events {HISTORY_DAY}",
        )
        data = (ev_body or {}).get("data", ev_body) if isinstance(ev_body, dict) else ev_body
        ev = first_match(data or [], HISTORY_DAY)
        if ev is not None:
            at = (pd.Timestamp(ev["commence_time"]) - HISTORY_LEAD).strftime("%Y-%m-%dT%H:%M:%SZ")
            # As many markets as the budget left can pay for, in priority order.
            room = (budget.cap - budget.spent) // HIST_COST
            mk = live_e0["markets"][: max(int(room), 0)]
            h.update(event=f"{ev['home_team']} v {ev['away_team']}", at=at, markets=mk)
            if mk:
                body = _cached(
                    base / "E0" / f"{ev['id']}_hist_{'-'.join(mk)}.json",
                    lambda: _get(
                        get,
                        f"{BASE}/historical/sports/{sport}/events/{ev['id']}/odds",
                        {
                            "apiKey": api_key,
                            "bookmakers": ",".join(live_e0["books"]),
                            "markets": ",".join(mk),
                            "oddsFormat": "decimal",
                            "date": at,
                        },
                        budget,
                        HIST_COST * len(mk),
                        f"E0 history {h['event']} at {at}",
                    ),
                    budget.log,
                    f"E0 history {h['event']}",
                )
                h["summary"] = summary(rows(body)) if body is not None else None
        out["history"] = h

    # Other regions: E0 only, when no US book priced any market on both sides.
    us_two = any(s["two_sided"] > 0 for r in out["leagues"].values() for s in r.get("summary", []))
    if live_e0 and live_e0["markets"] and not us_two:
        body = _get(
            get,
            f"{BASE}/sports/{SPORTS['E0']}/events/{live_e0['event_id']}/odds",
            {
                "apiKey": api_key,
                "regions": ",".join(OTHER_REGIONS),
                "markets": ",".join(live_e0["markets"]),
                "oddsFormat": "decimal",
            },
            budget,
            len(live_e0["markets"]) * len(OTHER_REGIONS),
            f"E0 other regions {live_e0['event']}",
        )
        if body is not None:
            _save(base / "E0" / f"regions_{stamp}_{live_e0['event_id']}.json", body)
            out["other_regions"] = summary(rows(body))
    return {**out, "spent": budget.spent, "left": budget.left}


def summary_lines(res: dict) -> list[str]:
    """Plain lines for the job log."""

    def fmt(s: dict) -> str:
        bits = [f"{s['players']} players", f"lines {s['lines'] or '-'}"]
        if s["two_sided"]:
            bits.append(
                f"two-sided {s['two_sided']}/{s['priced_lines']}, "
                f"margin {s['margin_two_sided']:.1%}"
            )
        else:
            bits.append("one side only")
            if s["yes_sum_per_event"] is not None:
                bits.append(f"Yes chances sum {s['yes_sum_per_event']:.2f} per event-line")
        if s["avg_over_odds"] is not None:
            bits.append(f"avg over/yes {s['avg_over_odds']:.2f}")
        return f"    {s['market']} at {s['book']}: " + ", ".join(bits)

    lines = [f"Player market probe: spent {res.get('spent')} of {res.get('cap')} credits"]
    for lg, r in res.get("leagues", {}).items():
        if not r.get("event"):
            lines.append(f"  {lg}: no upcoming match listed player markets")
            continue
        listed = ", ".join(f"{k} ({len(v)} books)" for k, v in sorted(r["keys"].items()))
        lines.append(f"  {lg} {r['event']} ({r.get('kickoff')}): listed {listed}")
        lines.append(
            f"  {lg} asked: {', '.join(r['markets']) or 'none'} at {', '.join(r['books'])}"
        )
        lines += [fmt(s) for s in r.get("summary", [])]
    h = res.get("history")
    if h:
        lines.append(
            f"  E0 history {h.get('day')}: {h.get('event')} at {h.get('at')}, "
            f"markets {h.get('markets')}"
        )
        if h.get("summary") is not None:
            lines += [fmt(s) for s in h["summary"]] or ["    no book priced them"]
    if res.get("other_regions") is not None:
        lines.append("  E0 other regions (uk, eu, au):")
        lines += [fmt(s) for s in res["other_regions"]] or ["    no book priced them"]
    return lines
