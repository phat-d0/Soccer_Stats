"""Does a lineup surprise move Pinnacle's price, and does the close absorb it?

Pre-registered in docs/lab.md ("Lineup surprises against Pinnacle's move"). Per match,
each side's surprise is the share of its recent xG held by regulars who don't start:

- W = the team's earlier league matches in the same season, the last up to WINDOW
  (MIN_EARLIER or more, else missing);
- regulars started at least ceil(2/3 * |W|) of W; each weighs its share of the team's
  xG over W;
- S(team, m) = summed weight of the regulars not in m's starting XI.

The signal is x = S(away) - S(home) (positive favours home). Only matches before m set
the regulars and weights; m contributes only its starting XI, the news being tested.
The XI is public about an hour before kickoff: after football-data's early price and
before its close.

Tests per league (run): 1. x against the early-to-close move in log(home/away); 2-3. a
walk-forward blend of Pinnacle early with x through lab.metrics (blend weight, 12%
rule CLV); 4. the same blend on the fair close, scored beside the close (does the
close absorb it?).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.special import softmax

from soccer_stats.edge import signals
from soccer_stats.lab import metrics

WINDOW = 6
MIN_EARLIER = 3
REGULAR_SHARE = 2 / 3
LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
FIRST_SEASON = 2016  # trains only
FIRST_SCORED = 2017
LAST = 2024  # 2025/26 is ~50% priced: dropped before anything is computed
HOLDOUT_START = pd.Timestamp("2025-07-01")
MIN_COVERAGE = 0.9
TESTS = 4 * len(LEAGUES)
REFIT = "28D"
MIN_ROWS = 300
H2H = ("home", "draw", "away")


def team_surprise(apps: pd.DataFrame) -> pd.DataFrame:
    """One row per team-match: surprise, missing regulars, regulars, earlier matches.

    `apps` has match_id, season, kickoff, team, home, player_id, started, xg (one row
    per player who played, as player_data.load_appearances returns).
    """
    a = apps[["match_id", "season", "kickoff", "team", "home", "player_id", "started", "xg"]]
    tm = (
        a.groupby(["team", "match_id"], sort=False)
        .agg(season=("season", "first"), kickoff=("kickoff", "first"), home=("home", "first"))
        .reset_index()
        .sort_values(["team", "kickoff", "match_id"])
    )
    starters = a[a["started"]].groupby(["team", "match_id"])["player_id"].agg(set)
    xg = a.groupby(["team", "match_id"]).apply(
        lambda d: dict(zip(d["player_id"], d["xg"].astype(float), strict=True)),
        include_groups=False,
    )
    rows = []
    for (_team, _season), g in tm.groupby(["team", "season"], sort=False):
        ids = g["match_id"].tolist()
        for i, mid in enumerate(ids):
            earlier = ids[max(0, i - WINDOW) : i]
            out = {
                "match_id": mid,
                "team": _team,
                "home": bool(g["home"].iloc[i]),
                "kickoff": g["kickoff"].iloc[i],
                "season": _season,
                "earlier": len(earlier),
                "surprise": np.nan,
                "missing": np.nan,
                "regulars": np.nan,
            }
            if len(earlier) >= MIN_EARLIER:
                need = math.ceil(REGULAR_SHARE * len(earlier) - 1e-9)
                starts: dict[str, int] = {}
                pxg: dict[str, float] = {}
                for e in earlier:
                    for p in starters.get((_team, e), set()):
                        starts[p] = starts.get(p, 0) + 1
                    for p, v in xg.get((_team, e), {}).items():
                        pxg[p] = pxg.get(p, 0.0) + v
                team_xg = sum(pxg.values())
                regs = [p for p, n in starts.items() if n >= need]
                now = starters.get((_team, mid), set())
                gone = [p for p in regs if p not in now]
                out["regulars"] = len(regs)
                out["missing"] = len(gone)
                if team_xg > 0:
                    out["surprise"] = sum(pxg.get(p, 0.0) for p in gone) / team_xg
            rows.append(out)
    return pd.DataFrame(rows)


def match_signal(surprise: pd.DataFrame, apps: pd.DataFrame) -> pd.DataFrame:
    """Per Understat match: date, home, away, home/away surprise and x = away - home."""
    names = (
        apps.groupby(["match_id", "home"])["team"]
        .first()
        .unstack()
        .rename(columns={True: "home", False: "away"})
    )
    s = surprise.set_index(["match_id", "home"])
    out = names.reset_index()
    for side, flag in (("home", True), ("away", False)):
        sub = s.xs(flag, level="home")
        out[f"{side}_surprise"] = out["match_id"].map(sub["surprise"])
        out[f"{side}_missing"] = out["match_id"].map(sub["missing"])
    ko = surprise.groupby("match_id")["kickoff"].first()
    out["kickoff"] = out["match_id"].map(ko)
    out["x"] = out["away_surprise"] - out["home_surprise"]
    return out


def attach(prices: pd.DataFrame, sig: pd.DataFrame, max_day_gap: int = 2) -> pd.DataFrame:
    """Add x (and each side's surprise) to football-data rows: same home and away, kickoff
    within `max_day_gap` days. Unpaired rows keep NaN."""
    out = prices.copy()
    for c in ("x", "home_surprise", "away_surprise", "home_missing", "away_missing"):
        out[c] = np.nan
    key = sig.assign(us_date=pd.to_datetime(sig["kickoff"]).dt.tz_localize(None).dt.normalize())
    m = out.reset_index().merge(key, on=["home", "away"], how="inner", suffixes=("", "_us"))
    m = m[(m["date"] - m["us_date"]).abs().dt.days <= max_day_gap].drop_duplicates("index")
    for c in ("x", "home_surprise", "away_surprise", "home_missing", "away_missing"):
        out.loc[m["index"], c] = m[f"{c}_us"].to_numpy() if f"{c}_us" in m else m[c].to_numpy()
    return out


def prepare(prices: pd.DataFrame) -> pd.DataFrame:
    """Margin-free early and close, the move, the result, the match id; drops 2025/26."""
    df = prices[prices["date"] < HOLDOUT_START].sort_values("date").reset_index(drop=True)
    pe = signals.devig_rows(df, [f"pinnacle_early_{m}" for m in H2H])
    pc = signals.devig_rows(df, [f"pinnacle_close_{m}" for m in H2H])
    for i, m in enumerate(H2H):
        df[f"early_{m}"], df[f"close_{m}"] = pe[:, i], pc[:, i]
    df["move_h2h"] = np.log(pc[:, 0] / pc[:, 2]) - np.log(pe[:, 0] / pe[:, 2])
    hg, ag = df["home_goals"].to_numpy(), df["away_goals"].to_numpy()
    df["y"] = np.select([hg > ag, hg == ag], [0, 1], 2)
    df["season_start"] = 2000 + df["season"].astype(str).str[:2].astype(int)
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    return df


def coverage(df: pd.DataFrame) -> dict[int, float]:
    cols = [f"pinnacle_{w}_{m}" for w in ("early", "close") for m in H2H]
    ok = df[cols].notna().all(axis=1)
    return {int(k): float(v) for k, v in ok.groupby(df["season_start"]).mean().items()}


def walk_forward(market, x, y, times, refit: str = REFIT, min_rows: int = MIN_ROWS):
    """Chances from score_k = a_k + b·log(market_k) + d·z_k (z = x/2, 0, -x/2), each
    block fitted on earlier rows only (NaN before `min_rows`)."""
    m = np.asarray(market, float)
    x = np.asarray(x, float)
    y = np.asarray(y, int)
    t = pd.to_datetime(pd.Series(np.asarray(times))).reset_index(drop=True)
    lm = np.log(np.clip(m, 1e-6, 1))
    z = np.zeros_like(m)
    z[:, 0], z[:, 2] = x / 2, -x / 2
    ok = np.isfinite(m).all(1) & np.isfinite(x)
    out = np.full(m.shape, np.nan)
    coefs = []
    edges = pd.date_range(t.min().normalize(), t.max() + pd.Timedelta(refit), freq=refit)
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        te = ((t >= lo) & (t < hi)).to_numpy() & ok
        tr = (t < lo).to_numpy() & ok
        if not te.any() or tr.sum() < min_rows:
            continue
        w = signals.fit_blend(lm[tr], z[tr], y[tr])
        out[te] = softmax(signals._score(w, lm[te], z[te]), axis=1)
        coefs.append(float(w[-1]))
    return out, coefs


def move_test(df: pd.DataFrame, scored: np.ndarray, level: float) -> dict:
    """Test 1: the slope of the move on x (range at `level`), and out-of-sample R²
    with each scored season predicted from a fit on all earlier seasons."""
    ok = np.isfinite(df["x"]) & np.isfinite(df["move_h2h"])
    d = df[ok.to_numpy()]
    s = scored[ok.to_numpy()]
    x, y = d["x"].to_numpy(), d["move_h2h"].to_numpy()
    if s.sum() < 50:
        return {"rows": int(s.sum())}
    a, b = signals._ols(x[s], y[s])
    pred, base, keep = [], [], []
    for season in sorted(d.loc[s, "season_start"].unique()):
        te = s & (d["season_start"] == season).to_numpy()
        tr = (d["season_start"] < season).to_numpy()
        if tr.sum() < 50:
            continue
        a_s, b_s = signals._ols(x[tr], y[tr])
        pred.append(a_s + b_s * x[te])
        base.append(np.full(te.sum(), y[tr].mean()))
        keep.append(np.flatnonzero(te))
    idx = np.concatenate(keep)
    yp, yb = np.concatenate(pred), np.concatenate(base)
    r2 = 1 - ((y[idx] - yp) ** 2).sum() / ((y[idx] - yb) ** 2).sum()
    rng = signals._slope_ci(x[s], y[s], level=level)
    return {
        "rows": int(s.sum()),
        "slope": float(b),
        "slope_range": rng,
        "corr": float(np.corrcoef(x[s], y[s])[0, 1]),
        "in_sample_r2": float(np.corrcoef(x[s], y[s])[0, 1] ** 2),
        "oos_r2": float(r2),
        "pass": bool(rng[0] > 0 or rng[1] < 0),
    }


def blend_test(df: pd.DataFrame, scored: np.ndarray, when: str, level: float) -> dict:
    """Tests 2-3 (when='early') and 4 (when='close'): the walk-forward blend of that
    price with x, through lab.metrics beside that price."""
    market = df[[f"{when}_{m}" for m in H2H]].to_numpy(float)
    odds = df[[f"pinnacle_{when}_{m}" for m in H2H]].to_numpy(float)
    fair = df[[f"close_{m}" for m in H2H]].to_numpy(float)
    y, g = df["y"].to_numpy(int), df["match"].to_numpy()
    p, coefs = walk_forward(market, df["x"], y, df["date"])
    rows = scored & np.isfinite(p).all(1) & np.isfinite(market).all(1)
    if rows.sum() < 50:
        return {"rows": int(rows.sum())}
    r = metrics.evaluate(p[rows], y[rows], market[rows], odds[rows], fair[rows], g[rows], level)
    r["d_last"] = coefs[-1] if coefs else None
    r["d_range_over_refits"] = (min(coefs), max(coefs)) if coefs else None
    cr = r["blend"].get("c_range")
    r["pass"] = metrics.passes(r) if when == "early" else bool(cr and cr[0] > 0)
    return r


def describe(df: pd.DataFrame, scored: np.ndarray) -> dict:
    """How often regulars miss, and the size of the move by surprise band."""
    d = df[scored & np.isfinite(df["x"]).to_numpy()]
    miss = pd.concat([d["home_missing"], d["away_missing"]])
    sur = pd.concat([d["home_surprise"], d["away_surprise"]])
    bands = pd.cut(d["x"], [-np.inf, -0.2, -0.05, 0.05, 0.2, np.inf])
    by = d.groupby(bands, observed=True)["move_h2h"].agg(["size", "mean", "std"])
    return {
        "matches": len(d),
        "team_matches_missing_any": float((miss > 0).mean()),
        "mean_surprise": float(sur.mean()),
        "share_surprise_over_20pct": float((sur > 0.2).mean()),
        "move_sd": float(d["move_h2h"].std()),
        "move_by_x": [
            {
                "x": str(k),
                "n": int(v["size"]),
                "move_mean": round(float(v["mean"]), 4),
                "move_sd": round(float(v["std"]), 4),
            }
            for k, v in by.iterrows()
        ],
    }


def run(df: pd.DataFrame, level: float) -> dict:
    """All four tests on one league's prepared, paired rows."""
    cov = coverage(df)
    priced = [k for k, v in cov.items() if v >= MIN_COVERAGE]
    scored = (
        (df["season_start"] >= FIRST_SCORED)
        & (df["season_start"] <= LAST)
        & df["season_start"].isin(priced)
    ).to_numpy()
    paired = float(np.isfinite(df["x"]).mean())
    return {
        "coverage": cov,
        "not_scored": [k for k in range(FIRST_SCORED, LAST + 1) if k not in priced],
        "paired_share": paired,
        "describe": describe(df, scored),
        "move": move_test(df, scored, level),
        "early": blend_test(df, scored, "early", level),
        "close": blend_test(df, scored, "close", level),
    }


def load(league: str) -> pd.DataFrame:
    """Network (GitHub Actions): football-data prices and Understat rosters, paired."""
    from soccer_stats.data import download, season_code
    from soccer_stats.edge import books
    from soccer_stats.player_data import load_appearances

    years = range(FIRST_SEASON, LAST + 1)
    frames = []
    for y in years:
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        frames.append(books.book_prices(raw, season=season_code(y)))
    prices = pd.concat(frames, ignore_index=True)
    apps, missing = load_appearances(league, years)
    print(f"{league}: {apps['match_id'].nunique()} Understat matches, {missing} missing")
    apps = apps[pd.to_datetime(apps["kickoff"]).dt.tz_localize(None) < HOLDOUT_START]
    sig = match_signal(team_surprise(apps), apps)
    df = prepare(attach(prices, sig))
    print(f"{league}: {len(df)} football-data matches, {np.isfinite(df['x']).mean():.0%} with x")
    return df


def _rng(v, f="{:+.4f}"):
    return f"{f.format(v[0])} to {f.format(v[1])}" if v else "–"


def report(league: str, res: dict, level: float) -> str:
    mv, e, c, d = res["move"], res["early"], res["close"], res["describe"]
    lines = [
        f"== {league}: lineup surprise vs Pinnacle (ranges {level:.2%}) ==",
        f"Pinnacle coverage: {res['coverage']}; not scored: {res['not_scored']}; "
        f"paired with Understat: {res['paired_share']:.0%}",
        f"Scored matches with x: {d['matches']}; team-matches missing a regular: "
        f"{d['team_matches_missing_any']:.0%}; mean surprise {d['mean_surprise']:.3f}; "
        f"surprise > 20%: {d['share_surprise_over_20pct']:.1%}; move sd {d['move_sd']:.4f}",
        "Move by x band: "
        + "; ".join(f"{b['x']} n={b['n']} mean {b['move_mean']:+.4f}" for b in d["move_by_x"]),
        f"1 move: slope {mv.get('slope', float('nan')):+.4f} ({_rng(mv.get('slope_range'))}), "
        f"R² in-sample {mv.get('in_sample_r2', float('nan')):.4f}, out-of-sample "
        f"{mv.get('oos_r2', float('nan')):+.4f}; pass {mv.get('pass')}",
    ]
    for name, r in (("2-3 early", e), ("4 close", c)):
        if "blend" not in r:
            lines.append(f"{name}: too few rows ({r.get('rows')})")
            continue
        b, bl = r["bets"], r["blend"]
        lines.append(
            f"{name}: rows {r['rows']}, log loss {r['log_loss']:.4f} vs price "
            f"{r['market_log_loss']:.4f} (gain {_rng(r['gain_vs_market']['range'])}), "
            f"blend c {bl.get('c', float('nan')):+.3f} ({_rng(bl.get('c_range'), '{:+.3f}')}), "
            f"d last {r['d_last']:+.3f}; 12% rule: {b['bets']} bets"
            + (
                f", CLV {b['clv']:+.4f} ({_rng(b.get('clv_range'))}), ROI {b['roi']:+.3f} "
                f"({_rng(b.get('roi_range'), '{:+.3f}')})"
                if b["bets"]
                else ""
            )
            + f"; pass {r['pass']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    from pathlib import Path

    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.lineups")
    ap.add_argument("--league", default="E0", choices=LEAGUES)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    level = bonferroni_level(TESTS)
    res = run(load(args.league), level)
    print(report(args.league, res, level))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        out = {"league": args.league, "level": level, "tests": TESTS, **res}
        text = json.dumps(_jsonable(out), default=str)
        Path(args.json).write_text(text)
        print("LINEUPS_JSON " + text)


if __name__ == "__main__":
    main()
