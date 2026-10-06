"""The 1X2 bake-off: `python -m soccer_stats.lab.run [--open-holdout --reason ... --finalists ...]`.

Development (default): seasons 2016/17 (warm-up for the stack) to 2024/25, scored on
2017/18-2024/25. The 2025/26 holdout is locked: its matches are cut before any model
sees them. `--open-holdout --reason "..." --finalists a,b` scores only the named
candidates on 2025/26, once, and prints a banner with the time it was opened.

Needs football-data and Understat (GitHub Actions; no Odds API credits).
"""

from __future__ import annotations

import argparse
import functools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.lab import features, metrics
from soccer_stats.lab.harness import Holdout, nested
from soccer_stats.lab.models import FACTORIES, GRIDS
from soccer_stats.odds import devig_shin

HOLDOUT_START = "2025-07-01"
FIRST_SEASON = 2014  # Understat's first EPL season: features warm up here
WARMUP = 2016  # first predicted season (feeds the stack's blend; not scored)
FIRST_SCORED = 2017
LAST = 2025
H2H = ("home", "draw", "away")
CANDIDATES = list(FACTORIES)
STACK = "e_stack"
STACK_MIN_ROWS = 300  # earlier out-of-sample matches before the stack's first fit
TESTS = 2 * (len(CANDIDATES) + 1)  # two pass metrics per candidate (a-e)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def _devig(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    v = df[cols].to_numpy(float)
    out = np.full(v.shape, np.nan)
    for i in np.flatnonzero(np.isfinite(v).all(1) & (v > 1).all(1)):
        out[i] = devig_shin(v[i])
    return out


def load(holdout: Holdout, league: str = "E0") -> pd.DataFrame:
    """One row per match: time, season, teams, goals, xG, FEATURES, y, the Dixon-Coles
    walk-forward chances, and Pinnacle's early and closing prices. With the holdout
    locked, matches from its start are dropped before anything is computed."""
    from soccer_stats import backtest
    from soccer_stats.data import download, load_matches, season_code
    from soccer_stats.edge import books
    from soccer_stats.models import DixonColes
    from soccer_stats.xg import with_xg

    matches = load_matches([league], range(FIRST_SEASON, LAST + 1))
    if not holdout.unlocked:
        matches = matches[matches["date"] < holdout.start].reset_index(drop=True)
    matches, err = with_xg(matches)
    if err:
        raise SystemExit(err)
    print(f"{len(matches)} matches, xG on {matches['home_xg'].notna().mean():.0%}")
    df = features.build(matches)
    df["time"] = df["date"]
    df["season_start"] = 2000 + df["season"].str[:2].astype(int)
    dc = backtest.walk_forward(
        matches,
        start=f"{WARMUP}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=0.7),
    )
    dc = dc[["date", "home", "away", "p_home", "p_draw", "p_away"]].rename(
        columns={f"p_{m}": f"dc_p_{m}" for m in H2H}
    )
    df = df.merge(dc, on=["date", "home", "away"], how="left")
    frames = []
    for y in range(WARMUP, LAST + 1):
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        frames.append(books.book_prices(raw, season=season_code(y)))
    px = pd.concat(frames, ignore_index=True)
    keep = ["date", "home", "away"] + [f"pinnacle_{w}_{m}" for w in ("early", "close") for m in H2H]
    df = df.merge(px[keep], on=["date", "home", "away"], how="left")
    early = _devig(df, [f"pinnacle_early_{m}" for m in H2H])
    close = _devig(df, [f"pinnacle_close_{m}" for m in H2H])
    for i, m in enumerate(H2H):
        df[f"mkt_{m}"], df[f"fair_{m}"] = early[:, i], close[:, i]
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    return df.sort_values("time").reset_index(drop=True)


def predict_all(df: pd.DataFrame, names: list[str], periods: list[int], holdout: Holdout):
    preds, tuning = {}, {}
    for name in names:
        pr, chosen = nested(
            df,
            FACTORIES[name],
            GRIDS[name],
            periods,
            period_col="season_start",
            holdout=holdout,
            train_from=f"{FIRST_SEASON + 1}-07-01",
        )
        preds[name] = pr.reindex(df.index).to_numpy(float)
        tuning[name] = chosen
        print(f"  {name}: {np.isfinite(preds[name]).all(1).sum()} predictions; settings {chosen}")
    return preds, tuning


def add_stack(df: pd.DataFrame, preds: dict, base: str) -> None:
    """(e): the base candidate blended with Pinnacle's early price, walk-forward."""
    market = df[[f"mkt_{m}" for m in H2H]].to_numpy(float)
    preds[STACK] = metrics.walk_forward_blend(
        market, preds[base], df["y"], df["time"], min_rows=STACK_MIN_ROWS
    )


def score(df: pd.DataFrame, preds: dict, scored: pd.Series, level: float) -> dict:
    market = df[[f"mkt_{m}" for m in H2H]].to_numpy(float)
    odds = df[[f"pinnacle_early_{m}" for m in H2H]].to_numpy(float)
    fair = df[[f"fair_{m}" for m in H2H]].to_numpy(float)
    y, g = df["y"].to_numpy(int), df["match"].to_numpy()
    s = scored.to_numpy()
    # Candidates a-d are scored on the same matches: those all of them predicted. The
    # stack is scored on the part of those where its blend had enough earlier rows.
    common = s & np.isfinite(market).all(1)
    for name, p in preds.items():
        if name != STACK:
            common &= np.isfinite(p).all(1)
    out = {}
    for name, p in preds.items():
        rows = common & np.isfinite(p).all(1)
        if not rows.any():
            continue
        r = metrics.evaluate(
            p[rows], y[rows], market[rows], odds[rows], fair[rows], g[rows], level=level
        )
        r["calibration"] = (
            metrics.calibration_table(p[rows], y[rows])
            .round(4)
            .reset_index(names="bin")
            .to_dict("records")
        )
        r["pass"] = (
            metrics.passes(r)
            if name != STACK
            else bool(
                (r["gain_vs_market"]["range"] or (0, 0))[0] > 0
                and (r["bets"].get("clv_range") or (0, 0))[0] > 0
            )
        )
        out[name] = r
    return out


def table(res: dict) -> pd.DataFrame:
    def rng(v, f="{:+.4f}"):
        return f"{f.format(v[0])}..{f.format(v[1])}" if v else ""

    rows = []
    for name, r in res.items():
        b, bl = r["bets"], r["blend"]
        rows.append(
            {
                "candidate": name,
                "matches": r["rows"],
                "log_loss": round(r["log_loss"], 4),
                "market_ll": round(r["market_log_loss"], 4),
                "gain_vs_mkt": rng(r["gain_vs_market"]["range"]),
                "brier": round(r["brier"], 4),
                "c": round(bl.get("c", np.nan), 3),
                "c_range": rng(bl.get("c_range"), "{:+.3f}"),
                "bets": b["bets"],
                "clv": round(b.get("clv") or np.nan, 4),
                "clv_range": rng(b.get("clv_range")),
                "roi": round(b.get("roi", np.nan), 3),
                "roi_range": rng(b.get("roi_range"), "{:+.3f}"),
                "pass": r["pass"],
            }
        )
    return pd.DataFrame(rows)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return round(float(x), 5)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def by_season(df: pd.DataFrame, preds: dict, scored: pd.Series) -> dict:
    out = {}
    y = df["y"].to_numpy(int)
    for s in sorted(df.loc[scored, "season_start"].unique()):
        m = (scored & (df["season_start"] == s)).to_numpy()
        row = {}
        for n, p in preds.items():
            k = m & np.isfinite(p).all(1)
            if k.any():
                row[n] = round(float(metrics.log_loss_rows(p[k], y[k]).mean()), 4)
        out[int(s)] = row
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m soccer_stats.lab.run")
    ap.add_argument("--open-holdout", action="store_true", help="score 2025/26 (once)")
    ap.add_argument("--reason", default="", help="why the holdout is opened (logged)")
    ap.add_argument("--finalists", default="", help="holdout: comma-separated candidates")
    ap.add_argument("--stack-base", default="", help="holdout: the stack's base candidate")
    ap.add_argument("--json")
    args = ap.parse_args(argv)

    from soccer_stats.edge.stats import bonferroni_level

    level = bonferroni_level(TESTS)
    holdout = Holdout(HOLDOUT_START)
    if args.open_holdout:
        names = [n.strip() for n in args.finalists.split(",") if n.strip()]
        if not names or set(names) - set(CANDIDATES) - {STACK}:
            raise SystemExit(f"--finalists must name candidates from {CANDIDATES + [STACK]}")
        if STACK in names and args.stack_base not in names:
            raise SystemExit("--stack-base must be one of the finalists.")
        holdout.unlock(args.reason)
    else:
        names = CANDIDATES + [STACK]
    print(f"Ranges are {level:.2%}: Bonferroni for {TESTS} tests (2 pass metrics x 5).")
    df = load(holdout)
    run_names = [n for n in names if n != STACK]
    last = LAST if args.open_holdout else LAST - 1
    periods = list(range(WARMUP, last + 1))
    print(f"Predicting seasons {periods[0]}-{periods[-1]} for {run_names}")
    preds, tuning = predict_all(df, run_names, periods, holdout)
    first = LAST if args.open_holdout else FIRST_SCORED
    scored = (df["season_start"] >= first) & (df["season_start"] <= last)
    stack_base = None
    if STACK in names:
        if args.open_holdout:
            stack_base = args.stack_base
        else:
            # Pre-registered: the best development log loss among a-d.
            dev = {
                n: metrics.log_loss_rows(p[ok], df["y"].to_numpy(int)[ok]).mean()
                for n, p in preds.items()
                for ok in [scored.to_numpy() & np.isfinite(p).all(1)]
            }
            stack_base = min(dev, key=dev.get)
        print(f"Stack base: {stack_base}")
        add_stack(df, preds, stack_base)
    res = score(df, preds, scored, level)
    label = "HOLDOUT 2025/26" if args.open_holdout else "DEVELOPMENT 2017/18-2024/25"
    print(f"\n== {label}: 1X2 at Pinnacle's early price (12% edge, 1 unit) ==")
    print(table(res).to_string(index=False))
    for name, r in res.items():
        print(f"\nCalibration, {name}:\n{pd.DataFrame(r['calibration']).to_string(index=False)}")
    seasons = by_season(df, preds, scored)
    print("\nLog loss by season:")
    print(pd.DataFrame(seasons).T.to_string())
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        out = {
            "mode": label,
            "level": level,
            "holdout_events": holdout.events,
            "stack_base": stack_base,
            "tuning": tuning,
            "results": res,
            "by_season": seasons,
        }
        Path(args.json).write_text(json.dumps(_jsonable(out), indent=1, default=str))


if __name__ == "__main__":
    main()
