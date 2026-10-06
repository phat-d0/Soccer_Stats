"""Which bookmakers price both sides of an EPL player-prop line, and at what margin?

FanDuel lists over sides only, so its margin can't be removed and its implied chances
run about 10 points above what happens. A book that prices the over AND the under gives
a margin we can measure and a fair chance we can bet against (or compare FanDuel to).

`pairs` turns one Odds API event-odds body into one row per (book, market, player,
threshold) with the over and under prices; `book_summary` reduces that per book;
`fanduel_vs_fair` prices FanDuel's overs against the two-sided books' fair chances.
`probe` makes the few paid calls (live, then historical), stopping at a credit budget.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable

import numpy as np
import pandas as pd
import requests

from soccer_stats.odds import devig_shin

BASE = "https://api.the-odds-api.com/v4"
SPORT = "soccer_epl"
MARKETS = ("player_shots", "player_shots_on_target")
REGIONS = ("us", "us2", "uk", "eu", "au")
SIDES = {"over": "over", "under": "under", "yes": "over", "no": "under"}


def threshold(point: float, two_sided: bool) -> int | None:
    """The count an over needs: over 1.5 -> 2. A whole-number line is FanDuel's "at
    least X" when only the over is listed; with an under too it would push, so skip it."""
    if point is None or (isinstance(point, float) and math.isnan(point)):
        return None
    if float(point).is_integer():
        return None if two_sided else int(point)
    return math.floor(point) + 1


def outcomes(event: dict) -> pd.DataFrame:
    """One row per priced side of every player market in an event-odds body."""
    ev = event.get("data", event) if isinstance(event, dict) else {}
    rows = []
    for b in ev.get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            if m.get("key") not in MARKETS:
                continue
            for o in m.get("outcomes", []) or []:
                side = SIDES.get(str(o.get("name", "")).lower())
                if side is None or not o.get("description") or o.get("price") is None:
                    continue
                rows.append(
                    {
                        "event_id": ev.get("id"),
                        "book": b.get("key"),
                        "market": m["key"],
                        "player": o["description"],
                        "point": float(o["point"]) if o.get("point") is not None else np.nan,
                        "side": side,
                        "odds": float(o["price"]),
                    }
                )
    cols = ["event_id", "book", "market", "player", "point", "side", "odds"]
    return pd.DataFrame(rows, columns=cols)


def pairs(event: dict) -> pd.DataFrame:
    """Per book, market, player and line: over/under prices, margin and fair over chance.

    `k` is the count an over needs. Books that list only overs get rows with under = NaN.
    """
    o = outcomes(event)
    cols = ["event_id", "book", "market", "player", "point", "k", "over", "under", "margin"]
    if o.empty:
        return pd.DataFrame(columns=[*cols, "fair_over"])
    o = o.drop_duplicates(["book", "market", "player", "point", "side"], keep="last")
    w = o.pivot_table(
        index=["event_id", "book", "market", "player", "point"],
        columns="side",
        values="odds",
        aggfunc="last",
    ).reset_index()
    for s in ("over", "under"):
        if s not in w:
            w[s] = np.nan
    two = w["over"].notna() & w["under"].notna()
    w["k"] = [threshold(p, t) for p, t in zip(w["point"], two, strict=True)]
    w["margin"] = np.where(two, 1 / w["over"] + 1 / w["under"] - 1, np.nan)
    fair = np.full(len(w), np.nan)
    for i in np.flatnonzero(two.to_numpy()):
        fair[i] = devig_shin(np.array([w["over"].iloc[i], w["under"].iloc[i]]))[0]
    w["fair_over"] = fair
    return w[[*cols, "fair_over"]]


def book_summary(p: pd.DataFrame) -> pd.DataFrame:
    """Per book and market: lines, how many have both sides, and the margin on those."""
    if p.empty:
        return pd.DataFrame(
            columns=["book", "market", "lines", "two_sided", "players", "margin", "margin_median"]
        )
    g = p.assign(two=p["under"].notna() & p["over"].notna())
    rows = []
    for (book, market), x in g.groupby(["book", "market"]):
        t = x[x["two"]]
        rows.append(
            {
                "book": book,
                "market": market,
                "lines": len(x),
                "two_sided": int(x["two"].sum()),
                "players": x["player"].nunique(),
                "margin": float(t["margin"].mean()) if len(t) else None,
                "margin_median": float(t["margin"].median()) if len(t) else None,
            }
        )
    return pd.DataFrame(rows)


def fanduel_vs_fair(p: pd.DataFrame, book: str = "fanduel") -> pd.DataFrame:
    """FanDuel's over prices against the average fair chance from two-sided books for
    the same player, market and count. ev = odds x fair - 1 (what an over returns)."""
    fd = p[(p["book"] == book) & p["k"].notna() & p["over"].notna()]
    fair = p[(p["book"] != book) & p["fair_over"].notna() & p["k"].notna()]
    if fd.empty or fair.empty:
        return pd.DataFrame(columns=["market", "player", "k", "over", "fair", "books", "ev"])
    f = fair.groupby(["event_id", "market", "player", "k"]).agg(
        fair=("fair_over", "mean"), books=("book", "nunique")
    )
    m = fd.merge(f.reset_index(), on=["event_id", "market", "player", "k"])
    m["ev"] = m["over"] * m["fair"] - 1
    return m[["market", "player", "k", "over", "fair", "books", "ev"]]


# ---------- paid probe ----------


class Budget:
    """Credits spent so far, read from x-requests-last (or the estimate when absent)."""

    def __init__(self, cap: int):
        self.cap, self.spent, self.left, self.log = cap, 0, None, []

    def allows(self, estimate: int) -> bool:
        return self.spent + estimate <= self.cap

    def charge(self, resp, estimate: int, label: str) -> None:
        cost = resp.headers.get("x-requests-last")
        rem = resp.headers.get("x-requests-remaining")
        used = int(float(cost)) if cost not in (None, "") else estimate
        self.spent += used
        self.left = int(float(rem)) if rem not in (None, "") else self.left
        self.log.append(f"{label}: HTTP {resp.status_code}, cost {used}, credits left {self.left}")


def _get(get, url, params, budget, estimate, label):
    if not budget.allows(estimate):
        budget.log.append(f"{label}: skipped, would pass the {budget.cap}-credit cap")
        return None
    try:
        r = get(url, params=params, timeout=60)
    except requests.RequestException as exc:  # the message would hold the URL and key
        budget.log.append(f"{label}: could not reach The Odds API ({type(exc).__name__})")
        return None
    budget.charge(r, estimate, label)
    return r.json() if r.ok else None


def probe(
    cap: int,
    hist_dates: list[str],
    now: pd.Timestamp | None = None,
    live_events: int = 2,
    regions: tuple[str, ...] = REGIONS,
    api_key: str | None = None,
    get: Callable = requests.get,
) -> dict:
    """Live then historical player-prop odds across all regions, within `cap` credits.

    Live: upcoming events in kickoff order until one has player prices (at most
    `live_events` calls, about markets x regions credits each). Historical: for each date
    in `hist_dates`, the event list (1 credit) and the first match that kicked off after
    it, an hour before kickoff (about 10 x markets x regions credits).
    Returns {"bodies": [(label, body)], "log": [...], "spent": n, "left": n}.
    """
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    budget = Budget(cap)
    out = {"bodies": [], "log": budget.log}
    if not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
        return {**out, "spent": 0, "left": None}
    nm, nr = len(MARKETS), len(regions)
    params = {
        "apiKey": api_key,
        "regions": ",".join(regions),
        "markets": ",".join(MARKETS),
        "oddsFormat": "decimal",
    }
    evs = _get(get, f"{BASE}/sports/{SPORT}/events", {"apiKey": api_key}, budget, 0, "events")
    for e in sorted(evs or [], key=lambda e: e["commence_time"])[:live_events]:
        body = _get(
            get,
            f"{BASE}/sports/{SPORT}/events/{e['id']}/odds",
            params,
            budget,
            nm * nr,
            f"live {e['home_team']} v {e['away_team']} ({e['commence_time']})",
        )
        if body is not None:
            out["bodies"].append(("live", body))
            if not pairs(body).empty:
                break
    for d in hist_dates:
        idx = _get(
            get,
            f"{BASE}/historical/sports/{SPORT}/events",
            {"apiKey": api_key, "date": d},
            budget,
            1,
            f"historical events at {d}",
        )
        cands = [
            e
            for e in (idx or {}).get("data", [])
            if pd.Timestamp(e["commence_time"]) < now - pd.Timedelta(hours=3)
        ]
        if not cands:
            budget.log.append(f"historical {d}: no finished match listed")
            continue
        e = min(cands, key=lambda e: e["commence_time"])
        at = pd.Timestamp(e["commence_time"]) - pd.Timedelta(hours=1)
        body = _get(
            get,
            f"{BASE}/historical/sports/{SPORT}/events/{e['id']}/odds",
            {**params, "date": at.strftime("%Y-%m-%dT%H:%M:%SZ")},
            budget,
            10 * nm * nr,
            f"historical {e['home_team']} v {e['away_team']} at {at.isoformat()}",
        )
        if body is not None:
            out["bodies"].append((f"historical {at.date()}", body))
    return {**out, "spent": budget.spent, "left": budget.left}
