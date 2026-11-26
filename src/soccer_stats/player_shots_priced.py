"""§17 gate (a) (docs/player_props.md): the best US book's markup on starters' shot lines.

Reads the bodies bought by `player_goal_backfill` design `shots17` (E0 2025/26,
`player_shots_alternate` and `player_shots_on_target_alternate` at seven books, entry at
kickoff - 50 min and close at - 2 min) and ESPN's appearance rows for the same season
(`espn_players.pull`, free). No API calls, no model.

- Starter: ESPN's post-match starter flag. A priced player who didn't play is void (never a
  row). Names map to ESPN's players by `match_in_fixture` within the match's two clubs;
  unmatched names are counted and dropped.
- Lines: a line wins on count >= ceil(point) (a whole line k is "at least k", a half line
  k + 0.5 needs k + 1). Counts are ESPN's `totalShots` / `shotsOnTarget`.
- Best price: per player, market, line and snapshot the longest Over among the seven books;
  the books' average Over price (mean decimal odds) beside it.
- Gate (a): starter lines at entry with best Over <= 2.0; markup = sum of best implied / sum
  won - 1 per market; 95% range resampling whole matches (2,000 draws, seed 0). A market
  stops when the range's lower end is above +8%; both stopping closes the test.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.data import RAW_DIR
from soccer_stats.player_data import match_in_fixture
from soccer_stats.player_goal_backfill import DESIGNS, SHOTS_BOOKS, SNAPSHOTS, cache_dir
from soccer_stats.player_goal_priced import link, matches_of
from soccer_stats.player_market_probe import rows as body_rows

DESIGN = "shots17"
LEAGUE = "E0"
MARKETS = {"player_shots_alternate": "shots", "player_shots_on_target_alternate": "sot"}
MAX_ODDS = 2.0
STOP_MARKUP = 0.08
DRAWS, SEED = 2000, 0
PRICE_COLS = [
    "event_id",
    "kind",
    "home",
    "away",
    "kickoff",
    "book",
    "market",
    "player",
    "point",
    "odds",
]


def _events(dirs: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in dirs:
        for path in sorted(d.glob("events_*.json")) if d.exists() else []:
            body = json.loads(path.read_text())
            for e in (body.get("data") if isinstance(body, dict) else body) or []:
                if isinstance(e, dict) and e.get("id"):
                    out[e["id"]] = e
    return out


def price_rows(raw_dir: Path = RAW_DIR, league: str = LEAGUE) -> pd.DataFrame:
    """One row per event, snapshot, book, market, player and line (Over side only)."""
    base = cache_dir(raw_dir, DESIGN) / league
    lists = [base]
    if DESIGNS[DESIGN].get("lists_from"):
        lists.append(Path(raw_dir) / "player_odds_history" / DESIGNS[DESIGN]["lists_from"] / league)
    frames = []
    for eid, e in _events(lists).items():
        for kind in SNAPSHOTS:
            path = base / f"{eid}_{kind}.json"
            if not path.exists():
                continue
            r = body_rows(json.loads(path.read_text()))
            r = r[(r["side"] == "over") & r["market"].isin(MARKETS) & r["point"].notna()]
            if r.empty:
                continue
            frames.append(
                r.assign(
                    event_id=eid,
                    kind=kind,
                    home=e.get("home_team"),
                    away=e.get("away_team"),
                    kickoff=pd.Timestamp(e["commence_time"]),
                    market=r["market"].map(MARKETS),
                    point=r["point"].astype(float),
                )
            )
    if not frames:
        return pd.DataFrame(columns=PRICE_COLS)
    out = pd.concat(frames, ignore_index=True)
    return out[(out["odds"] > 1) & out["book"].isin(SHOTS_BOOKS)][PRICE_COLS].reset_index(drop=True)


def join(apps: pd.DataFrame, pr: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Price rows joined to ESPN appearances: match_id, player_id, started, count, won.
    Unmatched names and players who didn't play are counted and dropped."""
    counts = {"price_rows": int(len(pr)), "events": int(pr["event_id"].nunique())}
    if pr.empty or apps.empty:
        return pr.assign(match_id=None).iloc[0:0], counts
    ev = pr.drop_duplicates("event_id")[["event_id", "home", "away", "kickoff"]]
    ids = link(ev, matches_of(apps))
    counts["events_linked"] = len(ids)
    x = pr[pr["event_id"].isin(ids)].assign(match_id=lambda d: d["event_id"].map(ids))
    played = {}
    for mid, g in apps.groupby("match_id"):
        played[mid] = g
    out, unmatched, names_total = [], 0, 0
    for mid, g in x.groupby("match_id"):
        a = played.get(mid)
        if a is None:
            continue
        rosters = {
            t: dict(zip(r["player_id"], r["player"], strict=True)) for t, r in a.groupby("team")
        }
        names = list(dict.fromkeys(g["player"]))
        got, miss = match_in_fixture(names, rosters)
        names_total += len(names)
        unmatched += len(miss)
        by_id = a.set_index("player_id")
        g = g[g["player"].isin(got)].copy()
        g["player_id"] = g["player"].map({n: v[0] for n, v in got.items()})
        g = g[g["player_id"].isin(by_id.index)]  # a player who didn't play: void
        g["started"] = g["player_id"].map(by_id["started"]).astype(bool)
        g["count"] = np.where(
            g["market"] == "shots",
            g["player_id"].map(by_id["shots"]),
            g["player_id"].map(by_id["sot"]),
        ).astype(int)
        out.append(g)
    counts["names"] = names_total
    counts["names_unmatched"] = unmatched
    if not out:
        return x.iloc[0:0], counts
    j = pd.concat(out, ignore_index=True)
    j["need"] = np.ceil(j["point"]).astype(int)
    j["won"] = (j["count"] >= j["need"]).astype(int)
    counts["rows_joined"] = int(len(j))
    counts["matches_joined"] = int(j["match_id"].nunique())
    return j, counts


def best_avg(j: pd.DataFrame) -> pd.DataFrame:
    """Per match, player, market, line and snapshot: best Over (and its book), the books'
    mean Over price, the number of books."""
    keys = ["match_id", "player_id", "market", "point", "kind"]
    idx = j.groupby(keys)["odds"].idxmax()
    b = j.loc[idx, keys + ["odds", "book", "started", "won", "count", "need"]]
    agg = j.groupby(keys)["odds"].agg(avg_odds="mean", books="count").reset_index()
    return b.merge(agg, on=keys).rename(columns={"odds": "best_odds", "book": "best_book"})


def markup(df: pd.DataFrame, odds_col: str, draws: int = DRAWS, seed: int = SEED) -> dict:
    """sum(1/odds) / sum(won) - 1, with a 95% range resampling whole matches."""
    if df.empty:
        return {"lines": 0}
    g = (
        df.assign(imp=1 / df[odds_col])
        .groupby("match_id")
        .agg(imp=("imp", "sum"), won=("won", "sum"))
    )
    imp, won = g["imp"].to_numpy(), g["won"].to_numpy()
    point = float(imp.sum() / won.sum() - 1) if won.sum() else math.inf
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(g), size=(draws, len(g)))
    w = won[pick].sum(1)
    boot = np.where(w > 0, imp[pick].sum(1) / np.maximum(w, 1) - 1, np.inf)
    lo, hi = np.percentile(boot, [2.5, 97.5], method="nearest")
    return {
        "lines": int(len(df)),
        "matches": int(len(g)),
        "implied": round(float(imp.sum()), 2),
        "won": int(won.sum()),
        "markup": round(point, 4),
        "ci95": [round(float(lo), 4), round(float(hi), 4)],
    }


def gate_a(j: pd.DataFrame) -> dict:
    """The pre-registered gate (a) per market plus the descriptive lines beside it."""
    ba = best_avg(j)
    entry = ba[(ba["kind"] == "entry") & ba["started"]]
    out: dict = {"stop_above": STOP_MARKUP, "max_odds": MAX_ODDS, "markets": {}}
    for mk in ("shots", "sot"):
        e = entry[entry["market"] == mk]
        rows = e[e["best_odds"] <= MAX_ODDS]
        m = markup(rows, "best_odds")
        stop = bool(m.get("lines")) and m["ci95"][0] > STOP_MARKUP
        close = ba[(ba["kind"] == "close") & ba["started"] & (ba["market"] == mk)]
        own = j[(j["kind"] == "entry") & j["started"] & (j["market"] == mk)]
        own = own[own["odds"] <= MAX_ODDS]
        out["markets"][mk] = {
            "gate": m,
            "stop": stop,
            "average_price": markup(rows, "avg_odds"),
            "close_best": markup(close[close["best_odds"] <= MAX_ODDS], "best_odds"),
            "by_line": {
                str(p): markup(g, "best_odds", draws=500) for p, g in rows.groupby("point")
            },
            "by_book": {b: markup(g, "odds", draws=500) for b, g in own.groupby("book")},
            "best_book_share": rows["best_book"].value_counts(normalize=True).round(3).to_dict(),
        }
    stops = [out["markets"][m]["stop"] for m in ("shots", "sot")]
    out["verdict"] = (
        "closed: both markets stop at gate (a)"
        if all(stops)
        else "gate (b) for: "
        + ", ".join(m for m, s in zip(("shots", "sot"), stops, strict=True) if not s)
    )
    return out


def summary_lines(res: dict) -> list[str]:
    c = res.get("counts", {})
    lines = [
        f"§17 gate (a): {c.get('events', 0)} events priced, {c.get('events_linked', 0)} linked, "
        f"{c.get('matches_joined', 0)} matches joined; names {c.get('names', 0)}, "
        f"unmatched {c.get('names_unmatched', 0)}"
    ]
    for mk, v in res.get("gate", {}).get("markets", {}).items():
        g, a = v["gate"], v["average_price"]
        if not g.get("lines"):
            lines.append(f"  {mk}: no lines")
            continue
        lines.append(
            f"  {mk}: best-book markup {g['markup']:+.1%} ({g['ci95'][0]:+.1%} to "
            f"{g['ci95'][1]:+.1%}), {g['lines']} lines, {g['matches']} matches -> "
            f"{'STOP' if v['stop'] else 'continues to gate (b)'}; average price "
            f"{a.get('markup', float('nan')):+.1%}"
        )
    if res.get("gate"):
        lines.append(f"  verdict: {res['gate']['verdict']}")
    return lines
