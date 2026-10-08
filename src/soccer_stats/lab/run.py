"""The 1X2 bake-off: `python -m soccer_stats.lab.run [--open-holdout --reason ... --finalists ...]`.

Development (default): seasons 2016/17 (warm-up for the stack) to 2024/25, scored on
2017/18-2024/25. The 2025/26 holdout is locked: its matches are cut before any model
sees them. `--open-holdout --reason "..." --finalists a,b` scores only the named
candidates on 2025/26, once, and prints a banner with the time it was opened.

`--league` picks the football-data division (default E0). Leagues Understat doesn't
cover (E1-E3) run on goals only: no xG target for Dixon-Coles or the hierarchical
Poisson, and goals-only features (features.FEATURES_GOALS) for LightGBM and the logit.
Seasons where Pinnacle's early and closing 1X2 prices cover under MIN_COVERAGE of the
matches are not scored (they are still used for training).

`--holdout-season Y` moves the locked holdout to season Y (development then ends at
Y-1); `--coverage-only` prints Pinnacle and xG coverage per season (counts only, no
scoring) so a holdout can be chosen before pre-registering. In development the live
model's (a's) bets at Pinnacle early also get the learned minimum edge
(lab.thresholds).

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
LOWER = ("E1", "E2", "E3")  # pre-registered together: one family across 3 leagues
TOP = ("SP1", "D1", "I1", "F1")  # bake-off 3: one family across 4 leagues
MIN_COVERAGE = 0.9  # share of a season's matches with Pinnacle early and close prices

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def _devig(df: pd.DataFrame, cols: list[str]) -> np.ndarray:
    v = df[cols].to_numpy(float)
    out = np.full(v.shape, np.nan)
    for i in np.flatnonzero(np.isfinite(v).all(1) & (v > 1).all(1)):
        out[i] = devig_shin(v[i])
    return out


def has_xg(league: str) -> bool:
    from soccer_stats.xg import LEAGUES

    return league in LEAGUES


def family(league: str) -> int:
    """How many leagues share one multiple-testing family (E1-E3 count together, and
    SP1/D1/I1/F1 together)."""
    for group in (LOWER, TOP):
        if league in group:
            return len(group)
    return 1


def coverage_report(league: str, last: int = LAST) -> list[dict]:
    """Per season: matches, the share with all six Pinnacle 1X2 prices (early and
    close), and the share with Understat xG. Counts only: no results are scored and no
    model is fitted, so it can be run before choosing a holdout."""
    from soccer_stats.data import download, load_matches, season_code
    from soccer_stats.edge import books
    from soccer_stats.xg import with_xg

    matches = load_matches([league], range(WARMUP, last + 1))
    if has_xg(league):
        matches, err = with_xg(matches)
        if err:
            print(err)
    else:
        matches = matches.assign(home_xg=np.nan)
    frames = []
    for y in range(WARMUP, last + 1):
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        frames.append(books.book_prices(raw, season=season_code(y)))
    px = pd.concat(frames, ignore_index=True)
    cols = [f"pinnacle_{w}_{m}" for w in ("early", "close") for m in H2H]
    px["priced"] = px[cols].notna().all(axis=1)
    out = []
    for y in range(WARMUP, last + 1):
        code = season_code(y)
        m = matches[matches["season"] == code]
        p = px[px["season"] == code]
        out.append(
            {
                "season": y,
                "matches": len(m),
                "pinnacle": float(p["priced"].mean()) if len(p) else 0.0,
                "xg": float(m["home_xg"].notna().mean()) if len(m) else 0.0,
            }
        )
    return out


def coverage(df: pd.DataFrame) -> dict[int, float]:
    """Share of each season's matches with all six Pinnacle early and close prices."""
    cols = [f"pinnacle_{w}_{m}" for w in ("early", "close") for m in H2H]
    ok = df[cols].notna().all(axis=1)
    return {int(k): float(v) for k, v in ok.groupby(df["season_start"]).mean().items()}


def load(holdout: Holdout, league: str = "E0", last: int | None = None) -> pd.DataFrame:
    """One row per match: time, season, teams, goals, xG, FEATURES, y, the Dixon-Coles
    walk-forward chances, and Pinnacle's early and closing prices. With the holdout
    locked, matches from its start are dropped before anything is computed."""
    from soccer_stats import backtest
    from soccer_stats.data import download, load_matches, season_code
    from soccer_stats.edge import books
    from soccer_stats.models import DixonColes
    from soccer_stats.xg import with_xg

    last = last or LAST
    matches = load_matches([league], range(FIRST_SEASON, last + 1))
    if not holdout.unlocked:
        matches = matches[matches["date"] < holdout.start].reset_index(drop=True)
    if has_xg(league):
        matches, err = with_xg(matches)
        if err:
            raise SystemExit(err)
    else:
        matches = matches.assign(home_xg=np.nan, away_xg=np.nan)
    print(f"{league}: {len(matches)} matches, xG on {matches['home_xg'].notna().mean():.0%}")
    df = features.build(matches)
    df["time"] = df["date"]
    df["season_start"] = 2000 + df["season"].str[:2].astype(int)
    dc = backtest.walk_forward(
        matches,
        start=f"{WARMUP}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if has_xg(league) else 0.0),
    )
    dc = dc[["date", "home", "away", "p_home", "p_draw", "p_away"]].rename(
        columns={f"p_{m}": f"dc_p_{m}" for m in H2H}
    )
    df = df.merge(dc, on=["date", "home", "away"], how="left")
    frames = []
    for y in range(WARMUP, last + 1):
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


def predict_all(
    df: pd.DataFrame,
    names: list[str],
    periods: list[int],
    holdout: Holdout,
    goals_only: bool = False,
):
    preds, tuning = {}, {}
    for name in names:
        factory = FACTORIES[name]
        if goals_only and name in ("c_gbm", "d_logit"):
            factory = functools.partial(factory, features=tuple(features.FEATURES_GOALS))
        pr, chosen = nested(
            df,
            factory,
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


def model_edge_threshold(
    df: pd.DataFrame, p: np.ndarray, scored: pd.Series, label: str = "match bets"
) -> dict:
    """lab.thresholds on one candidate's bets at Pinnacle's early price: the live rule at
    a threshold of 0 (the outcome with the largest positive edge, one per match)."""
    from soccer_stats.lab import thresholds as th

    odds = df[[f"pinnacle_early_{m}" for m in H2H]].to_numpy(float)
    ok = scored.to_numpy() & np.isfinite(p).all(1) & np.isfinite(odds).all(1)
    d, q, o = df[ok], p[ok], odds[ok]
    rows, k = metrics.pick_bets(q, o, 0.0)
    bets = pd.DataFrame(
        {
            "edge": q[rows, k] * o[rows, k] - 1,
            "p": q[rows, k],
            "odds": o[rows, k],
            "won": (d["y"].to_numpy(int)[rows] == k).astype(float),
            "group": d["match"].to_numpy()[rows],
            "season": d["season_start"].astype(int).astype(str).to_numpy()[rows],
            "time": pd.to_datetime(d["time"].to_numpy()[rows]),
        }
    )
    return th.edge_threshold(bets, label)


def _print_edge(name: str, edge_th: dict) -> None:
    me = edge_th.get("min_edge")
    print(f"\n== Learned minimum edge ({name} at Pinnacle early, development seasons) ==")
    print(
        f"  min_edge: {'none' if me is None else f'{me:.0%}'}; {edge_th.get('n_bets')} "
        f"bets; {edge_th.get('seasons')}"
    )
    print(f"  {edge_th.get('note')}")
    for r in edge_th.get("by_bucket", []):
        hi = "up" if r["edge_hi"] is None else f"{r['edge_hi']:.0%}"
        rng = f" ({r['roi_lo']:+.1%} to {r['roi_hi']:+.1%})" if r.get("roi_lo") is not None else ""
        print(
            f"  claimed edge {r['edge_lo']:.0%}-{hi}: {r['n']} bets, implied "
            f"{r['implied']:.1%}, model {r['model']:.1%}, won {r['realized']:.1%}, "
            f"ROI {r['roi']:+.1%}{rng}"
        )


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
    ap.add_argument("--league", default="E0", help="football-data division, e.g. E0 or E1")
    ap.add_argument(
        "--holdout-season",
        type=int,
        default=None,
        help="start year of the locked holdout season (default 2025); development ends "
        "the season before",
    )
    ap.add_argument(
        "--coverage-only",
        action="store_true",
        help="print Pinnacle and xG coverage per season (counts only) and stop",
    )
    ap.add_argument(
        "--edge-only",
        action="store_true",
        help="development only: run candidate a and print its learned minimum edge "
        "(no other candidates, no stack, no tuning)",
    )
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    league = args.league
    if args.coverage_only:
        rows = coverage_report(league)
        print(f"{league}: Pinnacle early+close and Understat xG coverage by season")
        for r in rows:
            print(
                f"  {r['season']}/{(r['season'] + 1) % 100:02d}: {r['matches']} matches, "
                f"Pinnacle {r['pinnacle']:.0%}, xG {r['xg']:.0%}"
            )
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps({"league": league, "coverage": rows}))
        return
    hold_season = args.holdout_season or LAST

    from soccer_stats.edge.stats import bonferroni_level

    holdout = Holdout(f"{hold_season}-07-01" if args.holdout_season else HOLDOUT_START)
    if args.open_holdout:
        names = [n.strip() for n in args.finalists.split(",") if n.strip()]
        if not names or set(names) - set(CANDIDATES) - {STACK}:
            raise SystemExit(f"--finalists must name candidates from {CANDIDATES + [STACK]}")
        if STACK in names and args.stack_base not in names:
            raise SystemExit("--stack-base must be one of the finalists.")
        holdout.unlock(args.reason)
    elif args.edge_only:
        names = [CANDIDATES[0]]
    else:
        names = CANDIDATES + [STACK]
    # Development: 2 pass metrics x 5 candidates. Holdout: 2 x the finalists. E1-E3 are
    # one family, so both are multiplied by the 3 leagues.
    tests = (2 * len(names) if args.open_holdout else TESTS) * family(league)
    level = bonferroni_level(tests)
    print(f"Ranges are {level:.2%}: Bonferroni for {tests} tests (2 pass metrics each).")
    df = load(holdout, league, last=hold_season) if args.holdout_season else load(holdout, league)
    cov = coverage(df)
    print(
        "Pinnacle early+close coverage by season: "
        + ", ".join(f"{k}: {v:.0%}" for k, v in cov.items())
    )
    run_names = [n for n in names if n != STACK]
    last = hold_season if args.open_holdout else hold_season - 1
    periods = list(range(WARMUP, last + 1))
    print(f"Predicting seasons {periods[0]}-{periods[-1]} for {run_names}")
    preds, tuning = predict_all(df, run_names, periods, holdout, goals_only=not has_xg(league))
    first = hold_season if args.open_holdout else FIRST_SCORED
    priced = [k for k, v in cov.items() if v >= MIN_COVERAGE]
    dropped = [k for k in range(first, last + 1) if k not in priced]
    if dropped:
        print(f"Not scored (Pinnacle coverage under {MIN_COVERAGE:.0%}): {dropped}")
    scored = (
        (df["season_start"] >= first)
        & (df["season_start"] <= last)
        & df["season_start"].isin(priced)
    )
    if args.edge_only:
        edge_th = model_edge_threshold(df, preds[names[0]], scored, "match bets at Pinnacle early")
        _print_edge(names[0], edge_th)
        # One line with the whole contract, so the log alone can fill lab/min_edge.json.
        print("EDGE_JSON " + json.dumps(_jsonable(edge_th), default=str))
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            out = {"mode": "edge-only", "league": league, "coverage": cov}
            out |= {"not_scored": dropped, "edge_threshold": edge_th}
            Path(args.json).write_text(json.dumps(_jsonable(out), indent=1, default=str))
        return
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
    label = f"{league} " + (
        f"HOLDOUT {hold_season}/{(hold_season + 1) % 100:02d}"
        if args.open_holdout
        else f"DEVELOPMENT {FIRST_SCORED}/{(FIRST_SCORED + 1) % 100:02d}-"
        f"{last}/{(last + 1) % 100:02d}"
    )
    print(f"\n== {label}: 1X2 at Pinnacle's early price (12% edge, 1 unit) ==")
    print(table(res).to_string(index=False))
    for name, r in res.items():
        print(f"\nCalibration, {name}:\n{pd.DataFrame(r['calibration']).to_string(index=False)}")
    seasons = by_season(df, preds, scored)
    print("\nLog loss by season:")
    print(pd.DataFrame(seasons).T.to_string())
    edge_th = None
    base = CANDIDATES[0]  # a: the live model (pre-registered)
    if not args.open_holdout and base in preds:
        edge_th = model_edge_threshold(df, preds[base], scored, "match bets at Pinnacle early")
        _print_edge(base, edge_th)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        out = {
            "mode": label,
            "league": league,
            "level": level,
            "coverage": cov,
            "not_scored": dropped,
            "holdout_events": holdout.events,
            "stack_base": stack_base,
            "tuning": tuning,
            "results": res,
            "by_season": seasons,
            "edge_threshold": edge_th,
        }
        Path(args.json).write_text(json.dumps(_jsonable(out), indent=1, default=str))


if __name__ == "__main__":
    main()
