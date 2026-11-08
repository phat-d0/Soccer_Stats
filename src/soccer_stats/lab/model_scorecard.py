"""Model evaluation stage 2: one out-of-sample scorecard per model beside the market's price
(docs/lab.md "Model evaluation, stage 2: scorecards"; written before any run).

Part 1: the 1X2 match model, the Lean blend and Pinnacle's fair early and close prices on the
same outcome rows (2019/20-2023/24), with the Model 12% and Lean threshold confusions; and model
(f)'s team-corner counts against a league-mean baseline (2017/18-2023/24). Descriptive only.
2024/25 is never loaded. No key, 0 credits.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson

from soccer_stats import calibration as cal
from soccer_stats.lab import auc as auc_mod
from soccer_stats.lab.harness import Holdout, blocks

SIDES = ("home", "draw", "away")
SERIES = {"model": "p_model", "blend": "p_blend", "pinnacle_early": "fair_early",
          "pinnacle_close": "fair_close"}  # fmt: skip
REF = "pinnacle_early"
MATCH_FIRST, MATCH_LAST = 2019, 2023
CORNER_FIRST, CORNER_LAST = 2017, 2023
LOCK = pd.Timestamp("2024-07-01")
EDGE_12 = 0.12
N_BOOT = 2000
SEED = 0
INTERVALS = (0.8, 0.9)
CORNER_REFIT = "28D"
CORNER_WINDOW = pd.Timedelta(days=730)
EPS = 1e-12
B64_CHUNK = 60_000


def _ci(values: np.ndarray, groups: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED):
    """Mean of `values` with a 95% range resampling `groups` (match ids)."""
    v = np.asarray(values, float)
    ok = np.isfinite(v)
    v, g = v[ok], np.asarray(groups)[ok]
    if len(v) < 2:
        return None, None
    codes, idx = np.unique(g, return_inverse=True)
    sums = np.bincount(idx, weights=v, minlength=len(codes))
    cnts = np.bincount(idx, minlength=len(codes)).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(codes), size=(n_boot, len(codes)))
    means = sums[draws].sum(axis=1) / cnts[draws].sum(axis=1)
    return float(v.mean()), [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def wide(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per match with p_<series>_<side> and y (0 home, 1 draw, 2 away)."""
    keys = ["league", "season", "match", "home", "away", "date"]
    out = None
    for s in SIDES:
        part = rows[rows["side"] == s].set_index(keys)
        cols = {c: f"{c}_{s}" for c in SERIES.values()}
        part = part[[*SERIES.values(), "won"]].rename(columns={**cols, "won": f"won_{s}"})
        out = part if out is None else out.join(part, how="inner")
    out = out.reset_index()
    out["y"] = np.argmax(out[[f"won_{s}" for s in SIDES]].to_numpy(float), axis=1)
    need = [f"{c}_{s}" for c in SERIES.values() for s in SIDES]
    return out.dropna(subset=need).reset_index(drop=True)


def _probs(w: pd.DataFrame, col: str) -> np.ndarray:
    p = w[[f"{col}_{s}" for s in SIDES]].to_numpy(float)
    return p / p.sum(axis=1, keepdims=True)


def series_card(w: pd.DataFrame, rows: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """Log loss, Brier, calibration and argmax confusion per series, with paired differences
    against Pinnacle's early price, on the matches of `w` (rows: their outcome rows)."""
    y = w["y"].to_numpy(int)
    mid = w["match"].to_numpy()
    onehot = np.eye(3)[y]
    ll, out = {}, {}
    for name, col in SERIES.items():
        p = _probs(w, col)
        ll[name] = -np.log(np.clip(p[np.arange(len(y)), y], EPS, 1))
        brier = ((p - onehot) ** 2).sum(axis=1)
        mean_ll, ll_ci = _ci(ll[name], mid, n_boot)
        pred = p.argmax(axis=1)
        conf = np.zeros((3, 3), dtype=int)
        np.add.at(conf, (y, pred), 1)
        r = rows[["match", col, "won"]].dropna()
        rel = cal.reliability(r[col].to_numpy(float), r["won"].to_numpy(float),
                              groups=r["match"].to_numpy())  # fmt: skip
        out[name] = {
            "log_loss": mean_ll,
            "log_loss_ci95": ll_ci,
            "brier": float(brier.mean()),
            "accuracy": float((pred == y).mean()),
            "confusion": {
                "rows": "actual home/draw/away",
                "cols": "predicted home/draw/away",
                "counts": conf.tolist(),
            },  # fmt: skip
            "calibration": {
                k: rel.get(k) for k in ("n", "intercept", "intercept_ci95", "slope", "slope_ci95")
            },  # fmt: skip
        }
    for name in SERIES:
        if name == REF:
            continue
        d, ci = _ci(ll[name] - ll[REF], mid, n_boot)
        out[name]["log_loss_minus_early"] = d
        out[name]["log_loss_minus_early_ci95"] = ci
    return out


def auc_card(rows: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    groups = {"all": np.ones(len(rows), dtype=bool)}
    diffs = [(s, REF) for s in SERIES if s != REF]
    res = auc_mod.compare(rows.reset_index(drop=True), SERIES, groups, diffs, n_boot)["all"]
    return {k: res.get(k) for k in ("auc", "auc_ci95", "diff")}


def pick_12(w: pd.DataFrame) -> pd.DataFrame:
    """Model 12%'s pick per match at Pinnacle's early quoted odds (odds_<side>): match, side."""
    edge = np.column_stack([w[f"p_model_{s}"].to_numpy(float) * w[f"odds_{s}"].to_numpy(float) - 1
                            for s in SIDES])  # fmt: skip
    best = np.nanargmax(np.where(np.isfinite(edge), edge, -np.inf), axis=1)
    top = edge[np.arange(len(w)), best]
    ok = top >= EDGE_12
    return pd.DataFrame({"match": w["match"].to_numpy()[ok],
                         "side": np.array(SIDES)[best[ok]]})  # fmt: skip


def threshold_card(rows: pd.DataFrame, picks: pd.DataFrame, odds: pd.DataFrame) -> dict:
    """2 x 2 counts of picked by won over every outcome row, and the picks' win rate beside
    their mean implied chance (1/odds) and mean Pinnacle fair early chance."""
    key = picks.assign(picked=1).drop_duplicates(["match", "side"])
    r = rows.merge(key, on=["match", "side"], how="left")
    picked = r["picked"].fillna(0).astype(bool).to_numpy()
    won = r["won"].to_numpy(float) == 1
    pk = r[picked].merge(odds, on=["match", "side"], how="left")
    return {
        "picked_won": int((picked & won).sum()),
        "picked_lost": int((picked & ~won).sum()),
        "not_picked_won": int((~picked & won).sum()),
        "not_picked_lost": int((~picked & ~won).sum()),
        "picks": int(picked.sum()),
        "win_rate": float(won[picked].mean()) if picked.any() else None,
        "implied": float((1 / pk["odds"]).mean()) if len(pk) and pk["odds"].notna().any() else None,
        "fair_early": float(r.loc[picked, "fair_early"].mean()) if picked.any() else None,
        "recall": float((picked & won).sum() / won.sum()) if won.any() else None,
    }


def match_card(rows: pd.DataFrame, pins: pd.DataFrame, lean: pd.DataFrame,
               n_boot: int = N_BOOT) -> dict:  # fmt: skip
    """The whole 1X2 scorecard for `rows` (one league or pooled)."""
    w = wide(rows)
    keep = set(w["match"])
    rows = rows[rows["match"].isin(keep)].reset_index(drop=True)
    w = w.merge(pins, on=["league", "season", "home", "away"], how="left")
    odds = pd.concat(
        [w[["match", f"odds_{s}"]].rename(columns={f"odds_{s}": "odds"}).assign(side=s)
         for s in SIDES], ignore_index=True)  # fmt: skip
    lean_picks = pd.DataFrame(columns=["match", "side"])
    if len(lean):
        lk = lean.merge(w[["league", "season", "home", "away", "match"]],
                        on=["league", "season", "home", "away"])  # fmt: skip
        lean_picks = lk.rename(columns={"market": "side"})[["match", "side"]]
    return {
        "matches": int(len(w)),
        "rows": int(len(rows)),
        "series": series_card(w, rows, n_boot),
        "auc": auc_card(rows, n_boot),
        "thresholds": {
            "model_12": threshold_card(rows, pick_12(w), odds),
            "lean": threshold_card(rows, lean_picks, odds),
        },
    }


# --- corners -----------------------------------------------------------------------------


def corner_baseline(cm: pd.DataFrame, start, end, refit: str = CORNER_REFIT) -> pd.DataFrame:
    """Each block's league-mean home and away corners over the 730 days before it."""
    cm = cm.sort_values("date").reset_index(drop=True)
    d = pd.to_datetime(cm["date"])
    out = []
    for lo, hi in blocks(start, end, refit):
        test = cm[(d >= lo) & (d < hi)]
        train = cm[(d < lo) & (d >= lo - CORNER_WINDOW)]
        if test.empty or train.empty:
            continue
        t = test[["date", "home", "away"]].copy()
        t["b_home"] = float(train["home_corners"].mean())
        t["b_away"] = float(train["away_corners"].mean())
        out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _nb(m: np.ndarray, a: np.ndarray):
    n = 1.0 / np.maximum(a, 1e-9)
    return n, n / (n + m)


def count_card(y: np.ndarray, m: np.ndarray, alpha: np.ndarray | None, mid: np.ndarray,
               n_boot: int = N_BOOT) -> tuple[dict, np.ndarray, np.ndarray]:  # fmt: skip
    """RMSE, MAE, log score and interval coverage for means `m` (NB2 with `alpha`, else
    Poisson). Returns the card and the per-row squared error and log score."""
    se = (m - y) ** 2
    if alpha is None:
        logs = poisson.logpmf(y, m)
        dist = poisson(m)
    else:
        n, p = _nb(m, alpha)
        logs = nbinom.logpmf(y, n, p)
        dist = nbinom(n, p)
    card = {"n": int(len(y)), "rmse": float(np.sqrt(se.mean())), "mae": float(np.abs(m - y).mean()),
            "log_score": float(logs.mean()), "mean_pred": float(m.mean()),
            "mean_count": float(y.mean())}  # fmt: skip
    for lvl in INTERVALS:
        lo, hi = dist.ppf((1 - lvl) / 2), dist.ppf((1 + lvl) / 2)
        card[f"coverage_{int(lvl * 100)}"] = float(((y >= lo) & (y <= hi)).mean())
    return card, se, logs


def corner_card(cw: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """Model (f) and the baseline on the same team-match rows (home and away stacked)."""
    y = np.r_[cw["home_corners"].to_numpy(float), cw["away_corners"].to_numpy(float)]
    mf = np.r_[cw["m_home"].to_numpy(float), cw["m_away"].to_numpy(float)]
    af = np.r_[cw["alpha_home"].to_numpy(float), cw["alpha_away"].to_numpy(float)]
    mb = np.r_[cw["b_home"].to_numpy(float), cw["b_away"].to_numpy(float)]
    key = (cw["date"].astype(str) + "|" + cw["home"].astype(str)).to_numpy()
    mid = np.r_[key, key]
    ok = np.isfinite(y) & np.isfinite(mf) & np.isfinite(mb) & np.isfinite(af)
    y, mf, af, mb, mid = y[ok], mf[ok], af[ok], mb[ok], mid[ok]
    f, se_f, ls_f = count_card(y, mf, af, mid, n_boot)
    b, se_b, ls_b = count_card(y, mb, None, mid, n_boot)
    d_se, d_se_ci = _ci(se_f - se_b, mid, n_boot)
    d_ls, d_ls_ci = _ci(ls_f - ls_b, mid, n_boot)
    return {"matches": int(len(np.unique(mid))), "model_f": f, "baseline": b,
            "mse_f_minus_baseline": d_se, "mse_f_minus_baseline_ci95": d_se_ci,
            "log_score_f_minus_baseline": d_ls,
            "log_score_f_minus_baseline_ci95": d_ls_ci}  # fmt: skip


# --- loading (Actions only) ----------------------------------------------------------------


def run_league(code: str, out_dir: Path) -> dict:
    from soccer_stats.corners_live import corner_matches
    from soccer_stats.data import pinnacle_prices
    from soccer_stats.lab import model_assumptions as ma
    from soccer_stats.lab import residual_map as rm

    holdout = Holdout(LOCK)
    info: dict = {"league": code}
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rows, _m, lean, rep = rm.load_league(code, holdout, MATCH_FIRST, MATCH_LAST)
        holdout.check(rows["date"])
        pins = pinnacle_prices(code, range(MATCH_FIRST, MATCH_LAST + 1))
        holdout.check(pins["date"])
        pins = pins.assign(league=code, season=pins["season"].astype(str))[
            ["league", "season", "home", "away", "odds_home", "odds_draw", "odds_away"]
        ].drop_duplicates(["league", "season", "home", "away"])
        rows.to_csv(out_dir / f"score_rows_{code}.csv.gz", index=False)
        pins.to_csv(out_dir / f"score_pins_{code}.csv.gz", index=False)
        lean = lean.assign(league=code) if len(lean) else lean
        lean.to_csv(out_dir / f"score_lean_{code}.csv.gz", index=False)
        info["match"] = {"rows": int(len(rows)), "lean_bets": int(len(lean)),
                         "xg_weight": rep.get("xg_weight"),
                         "seasons": rep.get("seasons")}  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - reported
        info["match"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        cm = corner_matches(code, seasons=range(CORNER_FIRST - 2, CORNER_LAST + 1))
        cm = cm[pd.to_datetime(cm["date"]) < LOCK]
        holdout.check(cm["date"])
        start = f"{CORNER_FIRST}-07-01"
        cw = ma.corner_walk(cm, start, LOCK)
        base = corner_baseline(cm, start, LOCK)
        cw = cw.merge(base, on=["date", "home", "away"], how="inner")
        cw.assign(league=code).to_csv(out_dir / f"score_corners_{code}.csv.gz", index=False)
        info["corners"] = {"rows": int(len(cw))}
    except Exception as exc:  # noqa: BLE001 - reported
        info["corners"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    (out_dir / f"score_{code}.json").write_text(json.dumps(info, default=str))
    return info


def _read(in_dir: Path, pattern: str, **kw) -> pd.DataFrame:
    parts = []
    for f in sorted(in_dir.rglob(pattern)):
        try:
            parts.append(pd.read_csv(f, **kw))
        except pd.errors.EmptyDataError:
            continue
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def pool(in_dir: Path, n_boot: int = N_BOOT) -> dict:
    in_dir = Path(in_dir)
    rows = _read(in_dir, "score_rows_*.csv.gz", dtype={"season": str})
    pins = _read(in_dir, "score_pins_*.csv.gz", dtype={"season": str})
    lean = _read(in_dir, "score_lean_*.csv.gz", dtype={"season": str})
    corners = _read(in_dir, "score_corners_*.csv.gz")
    infos = {json.loads(f.read_text())["league"]: json.loads(f.read_text())
             for f in sorted(in_dir.rglob("score_*.json"))}  # fmt: skip
    match = {}
    if len(rows):
        match["pooled"] = match_card(rows, pins, lean, n_boot)
        for lg, g in rows.groupby("league"):
            lg_lean = lean[lean["league"] == lg] if len(lean) else lean
            match[lg] = match_card(g, pins[pins["league"] == lg], lg_lean, n_boot)
    corner = {}
    if len(corners):
        corner["pooled"] = corner_card(corners, n_boot)
        for lg, g in corners.groupby("league"):
            corner[lg] = corner_card(g, n_boot)
    return {
        "kind": "model_scorecard",
        "part": 1,
        "development": True,
        "descriptive": True,
        "locked_from": str(LOCK.date()),
        "match_seasons": "2019/20-2023/24",
        "corner_seasons": "2017/18-2023/24",
        "match": match,
        "corners": corner,
        "leagues": infos,
        "note": ("Descriptive scorecards, out of sample, beside Pinnacle's price where one "
                 "exists. Log loss and Brier: lower is better; AUC: higher is better; a "
                 "calibration slope below 1 means over-confident. Corner counts have no price "
                 "history, so model (f) is set beside a league-mean baseline."),
        "runs": {"model_scorecard": os.environ.get("GITHUB_RUN_ID")},
    }  # fmt: skip


def summary_lines(res: dict) -> list[str]:
    out = []
    for lg, c in res["match"].items():
        s = c["series"]
        out.append(f"1X2 {lg}: {c['matches']} matches; log loss " + ", ".join(
            f"{k} {v['log_loss']:.4f}" for k, v in s.items()) + "; AUC " + ", ".join(
            f"{k} {v}" for k, v in (c["auc"]["auc"] or {}).items()))  # fmt: skip
        t = c["thresholds"]
        for k in ("model_12", "lean"):
            b = t[k]
            out.append(f"  {k}: {b['picks']} picks, won {b['win_rate']}, implied {b['implied']}, "
                       f"fair early {b['fair_early']}")  # fmt: skip
    for lg, c in res["corners"].items():
        f, b = c["model_f"], c["baseline"]
        out.append(f"corners {lg}: RMSE f {f['rmse']:.3f} vs base {b['rmse']:.3f}; log score "
                   f"{f['log_score']:.4f} vs {b['log_score']:.4f}; "
                   f"80% cover {f['coverage_80']:.3f}")  # fmt: skip
    return out


def print_file(text: str) -> None:
    sha = hashlib.sha256(text.encode()).hexdigest()
    b64 = base64.b64encode(gzip.compress(text.encode(), mtime=0)).decode()
    print(f"MODEL_SCORECARD_SHA256 {sha}")
    for i in range(0, len(b64), B64_CHUNK):
        print(f"MODEL_SCORECARD_B64 {i // B64_CHUNK} {b64[i : i + B64_CHUNK]}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="run one league into --out")
    p.add_argument("--pool", default="", help="pool every league's files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        res = run_league(a.league, Path(a.out))
        print(f"MODEL_SCORECARD_LEAGUE {json.dumps(res, default=str)}")
        return
    res = pool(Path(a.pool or a.out), a.boot)
    print("\n".join(summary_lines(res)))
    text = json.dumps(res, default=str, indent=1)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "model_scorecard.json").write_text(text)
    print_file(text)


if __name__ == "__main__":
    main()
