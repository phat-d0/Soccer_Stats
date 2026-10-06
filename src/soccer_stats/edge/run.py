"""Edge research jobs: `python -m soccer_stats.edge.run {props,match,fanduel} ...`.

* props: paid probe of two-sided player-prop books (needs ODDS_API_KEY; --cap credits).
* match: line shopping on football-data prices with the walk-forward match model
  (needs football-data and Understat, so it runs in GitHub Actions; no credits).
* shots: Understat's shot counts vs football-data (team) and ESPN (player).
* fanduel: slices of backtest/E0_player_lines.csv.gz (local file, no network).

Output is plain text for the job log, plus JSON with --json. The key is never printed.
"""

from __future__ import annotations

import argparse
import functools
import json
from pathlib import Path

import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 300)
pd.set_option("display.max_columns", 30)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, float):
        return round(x, 5)
    return x


def _dump(out: dict, path: str | None) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(_jsonable(out), indent=1, default=str))
        print(f"\nJSON written to {path}")


def cmd_props(args: argparse.Namespace) -> None:
    from soccer_stats.edge import props

    dates = [d.strip() for d in args.hist_dates.split(",") if d.strip()]
    print(
        f"Plan: live event odds (up to {args.live_events} calls, ~{len(props.MARKETS)} markets x "
        f"{len(props.REGIONS)} regions each), then per historical date 1 + ~"
        f"{10 * len(props.MARKETS) * len(props.REGIONS)} credits. Cap {args.cap}."
    )
    if args.cap <= 0:
        print("Cap is 0: dry run, nothing fetched.")
        return
    res = props.probe(args.cap, dates, live_events=args.live_events)
    for line in res["log"]:
        print(line)
    print(f"Credits used: {res['spent']}; credits left: {res['left']}")
    out = {"spent": res["spent"], "left": res["left"], "log": res["log"], "snapshots": []}
    for label, body in res["bodies"]:
        ev = body.get("data", body)
        p = props.pairs(body)
        books = sorted({b.get("key") for b in ev.get("bookmakers", [])})
        print(f"\n== {label}: {ev.get('home_team')} v {ev.get('away_team')} ==")
        print(f"Books returning player markets: {books or 'none'}")
        s = props.book_summary(p)
        print(s.round(4).to_string(index=False) if not s.empty else "No player lines.")
        fv = props.fanduel_vs_fair(p)
        fvs = {}
        if not fv.empty:
            g = fv.groupby("market").agg(lines=("ev", "size"), ev=("ev", "mean"))
            g["fd_implied"] = fv.groupby("market").apply(lambda x: (1 / x["over"]).mean())
            g["fair"] = fv.groupby("market")["fair"].mean()
            print("FanDuel overs priced against the two-sided books' fair chance:")
            print(g.round(4).to_string())
            fvs = g.reset_index().to_dict("records")
        out["snapshots"].append(
            {
                "label": label,
                "match": f"{ev.get('home_team')} v {ev.get('away_team')}",
                "books": books,
                "summary": s.to_dict("records"),
                "fanduel_vs_fair": fvs,
            }
        )
    _dump(out, args.json)


def cmd_match(args: argparse.Namespace) -> None:
    from soccer_stats import backtest
    from soccer_stats import trades as tr
    from soccer_stats.data import download, load_matches, season_code
    from soccer_stats.edge import books
    from soccer_stats.models import DixonColes
    from soccer_stats.xg import with_xg

    lo, _, hi = args.seasons.partition("-")
    years = list(range(int(lo), int(hi or lo) + 1))
    matches = load_matches([args.league], range(years[0] - 2, years[-1] + 1))
    matches, err = with_xg(matches)
    weight = 0.7 if not err else 0.0
    print(err or f"xG attached to {matches['home_xg'].notna().mean():.0%} of matches")
    preds = backtest.walk_forward(
        matches,
        start=f"{years[0]}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=weight),
    )
    print(f"{len(preds)} out-of-sample predictions (xg_weight {weight})")
    frames = []
    for y in years:
        raw = pd.read_csv(download(args.league, y), encoding="latin-1", on_bad_lines="skip")
        frames.append(books.book_prices(raw, season=season_code(y)))
    prices = pd.concat(frames, ignore_index=True)
    joined = books.add_fair_close(books.join(preds, prices))
    print(f"{len(joined)} predictions matched to football-data prices")

    mg = books.margins(prices)
    print("\n== Margin by book (sum of 1/odds - 1) ==")
    print(mg.round(4).to_string(index=False))

    rows, by_season, sweep = [], [], []
    for book in books.all_books():
        for when in books.WHEN:
            bets = books.replay(joined, book, when, threshold=args.threshold)
            s = books.summarize(bets)
            cover = joined[[f"{book}_{when}_{m}" for m in books.MARKETS[:3]]].notna().all(axis=1)
            rows.append({"book": book, "when": when, "priced": int(cover.sum()), **s})
            if not bets.empty:
                for season, b in bets.groupby("season"):
                    by_season.append(
                        {"book": book, "when": when, "season": season, **books.summarize(b)}
                    )
            if book in ("pinnacle", "best_named", "max", "avg") and when == "early":
                for t in tr.SWEEP:
                    sweep.append(
                        {
                            "book": book,
                            "threshold": t,
                            **books.summarize(books.replay(joined, book, when, threshold=t)),
                        }
                    )
    res = pd.DataFrame(rows)
    print(f"\n== Trade rule at {args.threshold:.0%} edge, by price source (1 unit per bet) ==")
    print(_fmt(res).to_string(index=False))
    bs = pd.DataFrame(by_season)
    keep = bs["book"].isin(["pinnacle", "best_named", "max", "avg", "bet365"])
    print("\n== By season ==")
    print(_fmt(bs[keep]).to_string(index=False))
    print("\n== Threshold sweep at early prices (descriptive; the rule's 12% is fixed) ==")
    print(_fmt(pd.DataFrame(sweep)).to_string(index=False))

    _dump(
        {
            "seasons": args.seasons,
            "threshold": args.threshold,
            "margins": mg.to_dict("records"),
            "results": res.to_dict("records"),
            "by_season": bs.to_dict("records"),
            "sweep": sweep,
        },
        args.json,
    )


def cmd_shots(args: argparse.Namespace) -> None:
    from soccer_stats.data import download, season_code
    from soccer_stats.edge import books, espn
    from soccer_stats.player_data import load_appearances

    lo, _, hi = args.seasons.partition("-")
    years = range(int(lo), int(hi or lo) + 1)
    apps, missing = load_appearances(args.league, years)
    print(f"{len(apps)} Understat appearances ({missing} match files missing)")
    prices = pd.concat(
        [
            books.book_prices(
                pd.read_csv(download(args.league, y), encoding="latin-1", on_bad_lines="skip"),
                season=season_code(y),
            )
            for y in years
        ],
        ignore_index=True,
    )
    team = books.shots_check(prices, apps)
    print("\n== Team shots: Understat vs football-data (HS/AS, HST/AST) ==")
    for k, v in team.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    days = sorted(pd.to_datetime(apps["kickoff"]).dt.tz_convert(None).dt.normalize().unique())[
        -args.days :
    ]
    print(f"\n== Player shots: Understat vs ESPN on {len(days)} match dates ==")
    pl = espn.run(apps, [pd.Timestamp(d) for d in days])
    for k, v in pl.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    _dump({"team": team, "player": pl}, args.json)


def _fmt(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for c in ("roi_ci95", "clv_ci95"):
        if c in d:
            d[c] = [f"{v[0]:+.3f}..{v[1]:+.3f}" if isinstance(v, tuple) else "" for v in d[c]]
    return d.round(4)


def cmd_fanduel(args: argparse.Namespace) -> None:
    from soccer_stats.edge import fanduel

    d = fanduel.prepare(pd.read_csv(args.lines))
    t, tests = fanduel.scan(d, min_lines=args.min_lines)
    print(f"{len(d)} FanDuel over lines from {d['match'].nunique()} matches; {tests} slices")
    print(t.round(3).to_string(index=False))
    ho = fanduel.holdout(d, args.holdout, min_lines=args.min_lines)
    print(f"\nTop slices before {args.holdout}, re-measured on {args.holdout}:")
    for r in ho:
        print(r)
    _dump({"tests": tests, "slices": t.to_dict("records"), "holdout": ho}, args.json)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("props", help="paid probe: two-sided player-prop books")
    p.add_argument("--cap", type=int, default=0, help="most credits to spend (0 = dry run)")
    p.add_argument("--hist-dates", default="", help="comma-separated ISO times")
    p.add_argument("--live-events", type=int, default=2)
    p.add_argument("--json")
    p.set_defaults(func=cmd_props)
    m = sub.add_parser("match", help="line shopping on football-data prices")
    m.add_argument("--league", default="E0")
    m.add_argument("--seasons", default="2017-2025", help="seasons to bet (start years)")
    m.add_argument("--threshold", type=float, default=0.12)
    m.add_argument("--json")
    m.set_defaults(func=cmd_match)
    sh = sub.add_parser("shots", help="Understat shot counts vs football-data and ESPN")
    sh.add_argument("--league", default="E0")
    sh.add_argument("--seasons", default="2025")
    sh.add_argument("--days", type=int, default=6, help="latest match dates to check on ESPN")
    sh.add_argument("--json")
    sh.set_defaults(func=cmd_shots)
    f = sub.add_parser("fanduel", help="slices of FanDuel's historical player lines")
    f.add_argument("lines", help="path to E0_player_lines.csv.gz")
    f.add_argument("--holdout", default="2526")
    f.add_argument("--min-lines", type=int, default=300)
    f.add_argument("--json")
    f.set_defaults(func=cmd_fanduel)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
