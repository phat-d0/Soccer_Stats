"""Live market probe: which books quote team totals, alternate totals and corners on
both sides, at which lines and at what margin (docs/totals.md, "Live market probe").

Pre-registered before any call. Per league (E0, SP1, D1, I1, F1, E1): the free events
list, one markets-discovery call on the soonest upcoming match (all five regions), then
one odds call for the target keys discovery found, with a `bookmakers=` list of up to
ten books. `player_goal_odds.CappedBudget` skips any call whose maximum cost would pass
the cap or leave under 3,000 credits on the shared key; the cost counted is
`x-requests-last`. Bodies are cached; the key is never printed.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats.edge.props import _get
from soccer_stats.odds_feed import SPORTS
from soccer_stats.player_goal_odds import CappedBudget
from soccer_stats.player_odds import BASE

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1")
REGIONS = ("us", "us2", "uk", "eu", "au")
MAX_BOOKS = 10  # ten books cost one region
DISCOVERY_ESTIMATE = 1  # replaced by the first call's real cost
# Target keys in priority order (lower ones are dropped first when credits are short).
PRIORITY = ("team_totals", "alternate_team_totals", "alternate_totals")
EXTRA = ("btts",)
E0_EXTRA = ("player_shots_on_target",)
TWO_SIDED = {"over": "over", "under": "under", "yes": "over", "no": "under"}


def target_keys(discovered: set[str], league: str) -> list[str]:
    """The keys to price, in priority order: team totals, alternate totals, corners,
    BTTS (and player shots on target in E0), as far as discovery found them."""
    keys = [k for k in PRIORITY if k in discovered]
    keys += sorted(k for k in discovered if "corner" in k and "total" in k)
    keys += [k for k in EXTRA if k in discovered]
    if league == "E0":
        keys += [k for k in E0_EXTRA if k in discovered]
    return keys


def discovered_markets(body: dict | None) -> dict[str, list[str]]:
    """{book: [market keys]} from a markets-discovery body."""
    out: dict[str, list[str]] = {}
    for b in (body or {}).get("bookmakers", []) or []:
        out[b.get("key")] = sorted({m.get("key") for m in b.get("markets", []) or []})
    return out


def choose_books(books: dict[str, list[str]], keys: list[str]) -> list[str]:
    """Up to MAX_BOOKS books listing any target key, most target keys first."""
    score = {b: len(set(ms) & set(keys)) for b, ms in books.items()}
    ranked = sorted((b for b, s in score.items() if s > 0), key=lambda b: (-score[b], b))
    return ranked[:MAX_BOOKS]


def quotes(body: dict | None) -> pd.DataFrame:
    """One row per book, market, subject (team or player; '' for match markets), line
    and side, with the decimal price."""
    rows = []
    for b in (body or {}).get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            for o in m.get("outcomes", []) or []:
                name = str(o.get("name", ""))
                side = TWO_SIDED.get(name.lower())
                if side is None or o.get("price") is None:
                    continue
                point = o.get("point")
                rows.append(
                    {
                        "book": b.get("key"),
                        "market": m.get("key"),
                        "subject": str(o.get("description") or ""),
                        "line": float(point) if point is not None else np.nan,
                        "side": side,
                        "odds": float(o["price"]),
                    }
                )
    cols = ["book", "market", "subject", "line", "side", "odds"]
    return pd.DataFrame(rows, columns=cols)


def pairs(q: pd.DataFrame) -> pd.DataFrame:
    """Over/under (or yes/no) pairs at the same book, market, subject and line, with the
    margin 1/over + 1/under - 1. One-sided quotes keep a NaN margin."""
    if q.empty:
        return pd.DataFrame(
            columns=["book", "market", "subject", "line", "over", "under", "margin"]
        )
    key = ["book", "market", "subject", "line"]
    q = q.assign(line=q["line"].fillna(-1.0))
    w = q.pivot_table(index=key, columns="side", values="odds", aggfunc="last").reset_index()
    for s in ("over", "under"):
        if s not in w:
            w[s] = np.nan
    w["margin"] = 1 / w["over"] + 1 / w["under"] - 1
    w["line"] = w["line"].where(w["line"] >= 0)
    return w[[*key, "over", "under", "margin"]]


def summarize(p: pd.DataFrame) -> list[dict]:
    """Per market and book: lines quoted, how many are two-sided, the margin (median,
    min, max) over the two-sided ones."""
    out = []
    for (market, book), x in p.groupby(["market", "book"]):
        two = x[x["margin"].notna()]
        lines = sorted({float(v) for v in x["line"].dropna()})
        out.append(
            {
                "market": market,
                "book": book,
                "quotes": int(len(x)),
                "two_sided": int(len(two)),
                "lines": lines,
                "subjects": int(x["subject"].replace("", np.nan).nunique()),
                "margin_median": float(two["margin"].median()) if len(two) else None,
                "margin_min": float(two["margin"].min()) if len(two) else None,
                "margin_max": float(two["margin"].max()) if len(two) else None,
            }
        )
    return out


def reading(margin: float | None) -> str:
    """The pre-registered reading of a book's median two-sided margin."""
    if margin is None:
        return "one-sided"
    if margin <= 0.05:
        return "close to fair"
    if margin <= 0.08:
        return "soft but testable"
    return "not bettable"


def run(
    cap: int,
    leagues: tuple[str, ...] = LEAGUES,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    out_dir: Path | None = None,
) -> dict:
    """The probe, never past `cap` credits. Returns per-league results plus the log,
    credits spent and the balance before and after."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    budget = CappedBudget(cap)
    res: dict = {"cap": cap, "leagues": {}, "log": budget.log, "left_before": None}
    if not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
        return {**res, "spent": 0, "left": None}
    est_discovery = DISCOVERY_ESTIMATE

    def save(name: str, body) -> None:
        if out_dir is not None and body is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_text(json.dumps(body))

    for i, lg in enumerate(leagues):
        sport = SPORTS[lg]
        r: dict = {"sport": sport}
        res["leagues"][lg] = r
        evs = _get(
            get, f"{BASE}/sports/{sport}/events", {"apiKey": api_key}, budget, 0, f"{lg} events"
        )
        if res["left_before"] is None:
            res["left_before"] = budget.left
        upcoming = sorted(
            [e for e in evs or [] if pd.Timestamp(e["commence_time"]) > now],
            key=lambda e: e["commence_time"],
        )
        if not upcoming:
            r["note"] = "no upcoming events"
            continue
        e = upcoming[0]
        r["event"] = f"{e['home_team']} v {e['away_team']}"
        r["kickoff"] = e["commence_time"]
        before = budget.spent
        disc = _get(
            get,
            f"{BASE}/sports/{sport}/events/{e['id']}/markets",
            {"apiKey": api_key, "regions": ",".join(REGIONS)},
            budget,
            est_discovery,
            f"{lg} markets {r['event']}",
        )
        if disc is None:
            r["note"] = "discovery skipped or failed"
            continue
        est_discovery = max(budget.spent - before, 1)
        save(f"{lg}_{e['id']}_markets.json", disc)
        books = discovered_markets(disc)
        r["books_markets"] = books
        found = set().union(*books.values()) if books else set()
        r["market_keys"] = sorted(found)
        keys = target_keys(found, lg)
        # Even share of what is left, kept for this and the leagues after it.
        share = (cap - budget.spent) // max(len(leagues) - i, 1)
        chosen = choose_books(books, keys)
        groups = max(math.ceil(len(chosen) / MAX_BOOKS), 1)
        while keys and len(keys) * groups > max(share, 1):
            keys = keys[:-1]
        r["requested"] = {"markets": keys, "bookmakers": choose_books(books, keys)}
        if not keys:
            r["note"] = "no target market listed" if not target_keys(found, lg) else "no credits"
            continue
        body = _get(
            get,
            f"{BASE}/sports/{sport}/events/{e['id']}/odds",
            {
                "apiKey": api_key,
                "bookmakers": ",".join(r["requested"]["bookmakers"]),
                "markets": ",".join(keys),
                "oddsFormat": "decimal",
            },
            budget,
            len(keys) * groups,
            f"{lg} odds {r['event']} ({len(keys)} markets)",
        )
        if body is None:
            r["note"] = "odds call skipped or failed"
            continue
        save(f"{lg}_{e['id']}_odds.json", body)
        p = pairs(quotes(body))
        r["summary"] = summarize(p)
    return {**res, "spent": budget.spent, "left": budget.left}


def report(res: dict) -> str:
    lines = [
        f"== Live market probe: cap {res['cap']}, spent {res['spent']}, credits before "
        f"{res['left_before']}, after {res['left']} =="
    ]
    for lg, r in res["leagues"].items():
        lines.append(f"{lg} ({r.get('event', '-')}, {r.get('kickoff', '-')}): {r.get('note', '')}")
        if "market_keys" in r:
            lines.append(f"  market keys found: {', '.join(r['market_keys']) or 'none'}")
            lines.append(f"  requested: {r.get('requested')}")
        for s in r.get("summary", []):
            m = s["margin_median"]
            lines.append(
                f"  {s['market']:>24} {s['book']:<16} quotes {s['quotes']:>3}, two-sided "
                f"{s['two_sided']:>3}, lines {s['lines'][:8]}"
                + (
                    f", margin {m:.1%} ({s['margin_min']:.1%}-{s['margin_max']:.1%})"
                    if m is not None
                    else ""
                )
                + f" -> {reading(m)}"
            )
    lines += ["Log:", *[f"  {x}" for x in res["log"]]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.market_probe")
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--json")
    ap.add_argument("--bodies", default="out/market_bodies")
    args = ap.parse_args(argv)
    res = run(args.cap, out_dir=Path(args.bodies) if args.cap > 0 else None)
    print(report(res))
    text = json.dumps(res, default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("MARKETS_JSON " + text)


if __name__ == "__main__":
    main()
