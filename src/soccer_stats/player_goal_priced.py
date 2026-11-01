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
- `band_edges` / `band_of`: five entry-price bands; the judged edges (`PRICE_BAND_EDGES`)
  are the 2025/26 development rule bets' odds quintiles at T, fixed 1 Nov 2026.
- `matched_excess`: the judged CLV measure (departure of 1 Nov 2026): the rule's mean
  best-to-best CLV minus the no-rule baseline's, the baseline reweighted to the rule's mix
  over the price bands; one shared match bootstrap (2,000 draws, seed 0).
- `verdict`: §16's pass rule on c's range and the matched excess.
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
T_JUDGED = 0.20  # §16: fixed by the volume rule on 2025/26 development (run 41346853368)
BAND_COUNT = 5
# §16: inner edges of the five entry-price bands (decimal odds; lo < odds <= hi), the
# 2025/26 development rule bets' odds quintiles at T_JUDGED, fixed 1 Nov 2026.
PRICE_BAND_EDGES: tuple[float, ...] | None = (6.5, 11.5, 17.0, 23.0)  # run 41350946771
MIN_JUDGED_BETS = 300
# B's chance is rounded to this many decimals before the threshold test, so a chance that
# differs only in its last digits between runs can't flip a bet (lead, 1 Nov 2026).
P_DECIMALS = 6
BOOT_DRAWS, BOOT_SEED = 2000, 0


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
    edge = edge_of(j) if len(j) else pd.Series(dtype=float)
    rows = []
    for t in candidates:
        n = int((edge >= t).sum())
        rows.append({"t": t, "bets": n, "per_match": round(n / matches, 3) if matches else 0.0})
    ok = [r["t"] for r in rows if r["per_match"] >= MIN_BETS_PER_MATCH]
    rule = f"largest T with >= {MIN_BETS_PER_MATCH} bets a match, else {T_FALLBACK}"
    return {"matches": matches, "by_t": rows, "T": max(ok) if ok else T_FALLBACK, "rule": rule}


def edge_of(j: pd.DataFrame, col: str = "p_B") -> pd.Series:
    """The claimed edge: chance (rounded to P_DECIMALS) × best entry odds − 1."""
    return j[col].round(P_DECIMALS) * j["entry_odds"] - 1


def bets_at(j: pd.DataFrame, t: float, col: str = "p_B") -> pd.Series:
    """True for the rule's bets: edge_of ≥ t."""
    return edge_of(j, col) >= t


def band_edges(j: pd.DataFrame, t: float, col: str = "p_B", k: int = BAND_COUNT) -> list[float]:
    """The k−1 inner edges splitting the rule's bets at `t` into k equal groups by entry odds."""
    odds = j.loc[bets_at(j, t, col), "entry_odds"].to_numpy(float)
    if len(odds) < k:
        return []
    qs = np.quantile(odds, [i / k for i in range(1, k)])
    return [round(float(q), 2) for q in qs]


def band_of(odds, edges) -> np.ndarray:
    """Band index 0..len(edges): band i holds edges[i−1] < odds <= edges[i]."""
    return np.searchsorted(np.asarray(edges, float), np.asarray(odds, float), side="left")


def matched_excess(
    j: pd.DataFrame,
    t: float,
    edges,
    col: str = "p_B",
    draws: int = BOOT_DRAWS,
    seed: int = BOOT_SEED,
) -> dict:
    """§16's judged CLV measure: the rule's mean best-to-best CLV minus the no-rule baseline's,
    with the baseline reweighted to the rule's share of bets in each entry-price band. Both on
    the same rows (every starter with an entry and a best close); the 95% range comes from one
    match bootstrap shared by the rule and the baseline. Also the unweighted baseline and
    excess (descriptive)."""
    v = (j["entry_odds"] / j["close_best"] - 1).replace([np.inf, -np.inf], np.nan)
    ok = v.notna().to_numpy()
    x = j.loc[ok]
    v = v[ok].to_numpy(float)
    rule = bets_at(x, t, col).to_numpy()
    nb = len(edges) + 1
    band = band_of(x["entry_odds"], edges)
    uniq, mi = np.unique(x["match_id"].astype(str).to_numpy(), return_inverse=True)
    m = len(uniq)
    out = {"t": t, "edges": list(edges), "bets": int(rule.sum()), "rows": int(len(x)),
           "matches": int(m), "draws": draws, "seed": seed}  # fmt: skip
    if not rule.any():
        return {**out, "rule_clv": None, "baseline_matched": None, "excess": None, "ci95": None}

    # Per match and band: sums and counts for the rule and the baseline.
    def per_match(mask):
        s = np.zeros((m, nb))
        c = np.zeros((m, nb))
        np.add.at(s, (mi[mask], band[mask]), v[mask])
        np.add.at(c, (mi[mask], band[mask]), 1)
        return s, c

    rs, rc = per_match(rule)
    bs, bc = per_match(np.ones(len(x), bool))

    def stats(w):
        r_s, r_c = w @ rs, w @ rc
        b_s, b_c = w @ bs, w @ bc
        n_rule = r_c.sum()
        if n_rule == 0:
            return np.nan, np.nan, np.nan, np.nan
        mix = r_c / n_rule
        with np.errstate(invalid="ignore", divide="ignore"):
            b_mean = np.where(b_c > 0, b_s / b_c, 0.0)
        rule_mean = r_s.sum() / n_rule
        matched = float((mix * b_mean).sum())
        plain = b_s.sum() / b_c.sum()
        return rule_mean, matched, rule_mean - matched, rule_mean - plain

    rule_mean, matched, excess, plain_excess = stats(np.ones(m))
    rng = np.random.default_rng(seed)
    boot = []
    for _ in range(draws):
        w = np.bincount(rng.integers(0, m, m), minlength=m).astype(float)
        boot.append(stats(w))
    boot = np.array(boot)

    def ci(col_i):
        b = boot[:, col_i]
        b = b[np.isfinite(b)]
        lo, hi = np.percentile(b, [2.5, 97.5])
        return [round(float(lo), 4), round(float(hi), 4)]

    r_c = rc.sum(axis=0)
    b_c, b_s = bc.sum(axis=0), bs.sum(axis=0)
    by_band = []
    lows = [None, *edges]
    highs = [*edges, None]
    for i in range(nb):
        base = round(float(b_s[i] / b_c[i]), 4) if b_c[i] else None
        by_band.append(
            {
                "lo": lows[i],
                "hi": highs[i],
                "rule_bets": int(r_c[i]),
                "rule_share": round(float(r_c[i] / r_c.sum()), 4),
                "baseline_rows": int(b_c[i]),
                "baseline_clv": base,
            }
        )
    return {
        **out,
        "rule_clv": round(float(rule_mean), 4),
        "rule_clv_ci95": ci(0),
        "baseline_matched": round(float(matched), 4),
        "excess": round(float(excess), 4),
        "ci95": ci(2),
        "baseline_plain": round(float(bs.sum() / bc.sum()), 4),
        "excess_plain": round(float(plain_excess), 4),
        "excess_plain_ci95": ci(3),
        "by_band": by_band,
    }


def verdict(c: dict, excess: dict, min_bets: int = MIN_JUDGED_BETS) -> str:
    """§16 (as amended 1 Nov 2026): pass when c's range and the matched CLV excess's range both
    lie above 0 with at least `min_bets` bets; inconclusive when c's range lies above 0 but
    there are fewer bets; fail otherwise."""
    c_ok = bool(c.get("ci95")) and c["ci95"][0] > 0
    if not c_ok:
        return "fail"
    if (excess.get("bets") or 0) < min_bets:
        return "inconclusive"
    return "pass" if excess.get("ci95") and excess["ci95"][0] > 0 else "fail"


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


def describe(j: pd.DataFrame, t: float, col: str = "p_B", edges=None) -> dict:
    """Development-only descriptive numbers at threshold `t` for chance column `col`; with
    `edges`, also the price-matched CLV excess (§16's judged measure, here descriptive)."""
    if not len(j):
        return {"rows": 0}
    bets = j[bets_at(j, t, col)]
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
        **({"clv_matched": matched_excess(j, t, edges, col)} if edges else {}),
    }
