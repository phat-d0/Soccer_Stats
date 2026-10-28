"""Strategy search with a locked holdout (owner via the lead, 28 Oct; design in docs/lab.md,
"Strategy search with a locked holdout", committed before this code).

Every rule tried is an *arm*, logged and counted, losers included. Free: football-data and
Understat only, no key.

* Data: the eight live leagues, football-data seasons before `HOLDOUT_START` only (the
  holdout is `lab.harness.Holdout` and is checked on every row loaded). Scored seasons
  follow `lean_backtest`'s coverage rule (Pinnacle early and close >= 90%), so 2019/20 to
  2023/24 in practice.
* Side table: one row per match, market and side, decided at Pinnacle's early price:
  - 1X2 and draw no bet from `lean_backtest.walk_forward` (model chance, blend, sigma,
    refitted every 28 days on earlier matches) and `dnb_backtest.candidates`;
  - Asian handicap and over/under 2.5 from `match_markets.candidates` on the model's
    walk-forward score matrices, at Pinnacle's own prices (`clean_prices`).
  CLV = entry odds x Pinnacle's margin-free close - 1 (an AH line that moved has none).
* Bases per market: the raw model's best side at an edge >= t (`EDGES`); Blend Lean's best
  z >= k (`ZS`; 1X2 and DNB, the markets with a blend); every side (the market alone).
* Filters (`FILTERS`), none, one or two of different kinds: league, price band, edge or z
  quintile, Lean's driver, side, favourite/underdog, weekend/midweek, lone or shared
  kickoff, the model's strength gap in quintiles.
* Line movement (`MOVES`): bets decided at the close on the side whose margin-free chance
  rose (follow) or fell (fade) by at least d from the early price, at Pinnacle's closing
  price, with none or one other filter. Judged on ROI only (CLV is 0 by construction);
  never finalists.
* Scoring: bets, ROI per $1 and CLV, each with a match-resampled 95% range (`N_BOOT`
  draws, seed `SEED`, the same draws for every arm).
* The search's correction (White's reality check): every arm's per-match values recentred
  to its own mean, the same draws, the maximum over arms per draw. An arm's adjusted p is
  the share of draws whose maximum reaches its real mean. The universe is the arms that
  could be finalists (>= `MIN_FINAL` bets); the same over every arm is printed beside it.
* Finalists: CLV range above 0, >= `MIN_FINAL` bets, adjusted p < `P_MAX`; at most
  `MAX_FINALISTS`, by the CLV range's lower end. With none, the holdout stays locked.
* Sizing (flat, quarter and half Kelly on the blend's chance capped at 5%, a stop after a
  20-unit drawdown in a season) on the finalists and the top arms, beside flat; exits come
  from the cash-out study's spent comeback sample, exploratory.
"""

from __future__ import annotations

import argparse
import functools
import itertools
import json
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse

from soccer_stats import dnb_backtest as dnb
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import match_markets as mm
from soccer_stats import trades as tr
from soccer_stats.bands import ODDS_BANDS
from soccer_stats.lab.harness import Holdout
from soccer_stats.odds import devig_shin

HOLDOUT_START = "2024-07-01"
LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
FROM_SEASON = 2019  # the blend's rows and the first scored season (lean_backtest's)
LAST_YEAR = 2023  # 2023/24, the last season before the holdout
BURN_IN = 2
MARKETS = ("1x2", "dnb", "ah", "ou")
SIDES = {
    "1x2": mc.H2H,
    "dnb": ("home", "away"),
    "ah": mc.AH,
    "ou": mc.TOTALS,
}
EDGES = (0.0, 0.05, 0.12, 0.20)
ZS = (0.5, 1.0, 2.0)
MOVES = (0.01, 0.02, 0.04)
QUINTILES = 5
N_BOOT = 2000
SEED = 0
CHUNK = 500  # arms per bootstrap chunk
MIN_FLAG = 100
MIN_FINAL = 300
P_MAX = 0.05
MAX_FINALISTS = 3
TOP = 30
KELLY_CAP = 0.05
STOP_UNITS = 20.0
FILTERS = ("league", "price", "band", "driver", "side", "fav", "day", "slot", "gap")
MOVE_FILTERS = ("league", "price", "side", "fav", "day", "slot", "gap")


# ---------- the side table ----------


def _won(y: np.ndarray, j: int) -> np.ndarray:
    return (y == j).astype(float)


def sides_1x2(scored: pd.DataFrame) -> pd.DataFrame:
    """1X2 sides from lean_backtest.walk_forward output: early quoted odds, Pinnacle's
    close, the model, the blend and Lean's z (and z with the model's term removed)."""
    rows = []
    close = scored[list(lb.PIN_CLOSE)].to_numpy(dtype=float)
    ok = np.isfinite(close).all(1) & (close > 1).all(1)
    fair_close = np.full(close.shape, np.nan)
    for i in np.flatnonzero(ok):
        fair_close[i] = devig_shin(close[i])
    y = scored["y"].to_numpy()
    sd = scored["sigma"].to_numpy(dtype=float)
    for j, m in enumerate(mc.H2H):
        odds = scored[f"odds_{m}"].to_numpy(dtype=float)
        codds = scored[f"close_{m}"].to_numpy(dtype=float)
        won = _won(y, j)
        pb = scored[f"pb_{m}"].to_numpy(dtype=float)
        pm = scored[f"pm_{m}"].to_numpy(dtype=float)
        rows.append(
            pd.DataFrame(
                {
                    **_ids(scored),
                    "market": "1x2",
                    "side": m,
                    "odds": odds,
                    "close_odds": codds,
                    "fair_early": scored[f"mkt_{m}"].to_numpy(dtype=float),
                    "fair_close": fair_close[:, j],
                    "p_model": scored[f"p_{m}"].to_numpy(dtype=float),
                    "p_blend": pb,
                    "z": (pb - 1 / odds) / sd,
                    "z_market": (pm - 1 / odds) / sd,
                    "profit": np.where(won > 0, odds - 1, -1.0),
                    "profit_close": np.where(won > 0, codds - 1, -1.0),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def sides_dnb(scored: pd.DataFrame) -> pd.DataFrame:
    """Draw no bet sides (dnb_backtest.candidates on the same scored rows): a draw refunds."""
    cand = dnb.candidates(scored)
    rows = []
    hg, ag = cand["home_goals"].to_numpy(), cand["away_goals"].to_numpy()
    draw = hg == ag
    for s in SIDES["dnb"]:
        won = (hg > ag) if s == "home" else (ag > hg)
        odds = cand[f"dnb_{s}"].to_numpy(dtype=float)
        codds = np.array(
            [
                tr.dnb_odds(a, b) or np.nan
                for a, b in zip(cand[f"close_{s}"], cand["close_draw"], strict=True)
            ],
            dtype=float,
        )
        qb = cand[f"qb_{s}"].to_numpy(dtype=float)
        rows.append(
            pd.DataFrame(
                {
                    **_ids(cand),
                    "market": "dnb",
                    "side": s,
                    "odds": odds,
                    "close_odds": codds,
                    "fair_early": cand[f"fair_{s}"].to_numpy(dtype=float),
                    "fair_close": cand[f"close_fair_{s}"].to_numpy(dtype=float),
                    "p_model": cand[f"q_{s}"].to_numpy(dtype=float),
                    "p_blend": qb,
                    "z": (qb - 1 / odds) / cand["sigma_dnb"].to_numpy(dtype=float),
                    "z_market": np.nan,
                    "profit": np.where(draw, 0.0, np.where(won, odds - 1, -1.0)),
                    "profit_close": np.where(draw, 0.0, np.where(won, codds - 1, -1.0)),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def sides_market(joined: pd.DataFrame, group: str, market: str) -> pd.DataFrame:
    """Asian handicap or over/under 2.5 sides at Pinnacle's early price (match_markets),
    with the close at the same line for movement arms."""
    early = mm.candidates(joined, group, "early")
    close = mm.candidates(joined, group, "close")
    keys = ["date", "home", "away"]
    names = mm.GROUPS[group]
    keep = [*keys, *[f"odds_pinnacle_close_{m}" for m in names]]
    keep += [f"win_{m}" for m in names] + [f"loss_{m}" for m in names]
    keep += ["line"] if "line" in close else []
    c = close[keep].rename(
        columns={
            **{f"win_{m}": f"cwin_{m}" for m in names},
            **{f"loss_{m}": f"closs_{m}" for m in names},
            "line": "close_line",
        }
    )
    df = early.merge(c.drop_duplicates(keys), on=keys, how="left")
    same = (
        (df["line"].to_numpy(dtype=float) == df["close_line"].to_numpy(dtype=float))
        if "line" in df
        else np.ones(len(df), dtype=bool)
    )
    rows = []
    for m, side in zip(names, SIDES[market], strict=True):
        odds = df[f"odds_pinnacle_early_{m}"].to_numpy(dtype=float)
        codds = df[f"odds_pinnacle_close_{m}"].to_numpy(dtype=float)
        win, loss = df[f"win_{m}"].to_numpy(dtype=float), df[f"loss_{m}"].to_numpy(dtype=float)
        cwin, closs = df[f"cwin_{m}"].to_numpy(dtype=float), df[f"closs_{m}"].to_numpy(dtype=float)
        rows.append(
            pd.DataFrame(
                {
                    **_ids(df),
                    "market": market,
                    "side": side,
                    "odds": odds,
                    "close_odds": np.where(same, codds, np.nan),
                    "fair_early": df[f"mkt_{m}"].to_numpy(dtype=float),
                    "fair_close": df[f"fair_close_{m}"].to_numpy(dtype=float),
                    "p_model": df[f"p_{m}"].to_numpy(dtype=float),
                    "p_blend": np.nan,
                    "z": np.nan,
                    "z_market": np.nan,
                    "profit": win * (odds - 1) - loss,
                    "profit_close": cwin * (codds - 1) - closs,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def _ids(df: pd.DataFrame) -> dict:
    return {
        "date": pd.to_datetime(df["date"]).dt.normalize().to_numpy(),
        "season": df["season"].astype(str).to_numpy(),
        "home": df["home"].to_numpy(),
        "away": df["away"].to_numpy(),
    }


def league_sides(
    league: str,
    preds: pd.DataFrame,
    pins: pd.DataFrame,
    prices: pd.DataFrame,
    times: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    """One league's side table over its scored seasons, and a short report."""
    base = preds.drop(columns=["matrix"], errors="ignore")
    merged = lb.with_pinnacle(base, pins)
    train = mc.training_rows(merged, "h2h") if not merged.empty else merged
    cov = lb.coverage(pins)
    included = {c["season"] for c in cov if c["included"]}
    scored, fits = lb.walk_forward(train, lb.target_rows(merged))
    scored = scored[scored["season"].astype(str).isin(included)].reset_index(drop=True)
    sig = dnb.sigma_dnb(train, fits)
    starts = pd.to_datetime(scored["fit_from"])
    scored["sigma_dnb"] = [
        sig.get(d.date().isoformat(), np.nan) if pd.notna(d) else np.nan for d in starts
    ]
    parts = [sides_1x2(scored), sides_dnb(scored)]
    if "matrix" in preds and len(prices):
        joined, _ = mm.clean_prices(mm.join(preds, prices))
        joined = joined[joined["season"].astype(str).isin(included)].reset_index(drop=True)
        if len(joined):
            parts += [sides_market(joined, "ah", "ah"), sides_market(joined, "totals", "ou")]
    out = pd.concat(parts, ignore_index=True)
    out = out[np.isfinite(out["odds"]) & (out["odds"] > 1)].reset_index(drop=True)
    out.insert(0, "league", league)
    gap = scored.assign(date=pd.to_datetime(scored["date"]).dt.normalize())
    gap["gap"] = (gap["p_home"] - gap["p_away"]).abs()
    out = out.merge(gap[["date", "home", "away", "gap"]].drop_duplicates(["date", "home"]),
                    on=["date", "home", "away"], how="left")  # fmt: skip
    if times is not None and len(times):
        t = times.assign(date=pd.to_datetime(times["date"]).dt.normalize())
        out = out.merge(t[["date", "home", "away", "time"]].drop_duplicates(["date", "home"]),
                        on=["date", "home", "away"], how="left")  # fmt: skip
    else:
        out["time"] = None
    rep = {
        "seasons": sorted(included),
        "matches": int(scored.shape[0]),
        "fits": len(fits),
        "sides": {m: int((out["market"] == m).sum()) for m in MARKETS},
    }
    return out, rep


def add_features(sides: pd.DataFrame) -> pd.DataFrame:
    """Match keys, edges, favourites, day, kickoff slot and the bases' picks."""
    df = sides.copy()
    df["key"] = df["league"] + "|" + df["date"].astype(str) + "|" + df["home"].astype(str)
    df["edge"] = df["p_model"] * df["odds"] - 1
    grp = ["key", "market"]
    best = df.groupby(grp)["fair_early"].transform("max")
    df["fav"] = np.where(df["fair_early"] >= best, "fav", "dog")
    df["day"] = np.where(pd.to_datetime(df["date"]).dt.dayofweek >= 5, "weekend", "midweek")
    # A lone kickoff vs one the league shares with other matches (football-data's Time).
    first = df.drop_duplicates("key")[["key", "league", "date", "time"]].dropna(subset=["time"])
    counts = first.groupby(["league", "date", "time"])["key"].transform("count")
    slot = dict(zip(first["key"], np.where(counts > 1, "shared", "lone"), strict=True))
    df["slot"] = df["key"].map(slot)
    df["move"] = df["fair_close"] - df["fair_early"]
    df["clv"] = df["odds"] * df["fair_close"] - 1
    for col, name in (("edge", "best_edge"), ("z", "best_z")):
        ok = df[col].notna()
        idx = df[ok].groupby(grp)[col].idxmax()
        df[name] = False
        df.loc[idx.to_numpy(), name] = True
    return df


def quintile_cuts(values: pd.Series) -> list[float]:
    v = pd.to_numeric(values, errors="coerce").dropna().to_numpy()
    if not len(v):
        return []
    return [float(x) for x in np.quantile(v, np.linspace(0, 1, QUINTILES + 1)[1:-1])]


def _band(values: np.ndarray, cuts: list[float]) -> np.ndarray:
    out = np.full(len(values), -1)
    ok = np.isfinite(values)
    out[ok] = np.searchsorted(cuts, values[ok], side="right")
    return out


# ---------- arms ----------


def filter_levels(df: pd.DataFrame, at_close: bool = False) -> dict:
    """{filter: [(level, mask)]}, masks over the side table. Band levels are per market
    (edge for edge bases, z for Lean bases); cuts are the search window's quintiles."""
    out: dict[str, list] = {}
    out["league"] = [(lg, (df["league"] == lg).to_numpy()) for lg in LEAGUES]
    odds = (df["close_odds"] if at_close else df["odds"]).to_numpy(dtype=float)
    out["price"] = [
        (
            f"<={hi:g}" if lo is None else f">{lo:g}" if hi is None else f"{lo:g}-{hi:g}",
            (odds > (lo or 1.0)) & (odds <= (hi or np.inf)),
        )
        for lo, hi in ODDS_BANDS
    ]
    out["side"] = [
        (s, (df["side"] == s).to_numpy()) for s in sorted({s for v in SIDES.values() for s in v})
    ]
    out["fav"] = [(v, (df["fav"] == v).to_numpy()) for v in ("fav", "dog")]
    out["day"] = [(v, (df["day"] == v).to_numpy()) for v in ("weekend", "midweek")]
    out["slot"] = [(v, (df["slot"] == v).to_numpy()) for v in ("lone", "shared")]
    gcuts = quintile_cuts(df.drop_duplicates("key")["gap"])
    g = _band(df["gap"].to_numpy(dtype=float), gcuts)
    out["gap"] = [(f"q{i + 1}", g == i) for i in range(QUINTILES)]
    if not at_close:
        out["driver"] = [
            ("market", (df["z_market"] >= tr.LEAN_Z).to_numpy()),
            ("model", (df["z_market"] < tr.LEAN_Z).to_numpy()),
        ]
        band = np.full(len(df), -1)
        cuts = {}
        for m in MARKETS:
            for col in ("edge", "z"):
                rows = (df["market"] == m).to_numpy()
                c = quintile_cuts(df.loc[rows, col])
                cuts[f"{m}:{col}"] = c
                if c:
                    b = _band(df[col].to_numpy(dtype=float), c)
                    band = np.where(rows & (b >= 0), b + (100 if col == "z" else 0), band)
        out["band"] = [(f"edge q{i + 1}", band == i) for i in range(QUINTILES)]
        out["band"] += [(f"z q{i + 1}", band == 100 + i) for i in range(QUINTILES)]
        out["_cuts"] = cuts
    return out


def bases(df: pd.DataFrame) -> list[tuple[str, str, np.ndarray]]:
    """(market, base, mask): the raw model's best edge >= t, Lean's best z >= k (1X2, DNB)
    and every side."""
    out = []
    for m in MARKETS:
        mk = (df["market"] == m).to_numpy()
        for t in EDGES:
            out.append((m, f"edge>={t:.0%}", mk & df["best_edge"].to_numpy()
                        & (df["edge"].to_numpy(dtype=float) >= t)))  # fmt: skip
        if m in ("1x2", "dnb"):
            for k in ZS:
                out.append((m, f"lean z>={k:g}", mk & df["best_z"].to_numpy()
                            & (df["z"].to_numpy(dtype=float) >= k)))  # fmt: skip
        out.append((m, "every side", mk))
    return out


def _applies(base: str, market: str, name: str, level: str) -> bool:
    if name == "driver":
        return base.startswith("lean") and market == "1x2"
    if name == "band":
        kind = "z" if base.startswith("lean") else "edge" if base.startswith("edge") else None
        return kind is not None and level.startswith(kind)
    if name == "side":
        return level in SIDES[market]
    return True


def arms(df: pd.DataFrame, levels: dict) -> list[dict]:
    """Every early-price arm: a base with none, one or two filters of different kinds."""
    out = []
    for market, base, mask in bases(df):
        opts = [
            (name, level, lm)
            for name in FILTERS
            for level, lm in levels.get(name, [])
            if _applies(base, market, name, level)
        ]
        combos = [()] + [(o,) for o in opts]
        combos += [(a, b) for a, b in itertools.combinations(opts, 2) if a[0] != b[0]]
        for combo in combos:
            m = mask
            for _, _, lm in combo:
                m = m & lm
            out.append(
                {
                    "family": "early",
                    "market": market,
                    "base": base,
                    "filters": [f"{n}={lv}" for n, lv, _ in combo],
                    "rows": np.flatnonzero(m),
                }
            )
    return out


def move_arms(df: pd.DataFrame, levels: dict) -> list[dict]:
    """Line-movement arms, decided and priced at the close: follow (the side's fair chance
    rose by >= d) or fade (fell by >= d), with none or one other filter."""
    out = []
    mv = df["move"].to_numpy(dtype=float)
    priced = np.isfinite(df["close_odds"].to_numpy(dtype=float)) & np.isfinite(mv)
    for market in MARKETS:
        mk = (df["market"] == market).to_numpy() & priced
        for d in MOVES:
            for way, m0 in (("follow", mv >= d), ("fade", mv <= -d)):
                base = mk & m0
                opts = [
                    (name, level, lm)
                    for name in MOVE_FILTERS
                    for level, lm in levels.get(name, [])
                    if _applies("move", market, name, level)
                ]
                for combo in [()] + [(o,) for o in opts]:
                    m = base
                    for _, _, lm in combo:
                        m = m & lm
                    out.append(
                        {
                            "family": "move",
                            "market": market,
                            "base": f"{way} move>={d:.0%} at the close",
                            "filters": [f"{n}={lv}" for n, lv, _ in combo],
                            "rows": np.flatnonzero(m),
                        }
                    )
    return out


# ---------- scoring with one shared bootstrap ----------


def weights(n_matches: int, n_boot: int = N_BOOT, seed: int = SEED) -> np.ndarray:
    """Match-resampling counts, one column per draw (matches x draws, float32)."""
    rng = np.random.default_rng(seed)
    out = np.empty((n_matches, n_boot), dtype=np.float32)
    p = np.full(n_matches, 1 / n_matches)
    for lo in range(0, n_boot, 250):
        hi = min(lo + 250, n_boot)
        out[:, lo:hi] = rng.multinomial(n_matches, p, size=hi - lo).T
    return out


def _matrix(arm_rows: list[np.ndarray], match: np.ndarray, vals: np.ndarray, n_matches: int):
    """Per arm and match: the sum of `vals` over the arm's bets with a finite value, and
    their count (CSR, arms x matches)."""
    r, c, v = [], [], []
    for i, rows in enumerate(arm_rows):
        x = vals[rows]
        ok = np.isfinite(x)
        r.append(np.full(int(ok.sum()), i))
        c.append(match[rows][ok])
        v.append(x[ok])
    r = np.concatenate(r) if r else np.zeros(0, int)
    c = np.concatenate(c) if c else np.zeros(0, int)
    v = np.concatenate(v) if v else np.zeros(0)
    shape = (len(arm_rows), n_matches)
    s = sparse.csr_matrix((v.astype(np.float32), (r, c)), shape=shape)
    n = sparse.csr_matrix((np.ones(len(v), np.float32), (r, c)), shape=shape)
    return s, n


def score(
    arm_list: list[dict],
    df: pd.DataFrame,
    w: np.ndarray,
    measures: dict[str, str],
    universe_min: int = MIN_FINAL,
) -> dict:
    """Adds n, mean and 95% range per measure to every arm, and returns the recentred
    maxima per draw ({measure: {"final": [...], "all": [...]}}) for the reality check."""
    match = pd.factorize(df["key"])[0]
    n_boot = w.shape[1]
    maxima = {
        k: {
            "final": np.full(n_boot, -np.inf),
            "all": np.full(n_boot, -np.inf),
            "pos": np.zeros(n_boot),  # recentred arms above 0 per draw (the null's share)
            "arms": 0,
        }
        for k in measures
    }
    for lo in range(0, len(arm_list), CHUNK):
        chunk = arm_list[lo : lo + CHUNK]
        rows = [a["rows"] for a in chunk]
        for key, col in measures.items():
            vals = df[col].to_numpy(dtype=float)
            s, n = _matrix(rows, match, vals, w.shape[0])
            tot, cnt = np.asarray(s.sum(1)).ravel(), np.asarray(n.sum(1)).ravel()
            with np.errstate(invalid="ignore", divide="ignore"):
                mean = tot / cnt
                boot = np.asarray(s @ w) / np.asarray(n @ w)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # arms with no bets
                lo_q = np.nanpercentile(boot, 2.5, axis=1)
                hi_q = np.nanpercentile(boot, 97.5, axis=1)
            centred = boot - mean[:, None]
            for i, a in enumerate(chunk):
                a[f"n_{key}"] = int(cnt[i])
                ok = cnt[i] > 0
                a[key] = float(mean[i]) if ok else None
                a[f"{key}_ci95"] = [float(lo_q[i]), float(hi_q[i])] if cnt[i] > 1 else None
            for name, keep in (("final", cnt >= universe_min), ("all", cnt > 1)):
                if keep.any():
                    part = np.nanmax(np.where(np.isfinite(centred[keep]), centred[keep], -np.inf),
                                     axis=0)  # fmt: skip
                    maxima[key][name] = np.maximum(maxima[key][name], part)
            final = cnt >= universe_min
            maxima[key]["pos"] += (centred[final] > 0).sum(0)
            maxima[key]["arms"] += int(final.sum())
    return maxima


def null_share(m: dict) -> dict | None:
    """The no-skill null's share of arms above 0 (recentred arms, per draw)."""
    if not m["arms"]:
        return None
    share = m["pos"] / m["arms"]
    return {"mean": round(float(share.mean()), 4), "q95": round(float(np.quantile(share, 0.95)), 4)}


def adjusted_p(value: float | None, maxima: np.ndarray) -> float | None:
    """The share of draws whose recentred maximum reaches `value` (White's reality check)."""
    if value is None:
        return None
    m = maxima[np.isfinite(maxima)]
    return float((m >= value).mean()) if len(m) else None


def positive_share(arm_list: list[dict], key: str, min_n: int) -> dict:
    vals = [a[key] for a in arm_list if a.get(f"n_{key}", 0) >= min_n and a.get(key) is not None]
    return {
        "arms": len(vals),
        "above_0": int(sum(v > 0 for v in vals)),
        "share": round(float(np.mean([v > 0 for v in vals])), 4) if vals else None,
    }


# ---------- sizing for the top arms ----------


def sizing(bets: pd.DataFrame) -> dict:
    """Flat $1, quarter and half Kelly on the blend's chance (capped at 5%, $1,000 start),
    and flat with a stop after a 20-unit drawdown within a season; bets in date order,
    each settled before the next is staked."""
    b = bets.sort_values("date")
    profit = b["profit"].to_numpy(dtype=float)
    out = {"flat": {"bets": len(b), "profit": round(float(profit.sum()), 2)}}
    stop_profit, staked = 0.0, 0
    for _, g in b.groupby("season", sort=False):
        run, peak = 0.0, 0.0
        for x in g["profit"].to_numpy(dtype=float):
            if peak - run >= STOP_UNITS:
                break
            run += x
            peak = max(peak, run)
            staked += 1
        stop_profit += run
    out["stop_20"] = {"bets_staked": staked, "profit": round(stop_profit, 2)}
    p = b["p_blend"].to_numpy(dtype=float)
    odds = b["odds"].to_numpy(dtype=float)
    for name, frac in (("kelly_quarter", 0.25), ("kelly_half", 0.5)):
        if not np.isfinite(p).any():
            out[name] = {"note": "no blend chance for this market"}
            continue
        bank, peak, worst, staked = 1000.0, 1000.0, 0.0, 0
        for pi, d, x in zip(p, odds, profit, strict=True):
            if not np.isfinite(pi) or d <= 1:
                continue
            f = min(KELLY_CAP, frac * max(0.0, (pi * d - 1) / (d - 1)))
            if f <= 0:
                continue
            stake = f * bank
            bank += stake * x
            staked += 1
            peak = max(peak, bank)
            worst = max(worst, 1 - bank / peak)
        out[name] = {
            "bets_staked": staked,
            "bankroll_end": round(bank, 2),
            "max_drawdown_pct": round(worst, 4),
        }
    return out


# ---------- the whole search ----------


def arm_row(a: dict) -> dict:
    keep = ("family", "market", "base", "filters", "n_clv", "clv", "clv_ci95", "n_roi", "roi",
            "roi_ci95", "p_clv", "p_roi", "flag")  # fmt: skip
    return {k: a.get(k) for k in keep}


def search(df: pd.DataFrame, n_boot: int = N_BOOT) -> tuple[dict, list[dict]]:
    """The search over a featured side table: every arm scored, the reality check, the
    share of arms above 0 against the null, finalists and sizing. Returns (result, arms)."""
    levels = filter_levels(df)
    close_levels = filter_levels(df, at_close=True)
    early = arms(df, levels)
    moves = move_arms(df, close_levels)
    w = weights(int(pd.factorize(df["key"])[0].max()) + 1, n_boot)
    max_e = score(early, df, w, {"clv": "clv", "roi": "profit"})
    max_m = score(moves, df, w, {"roi": "profit_close"})
    roi_final = np.maximum(max_e["roi"]["final"], max_m["roi"]["final"])
    roi_all = np.maximum(max_e["roi"]["all"], max_m["roi"]["all"])
    every = early + moves
    for a in every:
        a["flag"] = "under 100 bets" if a.get("n_roi", 0) < MIN_FLAG else None
        a["p_clv"] = (
            adjusted_p(a.get("clv"), max_e["clv"]["final"])
            if a["family"] == "early" and a.get("n_clv", 0) >= MIN_FINAL
            else None
        )
        a["p_roi"] = adjusted_p(a.get("roi"), roi_final) if a.get("n_roi", 0) >= MIN_FINAL else None
    elig = [a for a in early if a.get("n_clv", 0) >= MIN_FINAL and a.get("clv") is not None]
    elig_r = [a for a in every if a.get("n_roi", 0) >= MIN_FINAL and a.get("roi") is not None]
    best_clv = max(elig, key=lambda a: a["clv"]) if elig else None
    best_roi = max(elig_r, key=lambda a: a["roi"]) if elig_r else None
    with_bets = [a for a in early if a.get("n_clv", 0) > 1 and a.get("clv") is not None]
    best_any = max(with_bets, key=lambda a: a["clv"]) if with_bets else None
    finalists = sorted(
        (
            a
            for a in elig
            if a.get("clv_ci95") and a["clv_ci95"][0] > 0 and (a["p_clv"] or 1) < P_MAX
        ),
        key=lambda a: -a["clv_ci95"][0],
    )[:MAX_FINALISTS]
    top_clv = sorted(elig, key=lambda a: -(a["clv_ci95"] or [-9])[0])[:TOP]
    top_roi = sorted(elig_r, key=lambda a: -(a["roi_ci95"] or [-9])[0])[:TOP]
    sized = {}
    for a in finalists + top_clv[:10]:
        label = _label(a)
        if label not in sized:
            sized[label] = sizing(df.iloc[a["rows"]])
    result = {
        "settings": {
            "holdout_start": HOLDOUT_START,
            "leagues": list(LEAGUES),
            "edges": list(EDGES),
            "zs": list(ZS),
            "moves": list(MOVES),
            "n_boot": n_boot,
            "seed": SEED,
            "min_flag": MIN_FLAG,
            "min_final": MIN_FINAL,
            "p_max": P_MAX,
            "cuts": levels.get("_cuts"),
        },
        "arms_tried": {
            "early": len(early),
            "move": len(moves),
            "total": len(every),
            "empty": sum(1 for a in every if not a.get("n_roi")),
            "under_100": sum(1 for a in every if a["flag"]),
            "eligible_clv": len(elig),
            "eligible_roi": len(elig_r),
        },
        "best_clv": arm_row(best_clv) if best_clv else None,
        "best_clv_any_size": arm_row(best_any) if best_any else None,
        "best_roi": arm_row(best_roi) if best_roi else None,
        "reality_check": {
            "clv": {
                "universe": f">= {MIN_FINAL} bets",
                "p_best": best_clv and best_clv["p_clv"],
                "null_max_95": _q(max_e["clv"]["final"], 0.95),
                "p_best_all_arms": best_any and adjusted_p(best_any["clv"], max_e["clv"]["all"]),
            },
            "roi": {
                "universe": f">= {MIN_FINAL} bets, early and movement arms",
                "p_best": best_roi and best_roi["p_roi"],
                "null_max_95": _q(roi_final, 0.95),
                "null_max_95_all_arms": _q(roi_all, 0.95),
            },
        },
        "positive_share": {
            "clv": positive_share(early, "clv", MIN_FINAL),
            "roi": positive_share(every, "roi", MIN_FINAL),
            "null_clv": null_share(max_e["clv"]),
            "null_roi": null_share(max_e["roi"]),
        },
        "by_market": {
            m: arm_row(
                next(
                    a
                    for a in early
                    if a["market"] == m and a["base"] == "every side" and not a["filters"]
                )
            )  # fmt: skip
            for m in MARKETS
        },
        "top_clv": [arm_row(a) for a in top_clv],
        "top_roi": [arm_row(a) for a in top_roi],
        "finalists": [arm_row(a) for a in finalists],
        "sizing": sized,
        "verdict": "finalists found: open the holdout once with a logged reason"
        if finalists
        else "no strategy found: no arm passes; the holdout stays locked",
        "best_clv_bets": bet_rows(df, best_clv["rows"]) if best_clv else [],
    }
    return result, every


def bet_rows(df: pd.DataFrame, rows: np.ndarray) -> list[dict]:
    """An arm's bets in date order, for the app's curve."""
    b = df.iloc[rows].sort_values("date")
    return [
        {
            "date": pd.Timestamp(r.date).date().isoformat(),
            "lg": r.league,
            "home": r.home,
            "away": r.away,
            "market": r.market,
            "side": r.side,
            "odds": round(float(r.odds), 3),
            "profit": round(float(r.profit), 4),
            "clv": None if not np.isfinite(r.clv) else round(float(r.clv), 4),
        }
        for r in b.itertuples()
    ]


def _arm_app(a: dict | None) -> dict | None:
    if not a:
        return None
    return {
        "label": _label(a),
        "family": a["family"],
        "bets": a.get("n_roi"),
        "roi": a.get("roi"),
        "roi_ci95": a.get("roi_ci95"),
        "clv_bets": a.get("n_clv"),
        "clv": a.get("clv"),
        "clv_ci95": a.get("clv_ci95"),
        "p_clv": a.get("p_clv"),
        "p_roi": a.get("p_roi"),
    }


def app_file(res: dict, runs: dict | None = None) -> dict:
    """The app-ready research file (`lab/strategy_search_research.json`), in the shape of
    `lab/comeback_research.json`: title, question, rule, verdict, verdict_text, cost,
    runs, preregistered, designs, bets (the best arm's, for the curve), notes."""
    t, rc, ps = res["arms_tried"], res["reality_check"], res["positive_share"]
    best = res.get("best_clv")
    found = bool(res["finalists"])
    leagues = list(res.get("leagues", {}))
    seasons = sorted({s for r in res.get("leagues", {}).values() for s in r.get("seasons", [])})
    return {
        "title": "Strategy search: every rule we could think of, on five seasons",
        "question": "Study the data we already have to find a strategy (position sizing, when "
        "to cash out...) that would have had positive ROI. I know this can be overfitting "
        "but we can try first.",
        "rule": "Every rule tried is counted: the raw model's best side at an edge of 0, 5, "
        "12 or 20%, Blend Lean at z 0.5, 1 or 2, or every side, in 1X2, draw no bet, Asian "
        "handicap and over/under 2.5 at Pinnacle's early price, with up to two filters "
        "(league, price band, edge or z band, driver, side, favourite, day, kickoff slot, "
        "strength gap), plus line-movement rules at the close. A rule passes only if its CLV "
        "range is above 0 on 300+ bets and it beats the best a no-skill search would find "
        "by luck (p < 0.05). 2024/25 onward is locked away for any finalist.",
        "verdict": "pass" if found else "fail",
        "verdict_text": (
            "Finalists found; the locked seasons decide."
            if found
            else f"No strategy found. {t['total']:,} rules tried; the best beat the closing "
            f"price by {best['clv']:+.1%} on {best['n_clv']} bets, but a search with no skill "
            f"finds one that good {best['p_clv']:.0%} of the time. Only {ps['clv']['share']:.0%} "
            "of the large rules beat the close at all, against about half if prices were "
            "beatable by chance."
        )
        if best
        else "No rule had 300 bets.",
        "closed_at": pd.Timestamp.now(tz="UTC").date().isoformat(),
        "cost": {"credits": 0, "total": 0},
        "runs": runs or {},
        "preregistered": {"design": "76557de", "implementation_notes": "e8e4e81"},
        "designs": {
            "search": {
                "role": "judged",
                "label": f"{t['total']:,} rules, eight leagues, {len(seasons)} seasons "
                "(2024/25 onward locked)",
                "leagues": leagues,
                "seasons": [lb.season_label(s) for s in seasons],
                "arms": t,
                "best_clv": _arm_app(best),
                "best_roi": _arm_app(res.get("best_roi")),
                "reality_check": rc,
                "positive_share": ps,
                "by_market": [
                    {"market": m, **(_arm_app(a) or {})} for m, a in res["by_market"].items()
                ],
                "top_clv": [_arm_app(a) for a in res["top_clv"]],
                "top_roi": [_arm_app(a) for a in res["top_roi"]],
                "finalists": [_arm_app(a) for a in res["finalists"]],
                "sizing": res.get("sizing"),
            }
        },
        "bets_of": _label(best) if best else None,
        "bets": res.get("best_clv_bets", []),
        "notes": [
            "Every number is from 2019/20 to 2023/24 (seasons where Pinnacle's early and "
            "closing prices both cover 90% of matches). 2024/25 onward was never read.",
            "CLV is the entry price against Pinnacle's margin-free close; ROI is per $1. "
            "Ranges resample matches (2,000 draws).",
            "Line-movement rules are decided at the close, so they have no CLV and are judged "
            "on ROI only; none can be a finalist.",
            "The curve is the best rule by CLV: it beat the close but still lost or barely "
            "broke even, and the search's luck check says rules like it turn up by chance.",
            "Cashing out (the exits) was tested separately on the comeback sample: a fair "
            "cash-out never changes the average return, and a real one costs money.",
        ],
    }


def _q(x: np.ndarray, q: float) -> float | None:
    x = x[np.isfinite(x)]
    return float(np.quantile(x, q)) if len(x) else None


def _label(a: dict) -> str:
    f = f" [{', '.join(a['filters'])}]" if a["filters"] else ""
    return f"{a['market']} {a['base']}{f}"


# ---------- loading (Actions) ----------


def load_league(code: str, holdout: Holdout) -> tuple[pd.DataFrame, dict]:
    """One league's side table from football-data and Understat, before the holdout only."""
    from soccer_stats import backtest
    from soccer_stats import leagues as lgs
    from soccer_stats.data import download, load_matches, pinnacle_prices, season_code
    from soccer_stats.models import DixonColes
    from soccer_stats.publish import XG_WEIGHT
    from soccer_stats.xg import with_xg

    years = range(FROM_SEASON - BURN_IN, LAST_YEAR + 1)
    matches = load_matches([code], years)
    matches, err = with_xg(matches)
    weight = XG_WEIGHT if matches["home_xg"].notna().any() else 0.0
    if lgs.LEAGUES[code].understat and not weight:
        raise RuntimeError(f"no Understat xG attached ({err})")
    holdout.check(matches["date"])
    preds = backtest.walk_forward(
        matches,
        start=f"{FROM_SEASON}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=weight),
        keep_matrix=True,
    )
    pins = pinnacle_prices(code, range(FROM_SEASON, LAST_YEAR + 1))
    holdout.check(pins["date"])
    prices, times = [], []
    for y in range(FROM_SEASON, LAST_YEAR + 1):
        raw = pd.read_csv(download(code, y), encoding="latin-1", on_bad_lines="skip")
        prices.append(mm.load_prices(raw, season=season_code(y)))
        played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
        times.append(
            pd.DataFrame(
                {
                    "date": pd.to_datetime(played["Date"], dayfirst=True, format="mixed"),
                    "home": played["HomeTeam"],
                    "away": played["AwayTeam"],
                    "time": played["Time"] if "Time" in played else None,
                }
            )
        )
    prices = pd.concat(prices, ignore_index=True)
    holdout.check(prices["date"])
    sides, rep = league_sides(code, preds, pins, prices, pd.concat(times, ignore_index=True))
    holdout.check(sides["date"])
    rep["xg_weight"] = weight
    return sides, rep


def report(res: dict, reps: dict) -> str:
    t = res["arms_tried"]
    lines = [
        f"== Strategy search (holdout from {HOLDOUT_START} locked): {t['total']} arms tried "
        f"({t['early']} at the early price, {t['move']} line-movement), {t['empty']} empty, "
        f"{t['under_100']} under 100 bets; {t['eligible_clv']} with >= {MIN_FINAL} CLV bets =="
    ]
    for code, r in reps.items():
        lines.append(f"  {code}: {r}")
    for name, a in (("best CLV", res["best_clv"]), ("best ROI", res["best_roi"]),
                    ("best CLV, any size", res["best_clv_any_size"])):  # fmt: skip
        if a:
            lines.append(f"  {name}: {_line(a)}")
    rc = res["reality_check"]
    lines.append(
        f"  reality check: CLV p {rc['clv']['p_best']} (null max 95% {rc['clv']['null_max_95']}), "
        f"ROI p {rc['roi']['p_best']} (null max 95% {rc['roi']['null_max_95']})"
    )
    ps = res["positive_share"]
    lines.append(
        f"  arms above 0 (>= {MIN_FINAL} bets): CLV {ps['clv']}, ROI {ps['roi']}; "
        f"no-skill null CLV {ps['null_clv']}, ROI {ps['null_roi']}"
    )
    lines.append("  by market (every side, no filter):")
    lines += [f"    {_line(a)}" for a in res["by_market"].values()]
    lines.append("  top arms by the CLV range's lower end:")
    lines += [f"    {_line(a)}" for a in res["top_clv"][:15]]
    lines.append("  top arms by the ROI range's lower end:")
    lines += [f"    {_line(a)}" for a in res["top_roi"][:15]]
    lines.append(f"  finalists: {len(res['finalists'])}")
    lines += [f"    {_line(a)}" for a in res["finalists"]]
    lines.append(f"  VERDICT: {res['verdict']}")
    return "\n".join(lines)


def _line(a: dict) -> str:
    def pct(x):
        return "-" if x is None else f"{x:+.2%}"

    def rng(c):
        return "-" if not c else f"{c[0]:+.2%} to {c[1]:+.2%}"

    return (
        f"{_label(a)}: {a.get('n_roi')} bets, ROI {pct(a.get('roi'))} ({rng(a.get('roi_ci95'))}),"
        f" CLV {pct(a.get('clv'))} ({rng(a.get('clv_ci95'))}), p CLV {a.get('p_clv')},"
        f" p ROI {a.get('p_roi')}"
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default=",".join(LEAGUES), help="comma list of codes")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    p.add_argument("--json", default="", help="the result (summary)")
    p.add_argument("--arms-json", default="", help="every arm's row (large)")
    p.add_argument("--app-json", default="", help="the app-ready research file")
    args = p.parse_args(argv)
    holdout = Holdout(HOLDOUT_START)
    frames, reps = [], {}
    for code in [c.strip() for c in args.leagues.split(",") if c.strip()]:
        try:
            sides, rep = load_league(code, holdout)
            frames.append(sides)
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        reps[code] = rep
        print(f"{code}: {rep}", flush=True)
    df = add_features(pd.concat(frames, ignore_index=True))
    res, every = search(df, args.boot)
    res["leagues"] = reps
    print(report(res, reps))
    print(
        "STRATEGY_SEARCH_JSON "
        + json.dumps({k: v for k, v in res.items() if k != "best_clv_bets"}, default=str)
    )
    run = os.environ.get("GITHUB_RUN_ID")
    app = app_file(res, {"search": run} if run else {})
    print("STRATEGY_SEARCH_APP_JSON " + json.dumps(app, default=str))
    outs = ((args.json, res), (args.arms_json, [arm_row(a) for a in every]), (args.app_json, app))
    for path, obj in outs:
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(obj, default=str))


if __name__ == "__main__":
    main()
