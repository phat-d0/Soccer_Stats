"""Totals markets: corners, goal lines 0.5-5.5 and team totals (docs/totals.md).

Pre-registered before any data was loaded. Per league:

A. corners: independent NB2 regressions for home and away corners on earlier-match
   features (corners and shots for/against over the last 10, as log ratios to the
   league average; the match model's log supremacy and log total), convolved into the
   total; refitted every 28 days on the 730 days before. Baseline: NB2 at the league's
   mean and dispersion over the 730 days before. Check: a direct NB2 on the total, and
   the home/away residual correlation.
B. goal totals 0.5-5.5: the Dixon-Coles walk-forward score matrix, unchanged; baseline
   the league's over rate over the 365 days before. At 2.5, Pinnacle's early/close
   prices through lab.metrics.
C. team totals 0.5/1.5/2.5: the matrix's marginals; baseline the league's home (or
   away) over rate over the 365 days before.

Every feature, fit and baseline uses earlier matches only. 2025/26 is dropped first.
"""

from __future__ import annotations

import functools

import numpy as np
import pandas as pd
from scipy.special import expit

from soccer_stats.edge import signals
from soccer_stats.lab import metrics
from soccer_stats.lab.harness import walk_forward as harness_walk_forward
from soccer_stats.models.player_counts import NBRegression, nb_pmf

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1")
FIRST_DATA = 2014
DC_START = "2015-07-01"
FIRST_SCORED = 2017
LAST = 2024
HOLDOUT_START = pd.Timestamp("2025-07-01")
CORNER_LINES = (8.5, 9.5, 10.5, 11.5)
GOAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)
TEAM_LINES = (0.5, 1.5, 2.5)
FORM = 10
MIN_FORM = 3
LEAGUE_DAYS = 365
TRAIN_DAYS = 730
MIN_TRAIN = 1000
KMAX = 30  # corners per side and in total, 0..KMAX (last bucket holds the tail)
MIN_COVERAGE = 0.9
N_BOOT = 20000
MARKET_LINES = len(CORNER_LINES) + len(GOAL_LINES) + 2 * len(TEAM_LINES)  # 16
TESTS = MARKET_LINES * len(LEAGUES) + 2 * len(LEAGUES)  # 108
CORNER_STATS = ("cf", "ca", "sf", "sa")
CORNER_FEATURES = [f"{s}_{c}" for s in ("h", "a") for c in CORNER_STATS] + ["log_sup", "log_tot"]


# ---------- features ----------


def trailing_mean(dates, values, days: int) -> np.ndarray:
    """Per row, the mean of `values` over rows dated in [date - days, date): strictly
    earlier rows only (same-day rows excluded). NaN when there are none."""
    d = pd.to_datetime(pd.Series(dates)).to_numpy("datetime64[ns]")
    order = np.argsort(d, kind="stable")
    ds, vs = d[order], np.asarray(values, float)[order]
    ok = np.isfinite(vs)
    cs = np.r_[0.0, np.cumsum(np.where(ok, vs, 0.0))]
    cn = np.r_[0, np.cumsum(ok)]
    lo = np.searchsorted(ds, ds - np.timedelta64(days, "D"), side="left")
    hi = np.searchsorted(ds, ds, side="left")
    n = cn[hi] - cn[lo]
    mean = np.where(n > 0, (cs[hi] - cs[lo]) / np.maximum(n, 1), np.nan)
    out = np.empty_like(mean)
    out[order] = mean
    return out


def corner_features(df: pd.DataFrame) -> pd.DataFrame:
    """h_/a_{cf,ca,sf,sa}: each side's last-FORM-match means (earlier matches only, any
    venue, across seasons) as log ratios to the league's per-team mean over the
    LEAGUE_DAYS before; log_sup/log_tot from the match model's expected goals."""
    m = df.sort_values("date", kind="stable")
    long = (
        pd.concat(
            [
                pd.DataFrame(
                    {
                        "row": m.index,
                        "date": m["date"],
                        "team": m[side],
                        "side": side[0],
                        "cf": m[f"{side}_corners"],
                        "ca": m[f"{other}_corners"],
                        "sf": m[f"{side}_shots"],
                        "sa": m[f"{other}_shots"],
                    }
                )
                for side, other in (("home", "away"), ("away", "home"))
            ]
        )
        .sort_values(["date", "row"], kind="stable")
        .reset_index(drop=True)
    )
    g = long.groupby("team", sort=False)
    out = pd.DataFrame(index=df.index)
    for c in CORNER_STATS:
        form = g[c].transform(lambda s: s.shift(1).rolling(FORM, min_periods=MIN_FORM).mean())
        # League per-team mean over the year before: both sides' values pooled.
        league = trailing_mean(long["date"], long[c], LEAGUE_DAYS)
        long[f"f_{c}"] = np.log(np.clip(form, 0.1, None) / np.clip(league, 0.1, None))
    for s in ("h", "a"):
        part = long[long["side"] == s].set_index("row")
        for c in CORNER_STATS:
            out[f"{s}_{c}"] = part[f"f_{c}"]
    out["log_sup"] = np.log(df["exp_home"] / df["exp_away"])
    out["log_tot"] = np.log(df["exp_home"] + df["exp_away"])
    return out


# ---------- corner models (fit/predict on frames with features and counts) ----------


def _window(train: pd.DataFrame) -> pd.DataFrame:
    end = pd.to_datetime(train["time"]).max()
    return train[pd.to_datetime(train["time"]) > end - pd.Timedelta(days=TRAIN_DAYS)]


def convolve(ph: np.ndarray, pa: np.ndarray, kmax: int = KMAX) -> np.ndarray:
    """Row-wise distribution of home + away (independent), on 0..kmax with a tail bucket."""
    out = np.zeros((len(ph), 2 * kmax + 1))
    for i in range(ph.shape[1]):
        out[:, i : i + pa.shape[1]] += ph[:, [i]] * pa
    tail = out[:, kmax:].sum(axis=1)
    out = out[:, : kmax + 1]
    out[:, -1] = tail
    return out


class CornersIndependent:
    """Primary: NB2 for home corners and for away corners, convolved."""

    def __init__(self, l2: float = 1.0):
        self.l2 = l2

    def fit(self, train: pd.DataFrame):
        t = _window(train)
        one = np.ones(len(t))
        self.home_ = NBRegression(CORNER_FEATURES, self.l2).fit(t, t["home_corners"], one)
        self.away_ = NBRegression(CORNER_FEATURES, self.l2).fit(t, t["away_corners"], one)
        return self

    def sides(self, test: pd.DataFrame):
        ph = nb_pmf(self.home_.rate(test), self.home_.alpha_, KMAX)
        pa = nb_pmf(self.away_.rate(test), self.away_.alpha_, KMAX)
        return ph, pa

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return convolve(*self.sides(test))


class CornersTotal:
    """Check: one NB2 on total corners with the same features."""

    def __init__(self, l2: float = 1.0):
        self.l2 = l2

    def fit(self, train: pd.DataFrame):
        t = _window(train)
        y = t["home_corners"] + t["away_corners"]
        self.m_ = NBRegression(CORNER_FEATURES, self.l2).fit(t, y, np.ones(len(t)))
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return nb_pmf(self.m_.rate(test), self.m_.alpha_, KMAX)


class CornersBaseline:
    """Baseline: NB2 at the league's mean and dispersion of total corners (no teams)."""

    def fit(self, train: pd.DataFrame):
        t = _window(train)
        y = (t["home_corners"] + t["away_corners"]).to_numpy(float)
        self.mean_ = float(y.mean())
        self.alpha_ = max((y.var() - self.mean_) / self.mean_**2, 1e-6)
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return nb_pmf(np.full(len(test), self.mean_), self.alpha_, KMAX)


def corner_predictions(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Walk-forward total-corner distributions (index = df rows) for each model."""
    feats = corner_features(df)
    data = pd.concat([df[["date", "home_corners", "away_corners"]], feats], axis=1)
    data["time"] = data["date"]
    data = data.dropna(subset=["home_corners", "away_corners", *CORNER_FEATURES])
    data = data[np.isfinite(data[CORNER_FEATURES]).all(axis=1)]
    start, end = data["time"].min(), data["time"].max() + pd.Timedelta(days=1)
    run = functools.partial(
        harness_walk_forward, data, start=start, end=end, refit="28D", min_train=MIN_TRAIN
    )
    out = {
        "model": run(CornersIndependent, {}),
        "total_nb": run(CornersTotal, {}),
        "baseline": run(CornersBaseline, {}),
    }
    return out, data


def residual_correlation(df: pd.DataFrame, data: pd.DataFrame, pred_index) -> float | None:
    """Correlation of the home and away Pearson residuals of one independent fit on the
    scored rows (descriptive only: is independence a fair approximation?)."""
    d = data.loc[pred_index]
    if len(d) < 100:
        return None
    m = CornersIndependent().fit(d.assign(time=d["date"]))
    out = []
    for side, model in (("home", m.home_), ("away", m.away_)):
        mu = model.rate(d)
        var = mu + model.alpha_ * mu**2
        out.append((d[f"{side}_corners"].to_numpy(float) - mu) / np.sqrt(var))
    return float(np.corrcoef(out[0], out[1])[0, 1])


# ---------- scoring ----------


def over_from_pmf(pmf: np.ndarray, line: float) -> np.ndarray:
    return pmf[:, int(np.ceil(line)) :].sum(axis=1)


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def recal_slope(p, y) -> float:
    """Slope of a logistic fit of y on logit(p) (1 = well calibrated spread)."""
    x = _logit(p)
    y = np.asarray(y, float)
    w = np.zeros(2)
    X = np.column_stack([np.ones_like(x), x])
    for _ in range(25):
        mu = expit(X @ w)
        h = (X * (mu * (1 - mu))[:, None]).T @ X + 1e-9 * np.eye(2)
        step = np.linalg.solve(h, X.T @ (y - mu))
        w += step
        if np.abs(step).max() < 1e-8:
            break
    return float(w[1])


def _boot_slope(p, y, groups, n: int = 400, seed: int = 0):
    codes, _ = pd.factorize(pd.Series(np.asarray(groups)).astype(str))
    rng = np.random.default_rng(seed)
    k = codes.max() + 1
    members = [np.flatnonzero(codes == g) for g in range(k)]
    out = []
    for _ in range(n):
        idx = np.concatenate([members[g] for g in rng.integers(0, k, size=k)])
        out.append(recal_slope(p[idx], y[idx]))
    return tuple(float(v) for v in np.quantile(out, [0.025, 0.975]))


def score_line(p_model, p_base, y, groups, level: float) -> dict:
    """Over/under at one line: model vs baseline log loss (gain range at `level`), and
    calibration at 95% (in the large and slope) with a 10-bin table."""
    p_model, p_base = np.asarray(p_model, float), np.asarray(p_base, float)
    y, g = np.asarray(y, int), np.asarray(groups)
    two = lambda p: np.column_stack([p, 1 - p])  # noqa: E731
    yy = 1 - y  # outcome index: 0 = over, 1 = under
    llm = metrics.log_loss_rows(two(p_model), yy)
    llb = metrics.log_loss_rows(two(p_base), yy)
    gain = llb - llm
    resid = y - p_model
    bins = np.clip((p_model * 10).astype(int), 0, 9)
    table = (
        pd.DataFrame({"bin": bins, "p": p_model, "y": y})
        .groupby("bin")
        .agg(n=("p", "size"), predicted=("p", "mean"), observed=("y", "mean"))
        .round(4)
        .reset_index()
        .to_dict("records")
    )
    cil = metrics.boot_range(resid, g, level=0.95)
    slope_rng = _boot_slope(p_model, y, g)
    return {
        "rows": int(len(y)),
        "over_rate": float(y.mean()),
        "predicted": float(p_model.mean()),
        "log_loss": float(llm.mean()),
        "baseline_log_loss": float(llb.mean()),
        "gain": float(gain.mean()),
        "gain_range": metrics.boot_range(gain, g, n_boot=N_BOOT, level=level),
        "obs_minus_pred": float(resid.mean()),
        "obs_minus_pred_range": cil,
        "slope": recal_slope(p_model, y),
        "slope_range": slope_rng,
        "calibrated": bool(cil and cil[0] <= 0 <= cil[1] and slope_rng[0] <= 1 <= slope_rng[1]),
        "calibration": table,
    }


def pinnacle_25(df: pd.DataFrame, scored: np.ndarray, level: float) -> dict:
    """The model's over/under 2.5 beside Pinnacle early, CLV vs the fair close, in
    seasons where both prices cover MIN_COVERAGE."""
    cols = [f"pinnacle_{w}_{m}" for w in ("early", "close") for m in ("over25", "under25")]
    ok = df[cols].notna().all(axis=1)
    cov = {int(k): float(v) for k, v in ok.groupby(df["season_start"]).mean().items()}
    priced = [k for k, v in cov.items() if v >= MIN_COVERAGE]
    rows = scored & df["season_start"].isin(priced).to_numpy() & ok.to_numpy()
    if rows.sum() < 100:
        return {"coverage": cov, "rows": int(rows.sum())}
    d = df[rows]
    market = signals.devig_rows(d, ["pinnacle_early_over25", "pinnacle_early_under25"])
    fair = signals.devig_rows(d, ["pinnacle_close_over25", "pinnacle_close_under25"])
    odds = d[["pinnacle_early_over25", "pinnacle_early_under25"]].to_numpy(float)
    p = d["goals_over_2.5"].to_numpy(float)
    y = 1 - (d["home_goals"] + d["away_goals"] > 2.5).to_numpy(int)
    r = metrics.evaluate(
        np.column_stack([p, 1 - p]), y, market, odds, fair, d["match"].to_numpy(), level
    )
    close_ll = metrics.log_loss_rows(np.clip(fair, 1e-9, 1), y).mean()
    r.update(
        {
            "coverage": cov,
            "seasons": priced,
            "close_log_loss": float(close_ll),
            "pass": metrics.passes(r),
        }
    )
    return r


def goal_probs(df: pd.DataFrame) -> pd.DataFrame:
    """Over chances from each row's score matrix: totals, home and away team totals."""
    out = {}
    mats = np.stack(df["matrix"].to_numpy())
    n = mats.shape[1]
    tot = np.add.outer(np.arange(n), np.arange(n))
    for line in GOAL_LINES:
        out[f"goals_over_{line}"] = (mats * (tot > line)).sum(axis=(1, 2))
    home = mats.sum(axis=2)
    away = mats.sum(axis=1)
    k = np.arange(n)
    for line in TEAM_LINES:
        out[f"home_over_{line}"] = home[:, k > line].sum(axis=1)
        out[f"away_over_{line}"] = away[:, k > line].sum(axis=1)
    return pd.DataFrame(out, index=df.index)


def run(df: pd.DataFrame, level: float) -> dict:
    """All markets for one league's prepared frame (DC rows with matrices, counts and
    prices)."""
    df = df[df["date"] < HOLDOUT_START].sort_values("date").reset_index(drop=True)
    df = pd.concat([df, goal_probs(df)], axis=1)
    hg, ag = df["home_goals"].to_numpy(), df["away_goals"].to_numpy()
    scored = ((df["season_start"] >= FIRST_SCORED) & (df["season_start"] <= LAST)).to_numpy()
    g = df["match"].to_numpy()
    res: dict = {"matches": int(scored.sum()), "goals": {}, "team": {}, "corners": {}}
    for line in GOAL_LINES:
        y = (hg + ag > line).astype(int)
        base = trailing_mean(df["date"], y, LEAGUE_DAYS)
        ok = scored & np.isfinite(base)
        res["goals"][str(line)] = score_line(
            df[f"goals_over_{line}"].to_numpy()[ok], base[ok], y[ok], g[ok], level
        )
    for side, goals in (("home", hg), ("away", ag)):
        for line in TEAM_LINES:
            y = (goals > line).astype(int)
            base = trailing_mean(df["date"], y, LEAGUE_DAYS)
            ok = scored & np.isfinite(base)
            res["team"][f"{side}_{line}"] = score_line(
                df[f"{side}_over_{line}"].to_numpy()[ok], base[ok], y[ok], g[ok], level
            )
    res["pinnacle_25"] = pinnacle_25(df, scored, level)
    has_corners = df[["home_corners", "away_corners", "home_shots", "away_shots"]].notna()
    res["corner_data_share"] = float(has_corners.all(axis=1)[scored].mean())
    if res["corner_data_share"] > 0.5:
        preds, data = corner_predictions(df)
        common = preds["model"].index.intersection(preds["baseline"].index)
        common = common.intersection(preds["total_nb"].index)
        common = common[scored[common]]
        total = (df.loc[common, "home_corners"] + df.loc[common, "away_corners"]).to_numpy()
        pm = preds["model"].loc[common].to_numpy()
        pb = preds["baseline"].loc[common].to_numpy()
        pt = preds["total_nb"].loc[common].to_numpy()
        gc = g[common]
        tot_idx = np.clip(total.astype(int), 0, KMAX)
        res["corners"]["rows"] = int(len(common))
        res["corners"]["seasons"] = sorted(int(s) for s in df.loc[common, "season_start"].unique())
        res["corners"]["mean_total"] = float(total.mean())
        res["corners"]["var_total"] = float(total.var())
        res["corners"]["count_log_loss"] = {
            "independent": float(-np.log(pm[np.arange(len(pm)), tot_idx]).mean()),
            "total_nb": float(-np.log(pt[np.arange(len(pt)), tot_idx]).mean()),
            "baseline": float(-np.log(pb[np.arange(len(pb)), tot_idx]).mean()),
        }
        res["corners"]["residual_corr"] = residual_correlation(df, data, common)
        for line in CORNER_LINES:
            y = (total > line).astype(int)
            r = score_line(over_from_pmf(pm, line), over_from_pmf(pb, line), y, gc, level)
            ll_tot = metrics.log_loss_rows(
                np.column_stack([over_from_pmf(pt, line), 1 - over_from_pmf(pt, line)]), 1 - y
            )
            r["total_nb_log_loss"] = float(ll_tot.mean())
            res["corners"][str(line)] = r
    return res


# ---------- loading (network: GitHub Actions) ----------


def corner_price_columns(raws: list[pd.DataFrame]) -> list[str]:
    """Column names that could be a corner price: 'corner' in the name, or an
    over/under column at a line of 6 or more (goal lines stop below that)."""
    import re

    cols = sorted({c for r in raws for c in r.columns})
    hits = []
    for c in cols:
        if re.search(r"(?i)corner", c):
            hits.append(c)
            continue
        m = re.search(r"[<>](\d+(?:\.\d+)?)", c)
        if m and float(m.group(1)) >= 6:
            hits.append(c)
    return hits


def load(league: str) -> tuple[pd.DataFrame, dict]:
    from soccer_stats import backtest
    from soccer_stats.data import download, load_matches, season_code
    from soccer_stats.edge import books
    from soccer_stats.models import DixonColes
    from soccer_stats.xg import LEAGUES as XG_LEAGUES
    from soccer_stats.xg import with_xg

    years = range(FIRST_DATA, LAST + 1)
    raws, frames = [], []
    for y in years:
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        raws.append(raw)
        px = books.book_prices(raw, season=season_code(y))
        played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).reset_index(drop=True)
        for col, src in (("home_corners", "HC"), ("away_corners", "AC")):
            px[col] = pd.to_numeric(played[src], errors="coerce") if src in played else np.nan
        frames.append(px)
    px = pd.concat(frames, ignore_index=True)
    info = {
        "columns_scanned": len({c for r in raws for c in r.columns}),
        "corner_price_columns": corner_price_columns(raws),
    }
    matches = load_matches([league], years)
    xg = league in XG_LEAGUES
    if xg:
        matches, err = with_xg(matches)
        if err:
            raise SystemExit(err)
    dc = backtest.walk_forward(
        matches,
        start=DC_START,
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if xg else 0.0),
        keep_matrix=True,
    )
    keep = ["date", "home", "away", "exp_home", "exp_away", "matrix"]
    cols = [
        "date",
        "home",
        "away",
        "home_goals",
        "away_goals",
        "season",
        "home_shots",
        "away_shots",
        "home_corners",
        "away_corners",
        *[f"pinnacle_{w}_{m}" for w in ("early", "close") for m in ("over25", "under25")],
    ]
    df = px[cols].merge(dc[keep], on=["date", "home", "away"], how="inner")
    df["season_start"] = 2000 + df["season"].astype(str).str[:2].astype(int)
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    print(f"{league}: {len(px)} football-data matches, {len(df)} with a match-model matrix")
    return df, info


def _rng(v, f="{:+.4f}"):
    return f"{f.format(v[0])}..{f.format(v[1])}" if v else "–"


def _line(name: str, r: dict) -> str:
    beats = r["gain_range"] is not None and r["gain_range"][0] > 0
    return (
        f"  {name:>10}: n {r['rows']}, over {r['over_rate']:.3f} vs predicted "
        f"{r['predicted']:.3f} (obs-pred {_rng(r['obs_minus_pred_range'], '{:+.3f}')}), "
        f"slope {r['slope']:.2f} ({_rng(r['slope_range'], '{:.2f}')}), "
        f"calibrated {r['calibrated']}; log loss {r['log_loss']:.4f} vs baseline "
        f"{r['baseline_log_loss']:.4f}, gain {_rng(r['gain_range'])} beats {beats}"
    )


def report(league: str, res: dict, info: dict, level: float) -> str:
    lines = [f"== {league}: totals markets (gain ranges {level:.3%}, calibration 95%) =="]
    lines.append(f"Scored matches: {res['matches']}; corner data on {res['corner_data_share']:.0%}")
    lines.append(
        f"Corner price columns in football-data ({info['columns_scanned']} columns scanned): "
        f"{info['corner_price_columns'] or 'none'}"
    )
    lines.append("B. Goal totals:")
    lines += [_line(f"over {k}", r) for k, r in res["goals"].items()]
    lines.append("C. Team totals:")
    lines += [_line(k.replace("_", " over "), r) for k, r in res["team"].items()]
    p = res["pinnacle_25"]
    if "blend" in p:
        b = p["bets"]
        lines.append(
            f"B at 2.5 vs Pinnacle (seasons {p['seasons']}): n {p['rows']}, log loss model "
            f"{p['log_loss']:.4f} / early {p['market_log_loss']:.4f} / close "
            f"{p['close_log_loss']:.4f}; blend c {p['blend'].get('c', float('nan')):+.3f} "
            f"({_rng(p['blend'].get('c_range'), '{:+.3f}')}); 12% rule {b['bets']} bets"
            + (f", CLV {b['clv']:+.4f} ({_rng(b.get('clv_range'))})" if b["bets"] else "")
            + f"; pass {p['pass']}"
        )
    else:
        lines.append(
            f"B at 2.5 vs Pinnacle: too few priced rows ({p.get('rows')}); {p['coverage']}"
        )
    c = res["corners"]
    if c:
        ll = c["count_log_loss"]
        rc = c["residual_corr"]
        lines.append(
            f"A. Corners (n {c['rows']}, seasons {c['seasons']}): mean total "
            f"{c['mean_total']:.2f}, variance {c['var_total']:.2f}; count log loss "
            f"independent {ll['independent']:.4f} / direct total NB {ll['total_nb']:.4f} / "
            f"baseline {ll['baseline']:.4f}; home-away residual correlation "
            + (f"{rc:+.3f}" if rc is not None else "–")
        )
        lines += [_line(f"over {k}", c[str(k)]) for k in CORNER_LINES]
    else:
        lines.append("A. Corners: no corner data")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    from pathlib import Path

    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.totals")
    ap.add_argument("--league", default="E0", choices=LEAGUES)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    level = bonferroni_level(TESTS)
    df, info = load(args.league)
    res = run(df, level)
    print(report(args.league, res, info, level))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        out = {"league": args.league, "level": level, "tests": TESTS, **info, **res}
        text = json.dumps(_jsonable(out), default=str)
        Path(args.json).write_text(text)
        print("TOTALS_JSON " + text)


if __name__ == "__main__":
    main()
