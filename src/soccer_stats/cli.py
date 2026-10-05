"""Command line entry point: `soccer-stats backtest --league E0 --seasons 2019-2024`."""

from __future__ import annotations

import argparse

import pandas as pd

from soccer_stats import backtest
from soccer_stats.data import load_matches


def _years(spec: str) -> list[int]:
    """'2019-2023' -> [2019, ..., 2023]; '2021' -> [2021]."""
    lo, _, hi = spec.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


def cmd_backtest(args: argparse.Namespace) -> None:
    matches = load_matches(args.league, _years(args.seasons))
    start = args.start or f"{_years(args.seasons)[0] + args.burn_in}-08-01"
    preds = backtest.walk_forward(matches, start=start, lookback_days=args.lookback)
    preds = backtest.add_market_probs(preds)

    print(f"\n{len(preds)} matches predicted from {start}\n")
    print(backtest.score(preds).round(4).to_string())

    bets = backtest.simulate_bets(preds, min_edge=args.min_edge)
    print("\nBets at opening Pinnacle odds:")
    for k, v in backtest.summarize_bets(bets).items():
        print(f"  {k:>15}: {v:.4f}" if isinstance(v, float) else f"  {k:>15}: {v}")

    if args.out:
        preds.to_csv(args.out, index=False)
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
    bt.add_argument("--out", help="CSV path for predictions")
    bt.set_defaults(func=cmd_backtest)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
