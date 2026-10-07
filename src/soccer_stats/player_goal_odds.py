"""Anytime-goalscorer odds: the stage-0 live probe and the 5-match pilot (docs/player_props.md).

Pre-registered there before any call:
- Stage 0: live event odds for upcoming EPL matches, all five regions, market
  `player_goal_scorer_anytime` (1 credit per region per market returned, so at most 5
  a call; an empty answer costs nothing). Stops after two matches with prices.
- Pilot: five cached 2025/26 matches (first with a cached FanDuel close in Aug, Oct,
  Dec, Feb and Apr), at the same close snapshot (one minute before kickoff), one
  `bookmakers=` list (the books stage 0 found, topped up to ten with the known
  player-prop books: up to ten books cost one region), 1 market: at most 10 credits a
  call.
- Hard cap: every call is skipped if its maximum cost would take the running total
  past the cap; the cost read from x-requests-last is what is counted.

Measured on starters (Understat) at the close, ranges resampling whole matches: which
books price the market, whether any lists a "No" price (and the margin then), how many
names match the model's players, the gap between the best price's implied chance and
the scoring rate, back-all ROI at the best price, and the model beside the price.
`verdict` applies the pre-registered rules as written.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats.data import RAW_DIR
from soccer_stats.edge.props import Budget, _get
from soccer_stats.edge.stats import bootstrap_mean
from soccer_stats.odds_feed import SPORTS, _team
from soccer_stats.player_data import match_in_fixture
from soccer_stats.player_odds import BASE, PLAYER_BOOKMAKER, RESERVE_CREDITS, hist_path

MARKET = "player_goal_scorer_anytime"
REGIONS = ("us", "us2", "uk", "eu", "au")
PILOT_MONTHS = (8, 10, 12, 2, 4)
# Books seen pricing EPL player props on The Odds API (docs/edge.md, round 1), plus the
# big US books; the pilot list starts with whatever stage 0 finds.
KNOWN_BOOKS = (
    "fanduel",
    "draftkings",
    "betmgm",
    "williamhill_us",
    "betrivers",
    "ballybet",
    "unibet",
    "betparx",
    "onexbet",
    "pinnacle",
)
MAX_BOOKS = 10
LIVE_TRIES = 8  # upcoming events tried in kickoff order (an empty answer costs 0)
SIDES = {"yes": "yes", "over": "yes", "no": "no", "under": "no"}


class CappedBudget(Budget):
    """Budget that also keeps the shared key's live reserve: a call is skipped if its
    maximum cost would pass the cap, or leave fewer than RESERVE_CREDITS on the key."""

    def allows(self, estimate: int) -> bool:
        if self.left is not None and self.left - estimate < RESERVE_CREDITS:
            return False
        return super().allows(estimate)


def prices(body: dict) -> pd.DataFrame:
    """One row per book and player: yes (and no, when listed) prices for the market.

    Outcomes come either as name Yes/No with the player in `description`, or with the
    player as the name (Yes only).
    """
    ev = body.get("data", body) if isinstance(body, dict) else {}
    rows = []
    for b in ev.get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            if m.get("key") != MARKET:
                continue
            for o in m.get("outcomes", []) or []:
                if o.get("price") is None:
                    continue
                name, desc = str(o.get("name", "")), o.get("description")
                side = SIDES.get(name.lower())
                player = desc if desc else (None if side else name)
                if not player:
                    continue
                rows.append(
                    {
                        "event_id": ev.get("id"),
                        "book": b.get("key"),
                        "player": player,
                        "side": side or "yes",
                        "odds": float(o["price"]),
                    }
                )
    cols = ["event_id", "book", "player", "yes", "no"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows).drop_duplicates(["book", "player", "side"], keep="last")
    w = df.pivot_table(
        index=["event_id", "book", "player"], columns="side", values="odds", aggfunc="last"
    ).reset_index()
    for s in ("yes", "no"):
        if s not in w:
            w[s] = np.nan
    return w[cols]


def book_summary(p: pd.DataFrame) -> list[dict]:
    """Per book: players priced, how many with a No price, and the Yes+No margin then."""
    out = []
    for book, x in p.groupby("book"):
        two = x[x["yes"].notna() & x["no"].notna()]
        out.append(
            {
                "book": book,
                "players": int(x["player"].nunique()),
                "events": int(x["event_id"].nunique()),
                "two_sided": len(two),
                "margin_two_sided": round(float((1 / two["yes"] + 1 / two["no"] - 1).mean()), 4)
                if len(two)
                else None,
                "avg_yes_odds": round(float(x["yes"].mean()), 3),
            }
        )
    return sorted(out, key=lambda r: -r["players"])


def pilot_events(league: str = "E0", raw_dir: Path = RAW_DIR, season_start: int = 2025):
    """The pre-registered five: the first cached FanDuel close of 2025/26 in each of
    Aug, Oct, Dec, Feb and Apr. Returns [{event_id, requested, kickoff, home, away}]."""
    d = raw_dir / "player_odds_history" / league
    snaps = []
    for path in sorted(d.glob(f"*_close_{PLAYER_BOOKMAKER}.json")) if d.exists() else []:
        s = json.loads(path.read_text())
        ev = s.get("event") or {}
        if not ev.get("commence_time") or not ev.get("bookmakers"):
            continue
        k = pd.Timestamp(ev["commence_time"]).tz_convert("UTC")
        if not (
            pd.Timestamp(f"{season_start}-07-01", tz="UTC")
            <= k
            < pd.Timestamp(f"{season_start + 1}-07-01", tz="UTC")
        ):
            continue
        snaps.append(
            {
                "event_id": ev["id"],
                "requested": s["requested"],
                "kickoff": k,
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
            }
        )
    snaps.sort(key=lambda s: s["kickoff"])
    out = []
    for month in PILOT_MONTHS:
        hit = next((s for s in snaps if s["kickoff"].month == month), None)
        if hit:
            out.append(hit)
    return out


def run_calls(
    cap: int,
    events: list[dict],
    league: str = "E0",
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get: Callable = requests.get,
    raw_dir: Path = RAW_DIR,
) -> dict:
    """Stage 0 then the pilot, never past `cap` credits. Each body is cached next to the
    FanDuel history (`{event_id}_close_goalscorer.json`) so a rerun costs nothing."""
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    now = now or pd.Timestamp.now(tz="UTC")
    budget = CappedBudget(cap)
    out = {"live": [], "pilot": [], "log": budget.log, "books_requested": []}
    if not api_key:
        budget.log.append("No ODDS_API_KEY: nothing fetched")
        return {**out, "spent": 0, "left": None}
    sport = SPORTS[league]
    # Stage 0: live, all regions.
    evs = _get(get, f"{BASE}/sports/{sport}/events", {"apiKey": api_key}, budget, 0, "events")
    upcoming = sorted(
        [e for e in evs or [] if pd.Timestamp(e["commence_time"]) > now],
        key=lambda e: e["commence_time"],
    )
    priced = 0
    for e in upcoming[:LIVE_TRIES]:
        if priced >= 2:
            break
        body = _get(
            get,
            f"{BASE}/sports/{sport}/events/{e['id']}/odds",
            {
                "apiKey": api_key,
                "regions": ",".join(REGIONS),
                "markets": MARKET,
                "oddsFormat": "decimal",
            },
            budget,
            len(REGIONS),
            f"live {e['home_team']} v {e['away_team']} ({e['commence_time']})",
        )
        if body is None:
            continue
        p = prices(body)
        out["live"].append(
            {
                "event": f"{e['home_team']} v {e['away_team']}",
                "kickoff": e["commence_time"],
                "prices": p,
            }
        )
        if not p.empty:
            priced += 1
    found = []
    for x in out["live"]:
        for r in book_summary(x["prices"]):
            if r["book"] not in found:
                found.append(r["book"])
    books = (found + [b for b in KNOWN_BOOKS if b not in found])[:MAX_BOOKS]
    out["books_found_live"] = found
    out["books_requested"] = books
    # Pilot: historical close snapshots, one bookmakers= list.
    for ev in events:
        path = hist_path(league, ev["event_id"], "close", raw_dir).with_name(
            f"{ev['event_id']}_close_goalscorer.json"
        )
        if path.exists():
            body = json.loads(path.read_text())
            budget.log.append(f"pilot {ev['home']} v {ev['away']}: cached, 0 credits")
        else:
            at = pd.Timestamp(ev["requested"]).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = _get(
                get,
                f"{BASE}/historical/sports/{sport}/events/{ev['event_id']}/odds",
                {
                    "apiKey": api_key,
                    "bookmakers": ",".join(books),
                    "markets": MARKET,
                    "oddsFormat": "decimal",
                    "date": at,
                },
                budget,
                10,
                f"pilot {ev['home']} v {ev['away']} at {at}",
            )
            if body is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body))
        out["pilot"].append({**ev, "prices": prices(body)})
    return {**out, "spent": budget.spent, "left": budget.left}


def match_players(
    pilot: list[dict], apps: pd.DataFrame, preds: pd.DataFrame, known_teams: set[str]
) -> pd.DataFrame:
    """One row per book × player × pilot match, matched to Understat by name within the
    match's two clubs (their whole-season squads), with whether he played and started,
    whether he scored, and the model's chance (lineup known)."""
    rows = []
    for ev in pilot:
        p = ev["prices"]
        if p.empty:
            continue
        home, away = _team(ev["home"] or "", known_teams), _team(ev["away"] or "", known_teams)
        k = pd.Timestamp(ev["kickoff"])
        season = apps[(apps["kickoff"] - k).abs() <= pd.Timedelta(days=200)]
        rosters = {
            t: dict(
                season[season["team"] == t]
                .drop_duplicates("player_id")[["player_id", "player"]]
                .itertuples(index=False, name=None)
            )
            for t in (home, away)
        }
        found, _missed = match_in_fixture(p["player"].unique(), rosters)
        game = apps[
            (apps["team"].isin([home, away]))
            & ((apps["kickoff"] - k).abs() <= pd.Timedelta(hours=3))
        ]
        played = game.set_index("player_id")
        pr = preds[
            (preds["team"].isin([home, away]))
            & ((preds["kickoff"] - k).abs() <= pd.Timedelta(hours=3))
        ]
        model = dict(zip(pr["player_id"], pr["p_model"], strict=True))
        for r in p.itertuples(index=False):
            pid, team = found.get(r.player, (None, None))
            on = pid is not None and pid in played.index
            rows.append(
                {
                    "match": f"{home} v {away}",
                    "kickoff": k,
                    "book": r.book,
                    "player": r.player,
                    "yes": r.yes,
                    "no": r.no,
                    "player_id": pid,
                    "team": team,
                    "matched": pid is not None,
                    "played": bool(on),
                    "started": bool(on and played.loc[pid, "started"]),
                    "scored": int(on and played.loc[pid, "goals"] > 0) if on else None,
                    "p_model": model.get(pid),
                }
            )
    return pd.DataFrame(rows)


def evaluate(m: pd.DataFrame, threshold: float = 0.12) -> dict:
    """The pre-registered measures on matched starters at the best price."""
    if m.empty:
        return {"lines": 0}
    out = {
        "lines": len(m),
        "names": int(m.drop_duplicates(["match", "player"]).shape[0]),
        "names_matched": int(m[m["matched"]].drop_duplicates(["match", "player"]).shape[0]),
    }
    st = m[m["started"]]
    if st.empty:
        return out
    best = st.groupby(["match", "player_id"]).agg(
        best=("yes", "max"),
        books=("book", "nunique"),
        scored=("scored", "first"),
        p_model=("p_model", "first"),
    )
    best = best.reset_index()
    best["implied"] = 1 / best["best"]
    starters_all = st.drop_duplicates(["match", "player_id"]).shape[0]
    gap = best["implied"] - best["scored"]
    ret = best["best"] * best["scored"] - 1
    rng = bootstrap_mean(gap, best["match"])
    roi_rng = bootstrap_mean(ret, best["match"])
    out.update(
        starters_priced=starters_all,
        implied=round(float(best["implied"].mean()), 4),
        scored_rate=round(float(best["scored"].mean()), 4),
        gap=round(float(gap.mean()), 4),
        gap_range95=[round(v, 4) for v in rng] if rng else None,
        back_all_roi=round(float(ret.mean()), 4),
        back_all_roi_range95=[round(v, 4) for v in roi_rng] if roi_rng else None,
    )
    coverage = {}
    for book, x in st.groupby("book"):
        coverage[book] = round(x["player_id"].nunique() / max(starters_all, 1), 3)
    out["starter_coverage"] = coverage
    fd = st[st["book"] == PLAYER_BOOKMAKER].set_index(["match", "player_id"])["yes"]
    if len(fd):
        b = best.set_index(["match", "player_id"])
        common = b.index.intersection(fd.index)
        lift = b.loc[common, "best"] / fd.loc[common] - 1
        out["lift_vs_fanduel_median"] = round(float(lift.median()), 4) if len(lift) else None
    mp = best.dropna(subset=["p_model"])
    if len(mp):
        y = mp["scored"].to_numpy()
        pm = mp["p_model"].clip(1e-4, 1 - 1e-4).to_numpy()
        pi = mp["implied"].clip(1e-4, 1 - 1e-4).to_numpy()
        ll = lambda p: float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))  # noqa: E731
        edge = mp["p_model"] * mp["best"] - 1
        bets = mp[edge >= threshold]
        out["model"] = {
            "starters": len(mp),
            "p_model_mean": round(float(pm.mean()), 4),
            "log_loss_model": round(ll(pm), 4),
            "log_loss_price": round(ll(pi), 4),
            "bets_12pct": len(bets),
            "roi_12pct": round(float((bets["best"] * bets["scored"] - 1).mean()), 4)
            if len(bets)
            else None,
        }
    return out


def verdict(books: list[dict], ev: dict) -> dict:
    """docs/player_props.md §5, applied as written.

    Go (to the full 2025/26 holdout): gap <= 3 points with the upper end of its range
    <= 5, or a book prices Yes and No with a margin under 6%. Kill: gap above 5 points,
    or no book beyond FanDuel covers at least half the starters. Pilot step: ask for the
    100-match sample only if the pilot finds a second book or a two-sided book.
    """
    two = [b for b in books if b["two_sided"] and (b["margin_two_sided"] or 1) < 0.06]
    others = [b for b, c in (ev.get("starter_coverage") or {}).items() if b != PLAYER_BOOKMAKER]
    second_half = [
        b
        for b, c in (ev.get("starter_coverage") or {}).items()
        if b != PLAYER_BOOKMAKER and c >= 0.5
    ]
    gap, rng = ev.get("gap"), ev.get("gap_range95")
    kill = []
    if gap is not None and gap > 0.05:
        kill.append(f"gap {gap:.1%} is above 5 points")
    if not second_half:
        kill.append("no book beyond FanDuel covers at least half the starters")
    go = []
    if gap is not None and rng and gap <= 0.03 and rng[1] <= 0.05:
        go.append("gap at most 3 points with its upper end at most 5")
    if two:
        go.append(f"two-sided book(s) under 6% margin: {', '.join(b['book'] for b in two)}")
    sample = bool(others) or any(b["two_sided"] for b in books)
    decision = "kill" if kill else ("go" if go else "neither")
    return {
        "decision": decision,
        "kill_reasons": kill,
        "go_reasons": go,
        "ask_for_sample": sample and not kill,
        "second_books": others,
    }
