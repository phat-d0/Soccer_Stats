"""Residual map: where the match model and the market disagree, and who was right.

Exploratory and descriptive (owner via the lead, 5 Nov; docs/lab.md "Residual map", written
before any run). No verdict, no bets, 0 credits, no key.

Rows: every outcome (home, draw, away) of every match in the eight domestic leagues, in the
seasons lean_backtest scores, before the locked 2024-07-01 (the strategy search's holdout;
lab.harness.Holdout checks every frame). Per row: the model's walk-forward chance, the blend
p_bet where a fit exists, Pinnacle's Shin-fair early and close chances, and whether it won.

Core surface: x = the market's fair chance (the close), y = model minus market, z = won minus
market, averaged per cell, with n and a 95% range resampling matches within the cell. A
second surface uses the early price. Slices: a factor's levels x five bands of model minus
market (the close), z = won minus the close. Every factor is built from data before kickoff,
except the early-to-close price move (marked descriptive).

The file is printed in the log as gzipped base64 chunks with its SHA-256 (the sandbox
can't download artifacts); the committed lab/residual_map.json is that text.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import gzip
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats.data import PIN_CLOSE
from soccer_stats.lab import features as ft
from soccer_stats.lab.harness import Holdout
from soccer_stats.odds import devig_shin

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
FIRST_YEAR = 2019  # 2019/20, lean_backtest's first scored season
LAST_YEAR = 2023  # 2023/24; 2024/25 on stays locked (the strategy search's holdout)
BURN_IN = 2
SIDES = ("home", "draw", "away")
N_BOOT = 2000
SEED = 0
THIN = 200
LEVEL = 0.95

X_EDGES = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 1.0)
Y_EDGES = (-np.inf, -0.10, -0.05, -0.02, 0.02, 0.05, 0.10, np.inf)
BAND_EDGES = (-np.inf, -0.05, -0.02, 0.02, 0.05, np.inf)
BAND_LABELS = ("< -0.05", "-0.05 to -0.02", "-0.02 to +0.02", "+0.02 to +0.05", "> +0.05")
REST_EDGES = (-np.inf, -2.5, -0.5, 0.5, 2.5, np.inf)
REST_LABELS = ("<= -3", "-2 to -1", "0", "+1 to +2", ">= +3")
LUCK_EDGES = (-np.inf, -0.6, -0.2, 0.2, 0.6, np.inf)
LUCK_LABELS = ("<= -0.6", "-0.6 to -0.2", "-0.2 to +0.2", "+0.2 to +0.6", ">= +0.6")
MOVE_EDGES = (-np.inf, -0.04, -0.01, 0.01, 0.04, np.inf)
MOVE_LABELS = ("<= -0.04", "-0.04 to -0.01", "-0.01 to +0.01", "+0.01 to +0.04", ">= +0.04")
MONTHS = ("Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun-Jul")
EURO_DAYS = (1, 4)  # a UEFA match 1 to 4 days before the league match
EURO_LEVELS = ("team after Europe", "opponent after Europe", "both", "neither")
NEW_FIRST = 10
NEW_LEVELS = ("promoted, first 10", "relegated, first 10", "opponent new", "both new", "neither")
DRIVER_LEVELS = ("not a Lean pick", "Lean pick, market-driven", "Lean pick, model-driven")
HIGHER = {"E1": "E0", "E2": "E1", "E3": "E2"}
B64_CHUNK = 60000

WARNING = (
    "Exploratory. With this many cells some will look striking by chance: with no real "
    "pattern about 5% of cells would have a z range that excludes 0 (see `multiplicity`). "
    "The strategy search found 2% of its large rules beating the close under no skill, and its "
    "best rule was luck (p = 0.94). A cell that stands out is a lead to pre-register and test "
    "on data this map never read (2024/25 is kept locked for that), not a finding."
)
NOTE = (
    "Every outcome (home, draw, away) of every match with Pinnacle's full early and close 1X2 "
    "and a walk-forward model chance. x = the market's Shin-fair chance, y = the model's chance "
    "minus the market's, z = won minus the market's chance, averaged per cell; z > 0 where the "
    "model liked an outcome (y > 0) means the model was right there. Ranges resample matches "
    "within each cell (2,000 draws, seed 0). Cells under 200 rows are marked thin. Seasons "
    "2019/20-2023/24; 2024/25 on is locked as the test season for anything the map suggests. "
    "Two scope limits: 'after a European match' counts UEFA matches only (domestic cups are "
    "not counted), and rest days count league matches only. A slice level that holds every outcome of its "
    "matches (league, month) has a total z of 0 by construction (each match's outcomes sum to "
    "one win and to a chance of 1); read its bands, not its total."
)


# --- grids ------------------------------------------------------------------------------


def bin_index(values, edges) -> np.ndarray:
    """0-based bin per value (left-closed, the last bin closed), -1 for NaN."""
    v = np.asarray(values, dtype=float)
    idx = np.searchsorted(np.asarray(edges[1:-1], dtype=float), v, side="right")
    return np.where(np.isfinite(v), idx, -1)


def boot_range(resid, match, n_boot: int = N_BOOT, seed: int = SEED) -> list[float] | None:
    """95% range of the mean residual, resampling the cell's matches (a match's rows in the
    cell move together)."""
    resid = np.asarray(resid, dtype=float)
    if len(resid) < 2:
        return None
    codes, inv = np.unique(np.asarray(match), return_inverse=True)
    sums = np.bincount(inv, weights=resid)
    counts = np.bincount(inv).astype(float)
    m = len(codes)
    rng = np.random.default_rng(seed)
    means = []
    step = max(1, min(n_boot, 4_000_000 // max(m, 1)))
    done = 0
    while done < n_boot:
        k = min(step, n_boot - done)
        idx = rng.integers(0, m, size=(k, m))
        means.append(sums[idx].sum(axis=1) / counts[idx].sum(axis=1))
        done += k
    draws = np.concatenate(means)
    lo, hi = np.percentile(draws, [100 * (1 - LEVEL) / 2, 100 * (1 + LEVEL) / 2])
    return [round(float(lo), 5), round(float(hi), 5)]


def cell(df: pd.DataFrame, x: str, y: str, n_boot: int = N_BOOT) -> dict:
    """One cell's numbers: rows, matches, mean x and y, win rate, z = won - x with its range,
    won minus the model, thin."""
    n = len(df)
    out: dict = {"n": int(n), "matches": int(df["match"].nunique()) if n else 0}
    if not n:
        return {**out, "x": None, "y": None, "won": None, "z": None, "z_ci95": None,
                "model_resid": None, "thin": True}  # fmt: skip
    resid = df["won"].to_numpy(float) - df[x].to_numpy(float)
    out.update(
        {
            "x": round(float(df[x].mean()), 5),
            "y": round(float(df[y].mean()), 5),
            "won": round(float(df["won"].mean()), 5),
            "z": round(float(resid.mean()), 5),
            "z_ci95": boot_range(resid, df["match"].to_numpy(), n_boot),
            "model_resid": round(float((df["won"] - df["p_model"]).mean()), 5),
            "thin": bool(n < THIN),
        }
    )
    return out


def surface(rows: pd.DataFrame, price: str, n_boot: int = N_BOOT) -> dict:
    """x = the price's fair chance, y = model minus it, z = won minus it."""
    d = rows.dropna(subset=[price, "p_model"]).copy()
    d["_x"] = d[price]
    d["_y"] = d["p_model"] - d[price]
    xi, yi = bin_index(d["_x"], X_EDGES), bin_index(d["_y"], Y_EDGES)
    cells = []
    for i in range(len(X_EDGES) - 1):
        for j in range(len(Y_EDGES) - 1):
            c = cell(d[(xi == i) & (yi == j)], "_x", "_y", n_boot)
            cells.append({"xi": i, "yi": j, **c})
    return {
        "price": price,
        "x": f"the market's fair chance ({price})",
        "y": "model minus market",
        "z": "won minus market",
        "x_edges": list(X_EDGES),
        "y_edges": [_edge(e) for e in Y_EDGES],
        "rows": int(len(d)),
        "cells": cells,
    }


def _edge(e: float):
    return None if not np.isfinite(e) else e


def slice_grid(
    rows: pd.DataFrame, factor: str, levels, label: str, note: str = "", n_boot: int = N_BOOT
) -> dict:
    """The factor's levels x five bands of model minus the close; z = won minus the close."""
    d = rows[rows[factor].isin(levels)].dropna(subset=["fair_close", "p_model"]).copy()
    d["_x"] = d["fair_close"]
    d["_y"] = d["p_model"] - d["fair_close"]
    bi = bin_index(d["_y"], BAND_EDGES)
    cells, totals = [], []
    for lv in levels:
        at = (d[factor] == lv).to_numpy()
        totals.append({"level": lv, **cell(d[at], "_x", "_y", n_boot)})
        for j, band in enumerate(BAND_LABELS):
            cells.append({"level": lv, "band": band, **cell(d[at & (bi == j)], "_x", "_y", n_boot)})
    return {
        "label": label,
        "levels": list(levels),
        "bands": list(BAND_LABELS),
        "band_edges": [_edge(e) for e in BAND_EDGES],
        "rows": int(len(d)),
        "cells": cells,
        "totals": totals,
        "note": note,
    }


def multiplicity(core: dict, slices: dict) -> dict:
    """Cells with >= THIN rows and how many z ranges exclude 0, against about 5% by chance."""
    cs = [c for s in core.values() for c in s["cells"]]
    cs += [c for s in slices.values() if "cells" in s for c in s["cells"]]
    big = [c for c in cs if c["n"] >= THIN and c.get("z_ci95")]
    out = [c for c in big if c["z_ci95"][0] > 0 or c["z_ci95"][1] < 0]
    return {
        "cells": len(cs),
        "cells_200_plus": len(big),
        "range_excludes_0": len(out),
        "share": round(len(out) / len(big), 4) if big else None,
        "expected_by_chance": round(0.05 * len(big), 1),
        "note": "Cells overlap (a row sits in one core cell per surface and one cell per slice), "
        "so these counts are not independent; read them as a scale, not a test.",
    }


# --- factors (each from data before kickoff) --------------------------------------------


def season_start(season) -> int:
    return 2000 + int(str(season)[:2])


def team_matches(matches: pd.DataFrame) -> pd.DataFrame:
    """One row per (match, team): league, season, date, team, opponent, n (the team's earlier
    league matches this season)."""
    m = matches.sort_values("date", kind="stable")
    parts = []
    for side in ("home", "away"):
        parts.append(
            pd.DataFrame(
                {
                    "league": m["league"].to_numpy(),
                    "season": m["season"].astype(str).to_numpy(),
                    "date": pd.to_datetime(m["date"]).dt.normalize().to_numpy(),
                    "home": m["home"].to_numpy(),
                    "team": m[side].to_numpy(),
                    "side": side,
                }
            )
        )
    long = pd.concat(parts, ignore_index=True).sort_values(["date", "side"], kind="stable")
    long["n"] = long.groupby(["league", "season", "team"]).cumcount()
    return long


def newcomers(matches: pd.DataFrame) -> dict[tuple[str, str, str], str]:
    """(league, season, team) -> "promoted" / "relegated" for a team not in the league the
    season before. England: a team in the division above the season before was relegated;
    elsewhere every newcomer counts as promoted. Seasons whose previous one isn't loaded are
    left out."""
    members: dict[tuple[str, str], set] = {}
    for (lg, s), g in matches.groupby(["league", matches["season"].astype(str)]):
        members[(lg, s)] = set(g["home"]) | set(g["away"])
    out = {}
    for (lg, s), teams in members.items():
        y = season_start(s)
        prev = f"{(y - 1) % 100:02d}{y % 100:02d}"
        if (lg, prev) not in members:
            continue
        above = members.get((HIGHER.get(lg, ""), prev), set())
        for t in teams - members[(lg, prev)]:
            out[(lg, s, t)] = "relegated" if t in above else "promoted"
    return out


def euro_dates(events: list[dict], mapping: dict) -> dict[str, list[pd.Timestamp]]:
    """'<league>:<team>' -> sorted UTC dates of its UEFA matches."""
    out: dict[str, list] = {}
    for e in events:
        k = e.get("kickoff")
        if k is None:
            continue
        day = (
            pd.Timestamp(k).tz_convert(None).normalize()
            if pd.Timestamp(k).tzinfo
            else (pd.Timestamp(k).normalize())
        )
        for side in ("home", "away"):
            key = mapping.get(e.get(side))
            if key:
                out.setdefault(key, []).append(day)
    return {k: sorted(v) for k, v in out.items()}


def after_europe(dates: list | None, day: pd.Timestamp, lo: int = EURO_DAYS[0],
                 hi: int = EURO_DAYS[1]) -> bool:  # fmt: skip
    """A UEFA match lo..hi days before `day` (strictly earlier days only)."""
    if not dates:
        return False
    return any(lo <= (day - d).days <= hi for d in dates)


def match_rows(scored: pd.DataFrame, league: str) -> pd.DataFrame:
    """One row per outcome: league, season, date, home, away, match, side, p_model, p_blend,
    fair_early, fair_close, won, move."""
    d = scored.copy()
    ok = lb._priced(d, PIN_CLOSE)
    d = d[ok].reset_index(drop=True)
    if d.empty:
        return pd.DataFrame()
    close = np.vstack([devig_shin(r) for r in d[list(PIN_CLOSE)].to_numpy(dtype=float)])
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    key = league + "|" + d["date"].dt.date.astype(str) + "|" + d["home"].astype(str)
    parts = []
    for i, s in enumerate(SIDES):
        parts.append(
            pd.DataFrame(
                {
                    "league": league,
                    "season": d["season"].astype(str),
                    "date": d["date"],
                    "home": d["home"],
                    "away": d["away"],
                    "match": key,
                    "side": s,
                    "p_model": d[f"p_{s}"].astype(float),
                    "p_blend": d[f"pb_{s}"].astype(float),
                    "fair_early": d[f"mkt_{s}"].astype(float),
                    "fair_close": close[:, i],
                    "won": (d["y"].astype(int) == i).astype(float),
                }
            )
        )
    out = pd.concat(parts, ignore_index=True)
    out["move"] = out["fair_close"] - out["fair_early"]
    return out


def add_factors(
    rows: pd.DataFrame,
    matches: pd.DataFrame,
    lean: pd.DataFrame | None = None,
    euro: dict | None = None,
) -> pd.DataFrame:
    """The slice columns. `matches` = every league match loaded (burn-in seasons too), with
    league, season, date, home, away, goals and xG (NaN without Understat). `lean` = the
    Lean bets (league, date, home, market, driver). `euro` = euro_dates' map, or None."""
    df = rows.copy()
    df["month"] = [MONTHS[min((d.month - 8) % 12, 10)] for d in pd.to_datetime(df["date"])]
    m = matches.copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    frm = []
    for _, g in m.groupby("league"):
        f = ft.form(g.reset_index(drop=True))
        frm.append(pd.concat([g.reset_index(drop=True)[["league", "date", "home", "away"]], f],
                             axis=1))  # fmt: skip
    frm = pd.concat(frm, ignore_index=True).drop_duplicates(["league", "date", "home"])
    for side in ("home", "away"):
        frm[f"{side}_luck"] = (frm[f"{side}_xgf6"] - frm[f"{side}_xga6"]) - (
            frm[f"{side}_gf6"] - frm[f"{side}_ga6"]
        )
    keep = ["league", "date", "home", "home_rest", "away_rest", "home_luck", "away_luck"]
    df = df.merge(frm[keep], on=["league", "date", "home"], how="left")
    home, away = df["side"] == "home", df["side"] == "away"
    sgn = np.where(home, 1.0, np.where(away, -1.0, np.nan))
    rest = (df["home_rest"] - df["away_rest"]) * sgn
    luck = (df["home_luck"] - df["away_luck"]) * sgn
    df["rest_diff"] = _labels(rest, REST_EDGES, REST_LABELS)
    df["xg_luck"] = _labels(luck, LUCK_EDGES, LUCK_LABELS)
    df["price_move"] = _labels(df["move"], MOVE_EDGES, MOVE_LABELS)

    # New to the division, first NEW_FIRST league matches of the season.
    new = newcomers(m)
    tm = team_matches(m)
    # A plain dict (a DataFrame column would turn None into a truthy NaN).
    look = {
        (lg, d, h, sd): (new.get((lg, s, t)) if n < NEW_FIRST else None)
        for lg, s, d, h, sd, t, n in zip(
            tm["league"],
            tm["season"],
            tm["date"],
            tm["home"],
            tm["side"],
            tm["team"],
            tm["n"],
            strict=True,
        )  # fmt: skip
    }
    labels = []
    for r in df[["league", "date", "home", "side"]].itertuples(index=False):
        if r.side == "draw":
            labels.append(None)
            continue
        opp = "away" if r.side == "home" else "home"
        mine, theirs = (
            look.get((r.league, r.date, r.home, r.side)),
            look.get((r.league, r.date, r.home, opp)),
        )
        if mine and theirs:
            labels.append("both new")
        elif mine:
            labels.append(f"{mine}, first {NEW_FIRST}")
        elif theirs:
            labels.append("opponent new")
        else:
            labels.append("neither")
    df["new_team"] = labels

    if euro is not None:
        eu = []
        teams = df["home"].where(df["side"] == "home", df["away"])
        opps = df["away"].where(df["side"] == "home", df["home"])
        for side, lg, d, t, o in zip(df["side"], df["league"], df["date"], teams, opps,
                                     strict=True):  # fmt: skip
            if side == "draw":
                eu.append(None)
                continue
            a = after_europe(euro.get(f"{lg}:{t}"), d)
            b = after_europe(euro.get(f"{lg}:{o}"), d)
            eu.append(EURO_LEVELS[0] if a and not b else EURO_LEVELS[1] if b and not a
                      else EURO_LEVELS[2] if a and b else EURO_LEVELS[3])  # fmt: skip
        df["europe"] = eu
    else:
        df["europe"] = None

    df["driver"] = None
    has_fit = df["p_blend"].notna()
    df.loc[has_fit, "driver"] = DRIVER_LEVELS[0]
    if lean is not None and len(lean):
        lk = lean.assign(date=pd.to_datetime(lean["date"]).dt.normalize())
        dmap = {
            (lg, d, h, mk): dr
            for lg, d, h, mk, dr in zip(
                lk["league"], lk["date"], lk["home"], lk["market"], lk["driver"], strict=True
            )  # fmt: skip
        }
        got = [
            dmap.get((lg, d, h, s))
            for lg, d, h, s in zip(df["league"], df["date"], df["home"], df["side"], strict=True)
        ]
        for i, g in enumerate(got):
            if g in ("market", "model"):
                df.iat[i, df.columns.get_loc("driver")] = f"Lean pick, {g}-driven"
    return df


def _labels(values, edges, labels) -> list:
    idx = bin_index(values, edges)
    return [labels[i] if i >= 0 else None for i in idx]


FACTORS = [
    {"key": "side", "label": "Side", "levels": list(SIDES), "known": "before kickoff",
     "note": ""},
    {"key": "league", "label": "League", "levels": list(LEAGUES), "known": "before kickoff",
     "note": ""},
    {"key": "month", "label": "Month of the season", "levels": list(MONTHS),
     "known": "before kickoff", "note": "June and July are one level (2019/20's restart)."},
    {"key": "rest_diff", "label": "Rest days, team minus opponent", "levels": list(REST_LABELS),
     "known": "before kickoff",
     "note": "League matches only, each side capped at 14 days (lab.features). Home and away "
     "rows."},
    {"key": "europe", "label": "After a European match (1-4 days before)",
     "levels": list(EURO_LEVELS), "known": "before kickoff",
     "note": "UEFA matches only (Champions League, Europa League, Conference League and "
     "qualifiers, from ESPN); domestic cups are not counted. Home and away rows."},
    {"key": "xg_luck", "label": "xG luck, last 6, team minus opponent",
     "levels": list(LUCK_LABELS), "known": "before kickoff",
     "note": "(xG for - xG against) - (goals for - goals against) per game over the last 6 "
     "league matches; positive = results worse than the chances. Understat leagues only (E0, "
     "SP1, D1, I1, F1), 3+ earlier matches each. Home and away rows."},
    {"key": "new_team", "label": "New to the division, first 10 league matches",
     "levels": list(NEW_LEVELS), "known": "before kickoff",
     "note": "Relegated = in the division above the season before (England only); every other "
     "newcomer counts as promoted. Home and away rows."},
    {"key": "price_move", "label": "Price move, early to close (descriptive)",
     "levels": list(MOVE_LABELS), "known": "at the close only",
     "note": "The outcome's fair close minus its fair early chance. Not known when a bet is "
     "taken."},
    {"key": "driver", "label": "Lean's driver", "levels": list(DRIVER_LEVELS),
     "known": "before kickoff",
     "note": "Lean's pick at Pinnacle's early price (lean_backtest.bets); rows with a blend fit "
     "only."},
]  # fmt: skip


def build(rows: pd.DataFrame, n_boot: int = N_BOOT, info: dict | None = None) -> dict:
    """The file: core surfaces, slices, factors, multiplicity, note, warning."""
    core = {
        "close": surface(rows, "fair_close", n_boot),
        "early": surface(rows, "fair_early", n_boot),
    }
    slices = {}
    for f in FACTORS:
        if f["key"] not in rows or rows[f["key"]].notna().sum() == 0:
            slices[f["key"]] = {"label": f["label"], "error": "no data for this factor"}
            continue
        slices[f["key"]] = slice_grid(rows, f["key"], f["levels"], f["label"], f["note"], n_boot)
    info = info or {}
    return {
        "kind": "residual_map",
        "title": "Where the model and the market disagree, and who was right",
        "exploratory": True,
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seasons": sorted(rows["season"].astype(str).unique().tolist()),
        "leagues": sorted(rows["league"].unique().tolist()),
        "rows": int(len(rows)),
        "matches": int(rows["match"].nunique()),
        "locked_from": info.get("locked_from"),
        "boot": n_boot,
        "thin_below": THIN,
        "core": core,
        "slices": slices,
        "factors": FACTORS,
        "multiplicity": multiplicity(core, slices),
        "note": NOTE,
        "warning": WARNING,
        "league_reports": info.get("leagues", {}),
        "europe": info.get("europe"),
        "runs": info.get("runs", {}),
    }


# --- loading (Actions only: football-data, Understat, ESPN) -----------------------------


def load_league(code: str, holdout: Holdout, first: int, last: int):
    """One league's outcome rows, its matches (burn-in included) and its Lean bets."""
    from soccer_stats import backtest
    from soccer_stats import leagues as lgs
    from soccer_stats.data import load_matches, pinnacle_prices
    from soccer_stats.models import DixonColes
    from soccer_stats.publish import XG_WEIGHT
    from soccer_stats.xg import with_xg

    matches = load_matches([code], range(first - BURN_IN, last + 1))
    matches, err = with_xg(matches)
    weight = XG_WEIGHT if matches["home_xg"].notna().any() else 0.0
    if lgs.LEAGUES[code].understat and not weight:
        raise RuntimeError(f"no Understat xG attached ({err})")
    holdout.check(matches["date"])
    preds = backtest.walk_forward(
        matches,
        start=f"{first}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=weight),
    )
    pins = pinnacle_prices(code, range(first, last + 1))
    holdout.check(pins["date"])
    merged = lb.with_pinnacle(preds, pins)
    train = mc.training_rows(merged, "h2h")
    cov = lb.coverage(pins)
    included = {c["season"] for c in cov if c["included"]}
    scored, fits = lb.walk_forward(train, lb.target_rows(merged))
    scored = scored[scored["season"].astype(str).isin(included)].reset_index(drop=True)
    holdout.check(scored["date"])
    lean = lb.bets(scored, code)
    rows = match_rows(scored, code)
    m = matches.copy()
    m["league"] = code
    rep = {
        "seasons": sorted(included),
        "matches": int(rows["match"].nunique()) if len(rows) else 0,
        "rows": int(len(rows)),
        "fits": len(fits),
        "lean_picks": int(len(lean)),
        "xg_weight": weight,
    }
    return rows, m, lean, rep


def load_europe(matches: pd.DataFrame, first: int, last: int, cutoff: pd.Timestamp):
    """euro_dates for the loaded clubs, from ESPN's UEFA results (cached), and a report."""
    from soccer_stats import cup_model

    years = list(range(first - 1, last + 1))
    events, rep = cup_model.european_events(years, until=cutoff, today=cutoff)
    events = [e for e in events if pd.Timestamp(e["kickoff"]) < cutoff]
    teams = {lg: set(g["home"]) | set(g["away"]) for lg, g in matches.groupby("league")}
    names = sorted({e[s] for e in events for s in ("home", "away") if e.get(s)})
    mapping = cup_model.team_map(names, teams)
    mapped = sum(1 for v in mapping.values() if v)
    return euro_dates(events, mapping), {
        "events": len(events),
        "complete": rep.get("complete"),
        "why": rep.get("why"),
        "clubs": len(names),
        "clubs_mapped": mapped,
    }


def print_file(text: str) -> None:
    print(f"RESIDUAL_MAP_SHA256 {hashlib.sha256(text.encode()).hexdigest()} ({len(text)} bytes)")
    blob = base64.b64encode(gzip.compress(text.encode(), mtime=0)).decode()
    parts = [blob[i : i + B64_CHUNK] for i in range(0, len(blob), B64_CHUNK)]
    for i, part in enumerate(parts):
        print(f"RESIDUAL_MAP_B64 {i + 1}/{len(parts)} {part}")


def summary_lines(res: dict) -> list[str]:
    mu = res["multiplicity"]
    lines = [
        f"== Residual map: {res['matches']} matches, {res['rows']} rows, seasons "
        f"{res['seasons'][0]}-{res['seasons'][-1]}, leagues {','.join(res['leagues'])} ==",
        f"  cells {mu['cells']}, {mu['cells_200_plus']} with >= {THIN} rows; z range excludes 0 "
        f"in {mu['range_excludes_0']} (about {mu['expected_by_chance']} by chance)",
    ]
    for name, s in res["slices"].items():
        if "error" in s:
            lines.append(f"  {name}: {s['error']}")
            continue
        tot = ", ".join(f"{t['level']} n={t['n']} z={t['z']}" for t in s["totals"] if t["n"])
        lines.append(f"  {name}: {tot}")
    return lines


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default=",".join(LEAGUES), help="comma list of codes")
    p.add_argument("--first", type=int, default=FIRST_YEAR, help="first season's start year")
    p.add_argument("--last", type=int, default=LAST_YEAR, help="last season's start year")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    p.add_argument("--no-europe", action="store_true", help="skip the ESPN UEFA results")
    p.add_argument("--json", default="", help="write the file here too")
    args = p.parse_args(argv)
    cutoff = pd.Timestamp(f"{args.last + 1}-07-01")
    holdout = Holdout(cutoff)
    frames, mats, leans, reps = [], [], [], {}
    for code in [c.strip() for c in args.leagues.split(",") if c.strip()]:
        try:
            rows, m, lean, rep = load_league(code, holdout, args.first, args.last)
            frames.append(rows)
            mats.append(m)
            leans.append(lean.assign(league=code))
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        reps[code] = rep
        print(f"{code}: {rep}", flush=True)
    rows = pd.concat(frames, ignore_index=True)
    matches = pd.concat(mats, ignore_index=True)
    lean = pd.concat(leans, ignore_index=True) if leans else None
    euro, euro_rep = None, None
    if not args.no_europe:
        try:
            euro, euro_rep = load_europe(matches, args.first, args.last,
                                         pd.Timestamp(cutoff, tz="UTC"))  # fmt: skip
            if not euro_rep.get("complete"):
                print(f"Europe incomplete: {euro_rep}")
        except Exception as exc:  # noqa: BLE001 - the slice is dropped, the rest goes on
            euro_rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        print(f"Europe: {euro_rep}", flush=True)
    rows = add_factors(rows, matches, lean, euro)
    holdout.check(rows["date"])
    run = os.environ.get("GITHUB_RUN_ID")
    res = build(
        rows,
        args.boot,
        {
            "locked_from": str(cutoff.date()),
            "leagues": reps,
            "europe": euro_rep,
            "runs": {"residual_map": run} if run else {},
        },
    )
    print("\n".join(summary_lines(res)))
    text = json.dumps(res, default=str, separators=(",", ":"))
    print_file(text)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)


if __name__ == "__main__":
    main()
