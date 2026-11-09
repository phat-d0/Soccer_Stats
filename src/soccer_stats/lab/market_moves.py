"""Market-move factor model: can Pinnacle's early-to-close move be predicted at decision time?

Pre-registered in docs/lab.md "Market-move factor model" (owner via the lead, 8 Nov), committed
before this code. Development only: 2024/25 locked (lab.harness.Holdout from 2024-07-01), no
key, 0 credits.

Target per match and outcome: m_k = log(c_k / e_k), Pinnacle's Shin-fair close over its
Shin-fair early chance. Two designs:

* A: e = football-data's Pinnacle early price (eight leagues, scored 2019/20-2023/24);
* B: e = Pinnacle's quote at kickoff - 48 h in the bought ten-book history (scored
  2021/22-2023/24); football-data's early price is not used (it may be taken after the look).

Group lasso on two linear targets (m_home, m_away; draw derived), walk-forward in 28-day
blocks, with a ridge baseline beside it. Judged: out-of-sample R^2 and the T = 0.01 rule's CLV
against Pinnacle's close, 97.5% ranges (Bonferroni over the two designs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.lab import lasso_residual as lr
from soccer_stats.lab.harness import Holdout, blocks
from soccer_stats.odds import devig_shin

LEAGUES = lr.LEAGUES
SIDES = lr.SIDES
LOCK = lr.LOCK
EARLY_LEAD = lr.EARLY_LEAD
REFIT = "28D"
VALID_DAYS = 365
VALID_MIN_INNER = 1000  # fewer rows before the 365-day cut: validate on the last third
N_LAMBDA = 20
LAMBDA_RATIO = 1e-3
RIDGE_GRID = (0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0)  # x rows / 1,000
CLIP = 0.5
DRAW_FLOOR = 0.01
T_RULE = 0.01
T_DESCRIPTIVE = (0.0, 0.02)
N_BOOT = 2000
SEED = 0
LEVEL_JUDGED = 0.975  # Bonferroni over the two designs
LEVEL = 0.95
MIN_BETS = 300
LONG = 0.20
CUP_AFTER = 4
TOL = 1e-10
MAX_ITER = 5000

DESIGNS = {
    "A": {"label": "football-data's early price", "train_from": "2017-07-01",
          "scored_from": "2019-07-01"},
    "B": {"label": "ten-book 48 h look", "train_from": "2020-07-01",
          "scored_from": "2021-07-01"},
}  # fmt: skip
HIST_YEARS = range(2020, 2024)  # the bought history's development seasons (2024/25 locked)

FD_NAMED = ("B365", "BW", "IW", "WH", "VC")
FD_AGG = {"Avg": ("Avg", "BbAv"), "Max": ("Max", "BbMx")}  # 2019/20 on, else Betbrain
FD_FEATS = [f"fd_{k}_{s}" for k in ("b365", "avg", "max", "sd") for s in SIDES] + [
    "fd_books",
    "fd_pin_over",
    "fd_avg_over",
]
BK_FEATS = [f"bk_{k}_{s}" for k in ("mean", "max", "sd", "dk", "fd") for s in SIDES] + [
    "bk_count",
    "bk_pin_over",
    "bk_over",
]
LEAGUE_FLAGS = [f"lg_{c}" for c in LEAGUES if c != "E0"]
STRUCT_FEATS = ["fav_home", "fav_prob", "long_home", "long_away", *LEAGUE_FLAGS, "midweek"]
NEWS_FEATS = ["home_gih", "away_gih", "home_cup_next", "away_cup_next"]


def groups_for(design: str) -> dict[str, list[str]]:
    """The frozen factor groups of a design (docs/lab.md)."""
    g = {k: list(v) for k, v in lr.FACTORS.items()}
    if design == "A":
        g["cross_book_fd"] = list(FD_FEATS)
        g["market_structure"] = [*STRUCT_FEATS, "days_early"]
    else:
        g["cross_book_10"] = list(BK_FEATS)
        g["market_structure"] = list(STRUCT_FEATS)
    g["team_news"] = list(NEWS_FEATS)
    return g


# --- the group lasso and ridge on squared loss -------------------------------------------


def _gram(x: np.ndarray, y: np.ndarray):
    xa = np.hstack([np.ones((len(x), 1)), x])
    n = len(xa)
    return xa.T @ xa / n, xa.T @ y / n, float((y**2).sum() / n)


def _obj(g, c, yy, w, lam, idx, wts) -> float:
    q = 0.5 * float(np.sum(w * (g @ w))) - float(np.sum(w * c)) + 0.5 * yy
    return q + lam * sum(wt * np.linalg.norm(w[i]) for i, wt in zip(idx, wts, strict=True))


def fit_group(
    x: np.ndarray, y: np.ndarray, groups: list[np.ndarray], lam: float,
    w0: np.ndarray | None = None, tol: float = TOL, max_iter: int = MAX_ITER,
) -> np.ndarray:  # fmt: skip
    """FISTA on 1/(2n)||Y - [1 X]W||^2 + lam * sum_g sqrt(k|g|) ||W_g||_F (intercept row
    unpenalised). groups: column indices into x. Returns W (p+1, k)."""
    g, c, yy = _gram(x, y)
    return _fit_gram(g, c, yy, groups, lam, y.shape[1], w0, tol, max_iter)


def _fit_gram(g, c, yy, groups, lam, k, w0=None, tol=TOL, max_iter=MAX_ITER) -> np.ndarray:
    p1 = g.shape[0]
    idx = [np.asarray(gr, int) + 1 for gr in groups]
    wts = [np.sqrt(k * len(gr)) for gr in groups]
    lip = float(np.linalg.eigvalsh(g).max()) + 1e-12
    step = 1.0 / lip
    w = np.zeros((p1, k)) if w0 is None else w0.copy()
    z, t = w.copy(), 1.0
    obj = _obj(g, c, yy, w, lam, idx, wts)
    for _ in range(max_iter):
        nw = z - step * (g @ z - c)
        for i, wt in zip(idx, wts, strict=True):
            nrm = np.linalg.norm(nw[i])
            nw[i] *= max(0.0, 1.0 - step * lam * wt / nrm) if nrm > 0 else 0.0
        nt = (1 + np.sqrt(1 + 4 * t * t)) / 2
        z = nw + ((t - 1) / nt) * (nw - w)
        nobj = _obj(g, c, yy, nw, lam, idx, wts)
        if nobj > obj:  # restart the momentum on an uphill step
            z, nt = nw.copy(), 1.0
        done = abs(obj - nobj) <= tol * max(1.0, abs(obj))
        w, t, obj = nw, nt, nobj
        if done:
            break
    return w


def lambda_max(x: np.ndarray, y: np.ndarray, groups: list[np.ndarray]) -> float:
    """The smallest lambda that keeps no group (intercepts alone)."""
    r = y - y.mean(axis=0)
    k = y.shape[1]
    vals = [np.linalg.norm(x[:, gr].T @ r / len(x)) / np.sqrt(k * len(gr)) for gr in groups]
    return float(max(vals)) if vals else 0.0


def fit_ridge(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    """Ridge with an unpenalised intercept: W (p+1, k) on the objective ||Y - b - XB||^2 +
    alpha ||B||^2."""
    mx, my = x.mean(axis=0), y.mean(axis=0)
    xc, yc = x - mx, y - my
    b = np.linalg.solve(xc.T @ xc + alpha * np.eye(x.shape[1]), xc.T @ yc)
    return np.vstack([my - mx @ b, b])


def _apply(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    return w[0] + x @ w[1:]


def split_valid(train: pd.DataFrame, lo: pd.Timestamp):
    """Inner rows and validation rows: the last VALID_DAYS before lo, or the window's last
    third by kickoff when fewer than VALID_MIN_INNER rows precede that cut (amendment)."""
    train = train.sort_values("date", kind="stable")
    cut = lo - pd.Timedelta(days=VALID_DAYS)
    inner = train[train["date"] < cut]
    if len(inner) >= VALID_MIN_INNER:
        return inner, train[train["date"] >= cut], "365d"
    k = int(len(train) * 2 / 3)
    return train.iloc[:k], train.iloc[k:], "last_third"


def targets(df: pd.DataFrame) -> np.ndarray:
    """(n, 2) clipped home and away moves."""
    return np.clip(df[["m_home", "m_away"]].to_numpy(float), -CLIP, CLIP)


def choose(train: pd.DataFrame, lo, feats: list[str], groups: list[np.ndarray], kind: str):
    """Fit the path (group lasso) or the alpha grid (ridge) on the inner rows, score squared
    error on the validation rows, refit the whole window at the best setting."""
    inner, valid, how = split_valid(train, lo)
    if len(inner) < 100 or len(valid) < 50:
        raise ValueError("training window too short for the inner validation")
    sc = lr.Scaler(inner[feats].to_numpy(float))
    xi, yi = sc(inner[feats].to_numpy(float)), targets(inner)
    xv, yv = sc(valid[feats].to_numpy(float)), targets(valid)
    sc_all = lr.Scaler(train[feats].to_numpy(float))
    xa, ya = sc_all(train[feats].to_numpy(float)), targets(train)
    if kind == "ridge":
        alphas = [a * len(xi) / 1000 for a in RIDGE_GRID]
        sse = [float(((yv - _apply(fit_ridge(xi, yi, a), xv)) ** 2).sum()) for a in alphas]
        best = int(np.argmin(sse))
        w = fit_ridge(xa, ya, RIDGE_GRID[best] * len(xa) / 1000)
        return {"w": w, "scaler": sc_all, "setting": RIDGE_GRID[best], "valid": how}
    g, c, yy = _gram(xi, yi)
    lmax = max(lambda_max(xi, yi, groups), 1e-10)
    lams = np.geomspace(lmax, lmax * LAMBDA_RATIO, N_LAMBDA)
    w, sse = None, []
    for lam in lams:
        w = _fit_gram(g, c, yy, groups, lam, 2, w)
        sse.append(float(((yv - _apply(w, xv)) ** 2).sum()))
    best = int(np.argmin(sse))
    g2, c2, yy2 = _gram(xa, ya)
    w_all = None
    for lam in lams[: best + 1]:  # warm start down to the chosen lambda
        w_all = _fit_gram(g2, c2, yy2, groups, lam, 2, w_all)
    return {"w": w_all, "scaler": sc_all, "setting": float(lams[best]), "lam_index": best,
            "valid": how}  # fmt: skip


def walk(rows: pd.DataFrame, start, end, groups: dict[str, list[str]], kind: str = "group",
         holdout: Holdout | None = None) -> tuple[pd.DataFrame, list[dict]]:  # fmt: skip
    """Out-of-sample predicted moves (pred_home, pred_away) and the no-factor forecast (bar_home,
    bar_away = the training window's mean) for rows whose decision day is in [start, end). A
    block's fit uses matches kicked off before its first decision day."""
    feats = [f for fs in groups.values() for f in fs]
    gidx = [np.array([feats.index(f) for f in fs]) for fs in groups.values()]
    names = list(groups)
    rows = rows.sort_values("date", kind="stable")
    out, refits = [], []
    for lo, hi in blocks(start, end, REFIT):
        hi = min(hi, pd.Timestamp(end))
        test = rows[(rows["day"] >= lo) & (rows["day"] < hi)]
        if test.empty:
            continue
        if holdout is not None:
            holdout.check(test["date"])
        train = rows[rows["date"] < lo]
        fit = choose(train, lo, feats, gidx, kind)
        pred = _apply(fit["w"], fit["scaler"](test[feats].to_numpy(float)))
        bar = targets(train).mean(axis=0)
        out.append(pd.DataFrame({"pred_home": pred[:, 0], "pred_away": pred[:, 1],
                                 "bar_home": bar[0], "bar_away": bar[1]},
                                index=test.index))  # fmt: skip
        kept = [names[i] for i, gr in enumerate(gidx) if np.abs(fit["w"][gr + 1]).sum() > 0]
        refits.append({"block": str(lo.date()), "train": int(len(train)), "test": int(len(test)),
                       "setting": fit["setting"], "valid": fit["valid"],
                       "kept": kept})  # fmt: skip
    return (pd.concat(out) if out else pd.DataFrame()), refits


# --- predictions to chances, the rule, measures ------------------------------------------


def predicted_close(df: pd.DataFrame) -> np.ndarray:
    """(n, 3) predicted fair close chances: home/away from their moves, the draw the rest."""
    e = df[[f"e_{s}" for s in SIDES]].to_numpy(float)
    ch = e[:, 0] * np.exp(df["pred_home"].to_numpy(float))
    ca = e[:, 2] * np.exp(df["pred_away"].to_numpy(float))
    cd = np.maximum(1 - ch - ca, DRAW_FLOOR)
    return np.column_stack([ch, cd, ca])


def rule_bets(df: pd.DataFrame, t: float = T_RULE, odds_cols=None) -> pd.DataFrame:
    """One $1 bet per match on the outcome with the largest predicted CLV (odds x predicted
    close - 1) if >= t; CLV = odds x Pinnacle's fair close - 1."""
    odds_cols = odds_cols or [f"odds_{s}" for s in SIDES]
    odds = df[odds_cols].to_numpy(float)
    pc = predicted_close(df)
    pred = odds * pc - 1
    pred = np.where(np.isfinite(pred), pred, -np.inf)
    k = np.argmax(pred, axis=1)
    i = np.arange(len(df))
    keep = pred[i, k] >= t
    c = df[[f"c_{s}" for s in SIDES]].to_numpy(float)
    b = pd.DataFrame({
        "row": df.index.to_numpy()[keep],
        "league": df["league"].to_numpy()[keep],
        "season": df["season"].astype(str).to_numpy()[keep],
        "date": df["date"].to_numpy()[keep],
        "side": np.array(SIDES)[k[keep]],
        "odds": odds[i, k][keep],
        "pred_clv": pred[i, k][keep],
        "clv": (odds[i, k] * c[i, k] - 1)[keep],
        "won": (df["y"].to_numpy(int)[keep] == k[keep]).astype(float),
    })  # fmt: skip
    b["profit"] = np.where(b["won"] == 1, b["odds"] - 1, -1.0)
    return b


def _q(level: float) -> tuple[float, float]:
    return (1 - level) / 2, 1 - (1 - level) / 2


def boot_mean(v, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    v = np.asarray(v, float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return {"ci95": None, "ci975": None}
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), (n_boot, len(v)))].mean(axis=1)
    return {"ci95": [float(np.quantile(means, a)) for a in _q(LEVEL)],
            "ci975": [float(np.quantile(means, a)) for a in _q(LEVEL_JUDGED)]}  # fmt: skip


def r2_block(df: pd.DataFrame, n_boot: int = N_BOOT, seed: int = SEED, clip: bool = True) -> dict:
    """Out-of-sample R^2 of the home and away moves together, against each block's training
    mean, with match-resampled ranges (ratio of resampled sums)."""
    if df.empty:
        return {"matches": 0, "r2": None, "ci95": None, "ci975": None}
    y = targets(df) if clip else df[["m_home", "m_away"]].to_numpy(float)
    p = df[["pred_home", "pred_away"]].to_numpy(float)
    b = df[["bar_home", "bar_away"]].to_numpy(float)
    res, tot = ((y - p) ** 2).sum(axis=1), ((y - b) ** 2).sum(axis=1)
    out = {"matches": int(len(df)), "r2": float(1 - res.sum() / tot.sum())}
    rng = np.random.default_rng(seed)
    d = rng.integers(0, len(df), (n_boot, len(df)))
    r2 = 1 - res[d].sum(axis=1) / tot[d].sum(axis=1)
    out["ci95"] = [float(np.quantile(r2, a)) for a in _q(LEVEL)]
    out["ci975"] = [float(np.quantile(r2, a)) for a in _q(LEVEL_JUDGED)]
    return out


def rule_block(bets: pd.DataFrame, t: float, n_boot: int = N_BOOT) -> dict:
    clv = bets["clv"].to_numpy(float) if len(bets) else np.array([])
    ok = np.isfinite(clv)
    c, r = boot_mean(clv[ok], n_boot), boot_mean(bets["profit"] if len(bets) else [], n_boot)
    return {"threshold": t, "bets": int(len(bets)),
            "won": int(bets["won"].sum()) if len(bets) else 0,
            "avg_odds": float(bets["odds"].mean()) if len(bets) else None,
            "avg_pred_clv": float(bets["pred_clv"].mean()) if len(bets) else None,
            "clv_bets": int(ok.sum()), "clv": float(clv[ok].mean()) if ok.any() else None,
            "clv_ci95": c["ci95"], "clv_ci975": c["ci975"],
            "roi": float(bets["profit"].mean()) if len(bets) else None,
            "roi_ci95": r["ci95"],
            "by_side": {s: int((bets["side"] == s).sum()) for s in SIDES} if len(bets) else {},
            }  # fmt: skip


def top_decile(df: pd.DataFrame) -> dict:
    """Every side's predicted move; the realised (unclipped) move on the top decile."""
    pc = predicted_close(df)
    e = df[[f"e_{s}" for s in SIDES]].to_numpy(float)
    c = df[[f"c_{s}" for s in SIDES]].to_numpy(float)
    pm = np.log(pc / e).ravel()
    rm, pts = np.log(c / e).ravel(), (c - e).ravel()
    ok = np.isfinite(pm) & np.isfinite(rm)
    if ok.sum() < 20:
        return {"sides": int(ok.sum())}
    cut = np.quantile(pm[ok], 0.9)
    top = ok & (pm >= cut)
    return {"sides": int(ok.sum()), "cut": float(cut), "top_sides": int(top.sum()),
            "pred_move": float(pm[top].mean()), "move": float(rm[top].mean()),
            "move_points": float(pts[top].mean()),
            "move_all": float(rm[ok].mean()), "move_points_all": float(pts[ok].mean())}  # fmt: skip


def move_stats(rows: pd.DataFrame) -> dict:
    out = {}
    for s in SIDES:
        m = np.log(rows[f"c_{s}"] / rows[f"e_{s}"]).to_numpy(float)
        pts = (rows[f"c_{s}"] - rows[f"e_{s}"]).to_numpy(float)
        out[s] = {"mean": float(np.nanmean(m)), "sd": float(np.nanstd(m)),
                  "abs_points": float(np.nanmean(np.abs(pts))),
                  "clipped_share": float(np.nanmean(np.abs(m) > CLIP))}  # fmt: skip
    return out


def verdict(r2: dict, rule: dict) -> dict:
    ci_r, ci_c = r2.get("ci975"), rule.get("clv_ci975")
    checks = {"r2_range_above_0": bool(ci_r and ci_r[0] > 0),
              "bets_at_least_300": rule["bets"] >= MIN_BETS,
              "clv_range_above_0": bool(ci_c and ci_c[0] > 0)}  # fmt: skip
    return {**checks, "passes": all(checks.values())}


def design_report(scored: pd.DataFrame, ridge: pd.DataFrame, refits: list[dict],
                  per_group: dict, rows: pd.DataFrame, groups: dict,
                  n_boot: int = N_BOOT) -> dict:  # fmt: skip
    r2 = r2_block(scored, n_boot)
    bets = rule_bets(scored)
    rule = rule_block(bets, T_RULE, n_boot)
    rule["by_league"] = {lg: rule_block(g, T_RULE, n_boot) for lg, g in bets.groupby("league")}
    desc = {str(t): rule_block(rule_bets(scored, t), t, n_boot) for t in T_DESCRIPTIVE}
    if "best_home" in scored:
        desc["best_book_at_look"] = _best_entry(scored, n_boot)
    n = len(refits)
    kept = {g: (sum(g in r["kept"] for r in refits) / n if n else 0.0) for g in groups}
    return {
        "rows": int(len(rows)), "scored": int(len(scored)),
        "seasons": sorted(scored["season"].astype(str).unique().tolist()) if len(scored) else [],
        "moves": move_stats(scored) if len(scored) else {},
        "r2": r2, "r2_unclipped": r2_block(scored, n_boot, clip=False),
        "r2_ridge": r2_block(ridge, n_boot) if len(ridge) else None,
        "r2_by_league": {lg: r2_block(g, n_boot) for lg, g in scored.groupby("league")},
        "r2_by_season": {str(s): r2_block(g, n_boot) for s, g in scored.groupby("season")},
        "r2_by_group": per_group,
        "kept_share": kept,
        "top_decile": top_decile(scored) if len(scored) else {},
        "rule": rule, "rule_descriptive": desc,
        "verdict": verdict(r2, rule),
        "refits": [{k: v for k, v in r.items()} for r in refits],
    }  # fmt: skip


def _best_entry(scored: pd.DataFrame, n_boot: int) -> dict:
    """Design B, descriptive: the T = 0.01 picks (chosen at Pinnacle's look price) entered at
    the best of the ten books at the look."""
    bets = rule_bets(scored, T_RULE)
    if bets.empty:
        return rule_block(bets, T_RULE, n_boot)
    rows = scored.loc[bets["row"].to_numpy()]
    s = bets["side"].to_numpy()
    k = np.array([SIDES.index(x) for x in s])
    best = rows[[f"best_{x}" for x in SIDES]].to_numpy(float)[np.arange(len(rows)), k]
    c = rows[[f"c_{x}" for x in SIDES]].to_numpy(float)[np.arange(len(rows)), k]
    won = (rows["y"].to_numpy(int) == k).astype(float)
    b = pd.DataFrame({"league": rows["league"].to_numpy(), "side": s, "odds": best,
                      "pred_clv": np.nan, "clv": best * c - 1, "won": won})  # fmt: skip
    b["profit"] = np.where(b["won"] == 1, b["odds"] - 1, -1.0)
    return rule_block(b, T_RULE, n_boot)


# --- factors ---------------------------------------------------------------------------


def _shin_rows(v: np.ndarray) -> np.ndarray:
    out = np.full(v.shape, np.nan)
    ok = (np.isfinite(v) & (v > 1)).all(axis=1)
    if ok.any():
        out[ok] = np.vstack([devig_shin(r) for r in v[ok]])
    return out


def fd_books(code: str, years) -> pd.DataFrame:
    """football-data's other books' early 1X2 (B365, BW, IW, WH, VC, Avg, Max; Betbrain's
    BbAv/BbMx before 2019/20), keyed by season, home, away."""
    from soccer_stats.data import download, season_code

    parts = []
    for y in years:
        raw = pd.read_csv(download(code, y), encoding="latin-1", on_bad_lines="skip")
        raw = raw.dropna(subset=[c for c in ("HomeTeam", "AwayTeam", "FTHG") if c in raw])
        out = pd.DataFrame(
            {"season": season_code(y), "home": raw["HomeTeam"], "away": raw["AwayTeam"]}
        )
        for b in FD_NAMED:
            for s, x in zip(SIDES, "HDA", strict=True):
                col = f"{b}{x}"
                out[f"fdo_{b}_{s}"] = (
                    pd.to_numeric(raw[col], errors="coerce") if col in raw else np.nan
                )
        for name, (new, old) in FD_AGG.items():
            for s, x in zip(SIDES, "HDA", strict=True):
                col = f"{new}{x}" if f"{new}{x}" in raw else f"{old}{x}"
                out[f"fdo_{name}_{s}"] = (
                    pd.to_numeric(raw[col], errors="coerce") if col in raw else np.nan
                )
        parts.append(out)
    return pd.concat(parts, ignore_index=True).drop_duplicates(["season", "home", "away"])


def fd_features(m: pd.DataFrame) -> pd.DataFrame:
    """b-fd: cross-book disagreement at football-data's early moment (q_* = Pinnacle fair,
    odds_* = Pinnacle quoted)."""
    q = m[[f"q_{s}" for s in SIDES]].to_numpy(float)
    pin = m[[f"odds_{s}" for s in SIDES]].to_numpy(float)
    out = pd.DataFrame(index=m.index)
    fair = {b: _shin_rows(m[[f"fdo_{b}_{s}" for s in SIDES]].to_numpy(float))
            for b in (*FD_NAMED, "Avg")}  # fmt: skip
    mx = m[[f"fdo_Max_{s}" for s in SIDES]].to_numpy(float)
    named = np.stack([fair[b] for b in FD_NAMED])  # (books, n, 3)
    with np.errstate(all="ignore"):
        for k, s in enumerate(SIDES):
            out[f"fd_b365_{s}"] = np.log(fair["B365"][:, k] / q[:, k])
            out[f"fd_avg_{s}"] = np.log(fair["Avg"][:, k] / q[:, k])
            out[f"fd_max_{s}"] = np.log(mx[:, k] / pin[:, k])
            cnt = np.isfinite(named[:, :, k]).sum(axis=0)
            sd = np.nanstd(named[:, :, k], axis=0)
            out[f"fd_sd_{s}"] = np.where(cnt >= 2, sd, np.nan)
        out["fd_books"] = np.isfinite(named[:, :, 0]).sum(axis=0).astype(float)
        out["fd_pin_over"] = (1 / pin).sum(axis=1) - 1
        avg = m[[f"fdo_Avg_{s}" for s in SIDES]].to_numpy(float)
        out["fd_avg_over"] = (1 / avg).sum(axis=1) - 1
    return out


def days_early(dates: pd.Series) -> np.ndarray:
    """Days from football-data's collection day (the Friday on or before a Friday-Monday
    kickoff, the Tuesday on or before a Tuesday-Thursday one) to the kickoff date."""
    wd = pd.to_datetime(dates).dt.weekday.to_numpy()  # Mon 0 .. Sun 6
    fri = (wd - 4) % 7
    tue = (wd - 1) % 7
    return np.where(np.isin(wd, (1, 2, 3)), tue, fri).astype(float)


def structure_features(m: pd.DataFrame, e: np.ndarray, kickoff: pd.Series) -> pd.DataFrame:
    """(c): favourite, price level, long shots, league, midweek (e = the design's early fair)."""
    out = pd.DataFrame(index=m.index)
    out["fav_home"] = (np.argmax(e, axis=1) == 0).astype(float)
    out["fav_prob"] = e.max(axis=1)
    out["long_home"] = (e[:, 0] < LONG).astype(float)
    out["long_away"] = (e[:, 2] < LONG).astype(float)
    for f in LEAGUE_FLAGS:
        out[f] = (m["league"].to_numpy() == f[3:]).astype(float)
    wd = pd.to_datetime(kickoff).dt.weekday.to_numpy()
    out["midweek"] = np.isin(wd, (1, 2, 3)).astype(float)
    return out


def news_features(m: pd.DataFrame, euro: dict | None, cups: dict | None) -> pd.DataFrame:
    """(d): games in hand at kickoff - 4 days (league matches played before that day minus the
    league-season's median) and a cup or UEFA match within CUP_AFTER days after kickoff."""
    out = pd.DataFrame(index=m.index, columns=NEWS_FEATS, dtype=float)
    date = pd.to_datetime(m["date"]).dt.normalize()
    day = date - EARLY_LEAD
    for _, g in m.groupby(["league", m["season"].astype(str)]):
        gd = date.loc[g.index]
        teams = sorted(set(g["home"]) | set(g["away"]))
        sched = {t: np.sort(gd[(g["home"] == t) | (g["away"] == t)].to_numpy("datetime64[ns]"))
                 for t in teams}  # fmt: skip
        for i, h, a in zip(g.index, g["home"], g["away"], strict=True):
            d0 = np.datetime64(day.at[i])
            played = {t: int(np.searchsorted(sched[t], d0, side="left")) for t in teams}
            med = float(np.median(list(played.values())))
            out.at[i, "home_gih"] = med - played[h]
            out.at[i, "away_gih"] = med - played[a]
    euro_days = {k: np.array(sorted(d for d, _ in v), dtype="datetime64[ns]")
                 for k, v in (euro or {}).items()}  # fmt: skip
    cup_days = {k: np.array(sorted(v), dtype="datetime64[ns]") for k, v in (cups or {}).items()}
    for side in ("home", "away"):
        vals = []
        for lg, t, ko in zip(m["league"], m[side], date, strict=True):
            hi = ko + pd.Timedelta(days=CUP_AFTER)
            n = lr._count(euro_days.get(f"{lg}:{t}"), ko, hi, closed="right")
            n += lr._count(cup_days.get(t), ko, hi, closed="right")
            vals.append(float(n > 0))
        out[f"{side}_cup_next"] = vals
    return out.astype(float)


# --- design B: the bought ten-book history ---------------------------------------------


def snapshot_files(code: str, raw_dir=None) -> list[Path]:
    """The league's cached snapshot files requested before LOCK, by file name only (a file
    requested on or after 2024-07-01 is never opened)."""
    from soccer_stats.data import RAW_DIR
    from soccer_stats.odds_history import history_dir

    keep = []
    for p in sorted(history_dir(code, raw_dir or RAW_DIR).glob("*.json")):
        try:
            at = pd.Timestamp(dt.datetime.strptime(p.stem, "%Y%m%dT%H%MZ"))
        except ValueError:
            continue
        if at < LOCK:
            keep.append(p)
    return keep


def load_books(code: str, known: set[str], raw_dir=None) -> pd.DataFrame:
    """dk_price_backtest.load_books's rows, from the files snapshot_files keeps."""
    from soccer_stats.odds_feed import _team, book_prices

    rows = []
    for path in snapshot_files(code, raw_dir):
        snap = json.loads(path.read_text())
        at = pd.Timestamp(snap["requested"])
        ts = pd.Timestamp(snap.get("timestamp") or snap["requested"])
        for ev in snap.get("data") or []:
            books = book_prices(ev, known)
            if books:
                rows.append({"at": at, "snapshot_ts": ts,
                             "kickoff": pd.Timestamp(ev["commence_time"]).tz_convert("UTC"),
                             "home": _team(ev["home_team"], known),
                             "away": _team(ev["away_team"], known), "books": books})  # fmt: skip
    return pd.DataFrame(rows, columns=["at", "snapshot_ts", "kickoff", "home", "away", "books"])


def look_rows(m: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """Per match of `m` the history covers: Pinnacle at the 48 h look (b_odds_*, e48_*) and at
    the close (b_close_*), every other book's fresh look quote (b-10 features) and the best
    look price per side (best_*). Index = m's index."""
    from soccer_stats import dk_price_backtest as dkb
    from soccer_stats.odds_feed import US_BOOKS

    by_pair = {k: g.sort_values("snapshot_ts") for k, g in hist.groupby(["home", "away"])}
    rec = {}
    for i, r in m.iterrows():
        g = by_pair.get((r["home"], r["away"]))
        if g is None:
            continue
        day = pd.Timestamp(r["date"]).date()
        near = g[(g["kickoff"].dt.date - day).abs() <= pd.Timedelta(days=1)]
        if near.empty:
            continue
        kickoff = near["kickoff"].mode().iloc[0]
        look = kickoff - dkb.LOOK
        pin = dkb.quote(near, look, "pinnacle")
        close = dkb.quote(near, kickoff, "pinnacle")
        if pin is None or close is None:
            continue
        others = {b: dkb.quote(near, look, b) for b in US_BOOKS}
        others = {b: q for b, q in others.items() if q}
        rec[i] = book_features(pin, close, others, kickoff)
    return pd.DataFrame.from_dict(rec, orient="index")


def book_features(pin: dict, close: dict, others: dict, kickoff) -> dict:
    """b-10 features and the prices one match needs (pin/close/others: {side: odds})."""
    po = np.array([pin[s] for s in SIDES], float)
    pf = devig_shin(po)
    cf = devig_shin(np.array([close[s] for s in SIDES], float))
    row = {"kickoff": kickoff}
    for k, s in enumerate(SIDES):
        row[f"b_odds_{s}"], row[f"e48_{s}"], row[f"c48_{s}"] = po[k], pf[k], cf[k]
    lr_ = {}
    overs = []
    for b, q in others.items():
        o = np.array([q[s] for s in SIDES], float)
        if not (np.isfinite(o).all() and (o > 1).all()):
            continue
        lr_[b] = np.log(devig_shin(o) / pf)
        overs.append(float((1 / o).sum() - 1))
    arr = np.array(list(lr_.values())) if lr_ else np.empty((0, 3))
    for k, s in enumerate(SIDES):
        col = arr[:, k] if len(arr) else np.array([])
        row[f"bk_mean_{s}"] = float(col.mean()) if len(col) else np.nan
        row[f"bk_max_{s}"] = float(col.max()) if len(col) else np.nan
        row[f"bk_sd_{s}"] = float(col.std()) if len(col) >= 2 else np.nan
        row[f"bk_dk_{s}"] = float(lr_["draftkings"][k]) if "draftkings" in lr_ else np.nan
        row[f"bk_fd_{s}"] = float(lr_["fanduel"][k]) if "fanduel" in lr_ else np.nan
        best = [pin[s]] + [q[s] for q in others.values() if np.isfinite(q[s])]
        row[f"best_{s}"] = float(max(best))
    row["bk_count"] = float(len(lr_))
    row["bk_pin_over"] = float((1 / po).sum() - 1)
    row["bk_over"] = float(np.mean(overs)) if overs else np.nan
    return row


# --- assembling the designs ------------------------------------------------------------


def outcome(m: pd.DataFrame) -> np.ndarray:
    hg, ag = m["home_goals"].astype(int), m["away_goals"].astype(int)
    return np.where(hg > ag, 0, np.where(hg == ag, 1, 2))


def design_a(m: pd.DataFrame) -> pd.DataFrame:
    """Rows of design A: football-data's early and close; e_/c_ fair, odds_ quoted early."""
    d = m.copy()
    for s in SIDES:
        d[f"e_{s}"], d[f"c_{s}"] = d[f"q_{s}"], d[f"fair_close_{s}"]
    ok = np.isfinite(d[[f"e_{s}" for s in SIDES] + [f"c_{s}" for s in SIDES]]
                     .to_numpy(float)).all(axis=1)  # fmt: skip
    d = d[ok & (pd.to_datetime(d["date"]) >= pd.Timestamp(DESIGNS["A"]["train_from"]))].copy()
    d = pd.concat([d, fd_features(d)], axis=1)
    e = d[[f"e_{s}" for s in SIDES]].to_numpy(float)
    d = pd.concat([d, structure_features(d, e, d["date"])], axis=1)
    d["days_early"] = days_early(d["date"])
    return _finish(d)


def design_b(m: pd.DataFrame, looks: pd.DataFrame) -> pd.DataFrame:
    """Rows of design B: Pinnacle's 48 h look and close from the bought history; model_gap and
    price_shape recomputed on the look's fair price; football-data's early price unused."""
    if looks.empty:
        return pd.DataFrame()
    d = m.loc[looks.index].join(looks)
    for s in SIDES:
        d[f"e_{s}"], d[f"c_{s}"], d[f"odds_{s}"] = d[f"e48_{s}"], d[f"c48_{s}"], d[f"b_odds_{s}"]
        d[f"gap_{s}"] = np.log(d[f"p_{s}"].astype(float)) - np.log(d[f"e_{s}"].astype(float))
    d["shape_home"] = np.log(d["e_home"] / d["e_draw"])
    d["shape_away"] = np.log(d["e_away"] / d["e_draw"])
    for c in [c for c in d.columns if c.startswith(("fdo_", "q_", "fair_close_"))]:
        d[c] = np.nan  # football-data's early prices are never read in design B
    e = d[[f"e_{s}" for s in SIDES]].to_numpy(float)
    d = pd.concat([d, structure_features(d, e, pd.to_datetime(d["kickoff"]).dt.tz_convert(None))],
                  axis=1)  # fmt: skip
    return _finish(d)


def _finish(d: pd.DataFrame) -> pd.DataFrame:
    d = d.loc[:, ~d.columns.duplicated(keep="last")].copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    d["day"] = d["date"] - EARLY_LEAD
    d["y"] = outcome(d)
    for s in SIDES:
        d[f"m_{s}"] = np.log(d[f"c_{s}"] / d[f"e_{s}"])
    return d.sort_values("date", kind="stable").reset_index(drop=True)


def per_group_r2(rows: pd.DataFrame, start, end, groups: dict, holdout, n_boot: int) -> dict:
    out = {}
    for name, feats in groups.items():
        try:
            p, _ = walk(rows, start, end, {name: feats}, "ridge", holdout)
            out[name] = r2_block(rows.loc[p.index].join(p), n_boot)
        except Exception as exc:  # noqa: BLE001 - one group failing is reported
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:160]}
    return out


def run_design(name: str, rows: pd.DataFrame, holdout: Holdout, n_boot: int = N_BOOT) -> dict:
    groups = groups_for(name)
    start = DESIGNS[name]["scored_from"]
    holdout.check(rows["date"])
    p, refits = walk(rows, start, LOCK, groups, "group", holdout)
    scored = rows.loc[p.index].join(p)
    pr, _ = walk(rows, start, LOCK, groups, "ridge", holdout)
    ridge = rows.loc[pr.index].join(pr) if len(pr) else pd.DataFrame()
    pg = per_group_r2(rows, start, LOCK, groups, holdout, n_boot)
    rep = design_report(scored, ridge, refits, pg, rows, groups, n_boot)
    rep.update({"label": DESIGNS[name]["label"], "groups": groups,
                "features": sum(len(v) for v in groups.values())})  # fmt: skip
    return rep


def summary_lines(res: dict) -> list[str]:
    out = []
    for name, d in res["designs"].items():
        if "error" in d:
            out.append(f"Design {name}: ERROR {d['error']}")
            continue
        r, ru, v = d["r2"], d["rule"], d["verdict"]
        out.append(f"Design {name} ({d['label']}): {d['scored']} matches scored, seasons "
                   f"{', '.join(d['seasons'])}, {d['features']} features")  # fmt: skip
        out.append(f"  R2 (clipped, group lasso) {r['r2']:+.4f} 95% {r['ci95']} 97.5% {r['ci975']};"
                   f" unclipped {d['r2_unclipped']['r2']:+.4f}; ridge "
                   f"{(d['r2_ridge'] or {}).get('r2')}")  # fmt: skip
        out.append(f"  Rule T={ru['threshold']}: {ru['bets']} bets, CLV {ru['clv']} 97.5% "
                   f"{ru['clv_ci975']}, ROI {ru['roi']}")  # fmt: skip
        for t, b in d["rule_descriptive"].items():
            out.append(f"  (descriptive) {t}: {b['bets']} bets, CLV {b['clv']} {b['clv_ci95']}")
        out.append(
            "  Kept (share of refits): "
            + ", ".join(
                f"{k} {v:.0%}" for k, v in sorted(d["kept_share"].items(), key=lambda kv: -kv[1])
            )
        )
        out.append("  R2 by group alone: " + ", ".join(
            f"{k} {b.get('r2', float('nan')):+.4f}" for k, b in d["r2_by_group"].items()
            if b.get("r2") is not None))  # fmt: skip
        out.append(f"  Top decile: {d['top_decile']}")
        out.append(f"  Verdict: {v}")
    out.append(f"Overall: {res['verdict']}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default=",".join(LEAGUES), help="comma list of codes")
    p.add_argument("--designs", default="A,B", help="A, B or A,B")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    p.add_argument("--no-europe", action="store_true", help="skip the ESPN UEFA results")
    p.add_argument("--no-cups", action="store_true", help="skip the domestic cups")
    p.add_argument("--json", default="", help="write the result here too")
    args = p.parse_args(argv)
    codes = [c.strip() for c in args.leagues.split(",") if c.strip()]
    designs = [d.strip() for d in args.designs.split(",") if d.strip()]
    holdout = Holdout(LOCK)
    frames, reps = [], {}
    for code in codes:
        try:
            base, rep = lr.load_league(code, holdout)
            years = range(lr.FIRST_ROWS - lr.MODEL_BURN, lr.LAST_YEAR + 1)
            base = base.merge(fd_books(code, years), on=["season", "home", "away"], how="left")
            frames.append(base)
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        reps[code] = {k: v for k, v in rep.items() if k != "coverage"}
        print(f"{code}: {reps[code]}", flush=True)
    m = pd.concat(frames, ignore_index=True)
    m = m[pd.to_datetime(m["date"]) < LOCK]
    holdout.check(m["date"])
    euro, euro_rep = None, None
    if not args.no_europe:
        try:
            euro, euro_rep = lr.load_euro(m, lr.FIRST_ROWS, lr.LAST_YEAR,
                                          pd.Timestamp(LOCK, tz="UTC"))  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            euro_rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        print(f"Europe: {euro_rep}", flush=True)
    cup_map, cup_inc, cup_rep = None, set(), []
    if not args.no_cups:
        try:
            cup_map, cup_inc, cup_rep = lr.load_cups(m, lr.FIRST_ROWS, lr.LAST_YEAR)
        except Exception as exc:  # noqa: BLE001
            cup_rep = [{"error": f"{type(exc).__name__}: {exc}"[:200]}]
        print(f"Cups: {len(cup_rep)} cup-seasons, incomplete {sorted(cup_inc)}", flush=True)
    m = lr.add_early_fair(m)
    m = lr.build_factors(m, euro, cup_map, cup_inc)
    m = pd.concat([m, news_features(m, euro, cup_map)], axis=1)
    res: dict = {"kind": "market_moves", "development": True,
                 "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "locked_from": str(LOCK.date()), "leagues": reps, "europe": euro_rep,
                 "cups_incomplete": sorted(map(list, cup_inc)), "designs": {}}  # fmt: skip
    for name in designs:
        try:
            if name == "A":
                rows = design_a(m)
            else:
                looks, cover = [], {}
                for code in codes:
                    sub = m[
                        (m["league"] == code)
                        & m["season"]
                        .astype(str)
                        .isin([f"{y % 100:02d}{(y + 1) % 100:02d}" for y in HIST_YEARS])
                    ]
                    known = set(m.loc[m["league"] == code, "home"]) | set(
                        m.loc[m["league"] == code, "away"]
                    )
                    hist = load_books(code, known)
                    lk = look_rows(sub, hist) if len(hist) else pd.DataFrame()
                    cover[code] = {"matches": int(len(sub)), "snapshots_rows": int(len(hist)),
                                   "with_look_and_close": int(len(lk))}  # fmt: skip
                    print(f"History {code}: {cover[code]}", flush=True)
                    if len(lk):
                        looks.append(lk)
                rows = design_b(m, pd.concat(looks) if looks else pd.DataFrame())
                res.setdefault("history_coverage", cover)
            print(f"Design {name}: {len(rows)} rows", flush=True)
            res["designs"][name] = run_design(name, rows, holdout, args.boot)
        except Exception as exc:  # noqa: BLE001 - a design failing is reported
            res["designs"][name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    passed = [k for k, d in res["designs"].items() if d.get("verdict", {}).get("passes")]
    res["verdict"] = (
        f"pass in design {', '.join(passed)}: ask the lead before opening 2024/25"
        if passed else "fail: no design passes; the study closes and 2024/25 is not spent"
    )  # fmt: skip
    run = os.environ.get("GITHUB_RUN_ID")
    res["runs"] = {"market_moves": run} if run else {}
    print("\n".join(summary_lines(res)), flush=True)
    text = json.dumps(res, default=str, separators=(",", ":"))
    print(f"MARKET_MOVES_JSON {text}")
    if args.json:
        Path(args.json).write_text(text)


if __name__ == "__main__":
    main()
