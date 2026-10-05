"""Command line entry point.

soccer-stats backtest --league E0 --seasons 2019-2024
soccer-stats backtest --league E0 --seasons 2019-2024 --xg-weight 0 0.5 0.7 1
"""

from __future__ import annotations

import argparse
import functools

import pandas as pd

from soccer_stats import backtest
from soccer_stats.data import load_matches
from soccer_stats.models import DixonColes
from soccer_stats.xg import with_xg


def _years(spec: str) -> list[int]:
    """'2019-2023' -> [2019, ..., 2023]; '2021' -> [2021]."""
    lo, _, hi = spec.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


def cmd_backtest(args: argparse.Namespace) -> None:
    matches = load_matches(args.league, _years(args.seasons))
    if args.xg or any(w > 0 for w in args.xg_weight):
        matches, err = with_xg(matches)
        print(err or f"xG attached to {matches['home_xg'].notna().mean():.0%} of matches")
    start = args.start or f"{_years(args.seasons)[0] + args.burn_in}-08-01"

    results = {}
    for w in args.xg_weight:
        factory = functools.partial(DixonColes, xg_weight=w)
        preds = backtest.walk_forward(
            matches, start=start, lookback_days=args.lookback, model_factory=factory
        )
        preds = backtest.add_market_probs(preds)
        results[w] = preds
        bets = backtest.simulate_bets(preds, min_edge=args.min_edge)
        sc = backtest.score(preds)
        print(f"\n=== xg_weight={w}: {len(preds)} matches predicted from {start} ===")
        print(sc.round(4).to_string())
        print("Bets at opening Pinnacle odds:")
        for k, v in backtest.summarize_bets(bets).items():
            print(f"  {k:>15}: {v:.4f}" if isinstance(v, float) else f"  {k:>15}: {v}")

    if len(results) > 1:
        print("\n=== Model log loss by xg_weight (lower is better) ===")
        for w, preds in results.items():
            sc = backtest.score(preds)
            gap = sc.loc["model", "log_loss"] - sc.loc["market", "log_loss"]
            print(f"  {w:>4}: {sc.loc['model', 'log_loss']:.4f}  (vs market {gap:+.4f})")

    if args.out:
        list(results.values())[-1].to_csv(args.out, index=False)
        print(f"\nPredictions written to {args.out}")


def main(argv: list[str] | None = None) -> None:
    pd.set_option("display.width", 120)
    parser = argparse.ArgumentParser(prog="soccer-stats")
    sub = parser.add_subparsers(required=True)

    bt = sub.add_parser("backtest", help="walk-forward backtest against Pinnacle odds")
    bt.add_argument("--league", nargs="+", default=["E0"])
    bt.add_argument("--seasons", default="2019-2024", help="start years, e.g. 2019-2024")
    bt.add_argument("--burn-in", type=int, default=2, help="seasons of training before predicting")
    bt.add_argument("--start", help="first prediction date (overrides --burn-in)")
    bt.add_argument("--lookback", type=int, default=730, help="training window in days")
    bt.add_argument("--min-edge", type=float, default=0.03)
    bt.add_argument("--xg", action="store_true", help="load Understat xG (implied by --xg-weight)")
    bt.add_argument(
        "--xg-weight",
        type=float,
        nargs="+",
        default=[0.0],
        help="weight on xG vs goals; pass several to compare, e.g. 0 0.5 0.7 1",
    )
    bt.add_argument("--out", help="CSV path for predictions")
    bt.set_defaults(func=cmd_backtest)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
