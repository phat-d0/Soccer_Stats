"""Corner-market discovery (corners team, 8 Nov 2026; lead's cap 20 credits;
docs/totals.md "Corner-market discovery").

Which books on The Odds API quote corner markets (team corners, match total corners,
corner handicaps, first-half corners), at which lines and margins, and how often a book's
better side beats Pinnacle's Shin-fair price at the same line at the same moment. Prices
only: no result is read.

Per league (`corners_live.LEAGUES`): the free events list, then one markets-discovery
call on the soonest upcoming match (regions us, uk, eu, au; 1 credit). Then, for the
first `odds_leagues` leagues whose discovery lists a corner market, one odds call on that
match: every corner key found (team corners first, then match totals, handicaps, first
half), at Pinnacle plus up to nine more books listing them (exchanges first), so ten books
bill as one region, 1 credit per market. Stops everything if a discovery call bills more
than 1 credit or an odds call more than one per market (`stopped`). Budget:
`player_goal_odds.CappedBudget` (cap and the 3,000 reserve). The key is never printed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats.corners_live import LEAGUES
from soccer_stats.edge.props import _get
from soccer_stats.odds import devig_shin
from soccer_stats.odds_feed import SPORTS
from soccer_stats.player_goal_odds import CappedBudget
from soccer_stats.player_odds import BASE

REGIONS = ("us", "uk", "eu", "au")
MAX_BOOKS = 10  # ten books bill as one region
SHARP = "pinnacle"
EXCHANGES = ("betfair_ex_uk", "betfair_ex_eu", "betfair_ex_au", "matchbook", "smarkets")
ODDS_LEAGUES = 3
VALUE_CUTS = (0.0, 0.02)


def is_corner(key: str) -> bool:
    return "corner" in key


def kind_of(key: str) -> str:
    """team / total / handicap, with '_h1' for first-half keys (by the key's words)."""
    base = "team" if "team" in key else "handicap" if "spread" in key else "total"
    half = "_h1" if ("h1" in key or "1st" in key or "first" in key) else ""
    return base + half


ORDER = ("team", "total", "handicap", "team_h1", "total_h1", "handicap_h1")


def corner_keys(found: set[str]) -> list[str]:
    """Corner keys in priority order: team corners, match totals, handicaps, then the
    first-half ones; alternate keys before main ones within a kind."""
    keys = [k for k in found if is_corner(k)]
    return sorted(keys, key=lambda k: (ORDER.index(kind_of(k)), "alternate" not in k, k))


def discovered(body: dict | None) -> dict[str, list[str]]:
    """{book: [market keys]} from a markets-discovery body."""
    return {
        b.get("key"): sorted({m.get("key") for m in b.get("markets", []) or []})
        for b in (body or {}).get("bookmakers", []) or []
    }


def choose_books(books: dict[str, list[str]], keys: list[str]) -> list[str]:
    """Pinnacle, then exchanges, then the books listing most of `keys`; at most ten."""
    score = {b: len(set(ms) & set(keys)) for b, ms in books.items()}
    listing = [b for b, s in score.items() if s > 0]
    rank = sorted(listing, key=lambda b: (b != SHARP, b not in EXCHANGES, -score[b], b))
    return rank[:MAX_BOOKS]


def rows(body: dict | None, now: pd.Timestamp) -> pd.DataFrame:
    """One row per book, market, subject, line and side ('over'/'under'; on a handicap
    'over' = the home side at `line`, 'under' = the away side at −line), with the
    decimal price and the market's age in minutes."""
    body = body or {}
    home = body.get("home_team")
    out = []
    for b in body.get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            key = m.get("key", "")
            if not is_corner(key):
                continue
            upd = m.get("last_update") or b.get("last_update")
            age = (now - pd.Timestamp(upd)).total_seconds() / 60 if upd else np.nan
            hcap = kind_of(key).startswith("handicap")
            for o in m.get("outcomes", []) or []:
                if o.get("price") is None or o.get("point") is None:
                    continue
                name, point = str(o.get("name", "")), float(o["point"])
                if hcap:
                    side = "over" if name == home else "under"
                    line, subject = (point if side == "over" else -point), ""
                else:
                    side = name.lower()
                    if side not in ("over", "under"):
                        continue
                    line, subject = point, str(o.get("description") or "")
                out.append(
                    {
                        "book": b.get("key"),
                        "market": key,
                        "kind": kind_of(key),
                        "subject": subject,
                        "line": line,
                        "side": side,
                        "odds": float(o["price"]),
                        "age_min": age,
                    }
                )
    cols = ["book", "market", "kind", "subject", "line", "side", "odds", "age_min"]
    return pd.DataFrame(out, columns=cols)


def pairs(r: pd.DataFrame) -> pd.DataFrame:
    """Over/under at the same book, market, subject and line: margin 1/over + 1/under − 1
    and the Shin-fair over chance (NaN when one-sided)."""
    key = ["book", "market", "kind", "subject", "line"]
    if r.empty:
        return pd.DataFrame(columns=[*key, "over", "under", "age_min", "margin", "fair_over"])
    w = r.pivot_table(index=key, columns="side", values="odds", aggfunc="last").reset_index()
    age = r.groupby(key)["age_min"].max().reset_index()
    w = w.merge(age, on=key, how="left")
    for s in ("over", "under"):
        if s not in w:
            w[s] = np.nan
    w["margin"] = 1 / w["over"] + 1 / w["under"] - 1
    both = w["over"].notna() & w["under"].notna()
    w["fair_over"] = np.nan
    w.loc[both, "fair_over"] = [
        float(devig_shin(np.array([o, u]))[0])
        for o, u in zip(w.loc[both, "over"], w.loc[both, "under"], strict=True)
    ]
    return w[[*key, "over", "under", "age_min", "margin", "fair_over"]]


def against_sharp(p: pd.DataFrame) -> pd.DataFrame:
    """Each non-Pinnacle line that Pinnacle also quotes two-sided: the value of each side
    at Pinnacle's Shin-fair chance (odds × fair − 1) and of the better side."""
    key = ["market", "subject", "line"]
    pin = p[(p["book"] == SHARP) & p["fair_over"].notna()][[*key, "fair_over", "age_min"]]
    pin = pin.rename(columns={"fair_over": "pin_fair_over", "age_min": "pin_age_min"})
    o = p[p["book"] != SHARP].merge(pin, on=key, how="inner")
    o["value_over"] = o["over"] * o["pin_fair_over"] - 1
    o["value_under"] = o["under"] * (1 - o["pin_fair_over"]) - 1
    o["value_best"] = o[["value_over", "value_under"]].max(axis=1)
    return o


def summarize(p: pd.DataFrame, cmp: pd.DataFrame) -> list[dict]:
    """Per kind, market and book: lines, two-sided lines, margin (median, min), and on
    the lines Pinnacle shares: the better side's value vs Pinnacle's fair price (median,
    max) and the share above each of VALUE_CUTS."""
    out = []
    for (kind, market, book), x in p.groupby(["kind", "market", "book"]):
        two = x[x["margin"].notna()]
        c = cmp[(cmp["market"] == market) & (cmp["book"] == book)]
        row = {
            "kind": kind,
            "market": market,
            "book": book,
            "lines": int(len(x)),
            "two_sided": int(len(two)),
            "margin_median": float(two["margin"].median()) if len(two) else None,
            "margin_min": float(two["margin"].min()) if len(two) else None,
            "age_median_min": float(x["age_min"].median()) if x["age_min"].notna().any() else None,
            "vs_pinnacle": int(len(c)),
        }
        if len(c):
            row["value_median"] = float(c["value_best"].median())
            row["value_max"] = float(c["value_best"].max())
            for cut in VALUE_CUTS:
                row[f"share_above_{cut:g}"] = float((c["value_best"] > cut).mean())
        out.append(row)
    return out


def run(
    cap: int,
    leagues: tuple[str, ...] = LEAGUES,
    odds_leagues: int = ODDS_LEAGUES,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    out_dir: Path | None = None,
) -> dict:
    """The discovery, never past `cap` credits (0 = events lists only)."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    budget = CappedBudget(cap)
    res: dict = {"cap": cap, "regions": list(REGIONS), "leagues": {}, "log": budget.log}
    res["stopped"] = None
    if not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
        return {**res, "spent": 0, "left": None}

    def save(name: str, body) -> None:
        if out_dir is not None and body is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_text(json.dumps(body))

    # Phase 1: discovery in every league.
    for lg in leagues:
        r: dict = {}
        res["leagues"][lg] = r
        if res["stopped"]:
            r["note"] = "not run: " + res["stopped"]
            continue
        sport = SPORTS[lg]
        evs = _get(get, f"{BASE}/sports/{sport}/events", {"apiKey": api_key}, budget, 0, lg)
        up = sorted(
            (e for e in evs or [] if pd.Timestamp(e["commence_time"]) > now),
            key=lambda e: e["commence_time"],
        )
        r["upcoming"] = len(up)
        if not up:
            r["note"] = "no upcoming events"
            continue
        e = up[0]
        r.update(event_id=e["id"], event=f"{e['home_team']} v {e['away_team']}")
        r["kickoff"] = e["commence_time"]
        before = budget.spent
        disc = _get(
            get,
            f"{BASE}/sports/{sport}/events/{e['id']}/markets",
            {"apiKey": api_key, "regions": ",".join(REGIONS)},
            budget,
            1,
            f"{lg} markets {r['event']}",
        )
        if disc is None:
            r["note"] = "discovery skipped or failed"
            continue
        if budget.spent - before > 1:
            res["stopped"] = f"{lg} discovery billed {budget.spent - before} credits (> 1)"
        save(f"{lg}_{e['id']}_markets.json", disc)
        books = discovered(disc)
        r["books"] = len(books)
        keys = corner_keys(set().union(*books.values()) if books else set())
        r["corner_keys"] = keys
        r["listing"] = {k: sorted(b for b, ms in books.items() if k in ms) for k in keys}
        r["_books"] = books

    # Phase 2: priced calls in the first `odds_leagues` leagues with corner keys.
    todo = [lg for lg, r in res["leagues"].items() if r.get("corner_keys")][:odds_leagues]
    for i, lg in enumerate(todo):
        r = res["leagues"][lg]
        if res["stopped"]:
            r["note"] = "odds not run: " + res["stopped"]
            continue
        keys = list(r["corner_keys"])
        share = (cap - budget.spent) // max(len(todo) - i, 1)
        keys = keys[: max(share, 0)]
        if not keys:
            r["note"] = "no credits left for an odds call"
            continue
        chosen = choose_books(r["_books"], keys)
        r["requested"] = {"markets": keys, "bookmakers": chosen}
        before = budget.spent
        body = _get(
            get,
            f"{BASE}/sports/{SPORTS[lg]}/events/{r['event_id']}/odds",
            {
                "apiKey": api_key,
                "bookmakers": ",".join(chosen),
                "markets": ",".join(keys),
                "oddsFormat": "decimal",
            },
            budget,
            len(keys),
            f"{lg} odds {r['event']} ({len(keys)} markets)",
        )
        if body is None:
            r["note"] = "odds call skipped or failed"
            continue
        if budget.spent - before > len(keys):
            res["stopped"] = (
                f"{lg} odds billed {budget.spent - before} credits for {len(keys)} markets"
            )
        save(f"{lg}_{r['event_id']}_odds.json", body)
        p = pairs(rows(body, now))
        cmp = against_sharp(p)
        r["summary"] = summarize(p, cmp)
        r["compared_lines"] = int(len(cmp))
    for r in res["leagues"].values():
        r.pop("_books", None)
    return {**res, "spent": budget.spent, "left": budget.left}


def pct(x) -> str:
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.1%}"


def report(res: dict) -> str:
    lines = [
        f"== Corner-market discovery: cap {res['cap']}, spent {res['spent']}, credits "
        f"left {res['left']}, regions {','.join(res['regions'])} =="
    ]
    if res.get("stopped"):
        lines.append(f"STOPPED: {res['stopped']}")
    for lg, r in res["leagues"].items():
        lines.append(f"{lg}: {r.get('event', '-')} {r.get('kickoff', '')} {r.get('note', '')}")
        for k, bs in (r.get("listing") or {}).items():
            lines.append(f"  listed {k} ({kind_of(k)}): {len(bs)} books: {', '.join(bs)}")
        for s in r.get("summary", []):
            lines.append(
                f"  {s['kind']:<11} {s['market']:<34} {s['book']:<18} lines {s['lines']:>3}"
                f" two-sided {s['two_sided']:>3} margin {pct(s['margin_median'])}"
                f" (min {pct(s['margin_min'])}) vs Pinnacle {s['vs_pinnacle']:>3}"
                + (
                    f": better side median {pct(s['value_median'])}, max "
                    f"{pct(s['value_max'])}, >0 {s['share_above_0']:.0%}, >2% "
                    f"{s['share_above_0.02']:.0%}"
                    if s["vs_pinnacle"]
                    else ""
                )
            )
    lines += ["Log:", *[f"  {x}" for x in res["log"]]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.corner_markets")
    ap.add_argument("--cap", type=int, default=0, help="credits (0 = events lists only)")
    ap.add_argument("--odds-leagues", type=int, default=ODDS_LEAGUES)
    ap.add_argument("--json")
    ap.add_argument("--bodies", default="out/corner_market_bodies")
    args = ap.parse_args(argv)
    res = run(
        args.cap,
        odds_leagues=args.odds_leagues,
        out_dir=Path(args.bodies) if args.cap > 0 else None,
    )
    print(report(res))
    text = json.dumps(res, default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("CORNER_MARKETS_JSON " + text)


if __name__ == "__main__":
    main()
