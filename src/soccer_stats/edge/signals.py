"""Does a free signal know something the early price doesn't?

Each signal is a number per match, known before football-data's early price is taken
(one to three days before kickoff). It is judged two ways, both out of sample:

1. **Closing-line move.** Does it predict how Pinnacle's margin-free price moves from
   early to close? The target is the change in log(home/away) for 1X2, or in
   logit(over 2.5) for totals. Pinnacle's close is the sharpest public price, so a
   signal that predicts the move is information the early market lacked. Fits are
   season by season on earlier seasons only. A bet test backs the side the signal
   favours at Pinnacle's early price, on matches where the signal is in the top fifth
   of its earlier seasons' size, and scores CLV against the fair close.
2. **Blend weight.** Added to a conditional-logit blend on Pinnacle's early price
   (as in match_calibration), does it lower the log loss of the result on later
   seasons?

All features use earlier matches only. Nothing here reads the network.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import log_softmax, softmax

from soccer_stats.edge.stats import bootstrap_mean
from soccer_stats.odds import devig_shin

FORM_WINDOW = 6  # league matches; fixed in advance, not tuned
MIN_FORM = 3  # fewer earlier matches than this: no form value
REST_CAP = 14  # days; longer rests (season start, breaks) count as 14
TOP_SHARE = 0.2  # bet test: the largest fifth of signals, sized on earlier seasons
H2H = ("home", "draw", "away")
TOTALS = ("over25", "under25")

# signal -> (target group, description). The order is the testing family.
SIGNALS = {
    "xg_vs_goals": ("h2h", "6-match xG difference minus goal difference, home minus away"),
    "xg_form": ("h2h", "6-match xG difference, home minus away"),
    "rest": ("h2h", "days since the last league match, home minus away (cap 14)"),
    "model_vs_early": ("h2h", "model log(home/away) minus Pinnacle early's"),
    "soft_vs_sharp": ("h2h", "market-average early log(home/away) minus Pinnacle early's"),
    "xg_total_vs_goals": ("totals", "6-match xG total minus goal total, both teams"),
    "model_total_vs_early": ("totals", "model logit(over 2.5) minus Pinnacle early's"),
}


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def devig_rows(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    """Shin margin-free probabilities per row; NaN where a price is missing."""
    vals = df[cols].to_numpy(dtype=float)
    out = np.full(vals.shape, np.nan)
    for i in np.flatnonzero(np.isfinite(vals).all(axis=1) & (vals > 1).all(axis=1)):
        out[i] = devig_shin(vals[i])
    return out


def team_form(matches: pd.DataFrame, window: int = FORM_WINDOW) -> pd.DataFrame:
    """Per match, each side's form going in: from that team's earlier league matches only.

    `matches` has date, home, away, home_goals, away_goals, home_xg, away_xg. Returns the
    same rows (same index) with `{side}_{xgd,gd,xgt,gt}` (rolling means over the last
    `window` matches: xG difference, goal difference, xG total, goal total) and
    `{side}_rest` (days since the team's previous match, capped).
    """
    m = matches.sort_values("date")
    long = pd.concat(
        [
            pd.DataFrame(
                {
                    "row": m.index,
                    "date": m["date"],
                    "team": m[side],
                    "side": side,
                    "xgd": m[f"{side}_xg"] - m[f"{other}_xg"],
                    "gd": m[f"{side}_goals"] - m[f"{other}_goals"],
                    "xgt": m["home_xg"] + m["away_xg"],
                    "gt": m["home_goals"] + m["away_goals"],
                }
            )
            for side, other in (("home", "away"), ("away", "home"))
        ]
    ).sort_values(["date", "row"], kind="stable")
    g = long.groupby("team", sort=False)
    for c in ("xgd", "gd", "xgt", "gt"):
        # shift(1): the match itself is never in its own form.
        long[f"f_{c}"] = g[c].transform(
            lambda s: s.shift(1).rolling(window, min_periods=MIN_FORM).mean()
        )
    long["rest"] = (long["date"] - g["date"].shift(1)).dt.days.clip(upper=REST_CAP)
    long["rest"] = long["rest"].fillna(REST_CAP)
    out = pd.DataFrame(index=matches.index)
    for side in ("home", "away"):
        s = long[long["side"] == side].set_index("row")
        for c in ("xgd", "gd", "xgt", "gt"):
            out[f"{side}_{c}"] = s[f"f_{c}"]
        out[f"{side}_rest"] = s["rest"]
    return out


def build(joined: pd.DataFrame, form: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per match: signals, early and close fair chances, targets and the result.

    `joined` is books.join output (walk-forward p_* beside football-data prices). `form`
    is team_form output, keyed on (date, home, away) columns it carries.
    """
    df = joined.copy()
    if form is not None:
        df = df.merge(form, on=["date", "home", "away"], how="left")
    pe = devig_rows(df, [f"pinnacle_early_{m}" for m in H2H])
    pc = devig_rows(df, [f"pinnacle_close_{m}" for m in H2H])
    av = devig_rows(df, [f"avg_early_{m}" for m in H2H])
    oe = devig_rows(df, [f"pinnacle_early_{m}" for m in TOTALS])
    oc = devig_rows(df, [f"pinnacle_close_{m}" for m in TOTALS])
    for i, m in enumerate(H2H):
        df[f"early_{m}"], df[f"close_{m}"] = pe[:, i], pc[:, i]
    for i, m in enumerate(TOTALS):
        df[f"early_{m}"], df[f"close_{m}"] = oe[:, i], oc[:, i]
    lr_e = np.log(pe[:, 0] / pe[:, 2])
    df["move_h2h"] = np.log(pc[:, 0] / pc[:, 2]) - lr_e
    df["move_totals"] = _logit(oc[:, 0]) - _logit(oe[:, 0])
    df.loc[~np.isfinite(oc[:, 0] + oe[:, 0]), "move_totals"] = np.nan
    if form is not None:
        df["xg_vs_goals"] = (df["home_xgd"] - df["home_gd"]) - (df["away_xgd"] - df["away_gd"])
        df["xg_form"] = df["home_xgd"] - df["away_xgd"]
        df["rest"] = df["home_rest"] - df["away_rest"]
        df["xg_total_vs_goals"] = (df["home_xgt"] - df["home_gt"]) + (
            df["away_xgt"] - df["away_gt"]
        )
    df["model_vs_early"] = np.log(df["p_home"] / df["p_away"]) - lr_e
    df["soft_vs_sharp"] = np.log(av[:, 0] / av[:, 2]) - lr_e
    df["model_total_vs_early"] = _logit(df["p_over25"]) - _logit(oe[:, 0])
    hg, ag = df["home_goals"].to_numpy(), df["away_goals"].to_numpy()
    df["y_h2h"] = np.select([hg > ag, hg == ag], [0, 1], 2)
    df["y_totals"] = np.where(hg + ag > 2.5, 0, 1)
    return df


# ---------- 1. closing-line move ----------


def _ols(x, y):
    xm, ym = x.mean(), y.mean()
    vx = ((x - xm) ** 2).sum()
    b = ((x - xm) * (y - ym)).sum() / vx if vx > 0 else 0.0
    return ym - b * xm, b


def _slope_ci(x, y, seed=0, n=1000, level=0.95):
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n, len(x)))
    bs = np.array([_ols(x[i], y[i])[1] for i in idx])
    return tuple(float(v) for v in np.quantile(bs, [(1 - level) / 2, 1 - (1 - level) / 2]))


def _clv(df: pd.DataFrame, side: np.ndarray, group: str) -> np.ndarray:
    """CLV of backing `side` (+1 home/over, -1 away/under) at Pinnacle's early price."""
    a, b = ("home", "away") if group == "h2h" else ("over25", "under25")
    odds = np.where(side > 0, df[f"pinnacle_early_{a}"], df[f"pinnacle_early_{b}"])
    fair = np.where(side > 0, df[f"close_{a}"], df[f"close_{b}"])
    return odds * fair - 1


def move_test(df: pd.DataFrame, signal: str, level: float = 0.95) -> dict:
    """Out-of-sample test of `signal` against the early-to-close move.

    Seasons are walked in order: each is predicted from a fit on all earlier seasons.
    Reports the out-of-sample R² (against the earlier seasons' mean move), the slope on
    all rows with a bootstrap range at `level`, how many test seasons had the
    in-sample slope's sign, and the bet test's CLV.
    """
    group = SIGNALS[signal][0]
    y_col = f"move_{group}"
    d = df.dropna(subset=[signal, y_col, "season"])
    d = d[np.isfinite(d[signal]) & np.isfinite(d[y_col])].sort_values("date")
    seasons = sorted(d["season"].unique())
    if len(d) < 50 or len(seasons) < 2:
        return {"signal": signal, "group": group, "rows": len(d)}
    x, y = d[signal].to_numpy(), d[y_col].to_numpy()
    a, b = _ols(x, y)
    pred, base, keep, bets, same_sign = [], [], [], [], 0
    for s in seasons[1:]:
        tr, te = d["season"] < s, d["season"] == s
        a_s, b_s = _ols(x[tr], y[tr])
        pred.append(a_s + b_s * x[te])
        base.append(np.full(te.sum(), y[tr].mean()))
        keep.append(np.flatnonzero(te))
        same_sign += int(np.sign(_ols(x[te], y[te])[1]) == np.sign(b))
        cut = np.quantile(np.abs(x[tr]), 1 - TOP_SHARE)
        big = te & (np.abs(x) >= cut) & (b_s != 0)
        if big.any():
            side = np.sign(b_s * x[big])
            bets.append(_clv(d[big], side, group))
    idx = np.concatenate(keep)
    yp, yb, yt = np.concatenate(pred), np.concatenate(base), y[idx]
    r2 = 1 - ((yt - yp) ** 2).sum() / ((yt - yb) ** 2).sum()
    clv = np.concatenate(bets) if bets else np.array([])
    clv = clv[np.isfinite(clv)]
    return {
        "signal": signal,
        "group": group,
        "rows": len(d),
        "test_rows": len(idx),
        "slope": float(b),
        "slope_range": _slope_ci(x, y, level=level),
        "corr": float(np.corrcoef(x, y)[0, 1]),
        "oos_r2": float(r2),
        "same_sign_seasons": f"{same_sign}/{len(seasons) - 1}",
        "bets": len(clv),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_range": bootstrap_mean(clv, level=level) if len(clv) > 1 else None,
    }


# ---------- 2. blend weight ----------

RIDGE = 1e-3


def _design(df: pd.DataFrame, group: str, signal: str | None):
    names = H2H if group == "h2h" else TOTALS
    lm = np.log(np.clip(df[[f"early_{m}" for m in names]].to_numpy(float), 1e-6, 1))
    k = len(names)
    z = np.zeros((len(df), k))
    if signal:
        x = df[signal].to_numpy(float)
        if group == "h2h":
            z[:, 0], z[:, 2] = x / 2, -x / 2
        else:
            z[:, 0] = x
    return lm, z, df[f"y_{group}"].to_numpy(int)


def _score(w, lm, z):
    k = lm.shape[1]
    return np.concatenate([[0.0], w[: k - 1]]) + w[k - 1] * lm + w[k] * z


def fit_blend(lm, z, y) -> np.ndarray:
    """[a_2..a_K, b, d]: score_k = a_k + b·log(early_k) + d·z_k (ridge pulls b→1, d→0)."""
    n, k = lm.shape
    oh = np.eye(k)[y]

    def loss(w):
        nll = -(log_softmax(_score(w, lm, z), axis=1) * oh).sum(1).mean()
        return nll + RIDGE * ((w[k - 1] - 1) ** 2 + w[k] ** 2)

    def grad(w):
        g_s = (softmax(_score(w, lm, z), axis=1) - oh) / n
        g = np.concatenate([g_s[:, 1:].sum(0), [(g_s * lm).sum(), (g_s * z).sum()]])
        g[k - 1] += 2 * RIDGE * (w[k - 1] - 1)
        g[k] += 2 * RIDGE * w[k]
        return g

    w0 = np.concatenate([np.zeros(k - 1), [1.0, 0.0]])
    return minimize(loss, w0, jac=grad, method="L-BFGS-B").x


def _nll(w, lm, z, y):
    return -log_softmax(_score(w, lm, z), axis=1)[np.arange(len(y)), y]


def blend_test(df: pd.DataFrame, signal: str, level: float = 0.95) -> dict:
    """Out-of-sample log-loss gain from adding `signal` to a blend on Pinnacle early.

    Each season is scored with fits on earlier seasons: the early price alone, and the
    early price plus the signal. Gain > 0 means the signal helps; the range is a paired
    bootstrap over matches. The close's log loss is shown as the ceiling.
    """
    group = SIGNALS[signal][0]
    names = H2H if group == "h2h" else TOTALS
    cols = [signal, *[f"early_{m}" for m in names], *[f"close_{m}" for m in names]]
    d = df.dropna(subset=[*cols, "season"]).sort_values("date")
    d = d[np.isfinite(d[signal])]
    seasons = sorted(d["season"].unique())
    if len(d) < 50 or len(seasons) < 2:
        return {"signal": signal, "group": group, "rows": len(d)}
    lm, z, y = _design(d, group, signal)
    zero = np.zeros_like(z)
    base, with_sig, coefs = [], [], []
    for s in seasons[1:]:
        tr = (d["season"] < s).to_numpy()
        te = (d["season"] == s).to_numpy()
        w0 = fit_blend(lm[tr], zero[tr], y[tr])
        w1 = fit_blend(lm[tr], z[tr], y[tr])
        base.append(_nll(w0, lm[te], zero[te], y[te]))
        with_sig.append(_nll(w1, lm[te], z[te], y[te]))
        coefs.append(float(w1[-1]))
    nb, ns = np.concatenate(base), np.concatenate(with_sig)
    tested = d[d["season"] > seasons[0]]
    lc = np.log(np.clip(tested[[f"close_{m}" for m in names]].to_numpy(float), 1e-6, 1))
    close_ll = -lc[np.arange(len(tested)), tested[f"y_{group}"].to_numpy(int)]
    gain = nb - ns
    return {
        "signal": signal,
        "group": group,
        "test_rows": len(gain),
        "ll_early": float(nb.mean()),
        "ll_with_signal": float(ns.mean()),
        "ll_close": float(close_ll.mean()),
        "gain": float(gain.mean()),
        "gain_range": bootstrap_mean(gain, level=level),
        "d_by_season": [round(c, 3) for c in coefs],
    }


def run_all(df: pd.DataFrame, level: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    moves = pd.DataFrame([move_test(df, s, level) for s in SIGNALS])
    blends = pd.DataFrame([blend_test(df, s, level) for s in SIGNALS])
    return moves, blends


def move_baseline(df: pd.DataFrame) -> dict:
    """How big the early-to-close moves are, and the CLV of a random early bet."""
    out = {}
    for g, (a, b) in (("h2h", ("home", "away")), ("totals", ("over25", "under25"))):
        m = df[f"move_{g}"].dropna()
        both = np.r_[
            (df[f"pinnacle_early_{a}"] * df[f"close_{a}"] - 1).dropna(),
            (df[f"pinnacle_early_{b}"] * df[f"close_{b}"] - 1).dropna(),
        ]
        out[g] = {
            "rows": len(m),
            "move_sd": float(m.std()),
            "move_mean": float(m.mean()),
            "random_side_clv": float(both.mean()) if len(both) else None,
        }
    return out
