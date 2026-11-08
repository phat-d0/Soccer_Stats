"""Model evaluation stage 2, part 2 (docs/lab.md "Stage 2, part 2: method", written before
any run): the goal distributions, the cup model and goalscorer B. Descriptive; no key, 0
credits. 2024/25 domestic stays locked; the cup's 2024/25-2025/26 closes are the ones
cup_backtest already read.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson

from soccer_stats import calibration as cal
from soccer_stats.lab import auc as auc_mod
from soccer_stats.lab.harness import Holdout, blocks
from soccer_stats.lab.model_scorecard import _ci
from soccer_stats.odds import devig_shin

LOCK = pd.Timestamp("2024-07-01")
FIRST, LAST, BURN = 2019, 2023, 2
MAX_T = 6  # total goals 0..5 and 6+
WINDOW = pd.Timedelta(days=730)
REFIT = "28D"
T_GOAL = 0.20
N_BOOT = 2000
EPS = 1e-12
H2H = ("home", "draw", "away")


def _bin_ll(p, y):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _fair2(over, under) -> np.ndarray:
    """Shin-fair over chance per row (NaN without both prices)."""
    o, u = np.asarray(over, float), np.asarray(under, float)
    out = np.full(len(o), np.nan)
    ok = np.isfinite(o) & np.isfinite(u) & (o > 1) & (u > 1)
    for i in np.flatnonzero(ok):
        out[i] = devig_shin(np.array([o[i], u[i]]))[0]
    return out


def binary_card(df: pd.DataFrame, series: dict[str, str], ref: str, n_boot: int = N_BOOT) -> dict:
    """Log loss, Brier, AUC and calibration per series on rows of `df` (`won`, `match`),
    with paired log-loss differences against `ref`."""
    d = df.dropna(subset=list(series.values())).reset_index(drop=True)
    y = d["won"].to_numpy(float)
    mid = d["match"].to_numpy()
    out, ll = {}, {}
    for name, col in series.items():
        p = d[col].to_numpy(float)
        ll[name] = _bin_ll(p, y)
        m, ci = _ci(ll[name], mid, n_boot)
        rel = cal.reliability(p, y, groups=mid)
        out[name] = {"log_loss": m, "log_loss_ci95": ci, "brier": float(((p - y) ** 2).mean()),
                     "slope": rel.get("slope"), "slope_ci95": rel.get("slope_ci95"),
                     "auc": auc_mod.auc(p, y)}  # fmt: skip
    for name in series:
        if name != ref:
            d_, ci = _ci(ll[name] - ll[ref], mid, n_boot)
            out[name]["log_loss_minus_ref"] = d_
            out[name]["log_loss_minus_ref_ci95"] = ci
    return {"rows": int(len(d)), "ref": ref, "series": out}


# --- (a) goal distributions ---------------------------------------------------------------


def pinnacle_ou(code: str, years) -> pd.DataFrame:
    """Pinnacle's own O/U 2.5 early and close (P>2.5, P<2.5, PC>2.5, PC<2.5; no fallback)."""
    from soccer_stats.data import download, season_code

    parts = []
    for y in years:
        raw = pd.read_csv(download(code, y), encoding="latin-1", on_bad_lines="skip")
        raw = raw.dropna(subset=[c for c in ("HomeTeam", "AwayTeam", "FTHG") if c in raw])
        out = pd.DataFrame({"season": season_code(y), "home": raw["HomeTeam"],
                            "away": raw["AwayTeam"]})  # fmt: skip
        for col, name in (("P>2.5", "o_early"), ("P<2.5", "u_early"), ("PC>2.5", "o_close"),
                          ("PC<2.5", "u_close")):  # fmt: skip
            out[name] = pd.to_numeric(raw[col], errors="coerce") if col in raw else np.nan
        parts.append(out)
    return pd.concat(parts, ignore_index=True).drop_duplicates(["season", "home", "away"])


def goal_baseline(m: pd.DataFrame, start, end) -> pd.DataFrame:
    """Each 28-day block's league-mean home and away goals over the 730 days before it."""
    m = m.sort_values("date").reset_index(drop=True)
    d = pd.to_datetime(m["date"])
    out = []
    for lo, hi in blocks(start, end, REFIT):
        test = m[(d >= lo) & (d < hi)]
        train = m[(d < lo) & (d >= lo - WINDOW)]
        if test.empty or train.empty:
            continue
        t = test[["season", "home", "away"]].copy()
        t["b_home"] = float(train["home_goals"].mean())
        t["b_away"] = float(train["away_goals"].mean())
        out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def goal_rows(preds: pd.DataFrame) -> pd.DataFrame:
    """From walk-forward rows with `matrix`: the model's over 2.5, total-goals distribution
    (t0..t5, t6 = 6+), and the chance of each side's actual goal count."""
    rec = []
    for r in preds.itertuples(index=False):
        mx = np.asarray(r.matrix, float)
        mx = mx / mx.sum()
        n = mx.shape[0]
        tot = np.zeros(MAX_T + 1)
        for i in range(n):
            for j in range(n):
                tot[min(i + j, MAX_T)] += mx[i, j]
        hg, ag = int(r.home_goals), int(r.away_goals)
        rec.append({
            "season": str(r.season), "date": r.date, "home": r.home, "away": r.away,
            "home_goals": hg, "away_goals": ag, "exp_home": float(r.exp_home),
            "exp_away": float(r.exp_away), "p_over": float(tot[3:].sum()),
            "p_hg": float(mx[hg, :].sum()) if hg < n else 0.0,
            "p_ag": float(mx[:, ag].sum()) if ag < n else 0.0,
            **{f"t{k}": float(tot[k]) for k in range(MAX_T + 1)},
        })  # fmt: skip
    return pd.DataFrame(rec)


def rps(dist: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Ranked probability score over the MAX_T + 1 ordered total-goal classes."""
    cdf = np.cumsum(dist, axis=1)[:, :-1]
    obs = (np.arange(MAX_T)[None, :] >= np.minimum(y, MAX_T)[:, None]).astype(float)
    return ((cdf - obs) ** 2).sum(axis=1) / MAX_T


def poisson_total(mu: np.ndarray) -> np.ndarray:
    k = np.arange(MAX_T)
    p = poisson.pmf(k[None, :], mu[:, None])
    return np.column_stack([p, 1 - p.sum(axis=1)])


def goals_card(g: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    g = g.reset_index(drop=True)
    g["match"] = g["season"].astype(str) + "|" + g["home"] + "|" + g["away"]
    total = (g["home_goals"] + g["away_goals"]).to_numpy(int)
    g["won"] = (total >= 3).astype(float)
    g["q_early"] = _fair2(g["o_early"], g["u_early"])
    g["q_close"] = _fair2(g["o_close"], g["u_close"])
    ou = binary_card(g, {"model": "p_over", "pinnacle_early": "q_early",
                         "pinnacle_close": "q_close"}, "pinnacle_early", n_boot)  # fmt: skip
    ok = g[["b_home", "b_away"]].notna().all(axis=1).to_numpy()
    h = g[ok].reset_index(drop=True)
    tot = total[ok]
    mid = h["match"].to_numpy()
    dist = h[[f"t{k}" for k in range(MAX_T + 1)]].to_numpy(float)
    base = poisson_total(h["b_home"].to_numpy(float) + h["b_away"].to_numpy(float))
    r_m, r_b = rps(dist, tot), rps(base, tot)
    cdf = np.cumsum(dist, axis=1)
    lo = (cdf < 0.1).sum(axis=1)
    hi = (cdf < 0.9).sum(axis=1)
    tt = np.minimum(tot, MAX_T)
    hg, ag = h["home_goals"].to_numpy(float), h["away_goals"].to_numpy(float)
    eh, ea = h["exp_home"].to_numpy(float), h["exp_away"].to_numpy(float)
    ls_m = np.log(np.clip(np.r_[h["p_hg"], h["p_ag"]], EPS, 1))
    ls_b = np.r_[poisson.logpmf(hg, h["b_home"]), poisson.logpmf(ag, h["b_away"])]
    mid2 = np.r_[mid, mid]
    d_rps, d_rps_ci = _ci(r_m - r_b, mid, n_boot)
    d_ls, d_ls_ci = _ci(ls_m - ls_b, mid2, n_boot)
    return {
        "over_under_25": ou,
        "total_goals": {"matches": int(len(h)), "rps_model": float(r_m.mean()),
                        "rps_baseline": float(r_b.mean()), "rps_model_minus_baseline": d_rps,
                        "rps_model_minus_baseline_ci95": d_rps_ci,
                        "coverage_80": float(((tt >= lo) & (tt <= hi)).mean())},
        "team_goals": {"rows": int(2 * len(h)),
                       "rmse_model": float(np.sqrt(np.r_[(eh - hg) ** 2, (ea - ag) ** 2].mean())),
                       "rmse_baseline": float(np.sqrt(np.r_[(h["b_home"] - hg) ** 2,
                                                            (h["b_away"] - ag) ** 2].mean())),
                       "mae_model": float(np.r_[abs(eh - hg), abs(ea - ag)].mean()),
                       "log_score_model": float(ls_m.mean()),
                       "log_score_baseline": float(ls_b.mean()),
                       "log_score_model_minus_baseline": d_ls,
                       "log_score_model_minus_baseline_ci95": d_ls_ci},
    }  # fmt: skip


def run_goals(code: str, out_dir: Path) -> dict:
    from soccer_stats import backtest
    from soccer_stats import leagues as lgs
    from soccer_stats.data import load_matches
    from soccer_stats.models import DixonColes
    from soccer_stats.publish import XG_WEIGHT
    from soccer_stats.xg import with_xg

    holdout = Holdout(LOCK)
    m = load_matches([code], range(FIRST - BURN, LAST + 1))
    m, err = with_xg(m)
    weight = XG_WEIGHT if m["home_xg"].notna().any() else 0.0
    if lgs.LEAGUES[code].understat and not weight:
        raise RuntimeError(f"no Understat xG attached ({err})")
    m = m[m["date"] < LOCK]
    holdout.check(m["date"])
    ridge = lgs.match_ridge(code)
    factory = functools.partial(DixonColes, xg_weight=weight, ridge=ridge)
    preds = backtest.walk_forward(m, start=f"{FIRST}-07-01", model_factory=factory,
                                  keep_matrix=True)  # fmt: skip
    preds = preds[pd.to_datetime(preds["date"]) < LOCK]
    holdout.check(preds["date"])
    g = goal_rows(preds)
    keys = ["season", "home", "away"]
    m2 = m.assign(season=m["season"].astype(str))
    g = g.merge(goal_baseline(m2, f"{FIRST}-07-01", LOCK).drop_duplicates(keys), on=keys,
                how="left")  # fmt: skip
    g = g.merge(pinnacle_ou(code, range(FIRST, LAST + 1)), on=keys, how="left")
    g.insert(0, "league", code)
    out_dir.mkdir(parents=True, exist_ok=True)
    g.to_csv(out_dir / f"goals_{code}.csv.gz", index=False)
    info = {"league": code, "xg_weight": weight, "ridge": ridge, "rows": int(len(g))}
    (out_dir / f"goals_{code}.json").write_text(json.dumps(info))
    return info


# --- (b) the cup model --------------------------------------------------------------------


def cup_rows(preds: pd.DataFrame, pin: pd.DataFrame, dk: pd.DataFrame) -> pd.DataFrame:
    """One row per outcome of each covered, finished match with Pinnacle's full close:
    p_model, fair_pin, fair_dk (NaN without DraftKings), won, match, season."""
    from soccer_stats import cup_backtest as cb
    from soccer_stats import match_calibration as mc

    df = cb.joined(preds, pin, dk)
    keep = (
        df["covered"].astype(bool) & ~df["low_data"].astype(bool) & df["home_goals"].notna()
    ).to_numpy() & cb._ok(df, [f"close_{m}" for m in H2H])
    d = df[keep].reset_index(drop=True)
    if d.empty:
        return pd.DataFrame()
    pf = np.vstack([devig_shin(r) for r in d[[f"close_{m}" for m in H2H]].to_numpy(float)])
    dk_ok = cb._ok(d, [f"odds_{m}" for m in H2H])
    df_ = np.full((len(d), 3), np.nan)
    if dk_ok.any():
        df_[dk_ok] = np.vstack(
            [devig_shin(r) for r in d.loc[dk_ok, [f"odds_{m}" for m in H2H]].to_numpy(float)]
        )
    y = mc.outcome_index(d["home_goals"], d["away_goals"], "h2h")
    parts = []
    for i, s in enumerate(H2H):
        parts.append(pd.DataFrame({"match": d["espn_id"].astype(str),
                                   "season": d["season"].astype(str),
                                   "side": s, "p_model": d[f"p_{s}"].astype(float),
                                   "fair_pin": pf[:, i], "fair_dk": df_[:, i],
                                   "won": (np.asarray(y) == i).astype(float)}))  # fmt: skip
    return pd.concat(parts, ignore_index=True)


def cup_card(rows: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """Outcome rows -> per-match 1X2 log loss (model vs Pinnacle's close, and DraftKings'
    close on the matches it prices) plus the binary AUC and calibration."""
    out = {"matches": int(rows["match"].nunique())}
    for name, cols, sub in (("pinnacle", ("p_model", "fair_pin"), rows),
                            ("draftkings", ("p_model", "fair_pin", "fair_dk"),
                             rows[rows.groupby("match")["fair_dk"].transform(
                                 lambda v: v.notna().all())])):  # fmt: skip
        if sub.empty:
            continue
        won = sub[sub["won"] == 1].set_index("match")
        lls = {c: -np.log(np.clip(won[c].to_numpy(float), EPS, 1)) for c in cols}
        mid = won.index.to_numpy()
        blk = {"matches": int(len(won))}
        for c in cols:
            m_, ci = _ci(lls[c], mid, n_boot)
            blk[c] = {"log_loss": m_, "log_loss_ci95": ci}
        for c in cols:
            if c != "fair_pin":
                d_, ci = _ci(lls[c] - lls["fair_pin"], mid, n_boot)
                blk[c]["minus_pinnacle"] = d_
                blk[c]["minus_pinnacle_ci95"] = ci
        out[name] = blk
    out["binary"] = binary_card(rows, {"model": "p_model", "pinnacle_close": "fair_pin"},
                                "pinnacle_close", n_boot)  # fmt: skip
    return out


def run_cup(out_dir: Path, closes: str) -> dict:
    from soccer_stats import cup_backtest as cb
    from soccer_stats import cup_model as cm

    loaded = cb.load(closes)
    if loaded is None:
        raise RuntimeError("no UCL closes file")
    hist = cm.history()
    if hist.get("error"):
        raise RuntimeError(hist["error"])
    rows = cup_rows(hist["preds"], *loaded)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows.to_csv(out_dir / "cup_rows.csv.gz", index=False)
    return {"rows": int(len(rows)), "matches": int(rows["match"].nunique()) if len(rows) else 0}


# --- (c) goalscorer B ---------------------------------------------------------------------


def goalscorer_card(df: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    d = df.copy()
    d["price"] = 1.0 / d["entry_odds"].astype(float)
    d["won"] = d["scored"].astype(float)
    d["match"] = d["match_id"].astype(str)
    series = {"model_b": "p_B", "season_goals": "bench_goals", "season_xg": "bench_xg",
              "best_price": "price"}  # fmt: skip
    card = binary_card(d, series, "best_price", n_boot)
    d = d.dropna(subset=["p_B", "entry_odds", "won"])
    pick = (d["p_B"].astype(float) * d["entry_odds"].astype(float) - 1) >= T_GOAL
    w = d["won"].to_numpy(float) == 1
    card["threshold"] = {
        "t": T_GOAL, "picks": int(pick.sum()),
        "picked_won": int((pick & w).sum()), "picked_lost": int((pick & ~w).sum()),
        "not_picked_won": int((~pick & w).sum()), "not_picked_lost": int((~pick & ~w).sum()),
        "win_rate": float(w[pick].mean()) if pick.any() else None,
        "implied": float(d.loc[pick, "price"].mean()) if pick.any() else None,
        "model_mean": float(d.loc[pick, "p_B"].mean()) if pick.any() else None,
    }  # fmt: skip
    card["season"] = "2025/26 (development; §16's judged forward window not read)"
    return card


# --- pool -----------------------------------------------------------------------------------


def _read(in_dir: Path, pattern: str) -> pd.DataFrame:
    parts = []
    for f in sorted(Path(in_dir).rglob(pattern)):
        try:
            parts.append(pd.read_csv(f, dtype={"season": str}))
        except pd.errors.EmptyDataError:
            continue
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def pool(in_dir: Path, goal_dir: Path | None, n_boot: int = N_BOOT) -> dict:
    res: dict = {"kind": "model_scorecard", "part": 2, "development": True, "descriptive": True,
                 "runs": {"model_scorecard2": os.environ.get("GITHUB_RUN_ID")}}  # fmt: skip
    g = _read(in_dir, "goals_*.csv.gz")
    if len(g):
        res["goals"] = {
            "seasons": "2019/20-2023/24",
            "pooled": goals_card(g, n_boot),
            "leagues": {lg: goals_card(x, n_boot) for lg, x in g.groupby("league")},
        }
    c = _read(in_dir, "cup_rows.csv.gz")
    if len(c):
        res["cup"] = {"seasons": "2024/25-2025/26 (the bought closes)", **cup_card(c, n_boot)}
    if goal_dir:
        gs = _read(goal_dir, "judged_*.csv.gz")
        if len(gs):
            res["goalscorer"] = goalscorer_card(gs, n_boot)
    return res


def summary_lines(res: dict) -> list[str]:
    out = []
    if "goals" in res:
        p = res["goals"]["pooled"]
        s = p["over_under_25"]["series"]
        out.append("O/U 2.5 log loss: " + ", ".join(f"{k} {v['log_loss']:.4f} AUC {v['auc']:.3f}"
                                                    for k, v in s.items()))  # fmt: skip
        out.append(f"  model - early {s['model'].get('log_loss_minus_ref'):+.4f} "
                   f"{s['model'].get('log_loss_minus_ref_ci95')}")  # fmt: skip
        t, tg = p["total_goals"], p["team_goals"]
        out.append(f"total goals RPS model {t['rps_model']:.4f} vs base {t['rps_baseline']:.4f}; "
                   f"80% cover {t['coverage_80']:.3f}")  # fmt: skip
        out.append(
            f"team goals RMSE {tg['rmse_model']:.3f} vs {tg['rmse_baseline']:.3f}; "
            f"log score {tg['log_score_model']:.4f} vs {tg['log_score_baseline']:.4f}"
        )
    if "cup" in res:
        pin = res["cup"].get("pinnacle", {})
        if pin:
            out.append(f"cup: {pin['matches']} matches; model {pin['p_model']['log_loss']:.4f} vs "
                       f"Pinnacle close {pin['fair_pin']['log_loss']:.4f}")  # fmt: skip
    if "goalscorer" in res:
        s = res["goalscorer"]["series"]
        out.append(
            "goalscorer log loss: " + ", ".join(f"{k} {v['log_loss']:.4f}" for k, v in s.items())
        )
        t = res["goalscorer"]["threshold"]
        out.append(f"  T 20%: {t['picks']} picks, won {t['win_rate']}, implied {t['implied']}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="goal rows for one league into --out")
    p.add_argument("--cup", default="", help="cup rows into --out from this UCL closes file")
    p.add_argument("--pool", default="", help="pool the files under this folder")
    p.add_argument("--goal-dir", default="", help="goalscorer-test judged_*.csv.gz folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        print(f"SCORECARD2_LEAGUE {json.dumps(run_goals(a.league, Path(a.out)))}")
        return
    if a.cup:
        print(f"SCORECARD2_CUP {json.dumps(run_cup(Path(a.out), a.cup))}")
        return
    res = pool(Path(a.pool or a.out), Path(a.goal_dir) if a.goal_dir else None, a.boot)
    print("\n".join(summary_lines(res)))
    text = json.dumps(res, default=str, indent=1)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "model_scorecard2.json").write_text(text)
    from soccer_stats.lab.model_scorecard import print_file

    print_file(text)


if __name__ == "__main__":
    main()
