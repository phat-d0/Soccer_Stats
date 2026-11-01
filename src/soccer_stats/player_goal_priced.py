"""Priced goalscorer test (docs/player_props.md §16): prices joined to B's chances.

Shared by the development read (step 3, 2025/26: fixes the edge threshold T by volume only)
and, later, the judged read. Nothing here calls The Odds API: it reads the bodies
`player_goal_backfill` cached.

- `price_rows`: every cached entry and close body → one row per event, snapshot kind, book
  and player (Yes price; "No Scorer" dropped).
- `best`: per event, player and kind, the best Yes price over §16's nine bookmakers (the
  Betfair exchange is logged but never judged) and the book that offered it.
- `link`: each event to one football-data/Understat match (both teams' names through
  `odds_feed._team`, kickoff within 3 hours).
- `join`: B's scored rows (starters, at least 3 earlier appearances: `scored_rows`) matched
  by name within the match's two clubs (`match_in_fixture`, overrides first), with the
  entry best price and book, the close best price, and the entry book's own close.
- `t_volume`: bets per match at each candidate T (volume only, no result) and the T the
  amended §16 rule picks: the largest candidate giving at least `MIN_BETS_PER_MATCH`, else 5%.
- `describe`: development-only descriptive numbers (c, best-to-best CLV and the no-rule
  baseline, same-book CLV, ROI, gap), never a verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.edge.stats import bootstrap_mean
from soccer_stats.odds_feed import _team
from soccer_stats.player_data import match_in_fixture
from soccer_stats.player_goal_backfill import JUDGED_BOOKS, SNAPSHOTS, cache_dir
from soccer_stats.player_goal_odds import prices

T_CANDIDATES = (0.05, 0.08, 0.12, 0.16, 0.20)
T_FALLBACK = 0.05
MIN_BETS_PER_MATCH = 0.3
NOT_A_PLAYER = {"no scorer", "no goalscorer", "no goal scorer"}
LINK_HOURS = 3


def price_rows(league: str, base: Path | None = None) -> pd.DataFrame:
    """One row per event, kind, book and player from the cached bodies of one league."""
    base = Path(base) if base is not None else cache_dir()
    d = base / league
    events: dict[str, dict] = {}
    for path in sorted(d.glob("events_*.json")) if d.exists() else []:
        body = json.loads(path.read_text())
        for e in (body.get("data") if isinstance(body, dict) else body) or []:
            if isinstance(e, dict) and e.get("id"):
                events[e["id"]] = e
    frames = []
    for eid, e in events.items():
        for kind in SNAPSHOTS:
            path = d / f"{eid}_{kind}.json"
            if not path.exists():
                continue
            p = prices(json.loads(path.read_text()))
            if p.empty:
                continue
            p = p.assign(
                event_id=eid,
                kind=kind,
                home=e.get("home_team"),
                away=e.get("away_team"),
                kickoff=pd.Timestamp(e["commence_time"]),
            )
            frames.append(p[~p["player"].str.lower().str.strip().isin(NOT_A_PLAYER)])
    cols = ["event_id", "kind", "home", "away", "kickoff", "book", "player", "yes"]
    if not frames:
        return pd.DataFrame(columns=cols)
    out = pd.concat(frames, ignore_index=True)
    return out[out["yes"].notna() & (out["yes"] > 1)][cols]


def best(pr: pd.DataFrame, books=JUDGED_BOOKS) -> pd.DataFrame:
    """Per event, player and kind: best Yes odds over `books` and the book offering it."""
    x = pr[pr["book"].isin(books)]
    if x.empty:
        return pd.DataFrame(columns=["event_id", "player", "kind", "odds", "book"])
    idx = x.groupby(["event_id", "player", "kind"])["yes"].idxmax()
    return x.loc[idx, ["event_id", "player", "kind", "yes", "book"]].rename(columns={"yes": "odds"})


def link(events: pd.DataFrame, matches: pd.DataFrame) -> dict[str, str]:
    """event_id -> match_id: both teams named the same (home and away) and kickoff within
    LINK_HOURS. `matches` has match_id, kickoff, home_team, away_team."""
    known = set(matches["home_team"]) | set(matches["away_team"])
    out = {}
    for e in events.drop_duplicates("event_id").itertuples(index=False):
        h, a = _team(e.home or "", known), _team(e.away or "", known)
        near = matches[
            (matches["home_team"] == h)
            & (matches["away_team"] == a)
            & (
                (pd.to_datetime(matches["kickoff"], utc=True) - e.kickoff).abs()
                <= pd.Timedelta(hours=LINK_HOURS)
            )  # fmt: skip
        ]
        if len(near) == 1:
            out[e.event_id] = near["match_id"].iloc[0]
    return out


def matches_of(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per match from scored rows that carry team and home."""
    r = rows.dropna(subset=["team"])
    home = (
        r[r["home"].astype(bool)]
        .groupby("match_id")
        .agg(home_team=("team", "first"), kickoff=("kickoff", "first"))
    )
    away = r[~r["home"].astype(bool)].groupby("match_id").agg(away_team=("team", "first"))
    return home.join(away, how="inner").reset_index()


def join(rows: pd.DataFrame, pr: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Starters with B's chance and the prices (entry best + book, close best, same-book
    close). Returns (judged rows, counts)."""
    starters = rows[rows["started"].astype(bool)].copy()
    ev = pr[["event_id", "home", "away", "kickoff"]].drop_duplicates("event_id")
    ev_to_match = link(ev, matches_of(rows))
    entry, close = best(pr[pr["kind"] == "entry"]), best(pr[pr["kind"] == "close"])
    close_by_book = pr[pr["kind"] == "close"].set_index(["event_id", "player", "book"])["yes"]
    counts = {"events": len(ev), "events_linked": len(ev_to_match), "names": 0, "names_matched": 0}
    out = []
    for eid, mid in ev_to_match.items():
        game = rows[rows["match_id"] == mid]
        rosters = {
            t: dict(
                g.drop_duplicates("player_id")[["player_id", "player"]].itertuples(
                    index=False, name=None
                )
            )
            for t, g in game.groupby("team")
        }
        names = pr.loc[(pr["event_id"] == eid) & (pr["kind"] == "entry"), "player"].unique()
        found, _ = match_in_fixture(names, rosters)
        counts["names"] += len(names)
        counts["names_matched"] += len(found)
        pid_to_name = {pid: n for n, (pid, _t) in found.items()}
        e_best = entry[entry["event_id"] == eid].set_index("player")
        c_best = close[close["event_id"] == eid].set_index("player")
        for r in starters[starters["match_id"] == mid].itertuples(index=False):
            name = pid_to_name.get(r.player_id)
            if name is None or name not in e_best.index:
                continue
            book = e_best.at[name, "book"]
            out.append(
                {
                    **r._asdict(),
                    "event_id": eid,
                    "entry_odds": float(e_best.at[name, "odds"]),
                    "entry_book": book,
                    "close_best": float(c_best.at[name, "odds"])
                    if name in c_best.index
                    else np.nan,
                    "close_same": float(close_by_book.get((eid, name, book), np.nan)),
                }
            )
    j = pd.DataFrame(out)
    counts["judged_rows"] = len(j)
    counts["judged_matches"] = int(j["match_id"].nunique()) if len(j) else 0
    return j, counts


def t_volume(j: pd.DataFrame, candidates=T_CANDIDATES) -> dict:
    """Bets per judged match at each candidate T (volume only) and the T the rule picks."""
    matches = int(j["match_id"].nunique()) if len(j) else 0
    edge = j["p_B"] * j["entry_odds"] - 1 if len(j) else pd.Series(dtype=float)
    rows = []
    for t in candidates:
        n = int((edge >= t).sum())
        rows.append({"t": t, "bets": n, "per_match": round(n / matches, 3) if matches else 0.0})
    ok = [r["t"] for r in rows if r["per_match"] >= MIN_BETS_PER_MATCH]
    rule = f"largest T with >= {MIN_BETS_PER_MATCH} bets a match, else {T_FALLBACK}"
    return {"matches": matches, "by_t": rows, "T": max(ok) if ok else T_FALLBACK, "rule": rule}


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def blend_c(j: pd.DataFrame, draws: int = 2000, seed: int = 0) -> dict:
    """c in logit P(scored) = a + b·logit(1/entry odds) + c·logit(p_B), match bootstrap."""
    if len(j) < 50:
        return {"c": None, "ci95": None, "rows": len(j)}
    x = np.column_stack([np.ones(len(j)), _logit(1 / j["entry_odds"]), _logit(j["p_B"])])
    y = j["scored"].to_numpy(float)
    groups = j["match_id"].to_numpy()

    def fit(xx, yy):
        w = np.zeros(3)
        for _ in range(25):
            p = 1 / (1 + np.exp(-xx @ w))
            h = xx.T @ (xx * (p * (1 - p))[:, None]) + 1e-8 * np.eye(3)
            step = np.linalg.solve(h, xx.T @ (yy - p))
            w += step
            if np.abs(step).max() < 1e-8:
                break
        return w

    c = float(fit(x, y)[2])
    rng = np.random.default_rng(seed)
    uniq, inv = np.unique(groups, return_inverse=True)
    by = [np.flatnonzero(inv == k) for k in range(len(uniq))]
    cs = []
    for _ in range(draws):
        pick = np.concatenate([by[k] for k in rng.integers(0, len(by), len(by))])
        cs.append(fit(x[pick], y[pick])[2])
    lo, hi = np.percentile(cs, [2.5, 97.5])
    return {"c": round(c, 4), "ci95": [round(float(lo), 4), round(float(hi), 4)], "rows": len(j)}


def _clv(j: pd.DataFrame, col: str) -> dict:
    v = (j["entry_odds"] / j[col] - 1).replace([np.inf, -np.inf], np.nan)
    ok = v.notna()
    if ok.sum() < 2:
        return {"bets": int(ok.sum()), "mean": None, "ci95": None, "missing": int((~ok).sum())}
    vals = v[ok].to_numpy()
    rng = bootstrap_mean(vals, j.loc[ok, "match_id"].to_numpy())
    return {"bets": int(ok.sum()), "mean": round(float(vals.mean()), 4),
            "ci95": [round(rng[0], 4), round(rng[1], 4)] if rng else None,
            "missing": int((~ok).sum())}  # fmt: skip


def describe(j: pd.DataFrame, t: float, col: str = "p_B") -> dict:
    """Development-only descriptive numbers at threshold `t` for chance column `col`."""
    if not len(j):
        return {"rows": 0}
    bets = j[j[col] * j["entry_odds"] - 1 >= t]
    profit = np.where(bets["scored"] == 1, bets["entry_odds"] - 1, -1.0)
    rng = bootstrap_mean(profit, bets["match_id"].to_numpy()) if len(bets) > 1 else None
    roi = (float(profit.mean()), *rng) if rng else None
    return {
        "rows": len(j),
        "matches": int(j["match_id"].nunique()),
        "gap": round(float((1 / j["entry_odds"]).mean() - j["scored"].mean()), 4),
        "c": blend_c(j.assign(p_B=j[col])),
        "baseline_clv": _clv(j, "close_best"),
        "bets": len(bets),
        "clv_best": _clv(bets, "close_best"),
        "clv_same_book": _clv(bets, "close_same"),
        "hit_rate": round(float(bets["scored"].mean()), 4) if len(bets) else None,
        "roi": {"mean": round(roi[0], 4), "ci95": [round(roi[1], 4), round(roi[2], 4)]}
        if roi
        else None,
    }
