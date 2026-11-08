"""LASSO on the market residual: does any pre-match factor explain where the price is wrong?

Pre-registered in docs/lab.md "LASSO on the market residual" (owner, 8 Nov), committed before
any model was fitted. Development only: 2019/20-2023/24 out of sample, 2024/25 locked
(lab.harness.Holdout from 2024-07-01). No key, 0 credits.

One row per match. A multinomial logistic with the draw as reference and Pinnacle's Shin-fair
early chances as a fixed offset:

    score_draw = log q_draw
    score_home = log q_home + a_home + sum_j b_home,j x_j
    score_away = log q_away + a_away + sum_j b_away,j x_j

with an L1 penalty on every b (not the intercepts), fitted by FISTA in numpy (scikit-learn's
logistic regression takes no offset). Features are standardised on each refit's training rows.
lambda is chosen inside each training window: the path is fitted on rows before the window's
last 365 days and scored on those days; the best lambda is refitted on the whole window.

Walk-forward: decision day = kickoff - 4 days (lean_backtest.EARLY_LEAD); 28-day blocks of
decision days from 2019-07-01; each block's fit uses only matches that kicked off before the
block's first decision day. Results-based factors use only matches finished before the
decision day.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os

import numpy as np
import pandas as pd

from soccer_stats.lab.harness import Holdout, blocks
from soccer_stats.odds import devig_shin

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
SIDES = ("home", "draw", "away")
FIRST_ROWS = 2017  # training rows from 2017/18 (burn-in 2017/18-2018/19)
FIRST_SCORED = 2019  # scored 2019/20-2023/24
LAST_YEAR = 2023
MODEL_BURN = 2  # the Dixon-Coles model's own burn-in seasons before FIRST_ROWS
LOCK = pd.Timestamp("2024-07-01")
EARLY_LEAD = pd.Timedelta(days=4)
REFIT = "28D"
VALID_DAYS = 365
N_LAMBDA = 20
LAMBDA_RATIO = 1e-3
T_RULE = 0.03
N_BOOT = 2000
SEED = 0
FORM_N = 6
FORM_MIN = 3
REST_CAP = 14
EURO_DAYS = (1, 4)
NEW_FIRST = 10
CONG_BEFORE = 14  # days before kickoff, every competition
CONG_AFTER = 4  # days after kickoff, league matches only
BIG_MARGIN = 3
GAP_CLIP = 2.0
# Domestic cups counted in congestion, per league, and whether they are complete in the data
# (docs/lab.md "LASSO on the market residual", frozen 8 Nov; run 42462857463).
LEAGUE_CUPS = {
    "E0": ("FAC", "EFLC"), "E1": ("FAC", "EFLC"), "E2": ("FAC", "EFLC"), "E3": ("FAC", "EFLC"),
    "SP1": ("CDR",), "D1": ("DFBP",), "I1": ("CI",), "F1": ("CDF", "CDL"),
}  # fmt: skip
CUPS_COMPLETE = {"E0": 1, "E1": 1, "E2": 0, "E3": 0, "SP1": 0, "D1": 1, "I1": 1, "F1": 1}
CDL_LAST = 2019  # the Coupe de la Ligue's last season (2019/20)
TOL = 1e-9
MAX_ITER = 5000

FACTORS: dict[str, list[str]] = {
    "rest": ["home_rest", "away_rest", "rest_gap"],
    "europe": ["home_euro", "home_euro_away", "away_euro", "away_euro_away"],
    "xg_luck": ["home_luck", "away_luck"],
    "form_vs_market": ["home_fvm", "away_fvm"],
    "newcomer": ["home_new", "home_new_first10", "away_new", "away_new_first10"],
    "season_phase": ["phase_aug_sep", "phase_jan_feb", "phase_mar_jul"],
    "model_gap": ["gap_home", "gap_draw", "gap_away"],
    "price_shape": ["shape_home", "shape_away"],
    "motivation": [
        f"{side}_{x}"
        for side in ("home", "away")
        for x in ("gap_hi", "gap_mid", "gap_rel", "games_left", "nothing")
    ],
    "congestion": ["home_cong", "away_cong", "cups_covered"],
    "last_match": [
        f"{side}_{x}" for side in ("home", "away") for x in ("big_win", "big_loss", "shock")
    ],
    "red_card": ["home_red_last", "away_red_last"],
    "sot_form": ["home_sot", "away_sot"],
}
FEATURES = [f for fs in FACTORS.values() for f in fs]
FACTOR_OF = {f: k for k, fs in FACTORS.items() for f in fs}


# --- the L1 multinomial with an offset ---------------------------------------------------


def _probs(xa: np.ndarray, off: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Chances (n, 3) home/draw/away. xa = [1, X]; w (p+1, 2) = home and away columns."""
    s = off.copy()
    s[:, 0] += xa @ w[:, 0]
    s[:, 2] += xa @ w[:, 1]
    s -= s.max(axis=1, keepdims=True)
    e = np.exp(s)
    return e / e.sum(axis=1, keepdims=True)


def _loss(xa, off, y, w) -> float:
    p = _probs(xa, off, w)
    return float(-np.log(np.clip(p[np.arange(len(y)), y], 1e-15, None)).mean())


def _grad(xa, off, yh, w) -> np.ndarray:
    r = _probs(xa, off, w) - yh
    return xa.T @ r[:, [0, 2]] / len(xa)


def _soft(v: np.ndarray, t: float) -> np.ndarray:
    return np.sign(v) * np.maximum(np.abs(v) - t, 0.0)


def fit_l1(
    x: np.ndarray,
    off: np.ndarray,
    y: np.ndarray,
    lam: float,
    w0: np.ndarray | None = None,
    tol: float = TOL,
    max_iter: int = MAX_ITER,
) -> np.ndarray:
    """FISTA on mean log loss + lam * sum |b| (intercept row unpenalised). x standardised,
    off = log q (n, 3), y in {0, 1, 2}. Returns w (p+1, 2)."""
    n, p = x.shape
    xa = np.hstack([np.ones((n, 1)), x])
    yh = np.eye(3)[y]
    lip = 0.5 * np.linalg.norm(xa, 2) ** 2 / n + 1e-12
    step = 1.0 / lip
    w = np.zeros((p + 1, 2)) if w0 is None else w0.copy()
    z, t = w.copy(), 1.0
    obj = _loss(xa, off, y, w) + lam * np.abs(w[1:]).sum()
    for _ in range(max_iter):
        g = _grad(xa, off, yh, z)
        nw = z - step * g
        nw[1:] = _soft(nw[1:], step * lam)
        nt = (1 + np.sqrt(1 + 4 * t * t)) / 2
        z = nw + ((t - 1) / nt) * (nw - w)
        nobj = _loss(xa, off, y, nw) + lam * np.abs(nw[1:]).sum()
        if nobj > obj:  # restart the momentum on an uphill step
            z, nt = nw.copy(), 1.0
        done = abs(obj - nobj) < tol
        w, t, obj = nw, nt, nobj
        if done:
            break
    return w


def lambda_max(x: np.ndarray, off: np.ndarray, y: np.ndarray) -> float:
    """The smallest lambda that keeps no factor (intercepts fitted alone)."""
    w = fit_l1(np.zeros((len(x), 0)), off, y, 0.0)
    xa = np.hstack([np.ones((len(x), 1)), np.zeros((len(x), 0))])
    r = _probs(xa, off, w) - np.eye(3)[y]
    g = x.T @ r[:, [0, 2]] / len(x)
    return float(np.abs(g).max()) if g.size else 0.0


def lambda_path(lmax: float, n: int = N_LAMBDA, ratio: float = LAMBDA_RATIO) -> np.ndarray:
    return np.geomspace(max(lmax, 1e-8), max(lmax, 1e-8) * ratio, n)


class Scaler:
    """Training-rows mean and sd; a missing value becomes 0 (the mean); sd 0 -> column 0."""

    def __init__(self, x: np.ndarray):
        self.mu = np.nanmean(np.where(np.isfinite(x), x, np.nan), axis=0)
        self.mu = np.where(np.isfinite(self.mu), self.mu, 0.0)
        sd = np.nanstd(np.where(np.isfinite(x), x, np.nan), axis=0)
        self.sd = np.where(np.isfinite(sd) & (sd > 1e-12), sd, np.inf)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.mu) / self.sd
        return np.where(np.isfinite(z), z, 0.0)


def choose_lambda(train: pd.DataFrame, lo: pd.Timestamp, feats=FEATURES) -> dict:
    """Fit the path on rows before lo - VALID_DAYS, score it on the rest of the window; keep
    the best lambda and refit the whole window (scaled on the whole window)."""
    cut = lo - pd.Timedelta(days=VALID_DAYS)
    inner, valid = train[train["date"] < cut], train[train["date"] >= cut]
    if len(inner) < 100 or len(valid) < 50:
        raise ValueError("training window too short for the inner validation")
    sc = Scaler(inner[feats].to_numpy(float))
    xi, oi, yi = sc(inner[feats].to_numpy(float)), _off(inner), inner["y"].to_numpy(int)
    xv, ov, yv = sc(valid[feats].to_numpy(float)), _off(valid), valid["y"].to_numpy(int)
    lams = lambda_path(lambda_max(xi, oi, yi))
    w, scores = None, []
    xva = np.hstack([np.ones((len(xv), 1)), xv])
    for lam in lams:
        w = fit_l1(xi, oi, yi, lam, w)
        scores.append(_loss(xva, ov, yv, w))
    best = int(np.argmin(scores))
    sc_all = Scaler(train[feats].to_numpy(float))
    xa_, oa, ya = sc_all(train[feats].to_numpy(float)), _off(train), train["y"].to_numpy(int)
    lmax_all = lambda_max(xa_, oa, ya)
    w_all = None
    for lam in lams[: best + 1]:  # warm start down to the chosen lambda
        w_all = fit_l1(xa_, oa, ya, lam, w_all)
    return {
        "lam": float(lams[best]),
        "lam_index": best,
        "lam_max_inner": float(lams[0]),
        "lam_max_window": lmax_all,
        "valid_log_loss": [float(s) for s in scores],
        "w": w_all,
        "scaler": sc_all,
    }


def _off(df: pd.DataFrame) -> np.ndarray:
    return np.log(df[[f"q_{s}" for s in SIDES]].to_numpy(float))


def predict(fit: dict, df: pd.DataFrame, feats=FEATURES) -> np.ndarray:
    x = fit["scaler"](df[feats].to_numpy(float))
    xa = np.hstack([np.ones((len(x), 1)), x])
    return _probs(xa, _off(df), fit["w"])


def walk(rows: pd.DataFrame, start, end, holdout: Holdout | None = None, feats=FEATURES):
    """Out-of-sample chances for rows whose decision day is in [start, end), and one record
    per refit (lambda, coefficients). A block's fit uses matches kicked off before its first
    decision day."""
    rows = rows.sort_values("date", kind="stable")
    preds, refits = [], []
    for lo, hi in blocks(start, end, REFIT):
        hi = min(hi, pd.Timestamp(end))
        test = rows[(rows["day"] >= lo) & (rows["day"] < hi)]
        if test.empty:
            continue
        if holdout is not None:
            holdout.check(test["date"])
        train = rows[rows["date"] < lo]
        fit = choose_lambda(train, lo, feats)
        p = predict(fit, test, feats)
        preds.append(pd.DataFrame(p, index=test.index, columns=[f"p_l1_{s}" for s in SIDES]))
        coefs = {f: [float(fit["w"][i + 1, 0]), float(fit["w"][i + 1, 1])]
                 for i, f in enumerate(feats)}  # fmt: skip
        refits.append(
            {
                "block": str(lo.date()),
                "train": int(len(train)),
                "test": int(len(test)),
                "lam": fit["lam"],
                "lam_index": fit["lam_index"],
                "lam_max": fit["lam_max_inner"],
                "intercepts": [float(fit["w"][0, 0]), float(fit["w"][0, 1])],
                "coefs": coefs,
                "selected": sorted(
                    {FACTOR_OF.get(f, f) for f, c in coefs.items() if any(abs(v) > 0 for v in c)}
                ),  # fmt: skip
            }
        )
    out = pd.concat(preds) if preds else pd.DataFrame()
    return out, refits


# --- factors (no look-ahead) --------------------------------------------------------------


LONG_COLS = ("fvm", "luck", "big_win", "big_loss", "red", "sot")


def _team_long(m: pd.DataFrame) -> pd.DataFrame:
    """One row per (match, team) with kickoff date, points minus expected points, xG luck,
    a 3+ goal win or loss, a red card (0/1) and shots-on-target share minus the price's
    expected share (win + draw / 2)."""
    parts = []
    for side, other in (("home", "away"), ("away", "home")):
        gf, ga = m[f"{side}_goals"].astype(float), m[f"{other}_goals"].astype(float)
        pts = np.where(gf > ga, 3.0, np.where(gf == ga, 1.0, 0.0))
        xpts = 3 * m[f"pre_{side}"] + m["pre_draw"]
        sf, sa = m[f"{side}_hst"].astype(float), m[f"{other}_hst"].astype(float)
        tot = (sf + sa).to_numpy(float)
        share = np.divide(sf.to_numpy(float), tot, out=np.full(len(m), np.nan), where=tot > 0)
        red = m[f"{side}_red"].astype(float).to_numpy()
        parts.append(
            pd.DataFrame(
                {
                    "team": m[side].to_numpy(),
                    "date": m["date"].to_numpy(),
                    "fvm": pts - xpts.to_numpy(float),
                    "luck": ((m[f"{side}_xg"] - m[f"{other}_xg"]) - (gf - ga)).to_numpy(float),
                    "big_win": (gf - ga >= BIG_MARGIN).astype(float).to_numpy(),
                    "big_loss": (ga - gf >= BIG_MARGIN).astype(float).to_numpy(),
                    "red": np.where(np.isfinite(red), (red > 0).astype(float), np.nan),
                    "sot": share - (m[f"pre_{side}"] + m["pre_draw"] / 2).to_numpy(float),
                }
            )
        )
    return pd.concat(parts, ignore_index=True).sort_values(["team", "date"], kind="stable")


def _results_before(long: pd.DataFrame, col: str, team, day) -> float:
    """Mean of `col` over the team's last FORM_N matches kicked off before `day` (>= FORM_MIN
    finite values), else NaN."""
    g = long.get(team)
    if g is None:
        return np.nan
    dates, vals = g
    i = int(np.searchsorted(dates, np.datetime64(day), side="left"))
    v = vals[col][max(0, i - FORM_N) : i]
    v = v[np.isfinite(v)]
    return float(v.mean()) if len(v) >= FORM_MIN else np.nan


def expected_points_prices(m: pd.DataFrame) -> pd.DataFrame:
    """pre_home/pre_draw/pre_away: Pinnacle's Shin-fair close, else its early price."""
    out = pd.DataFrame(index=m.index, columns=["pre_home", "pre_draw", "pre_away"], dtype=float)
    for cols in (
        ("odds_home", "odds_draw", "odds_away"),
        ("close_home", "close_draw", "close_away"),
    ):
        if not set(cols) <= set(m.columns):
            continue
        v = m[list(cols)].to_numpy(float)
        ok = (np.isfinite(v) & (v > 1)).all(axis=1)
        if ok.any():
            fair = np.vstack([devig_shin(r) for r in v[ok]])
            out.loc[m.index[ok], ["pre_home", "pre_draw", "pre_away"]] = fair
    return out


def newcomer_flags(m: pd.DataFrame) -> dict[tuple[str, str, str], bool]:
    """(league, season, team) -> True for a team not in the league the season before (only
    for seasons whose previous one is loaded)."""
    members = {
        (lg, str(s)): set(g["home"]) | set(g["away"])
        for (lg, s), g in m.groupby(["league", m["season"].astype(str)])
    }
    out = {}
    for (lg, s), teams in members.items():
        y = 2000 + int(s[:2])
        prev = f"{(y - 1) % 100:02d}{y % 100:02d}"
        if (lg, prev) not in members:
            continue
        for t in teams:
            out[(lg, s, t)] = t not in members[(lg, prev)]
    return out


def euro_trips(events: list[dict], mapping: dict) -> dict[str, list[tuple]]:
    """'<league>:<team>' -> sorted [(day, played away)] of its UEFA matches."""
    out: dict[str, list] = {}
    for e in events:
        k = e.get("kickoff")
        if k is None:
            continue
        ts = pd.Timestamp(k)
        day = (ts.tz_convert(None) if ts.tzinfo else ts).normalize()
        for side in ("home", "away"):
            key = mapping.get(e.get(side))
            if key:
                away = side == "away" and not e.get("neutral")
                out.setdefault(key, []).append((day, bool(away)))
    return {k: sorted(v) for k, v in out.items()}


def _euro(trips, day) -> tuple[float, float]:
    hit = [a for d, a in (trips or []) if EURO_DAYS[0] <= (day - d).days <= EURO_DAYS[1]]
    return (1.0 if hit else 0.0, 1.0 if any(hit) else 0.0)


def places(league: str, season: str) -> tuple[int, int, int, int]:
    """The frozen place table: (clubs, gap_hi place, gap_mid place, first relegation place)."""
    s = str(season)
    if league in ("E0", "SP1", "I1"):
        return (20, 4, 6, 18)
    if league == "D1":
        return (18, 4, 6, 16)
    if league == "F1":
        if s == "2223":
            return (20, 4, 6, 17)
        return (18, 4, 6, 16) if s >= "2324" else (20, 4, 6, 18)
    if league == "E1":
        return (24, 2, 6, 22)
    if league == "E2":
        return (23 if s == "1920" else 24, 2, 6, 21)
    if league == "E3":
        return (24, 3, 7, 23)
    raise KeyError(league)


def table_features(m: pd.DataFrame) -> dict[tuple, dict[str, float]]:
    """(row index, side) -> the motivation features, from the league table of the row's
    season built from league results kicked off before its decision day (m['day'])."""
    out: dict[tuple, dict[str, float]] = {}
    for (lg, s), g in m.groupby(["league", m["season"].astype(str)], sort=False):
        n, hi, mid, rel = places(lg, s)
        g = g.sort_values("date", kind="stable")
        dates = g["date"].to_numpy("datetime64[ns]")
        hs, as_ = g["home"].to_numpy(), g["away"].to_numpy()
        hg, ag = g["home_goals"].to_numpy(float), g["away_goals"].to_numpy(float)
        teams = sorted(set(hs) | set(as_))
        pts = dict.fromkeys(teams, 0.0)
        gd = dict.fromkeys(teams, 0.0)
        gf = dict.fromkeys(teams, 0.0)
        pl = dict.fromkeys(teams, 0)
        j = 0
        for day, sub in g.groupby("day", sort=True):
            cut = np.datetime64(day)
            while j < len(g) and dates[j] < cut:
                if np.isfinite(hg[j]) and np.isfinite(ag[j]):
                    h, a = hs[j], as_[j]
                    ph = 3.0 if hg[j] > ag[j] else (1.0 if hg[j] == ag[j] else 0.0)
                    pa = 3.0 if ag[j] > hg[j] else (1.0 if hg[j] == ag[j] else 0.0)
                    pts[h] += ph
                    pts[a] += pa
                    gd[h] += hg[j] - ag[j]
                    gd[a] += ag[j] - hg[j]
                    gf[h] += hg[j]
                    gf[a] += ag[j]
                    pl[h] += 1
                    pl[a] += 1
                j += 1
            ranked = sorted(teams, key=lambda t: (-pts[t], -gd[t], -gf[t], t))
            pos = {t: i + 1 for i, t in enumerate(ranked)}

            def at(place, ranked=ranked, pts=pts):
                return pts[ranked[min(place, len(ranked)) - 1]]

            for idx, h, a in zip(sub.index, sub["home"], sub["away"], strict=True):
                for side, t in (("home", h), ("away", a)):
                    if pl[t] < FORM_MIN:
                        out[(idx, side)] = dict.fromkeys(
                            ("gap_hi", "gap_mid", "gap_rel", "games_left", "nothing"), np.nan
                        )
                        continue
                    left = max(2 * (n - 1) - pl[t], 0)
                    den = 3.0 * left + 3.0

                    def gap(place, t=t, den=den, pts=pts):
                        return float(np.clip((pts[t] - at(place)) / den, -GAP_CLIP, GAP_CLIP))

                    nothing = (pos[t] > mid and pts[t] + 3 * left < at(mid)
                               and pts[t] - at(rel) > 3 * left)  # fmt: skip
                    out[(idx, side)] = {"gap_hi": gap(hi), "gap_mid": gap(mid),
                                        "gap_rel": gap(rel), "games_left": float(left),
                                        "nothing": float(nothing)}  # fmt: skip
    return out


def _last_before(long: dict, col: str, team, day) -> float:
    """`col` of the team's last match kicked off before `day`, else NaN."""
    g = long.get(team)
    if g is None:
        return np.nan
    dates, vals = g
    i = int(np.searchsorted(dates, np.datetime64(day), side="left"))
    return float(vals[col][i - 1]) if i > 0 else np.nan


def _count(days: np.ndarray, lo, hi, closed: str = "left") -> int:
    """Days in [lo, hi) (closed='left') or (lo, hi] (closed='right')."""
    if days is None or not len(days):
        return 0
    lo, hi = np.datetime64(lo), np.datetime64(hi)
    side_lo, side_hi = ("left", "left") if closed == "left" else ("right", "right")
    return int(np.searchsorted(days, hi, side=side_hi) - np.searchsorted(days, lo, side=side_lo))


def build_factors(m: pd.DataFrame, euro: dict | None = None, cups: dict | None = None,
                  cups_incomplete: set | None = None) -> pd.DataFrame:  # fmt: skip
    """Every FEATURES column for every match in `m` (every loaded league match: league,
    season, date, home, away, goals, xG (NaN without), Pinnacle early odds_*/close_*, and
    q_* (Shin-fair early) and p_* (model; NaN without)). Results-based factors use matches
    kicked off before the decision day (date - EARLY_LEAD) only. `cups`: football-data name ->
    sorted cup-match days (None = no cup data: cups_covered 0); `cups_incomplete`:
    (league, season) whose cup fetch did not complete (cups_covered 0 there)."""
    m = m.copy()
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    m["day"] = m["date"] - EARLY_LEAD
    m = m.reset_index(drop=True)
    m = pd.concat([m.drop(columns=["pre_home", "pre_draw", "pre_away"], errors="ignore"),
                   expected_points_prices(m)], axis=1)  # fmt: skip
    for c in ("home_xg", "away_xg", "home_hst", "away_hst", "home_red", "away_red"):
        if c not in m:
            m[c] = np.nan
    long = _team_long(m)
    by_team = {
        t: (
            g["date"].to_numpy("datetime64[ns]"),
            {c: g[c].to_numpy(float) for c in LONG_COLS},
        )
        for t, g in long.groupby("team", sort=False)
    }
    table = table_features(m)
    euro_days = {
        k: np.array(sorted(d for d, _ in v), dtype="datetime64[ns]")
        for k, v in (euro or {}).items()
    }
    cup_days = {k: np.array(sorted(v), dtype="datetime64[ns]") for k, v in (cups or {}).items()}
    incomplete = cups_incomplete or set()
    # League schedule for rest days: every league match's date per team (fixtures known).
    sched = {
        t: np.sort(np.concatenate([m.loc[m["home"] == t, "date"].to_numpy("datetime64[ns]"),
                                   m.loc[m["away"] == t, "date"].to_numpy("datetime64[ns]")]))
        for t in set(m["home"]) | set(m["away"])
    }  # fmt: skip
    new = newcomer_flags(m)
    order = m.sort_values("date", kind="stable")
    nth: dict[tuple, int] = {}
    count: dict[tuple, int] = {}
    for idx, lg, s, h, a in zip(order.index, order["league"], order["season"].astype(str),
                                order["home"], order["away"], strict=True):  # fmt: skip
        for side, t in (("home", h), ("away", a)):
            k = (lg, s, t)
            nth[(idx, side)] = count.get(k, 0)
            count[k] = count.get(k, 0) + 1
    rec = []
    for i, r in m.iterrows():
        f: dict[str, float] = {}
        for side in ("home", "away"):
            t = r[side]
            d = sched.get(t, np.array([], dtype="datetime64[ns]"))
            j = int(np.searchsorted(d, np.datetime64(r["date"]), side="left"))
            rest = min((r["date"] - pd.Timestamp(d[j - 1])).days, REST_CAP) if j > 0 else REST_CAP
            f[f"{side}_rest"] = float(rest)
            f[f"{side}_luck"] = _results_before(by_team, "luck", t, r["day"])
            f[f"{side}_fvm"] = _results_before(by_team, "fvm", t, r["day"])
            isnew = new.get((r["league"], str(r["season"]), t))
            f[f"{side}_new"] = np.nan if isnew is None else float(isnew)
            f[f"{side}_new_first10"] = (
                np.nan if isnew is None else float(isnew and nth[(i, side)] < NEW_FIRST)
            )
            if euro is None:
                f[f"{side}_euro"], f[f"{side}_euro_away"] = np.nan, np.nan
            else:
                f[f"{side}_euro"], f[f"{side}_euro_away"] = _euro(
                    euro.get(f"{r['league']}:{t}"), r["date"]
                )
            for k, v in table[(i, side)].items():
                f[f"{side}_{k}"] = v
            ko = r["date"]
            lo, hi = ko - pd.Timedelta(days=CONG_BEFORE), ko + pd.Timedelta(days=CONG_AFTER)
            cong = _count(d, lo, ko) + _count(d, ko, hi, closed="right")
            cong += _count(euro_days.get(f"{r['league']}:{t}"), lo, ko)
            if cups is not None:
                cong += _count(cup_days.get(t), lo, ko)
            f[f"{side}_cong"] = float(cong)
            f[f"{side}_big_win"] = _last_before(by_team, "big_win", t, r["day"])
            f[f"{side}_big_loss"] = _last_before(by_team, "big_loss", t, r["day"])
            f[f"{side}_shock"] = _last_before(by_team, "fvm", t, r["day"])
            f[f"{side}_red_last"] = _last_before(by_team, "red", t, r["day"])
            f[f"{side}_sot"] = _results_before(by_team, "sot", t, r["day"])
        covered = cups is not None and CUPS_COMPLETE.get(r["league"], 0)
        f["cups_covered"] = float(
            bool(covered) and (r["league"], str(r["season"])) not in incomplete
        )
        f["rest_gap"] = f["home_rest"] - f["away_rest"]
        mo = r["date"].month
        f["phase_aug_sep"] = float(mo in (8, 9))
        f["phase_jan_feb"] = float(mo in (1, 2))
        f["phase_mar_jul"] = float(mo in (3, 4, 5, 6, 7))
        rec.append(f)
    fx = pd.DataFrame(rec, index=m.index)
    for s in SIDES:
        fx[f"gap_{s}"] = np.log(m[f"p_{s}"].astype(float)) - np.log(m[f"q_{s}"].astype(float))
    fx["shape_home"] = np.log(m["q_home"] / m["q_draw"])
    fx["shape_away"] = np.log(m["q_away"] / m["q_draw"])
    return pd.concat([m, fx[FEATURES]], axis=1)


def add_early_fair(m: pd.DataFrame) -> pd.DataFrame:
    """q_* (Shin-fair early) and fair_close_* where the prices are full, else NaN."""
    m = m.copy()
    for prefix, cols in (("q", ("odds_home", "odds_draw", "odds_away")),
                         ("fair_close", ("close_home", "close_draw", "close_away"))):  # fmt: skip
        v = (
            m[list(cols)].to_numpy(float)
            if set(cols) <= set(m.columns)
            else np.full((len(m), 3), np.nan)
        )
        ok = (np.isfinite(v) & (v > 1)).all(axis=1)
        out = np.full((len(m), 3), np.nan)
        if ok.any():
            out[ok] = np.vstack([devig_shin(r) for r in v[ok]])
        for k, s in enumerate(SIDES):
            m[f"{prefix}_{s}"] = out[:, k]
    return m


# --- the development read -------------------------------------------------------------------


def boot_mean(v: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> list[float] | None:
    v = np.asarray(v, float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), (n_boot, len(v)))].mean(axis=1)
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    return -np.log(np.clip(p[np.arange(len(y)), y], 1e-15, None))


def ll_block(df: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    y = df["y"].to_numpy(int)
    q = df[[f"q_{s}" for s in SIDES]].to_numpy(float)
    p = df[[f"p_l1_{s}" for s in SIDES]].to_numpy(float)
    diff = _ll(q, y) - _ll(p, y)
    out = {
        "matches": int(len(df)),
        "log_loss_price": float(_ll(q, y).mean()),
        "log_loss_l1": float(_ll(p, y).mean()),
        "improvement": float(diff.mean()),
        "improvement_ci95": boot_mean(diff, n_boot),
    }
    c = df[[f"fair_close_{s}" for s in SIDES]].to_numpy(float)
    ok = np.isfinite(c).all(axis=1)
    if ok.any():
        out["close_matches"] = int(ok.sum())
        out["log_loss_close"] = float(_ll(c[ok], y[ok]).mean())
        out["log_loss_price_same"] = float(_ll(q[ok], y[ok]).mean())
    return out


def rule_bets(df: pd.DataFrame, t: float = T_RULE) -> pd.DataFrame:
    """One bet per match: the outcome with the largest p_l1 x early odds - 1, if > t."""
    odds = df[["odds_home", "odds_draw", "odds_away"]].to_numpy(float)
    p = df[[f"p_l1_{s}" for s in SIDES]].to_numpy(float)
    edge = p * odds - 1
    k = np.argmax(edge, axis=1)
    i = np.arange(len(df))
    keep = edge[i, k] > t
    close = df[[f"fair_close_{s}" for s in SIDES]].to_numpy(float)
    bets = pd.DataFrame(
        {
            "league": df["league"].to_numpy()[keep],
            "date": df["date"].to_numpy()[keep],
            "side": np.array(SIDES)[k[keep]],
            "odds": odds[i, k][keep],
            "edge": edge[i, k][keep],
            "won": (df["y"].to_numpy(int)[keep] == k[keep]).astype(float),
            "clv": (odds[i, k] * close[i, k] - 1)[keep],
        }
    )
    bets["profit"] = np.where(bets["won"] == 1, bets["odds"] - 1, -1.0)
    return bets


def rule_block(bets: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    clv = bets["clv"].to_numpy(float)
    ok = np.isfinite(clv)
    return {
        "threshold": T_RULE,
        "bets": int(len(bets)),
        "won": int(bets["won"].sum()) if len(bets) else 0,
        "avg_odds": float(bets["odds"].mean()) if len(bets) else None,
        "avg_edge": float(bets["edge"].mean()) if len(bets) else None,
        "clv_bets": int(ok.sum()),
        "clv": float(clv[ok].mean()) if ok.any() else None,
        "clv_ci95": boot_mean(clv[ok], n_boot),
        "roi": float(bets["profit"].mean()) if len(bets) else None,
        "roi_ci95": boot_mean(bets["profit"].to_numpy(float), n_boot),
    }


def selection(refits: list[dict]) -> dict:
    n = len(refits)
    fac = {k: sum(k in r["selected"] for r in refits) / n if n else 0.0 for k in FACTORS}
    feat = {
        f: (
            sum(any(abs(v) > 0 for v in r["coefs"].get(f, [0, 0])) for r in refits) / n
            if n
            else 0.0
        )
        for f in FEATURES
    }
    return {"refits": n, "factors": fac, "features": feat}


def gate(pooled: dict, sel: dict) -> dict:
    ci = pooled.get("improvement_ci95")
    ll_ok = bool(ci and ci[0] > 0)
    most = [k for k, v in sel["factors"].items() if v > 0.5]
    ok = ll_ok and bool(most)
    return {
        "log_loss_range_above_0": ll_ok,
        "factors_in_most_refits": most,
        "passes": ok,
        "verdict": (
            "gate met: the lead asks the owner to open 2024/25 for one judged read"
            if ok
            else "no edge in these factors: the study closes, the 2024/25 holdout is not spent"
        ),
    }


def report(scored: pd.DataFrame, refits: list[dict], n_boot: int = N_BOOT) -> dict:
    pooled = ll_block(scored, n_boot)
    sel = selection(refits)
    bets = rule_bets(scored)
    by_league = {lg: ll_block(g, n_boot) for lg, g in scored.groupby("league")}
    by_season = {str(s): ll_block(g, n_boot) for s, g in scored.groupby("season")}
    rb = rule_block(bets, n_boot)
    rb["by_league"] = {lg: rule_block(g, n_boot) for lg, g in bets.groupby("league")}
    return {
        "kind": "lasso_residual",
        "development": True,
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seasons": sorted(scored["season"].astype(str).unique().tolist()),
        "locked_from": str(LOCK.date()),
        "factors": FACTORS,
        "log_loss": pooled,
        "log_loss_by_league": by_league,
        "log_loss_by_season": by_season,
        "selection": sel,
        "rule": rb,
        "gate": gate(pooled, sel),
        "refits": refits,
    }


def summary_lines(res: dict) -> list[str]:
    ll = res["log_loss"]
    ci = ll["improvement_ci95"] or [float("nan")] * 2
    out = [
        f"LASSO residual: {ll['matches']} matches scored, {res['selection']['refits']} refits",
        f"Log loss: price {ll['log_loss_price']:.5f}, L1 {ll['log_loss_l1']:.5f}, "
        f"improvement {ll['improvement']:+.5f} ({ci[0]:+.5f} to {ci[1]:+.5f})"
        + (f"; close {ll['log_loss_close']:.5f}" if "log_loss_close" in ll else ""),
    ]
    for lg, b in res["log_loss_by_league"].items():
        c = b["improvement_ci95"] or [float("nan")] * 2
        out.append(
            f"  {lg}: {b['matches']} matches, {b['improvement']:+.5f} ({c[0]:+.5f} to {c[1]:+.5f})"
        )
    fac = sorted(res["selection"]["factors"].items(), key=lambda kv: -kv[1])
    out.append("Selected (share of refits): " + ", ".join(f"{k} {v:.0%}" for k, v in fac))
    r = res["rule"]
    if r["bets"]:
        cc = r["clv_ci95"] or [float("nan")] * 2
        out.append(
            f"Rule T={r['threshold']}: {r['bets']} bets, CLV {r['clv']:+.4f} ({cc[0]:+.4f} to "
            f"{cc[1]:+.4f}), ROI {r['roi']:+.4f}"
        )
    else:
        out.append(f"Rule T={r['threshold']}: no bets")
    out.append(f"Gate: {res['gate']['verdict']}")
    return out


# --- loading (Actions only: football-data, Understat, ESPN) --------------------------------


def load_league(code: str, holdout: Holdout, first: int = FIRST_ROWS, last: int = LAST_YEAR):
    """Every league match from first - MODEL_BURN with goals, xG, Pinnacle's early/close
    and the walk-forward model chance from first-07-01 (NaN before or without one)."""
    from soccer_stats import backtest
    from soccer_stats import leagues as lgs
    from soccer_stats import lean_backtest as lb
    from soccer_stats.data import load_matches, pinnacle_prices
    from soccer_stats.models import DixonColes
    from soccer_stats.publish import XG_WEIGHT
    from soccer_stats.xg import with_xg

    years = range(first - MODEL_BURN, last + 1)
    matches = load_matches([code], years)
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
    pins = pinnacle_prices(code, years)
    holdout.check(pins["date"])
    keys = ["season", "home", "away"]
    base = matches.drop(
        columns=[c for c in matches.columns if c.startswith(("odds_", "close_"))]
    ).copy()
    base["season"] = base["season"].astype(str)
    pins["season"] = pins["season"].astype(str)
    base = base.merge(pins.drop(columns="date").drop_duplicates(keys), on=keys, how="left")
    if len(preds):
        pr = preds[[*keys, "p_home", "p_draw", "p_away"]].copy()
        pr["season"] = pr["season"].astype(str)
        base = base.merge(pr.drop_duplicates(keys), on=keys, how="left")
    else:
        for s in SIDES:
            base[f"p_{s}"] = np.nan
    extra = fd_extra(code, years)
    base = base.merge(extra, on=keys, how="left")
    base["league"] = code
    cov = {c["season"]: c for c in lb.coverage(pins)}
    return base, {"xg_weight": weight, "matches": int(len(base)),
                  "model_chances": int(base["p_home"].notna().sum()),
                  "shots_on_target": int(base["home_hst"].notna().sum()),
                  "red_cards": int(base["home_red"].notna().sum()),
                  "coverage": cov}  # fmt: skip


def fd_extra(code: str, years) -> pd.DataFrame:
    """Shots on target (HST/AST) and red cards (HR/AR) from football-data's own files, keyed
    by season, home, away (the shared data.COLUMNS is left alone)."""
    from soccer_stats.data import download, season_code

    parts = []
    for y in years:
        raw = pd.read_csv(download(code, y), encoding="latin-1", on_bad_lines="skip")
        raw = raw.dropna(subset=[c for c in ("HomeTeam", "AwayTeam", "FTHG") if c in raw])
        out = pd.DataFrame(
            {"season": season_code(y), "home": raw["HomeTeam"], "away": raw["AwayTeam"]}
        )
        for col, name in (("HST", "home_hst"), ("AST", "away_hst"), ("HR", "home_red"),
                          ("AR", "away_red")):  # fmt: skip
            out[name] = pd.to_numeric(raw[col], errors="coerce") if col in raw else np.nan
        parts.append(out)
    return pd.concat(parts, ignore_index=True).drop_duplicates(["season", "home", "away"])


def cup_days(events: list[dict], teams: dict[str, set], match, cutoff) -> tuple[dict, int]:
    """football-data name -> [cup-match day] for every event side `match` maps to a club of
    `teams` ({league: names}); events at or after `cutoff` are left out."""
    out: dict[str, list] = {}
    seen: dict[str, str | None] = {}
    n = 0
    for e in events:
        k = e.get("kickoff")
        if k is None:
            continue
        ts = pd.Timestamp(k)
        if ts >= cutoff:
            continue
        day = (ts.tz_convert(None) if ts.tzinfo else ts).normalize()
        for side in ("home", "away"):
            name = e.get(side)
            if not name:
                continue
            if name not in seen:
                mt = match(name, teams)
                seen[name] = mt["name"] if mt else None
            if seen[name]:
                out.setdefault(seen[name], []).append(day)
                n += 1
    return out, n


def load_cups(m: pd.DataFrame, first: int, last: int, seconds: float = 100 * 60):
    """Domestic-cup match days per club (LEAGUE_CUPS), the (league, season) pairs whose cup
    fetch did not complete, and one report row per cup-season."""
    import time

    from soccer_stats import cups as cupmod
    from soccer_stats.data import season_code

    stop = time.time() + seconds
    cutoff = pd.Timestamp(LOCK, tz="UTC")
    teams = {lg: set(g["home"]) | set(g["away"]) for lg, g in m.groupby("league")}
    days: dict[str, list] = {}
    incomplete: set = set()
    rep = []
    for code, (slug, lgs) in DOMESTIC_CUPS.items():
        ours = [lg for lg in lgs if lg in teams and code in LEAGUE_CUPS.get(lg, ())]
        if not ours:
            continue
        tm = {lg: teams[lg] for lg in ours}
        for y in range(first, last + 1):
            if code == "CDL" and y > CDL_LAST:
                continue
            row: dict = {"cup": code, "season": season_code(y)}
            if time.time() > stop:
                row["error"] = "time budget"
            else:
                stats: dict = {}
                try:
                    cal = cupmod.season_calendar(
                        slug, pd.Timestamp(f"{y}-07-01", tz="UTC"),
                        pd.Timestamp(f"{y + 1}-07-01", tz="UTC"), stats=stats, today=cutoff,
                    )  # fmt: skip
                    got, n = cup_days(cal["events"], tm, cupmod.match_team, cutoff)
                    for k, v in got.items():
                        days.setdefault(k, []).extend(v)
                    row.update({"events": len(cal["events"]), "club_matches": n,
                                "failures": stats.get("failures", 0)})  # fmt: skip
                    if stats.get("failures", 0):
                        row["error"] = "fetch failures"
                except Exception as exc:  # noqa: BLE001 - that league-season loses its cups
                    row["error"] = type(exc).__name__
            if "error" in row:
                incomplete.update((lg, row["season"]) for lg in ours)
            rep.append(row)
    return {k: sorted(set(v)) for k, v in days.items()}, incomplete, rep


def load_euro(matches: pd.DataFrame, first: int, last: int, cutoff: pd.Timestamp):
    from soccer_stats import cup_model

    years = list(range(first - 1, last + 1))
    events, rep = cup_model.european_events(years, until=cutoff, today=cutoff)
    events = [e for e in events if pd.Timestamp(e["kickoff"]) < cutoff]
    teams = {lg: set(g["home"]) | set(g["away"]) for lg, g in matches.groupby("league")}
    names = sorted({e[s] for e in events for s in ("home", "away") if e.get(s)})
    mapping = cup_model.team_map(names, teams)
    return euro_trips(events, mapping), {
        "events": len(events),
        "complete": rep.get("complete"),
        "clubs": len(names),
        "clubs_mapped": sum(1 for v in mapping.values() if v),
    }


def scored_rows(m: pd.DataFrame) -> pd.DataFrame:
    """Rows with a full early price from FIRST_ROWS on, with y."""
    m = m[pd.to_datetime(m["date"]) >= pd.Timestamp(f"{FIRST_ROWS}-07-01")]
    m = m[np.isfinite(m[[f"q_{s}" for s in SIDES]].to_numpy(float)).all(axis=1)].copy()
    hg, ag = m["home_goals"].astype(int), m["away_goals"].astype(int)
    m["y"] = np.where(hg > ag, 0, np.where(hg == ag, 1, 2))
    return m.reset_index(drop=True)


# --- coverage (counts only: no model, no fit; for agreeing the factor list) ---------------

FD_COLUMNS = ("Referee", "Time", "HS", "AS", "HST", "AST", "HC", "AC", "HF", "AF", "HY", "AY",
              "HR", "AR", "HTHG", "HTAG", "PSH", "PSCH")  # fmt: skip
DOMESTIC_CUPS = {
    "FAC": ("eng.fa", ("E0", "E1", "E2", "E3")),
    "EFLC": ("eng.league_cup", ("E0", "E1", "E2", "E3")),
    "EFLT": ("eng.league_trophy", ("E2", "E3")),
    "CDR": ("esp.copa_del_rey", ("SP1",)),
    "DFBP": ("ger.dfb_pokal", ("D1",)),
    "CI": ("ita.coppa_italia", ("I1",)),
    "CDF": ("fra.coupe_de_france", ("F1",)),
    "CDL": ("fra.coupe_de_la_ligue", ("F1",)),
}


def fd_coverage(code: str, years) -> list[dict]:
    """Per season: matches, and the share of matches with each FD_COLUMNS value present."""
    from soccer_stats.data import download

    out = []
    for y in years:
        raw = pd.read_csv(download(code, y), encoding="latin-1", on_bad_lines="skip")
        raw = raw.dropna(subset=[c for c in ("HomeTeam", "AwayTeam", "FTHG") if c in raw])
        row = {"league": code, "season": f"{y % 100:02d}{(y + 1) % 100:02d}", "matches": len(raw)}
        for c in FD_COLUMNS:
            if c not in raw:
                row[c] = 0.0
                continue
            v = raw[c]
            ok = v.notna() & (v.astype(str).str.strip() != "")
            row[c] = round(float(ok.mean()), 3) if len(raw) else 0.0
        out.append(row)
    return out


def cup_coverage(matches: pd.DataFrame, years, seconds: float = 1500, only=None) -> list[dict]:
    """Per domestic cup and season: ESPN events and those with a club of its leagues."""
    import time

    from soccer_stats import cups

    stop = time.time() + seconds
    teams = {lg: set(g["home"]) | set(g["away"]) for lg, g in matches.groupby("league")}
    out = []
    for code, (slug, lgs) in DOMESTIC_CUPS.items():
        if only and code not in only:
            continue
        tm = {lg: t for lg, t in teams.items() if lg in lgs}
        stats: dict = {}
        for y in years:
            if time.time() > stop:
                out.append({"cup": code, "season": y, "error": "time budget"})
                continue
            try:
                cal = cups.season_calendar(slug, pd.Timestamp(f"{y}-07-01", tz="UTC"),
                                           pd.Timestamp(f"{y + 1}-07-01", tz="UTC"), stats=stats,
                                           today=pd.Timestamp(LOCK, tz="UTC"))  # fmt: skip
            except Exception as exc:  # noqa: BLE001 - reported
                out.append({"cup": code, "season": y, "error": type(exc).__name__})
                continue
            evs = cal["events"]
            names = {e[s] for e in evs for s in ("home", "away") if e.get(s)}
            mapped = {n for n in names if cups.match_team(n, tm)}
            with_club = sum(1 for e in evs if e.get("home") in mapped or e.get("away") in mapped)
            out.append({"cup": code, "season": y, "events": len(evs), "clubs": len(names),
                        "clubs_mapped": len(mapped), "events_with_club": with_club,
                        "mode": cal.get("mode"), "failures": cal.get("failures")})  # fmt: skip
    return out


def cups_only(codes: list[str], cups: list[str], seconds: float = 100 * 60) -> None:
    """Domestic-cup coverage alone (football-data names for the mapping; no xG, no UEFA)."""
    from soccer_stats.data import load_matches

    years = range(FIRST_ROWS - 1, LAST_YEAR + 1)
    m = pd.concat([load_matches([c], years).assign(league=c) for c in codes], ignore_index=True)
    m = m[pd.to_datetime(m["date"]) < LOCK]
    for r in cup_coverage(m, list(years), seconds, cups):
        print("COVERAGE_CUP " + json.dumps(r), flush=True)


def coverage_main(codes: list[str]) -> None:
    from soccer_stats.data import load_matches
    from soccer_stats.xg import with_xg

    years = range(FIRST_ROWS - 1, LAST_YEAR + 1)
    fd, mats = [], []
    for code in codes:
        try:
            fd += fd_coverage(code, years)
            m = load_matches([code], years)
            m, _ = with_xg(m)
            m["league"] = code
            mats.append(m)
            xg = m.groupby(m["season"].astype(str))["home_xg"].apply(
                lambda v: round(float(v.notna().mean()), 3)
            )
            print(f"COVERAGE_XG {code} {xg.to_dict()}", flush=True)
        except Exception as exc:  # noqa: BLE001 - reported
            print(f"COVERAGE_ERROR {code} {type(exc).__name__}: {exc}"[:300], flush=True)
    for r in fd:
        print("COVERAGE_FD " + json.dumps(r), flush=True)
    m = pd.concat(mats, ignore_index=True)
    m = m[pd.to_datetime(m["date"]) < LOCK]
    try:
        euro, rep = load_euro(m, FIRST_ROWS, LAST_YEAR, pd.Timestamp(LOCK, tz="UTC"))
        per = {}
        for key, trips in euro.items():
            lg = key.split(":", 1)[0]
            for d, _ in trips:
                s = (
                    f"{d.year % 100:02d}{(d.year + 1) % 100:02d}"
                    if d.month >= 7
                    else f"{(d.year - 1) % 100:02d}{d.year % 100:02d}"
                )
                per.setdefault(lg, {}).setdefault(s, 0)
                per[lg][s] += 1
        print("COVERAGE_UEFA " + json.dumps({"report": rep, "club_matches": per}), flush=True)
    except Exception as exc:  # noqa: BLE001 - reported
        print(f"COVERAGE_UEFA error {type(exc).__name__}: {exc}"[:300], flush=True)
    for r in cup_coverage(m, list(years)):
        print("COVERAGE_CUP " + json.dumps(r), flush=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default=",".join(LEAGUES), help="comma list of codes")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    p.add_argument("--no-europe", action="store_true", help="skip the ESPN UEFA results")
    p.add_argument("--no-cups", action="store_true", help="skip the domestic cups (flag 0)")
    p.add_argument("--json", default="", help="write the result here too")
    p.add_argument("--coverage", action="store_true", help="counts only: no model is fitted")
    p.add_argument("--cups", default="", help="coverage of these domestic cups only (counts)")
    args = p.parse_args(argv)
    codes = [c.strip() for c in args.leagues.split(",") if c.strip()]
    if args.cups:
        cups_only(codes, [c.strip() for c in args.cups.split(",") if c.strip()])
        return
    if args.coverage:
        coverage_main(codes)
        return
    holdout = Holdout(LOCK)
    frames, reps = [], {}
    for code in codes:
        try:
            base, rep = load_league(code, holdout)
            frames.append(base)
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        reps[code] = rep
        print(f"{code}: { {k: v for k, v in rep.items() if k != 'coverage'} }", flush=True)
    m = pd.concat(frames, ignore_index=True)
    m = m[pd.to_datetime(m["date"]) < LOCK]
    holdout.check(m["date"])
    euro, euro_rep = None, None
    if not args.no_europe:
        try:
            euro, euro_rep = load_euro(m, FIRST_ROWS, LAST_YEAR, pd.Timestamp(LOCK, tz="UTC"))
        except Exception as exc:  # noqa: BLE001 - the factor is then missing (0)
            euro_rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        print(f"Europe: {euro_rep}", flush=True)
    cup_map, cup_inc, cup_rep = None, set(), []
    if not args.no_cups:
        try:
            cup_map, cup_inc, cup_rep = load_cups(m, FIRST_ROWS, LAST_YEAR)
        except Exception as exc:  # noqa: BLE001 - no cup data: cups_covered 0 everywhere
            cup_rep = [{"error": f"{type(exc).__name__}: {exc}"[:200]}]
        for r in cup_rep:
            print("LASSO_CUP " + json.dumps(r), flush=True)
        print(f"Cups: incomplete league-seasons {sorted(cup_inc)}", flush=True)
    m = add_early_fair(m)
    m = build_factors(m, euro, cup_map, cup_inc)
    rows = scored_rows(m)
    holdout.check(rows["date"])
    preds, refits = walk(rows, f"{FIRST_SCORED}-07-01", LOCK, holdout)
    scored = rows.loc[preds.index].join(preds)
    res = report(scored, refits, args.boot)
    run = os.environ.get("GITHUB_RUN_ID")
    res.update({"leagues": reps, "europe": euro_rep, "cups": cup_rep,
                "cups_incomplete": sorted(map(list, cup_inc)),
                "runs": {"lasso_residual": run} if run else {},
                "rows": int(len(rows))})  # fmt: skip
    print("\n".join(summary_lines(res)))
    text = json.dumps(res, default=str, separators=(",", ":"))
    print(f"LASSO_RESIDUAL_JSON {text}")
    if args.json:
        with open(args.json, "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()
