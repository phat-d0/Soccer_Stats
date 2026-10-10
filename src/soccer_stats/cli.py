"""Command line entry point.

soccer-stats backtest --league E0 --seasons 2019-2024
soccer-stats backtest --league E0 --seasons 2019-2024 --xg-weight 0 0.5 0.7 1
soccer-stats publish --out _site
soccer-stats log-odds --site _site --log-dir ../log
soccer-stats paper --site _site --log-dir ../log
soccer-stats backfill-odds --seasons 2025 --dry-run
soccer-stats backtest-dk --seasons 2023-2025 --out dk_trades.csv
soccer-stats match-markets --seasons 2017-2025 --json out/match_markets.json
"""

from __future__ import annotations

import argparse
import functools
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import backtest
from soccer_stats.data import current_season, load_matches
from soccer_stats.models import DixonColes
from soccer_stats.xg import with_xg


def _years(spec: str) -> list[int]:
    """'2019-2023' -> [2019, ..., 2023]; '2021' -> [2021]; 'now' is the season in
    progress ('2023-now')."""
    lo, _, hi = spec.partition("-")
    lo_y = current_season() if lo == "now" else int(lo)
    hi_y = current_season() if hi == "now" else int(hi or lo_y)
    return list(range(lo_y, hi_y + 1))


def _season_not_ready(league: str, year: int) -> str | None:
    """Why a season can't be backtested yet (no played match in football-data's or
    Understat's file, or a file can't be read), or None when it can."""
    from soccer_stats.player_data import season_matches

    try:
        played = len(load_matches([league], [year]))
        understat = int(season_matches(league, year)["played"].sum())
    except Exception as exc:  # file not posted yet, or a source down
        return f"its data can't be read yet ({type(exc).__name__})"
    if not played or not understat:
        return "no matches played yet"
    return None


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


def cmd_publish(args: argparse.Namespace) -> None:
    from soccer_stats.publish import publish

    out = publish(Path(args.out), league=args.league)
    print(
        f"Site written to {out}/ (open index.html via a local server, e.g. "
        f"python -m http.server -d {out})"
    )


def cmd_log_news(args: argparse.Namespace) -> None:
    import json

    from soccer_stats.players import append_news_log

    snapshot = json.loads(Path(args.snapshot).read_text())
    n = append_news_log(snapshot, Path(args.log_dir))
    print(f"{n} team-news changes logged")


def cmd_log_odds(args: argparse.Namespace) -> None:
    """Append the build's DraftKings prices to odds_log/ (no API calls; deduplicated)."""
    import json

    from soccer_stats import odds_log

    data = json.loads((Path(args.site) / "data.json").read_text())
    rows = odds_log.rows_from_data(data, pd.Timestamp.now(tz="UTC"), args.league)
    n = odds_log.append(Path(args.log_dir), rows, args.league)
    print(f"Odds log: {n} new rows ({len(rows)} priced fixture markets this build)")


def cmd_log_team_totals(args: argparse.Namespace) -> None:
    """Append the build's team-total rows and calls to odds_log/ (deduplicated)."""
    from soccer_stats import team_totals

    rows, calls = team_totals.log(Path(args.pending), Path(args.log_dir))
    print(f"Team-total log: {rows} new rows, {calls} new calls")


def cmd_team_totals_report(args: argparse.Namespace) -> None:
    """Model and look price vs FanDuel's de-margined team-total close (no key)."""
    import json

    from soccer_stats import team_totals
    from soccer_stats.odds_feed import TEAM_TOTAL_LEAGUES

    rows = team_totals.load_rows(Path(args.log_dir))
    if rows.empty:
        print("Team totals: nothing logged yet")
        return
    results = load_matches(list(TEAM_TOTAL_LEAGUES), [current_season()])
    # Round 13: FanDuel against the main market (docs/totals.md); counts only below the gate.
    rule = None
    if args.rule:
        cand, t = args.rule.split(":")
        rule = (cand.strip(), float(t))
        if rule[0] not in team_totals.MM_CANDIDATES or not args.after:
            raise SystemExit(f"--rule needs one of {team_totals.MM_CANDIDATES} and --after")
    dk = team_totals.load_dk(Path(args.log_dir))
    mm = team_totals.market_report(
        rows, dk, results, min_matches=args.min_matches, after=args.after or None, rule=rule
    )
    # The older model-vs-close report scores candidate (b)'s outcomes, so it waits for the
    # round-13 gate too: the development run stays the only look (amendment 1).
    team = rows[team_totals.market_kind(rows) == "team_totals"]
    out = team_totals.report(
        team, results, min_matches=team_totals.gated_min_matches(mm, args.min_matches)
    )
    print(json.dumps(out, indent=1, default=str))
    print("MARKET_TEST_JSON " + json.dumps(mm, default=str))


def cmd_espn_probe(args: argparse.Namespace) -> None:
    """Which ESPN league slugs answer, their team names and summary shape (no key)."""
    import json

    from soccer_stats import espn_news

    print(json.dumps(espn_news.probe(), indent=1, ensure_ascii=False, default=str))


def cmd_log_team_news(args: argparse.Namespace) -> None:
    """Append the build's ESPN team news to team_news/ (deduplicated; no API calls)."""
    import json

    from soccer_stats import espn_news

    data = json.loads((Path(args.site) / "data.json").read_text())
    rows = espn_news.rows_from_data(data, pd.Timestamp.now(tz="UTC"))
    n = espn_news.log(Path(args.log_dir), rows)
    print(f"Team-news log: {n} new rows ({len(rows)} team rows this build)")


def cmd_estimate_credits(args: argparse.Namespace) -> None:
    """Expected Odds API credits per league for a month under the refresh rules."""
    from soccer_stats.data import season_kickoffs
    from soccer_stats.leagues import LEAGUES
    from soccer_stats.odds_feed import estimate_credits
    from soccer_stats.xg import load_schedule

    month_start = pd.Timestamp(f"{args.month}-01", tz="UTC")
    year = month_start.year if month_start.month >= 7 else month_start.year - 1
    total = 0
    print(f"Odds API credits for {args.month} (refresh rules only; an upper bound):")
    for code, lg in LEAGUES.items():
        start, note = month_start, ""
        try:
            if lg.understat:  # Understat lists the whole season, kickoff times included
                kickoffs = list(load_schedule(code, year, include_played=True)["kickoff"])
            else:  # football-data only has played matches: use the month a year earlier
                start = month_start - pd.DateOffset(years=1)
                kickoffs = list(season_kickoffs(code, year - 1))
                note = f", {start:%b %Y} calendar"
        except Exception as exc:  # no schedule: say so rather than guess
            print(f"  {lg.name:<15} schedule unavailable ({type(exc).__name__})")
            continue
        end = start + pd.offsets.MonthBegin(1)
        e = estimate_credits(code, kickoffs, start, end)
        if lg.live:
            total += e["credits"]
        month = [k for k in kickoffs if start <= k < end]
        times = len({(k.hour, k.minute) for k in month})
        print(
            f"  {lg.name:<15} {'live' if lg.live else 'off ':<4}  {e['matches']:>3} matches  "
            f"{e['calls']:>4} calls  {e['credits']:>5} credits ({lg.odds_policy}; "
            f"{times} distinct kickoff times{note})"
        )
    print(f"Live leagues now: {total} credits")


def cmd_estimate_team_totals(args: argparse.Namespace) -> None:
    """Credits to log team-total prices per match under each snapshot plan (estimate only:
    no key, no calls). Replays each league's real fixture calendar (Understat)."""
    from soccer_stats.leagues import LEAGUES
    from soccer_stats.odds_feed import (
        SNAPSHOT_PLANS,
        TEAM_TOTAL_LEAGUES,
        estimate_snapshot_credits,
        matches_by,
    )
    from soccer_stats.xg import load_schedule

    months = [pd.Timestamp(f"{m.strip()}-01", tz="UTC") for m in args.months.split(",")]
    year = months[0].year if months[0].month >= 7 else months[0].year - 1
    kickoffs = {}
    for code in TEAM_TOTAL_LEAGUES:
        try:
            kickoffs[code] = list(load_schedule(code, year, include_played=True)["kickoff"])
        except Exception as exc:  # no schedule: say so rather than guess
            print(f"  {LEAGUES[code].name}: schedule unavailable ({type(exc).__name__})")
    markets = (1, 2)  # team_totals; + alternate_team_totals
    print("Team-total snapshots: credits per month (estimate; 1 credit per market per call)")
    print("  plan: lean = close only; base = 24 h + close; rich = 24 h + 6 h + close")
    grand = {(p, m): 0 for p in SNAPSHOT_PLANS for m in markets}
    for start in months:
        end = start + pd.offsets.MonthBegin(1)
        print(f"{start:%b %Y}")
        month_total = {(p, m): 0 for p in SNAPSHOT_PLANS for m in markets}
        for code, ks in kickoffs.items():
            cells = []
            for plan in SNAPSHOT_PLANS:
                e = estimate_snapshot_credits(code, ks, start, end, plan)
                for m in markets:
                    month_total[(plan, m)] += e["calls"] * m
                cells.append(f"{plan} {e['calls']:>3}/{2 * e['calls']:>3}")
            base = estimate_snapshot_credits(code, ks, start, end, "base")
            print(
                f"  {LEAGUES[code].name:<15} {base['matches']:>3} matches  "
                + "  ".join(cells)
                + f"  (close within 30 min: {base['close_in_window']})"
            )
        for k, v in month_total.items():
            grand[k] += v
        print(
            "  All five        "
            + "  ".join(
                f"{p} {month_total[(p, 1)]:>4}/{month_total[(p, 2)]:>4}" for p in SNAPSHOT_PLANS
            )
        )
    n = len(months)
    print(f"Average a month over {n} month(s) (team_totals / + alternate_team_totals):")
    for plan in SNAPSHOT_PLANS:
        print(f"  {plan:<5} {grand[(plan, 1)] / n:>6.0f} / {grand[(plan, 2)] / n:>6.0f}")
    since = pd.Timestamp(args.since, tz="UTC")
    allk = [k for ks in kickoffs.values() for k in ks]
    print(f"Priced matches from {since:%d %b %Y} (all five leagues; E0 alone):")
    e0 = kickoffs.get("E0", [])
    for w in (4, 8):
        print(f"  {w} weeks: {matches_by(allk, since, w)}; E0 {matches_by(e0, since, w)}")
    for label, ks in (("all five", allk), ("E0", e0)):
        weeks = next((w for w in range(1, 53) if matches_by(ks, since, w) >= 150), None)
        print(
            f"  150 matches ({label}): {weeks} weeks" if weeks else f"  150 ({label}): over a year"
        )


def cmd_paper(args: argparse.Namespace) -> None:
    import json

    from soccer_stats import paper
    from soccer_stats.data import current_season
    from soccer_stats.publish import _clean

    site = Path(args.site)
    data = json.loads((site / "data.json").read_text())
    season = current_season()
    try:  # every league with fixtures or trades this build (leagues.py), primary first
        results = load_matches(paper.leagues_in_play(data, args.league), [season - 1, season])
    except Exception as exc:  # no results: trades stay open until the next build
        print(f"Results unavailable ({type(exc).__name__}); nothing settled this time")
        results = pd.DataFrame(columns=["home", "away", "season"])
    apps = None
    if args.league == "E0":
        try:
            from soccer_stats.player_data import load_appearances

            apps, _ = load_appearances(args.league, [season], max_new=20)
        except Exception as exc:  # player trades settle on a later build
            print(f"Player data unavailable ({type(exc).__name__})")
    log_dir = Path(args.log_dir) if args.log_dir else None
    n = paper.run(data, log_dir, results, league=args.league, apps=apps)
    detail = log_dir / "backtest" / f"{args.league}_players_detail.json" if log_dir else None
    if detail and detail.exists():  # the Players view loads this on demand
        shutil.copy(detail, site / "players_backtest.json")
        data["players_backtest"] = "players_backtest.json"
    (site / "data.json").write_text(json.dumps(_clean(data), separators=(",", ":")))
    live = data["portfolio"]["live"]
    s = live.get("summary", {})
    print(
        live.get("error")
        or f"Paper trades: {n} ledger events written; {s.get('trades', 0)} trades, "
        f"{s.get('open', 0)} open"
        + (f". {live['note']}" if live.get("note") else "")
    )


def _plan(league: str, seasons: list[int], looks: list[float]):
    from soccer_stats.odds_history import plan_snapshots
    from soccer_stats.xg import load_schedule

    kickoffs = pd.concat(
        [load_schedule(league, y, include_played=True)["kickoff"] for y in seasons]
    )
    now = pd.Timestamp.now(tz="UTC")
    return plan_snapshots(kickoffs[kickoffs < now], looks_hours=looks)


def cmd_backfill(args: argparse.Namespace) -> None:
    from soccer_stats.odds_history import backfill

    plan = _plan(args.league, _years(args.seasons), args.looks)
    print(f"{plan['kickoff'].nunique()} kickoffs played in {args.seasons}")
    rep = backfill(
        plan["at"],
        args.league,
        max_credits=args.max_credits,
        keep_credits=args.keep_credits,
        dry_run=args.dry_run,
    )
    for line in rep.lines():
        print(line)
    if args.dry_run:
        print("Dry run: no API calls made.")


def _pinnacle_edge_threshold(preds: pd.DataFrame, league: str) -> dict:
    """lab.thresholds on the model's bets at Pinnacle's early price (football-data)."""
    from soccer_stats.data import download, season_code
    from soccer_stats.edge import books
    from soccer_stats.lab import thresholds as th

    if preds.empty:
        return th.edge_threshold(pd.DataFrame(columns=["edge", "won"]), "match bets")
    years = sorted({2000 + int(str(s)[:2]) for s in preds["season"].dropna().unique()})
    frames = []
    for y in years:
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        frames.append(books.book_prices(raw, season=season_code(y)))
    joined = books.add_fair_close(books.join(preds, pd.concat(frames, ignore_index=True)))
    bets = books.replay(joined, "pinnacle", "early", threshold=0.0)
    pool = pd.DataFrame(
        {
            "edge": bets["edge"],
            "p": bets["model_p"],
            "odds": bets["odds"],
            "won": bets["won"].astype(float),
            "group": bets["season"].astype(str) + "|" + bets["home"] + "|" + bets["away"],
            "season": bets["season"].astype(str),
            "time": pd.to_datetime(bets["date"]),
        }
    )
    return th.edge_threshold(pool, "match bets at Pinnacle's early price")


def _print_edge_threshold(name: str, et: dict | None) -> None:
    if not et:
        return
    me = et.get("min_edge")
    print(f"\n== Learned minimum edge: {name} ==")
    print(
        f"  min_edge: {'none' if me is None else f'{me:.0%}'} ({et.get('n_bets')} bets; "
        f"{et.get('seasons')})"
    )
    print(f"  {et.get('note')}")
    for r in et.get("by_bucket", []):
        hi = r["edge_hi"]
        print(
            f"  claimed {r['edge_lo']:.0%}-{'up' if hi is None else f'{hi:.0%}'}: n {r['n']}, "
            f"implied {r['implied']:.3f}, model {r['model']:.3f}, won {r['realized']:.3f} "
            f"({r['realized_lo']:.3f}-{r['realized_hi']:.3f}), return {r['roi']:+.3f} "
            f"({r['roi_lo']:+.3f} to {r['roi_hi']:+.3f})"
        )


def cmd_backtest_dk(args: argparse.Namespace) -> None:
    import json

    from soccer_stats import match_calibration as mc
    from soccer_stats import paper
    from soccer_stats import trades as tr
    from soccer_stats.odds_history import coverage, load_history
    from soccer_stats.publish import _clean

    seasons = _years(args.seasons)
    first = seasons[0] - args.burn_in - args.blend_seasons
    matches = load_matches([args.league], range(first, seasons[-1] + 1))
    matches, err = with_xg(matches)
    print(err or f"xG attached to {matches['home_xg'].notna().mean():.0%} of matches")
    known = set(matches["home"])
    history = load_history(args.league, known)
    played = matches[matches["season"].isin([f"{y % 100:02d}{(y + 1) % 100:02d}" for y in seasons])]
    cov = coverage(played, history)
    print(
        "DraftKings coverage: "
        + ", ".join(f"{c['season']}: {c['priced']}/{c['matches']}" for c in cov)
    )
    if history.empty:
        print("No historical snapshots cached: run backfill-odds first.")
        return

    weight = args.xg_weight if matches["home_xg"].notna().any() else 0.0
    factory = functools.partial(DixonColes, xg_weight=weight)
    cands = backtest.dk_candidates(
        matches,
        history,
        start=f"{seasons[0]}-07-01",
        looks_hours=tuple(args.looks),
        model_factory=factory,
    )
    ref = {"xg_weight": weight, "matches_fit": len(matches)}

    # The blend is fitted on Pinnacle's close (football-data) beside the same model's
    # walk-forward chances, starting blend_seasons before the first DraftKings season.
    preds = backtest.walk_forward(
        matches, start=f"{seasons[0] - args.blend_seasons}-07-01", model_factory=factory
    )
    pool = {g: mc.training_rows(preds, g) for g in mc.GROUPS}
    print("Blend training matches: " + ", ".join(f"{g} {len(d)}" for g, d in pool.items()))
    cands, fits = backtest.add_blend(cands, pool)
    strategies = backtest.dk_strategies(cands, args.threshold, args.max_odds, args.league, ref)
    trades = strategies["raw"]["trades_df"]
    capped = backtest.dk_trades(
        cands, threshold=args.threshold, max_odds=tr.CAP_ODDS, league=args.league, model_ref=ref
    )
    rep = tr.report(trades)
    sweep = strategies["raw"]["sweep"]
    out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "seasons": args.seasons,
        "threshold": args.threshold,
        "max_odds": args.max_odds,
        "looks": [f"{h:g}h" for h in args.looks],
        "coverage": cov,
        "log_loss": backtest.dk_log_loss(cands),
        "summary": rep["summary"],
        "summary_capped": tr.summarize(capped),
        "breakdowns": rep["breakdowns"],
        "sweep": sweep,
        "totals_priced": float(cands["odds_over25"].notna().mean())
        if "odds_over25" in cands
        else 0.0,
        "strategies": {
            k: {key: val for key, val in v.items() if key != "trades_df"}
            for k, v in strategies.items()
        },
        # The learned minimum edge for the chance the live app trades on (the blend
        # once it is fitted), and for the model alone on Pinnacle's early prices over
        # the longer football-data history (lab.thresholds; docs/lab.md).
        "edge_threshold": strategies.get("blend", strategies["raw"])["edge_threshold"],
        "edge_threshold_strategy": "blend" if "blend" in strategies else "raw",
        "edge_threshold_pinnacle": _pinnacle_edge_threshold(preds, args.league),
        "blend": {
            "pool": "Pinnacle closing odds (football-data) with the model's walk-forward chances",
            "fits": fits,
            "live": {g: mc.live_fit(d, g) for g, d in pool.items()},
        },
        "trades": trades.sort_values("kickoff", ascending=False).to_dict("records")
        if not trades.empty
        else [],
    }
    _print_report(out)
    _print_strategies(out)
    for k, v in strategies.items():
        _print_edge_threshold(f"DraftKings, {k}", v.get("edge_threshold"))
    _print_edge_threshold("Pinnacle early (football-data)", out["edge_threshold_pinnacle"])
    blend_trades = strategies["blend"]["trades_df"]
    if args.out and not blend_trades.empty:
        bpath = Path(args.out).with_name(Path(args.out).stem + "_blend.csv")
        bpath.parent.mkdir(parents=True, exist_ok=True)
        blend_trades.to_csv(bpath, index=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(args.out, index=False)
        print(f"\nTrades written to {args.out}")
    if args.json:
        path = Path(args.json)
    elif args.log_dir:
        path = paper.backtest_path(Path(args.log_dir), args.league)
    else:
        path = None
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_clean(out), separators=(",", ":")))
        print(f"App data written to {path}")


def _print_goals(goals: dict) -> None:
    print("\n=== Anytime goalscorer (stage 1: no odds), P(scores | plays) ===")
    for view in ("before_lineups", "lineup_known"):
        for split, s in (goals.get(view) or {}).items():
            if not s.get("n"):
                continue
            parts = [
                f"{view:>14} {split:>11}: {s['n']} apps, scored {s['scored_rate']:.1%} "
                f"vs predicted {s['predicted_rate']:.1%}; log loss {s['model']['log_loss']:.4f}"
            ]
            for b in ("season_goals", "season_xg"):
                v = s[f"vs_{b}"]
                rng = v["range95"] or [float("nan")] * 2
                parts.append(f"vs {b} {v['diff']:+.4f} ({rng[0]:+.4f} to {rng[1]:+.4f})")
            print("; ".join(parts))
    print(f"Goalscorer gate ({goals['gate']['rule']}): {goals['gate']['passed']}")


def cmd_backtest_players(args: argparse.Namespace) -> None:
    import json

    from soccer_stats import player_backtest as pb
    from soccer_stats import player_goals as pg
    from soccer_stats import trades as tr
    from soccer_stats.factors import ALL_FACTORS, build_features
    from soccer_stats.player_data import load_appearances
    from soccer_stats.player_odds import load_history as load_player_odds
    from soccer_stats.publish import _clean

    seasons = _years(args.seasons)
    # The season in progress joins once it has played matches; before that (or with a
    # source down) it's left out rather than failing the weekly run.
    if len(seasons) > 1 and seasons[-1] >= current_season():
        why = _season_not_ready(args.league, seasons[-1])
        if why:
            print(f"Leaving out {seasons[-1]}/{(seasons[-1] + 1) % 100:02d}: {why}")
            seasons = seasons[:-1]
    args.seasons = f"{seasons[0]}-{seasons[-1]}" if len(seasons) > 1 else str(seasons[0])
    years = range(seasons[0] - args.burn_in, seasons[-1] + 1)
    apps, missing = load_appearances(args.league, years)
    if apps.empty:
        raise SystemExit(
            f"No player appearances loaded ({missing} match files missing): Understat's match "
            "data couldn't be read, so the player model can't be tested."
        )
    print(
        f"{len(apps)} appearances from {apps['match_id'].nunique()} matches"
        + (f" ({missing} matches couldn't be downloaded)" if missing else "")
    )
    starters = apps[apps["started"]].groupby(["match_id", "team"]).size()
    print(f"Starters per team per match: {starters.value_counts().sort_index().to_dict()}")
    matches = load_matches([args.league], years)
    matches, err = with_xg(matches)
    preds = backtest.walk_forward(
        matches,
        start=f"{seasons[0] - args.burn_in + 1}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if not err else 0.0),
    )
    feats = build_features(apps, pb.match_info(preds, apps))
    start = f"{seasons[0]}-07-01"

    before = pb.walk_forward(feats, start)
    known = pb.walk_forward(feats, start, lineup_known=True)
    s_before, s_known = pb.score(before), pb.score(known)
    sot_count = pb.score(pb.walk_forward(feats, start, sot_method="count"))
    sot_method = "count" if sot_count["sot"]["model"] < s_before["sot"]["model"] else "thin"
    ab = pb.ablation(feats, start, s_before)
    factors = pb.kept_factors(ab["kept_groups"]) or ALL_FACTORS
    if sot_method == "count" or factors != ALL_FACTORS:  # final model with the choices made
        before = pb.walk_forward(feats, start, factors, sot_method=sot_method)
        s_final = pb.score(before)
    else:
        s_final = s_before
    rec = pb.reconcile_team_totals(known)
    gate = {
        "shots": s_final["shots"]["beats_baseline"],
        "sot": s_final["sot"]["beats_baseline"],
    }
    gate["passed"] = gate["shots"] and gate["sot"]

    out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "seasons": args.seasons,
        "gate": gate,
        "factors": factors,
        "sot_method": sot_method,
        "before_lineups": s_final,
        "lineup_known": s_known,
        # stage 1 per season, so a new season's few matches are visible on their own
        "by_season": pb.score_by_season(before),
        "sot_methods": {"thin": s_before["sot"]["model"], "count": sot_count["sot"]["model"]},
        "ablation": ab,
        "team_totals": rec,
        "starters_per_team": {str(k): int(v) for k, v in starters.value_counts().items()},
    }
    hist = load_player_odds(args.league, set(apps["team"]))
    if not hist.empty:
        known_final = pb.walk_forward(
            feats, start, factors, lineup_known=True, sot_method=sot_method
        )
        trades, info = pb.priced_trades(before, hist, apps, league=args.league, known=known_final)
        lines = info.pop("_lines", {})
        if args.out and any(not d.empty for d in lines.values()):
            keep = [
                "kickoff",
                "home",
                "away",
                "player",
                "player_id",
                "team",
                "position",
                "started",
                "market",
                "line",
                "side",
                "odds",
                "implied",
                "p_model",
                "p",
                "exp_count",
                "actual",
                "won",
            ]
            frames = [d.assign(kind=k)[["kind", *keep]] for k, d in lines.items() if not d.empty]
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            pd.concat(frames).round(4).to_csv(
                Path(args.out).with_name(f"{args.league}_player_lines.csv.gz"), index=False
            )
        out["priced"] = {**info, **tr.report(trades)} if not trades.empty else info
        out["edge_threshold"] = info.get("edge_threshold")
        for name, st in info.get("strategies", {}).items():
            _print_edge_threshold(f"player shots, {name}", st.get("edge_threshold"))
        out["trades"] = (
            trades.sort_values("kickoff", ascending=False).to_dict("records")
            if not trades.empty
            else []
        )
        if args.out and not trades.empty:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            trades.to_csv(args.out, index=False)

    # Anytime goalscorer, stage 1 (no odds): same appearances and match model.
    # The forward window (from 2026-10-10) stays out until opened with its reason.
    goals = {
        "generated_at": out["generated_at"],
        "seasons": args.seasons,
        **pg.stage1_goals(pg.goal_features(feats), start, args.goal_forward_reason or None),
    }
    fw = goals["forward_window"]
    print(
        f"Goalscorer forward window from {fw['start']}: "
        + ("open" if fw["open"] else f"locked, {fw['dropped']} appearances left out")
    )
    _print_goals(goals)

    print(f"\n=== Player model, {args.seasons} (stage 1: no odds) ===")
    for name, sc in (("Before lineups", s_final), ("Lineup known", s_known)):
        for c in ("shots", "sot"):
            r = sc[c]
            print(
                f"{name:>15} {c:>5}: log loss {r['model']:.4f} vs baseline {r['baseline']:.4f}"
                f" ({'beats' if r['beats_baseline'] else 'does NOT beat'} it)"
            )
    for season, r in out["by_season"].items():
        print(
            f"  {season}: {r['appearances']} appearances, shots {r['shots']['model']:.4f} vs "
            f"{r['shots']['baseline']:.4f}, on target {r['sot']['model']:.4f} vs "
            f"{r['sot']['baseline']:.4f}"
        )
    print(
        f"Shots on target method: {sot_method} "
        f"(thin {out['sot_methods']['thin']:.4f}, count {out['sot_methods']['count']:.4f})"
    )
    print("Ablation (log loss without each group; kept if removing it hurts):")
    for g in ab["groups"]:
        print(
            f"  without {g['without']:>9}: {g['log_loss']:.4f} ({g['change']:+.4f}) "
            f"{'kept' if g['kept'] else 'DROPPED'}"
        )
    if rec:
        print(
            f"Team totals: players' expected shots / team expected shots = "
            f"{rec['ratio_to_expected']:.3f} (tolerance ±{rec['tolerance']:.0%})"
        )
    print(
        f"Gate: {'PASSED' if gate['passed'] else 'not passed'}: player odds and trades are "
        f"{'on' if gate['passed'] else 'off'}"
    )
    if "priced" in out:
        pr = out["priced"]
        s = pr.get("summary", {})
        print(
            f"Stage 2: {s.get('trades', 0)} player trades, ROI {_fmt(s.get('roi'), 'pct')}, "
            f"CLV {_fmt(s.get('clv_dk'), 'pct')}; {pr['unmatched_names']} names unmatched"
        )
        for name, st in pr.get("strategies", {}).items():
            sweep = ", ".join(
                f"{th}: {x.get('trades', 0)} / {_fmt(x.get('roi'), 'pct')}"
                for th, x in st["sweep"].items()
            )
            print(f"  {name:>13} (trades / ROI by edge threshold): {sweep}")
        detail = {k: v for k, v in pr.items() if k not in ("summary", "breakdowns", "strategies")}
        print("Stage 2 detail: " + json.dumps(detail, default=str))

    path = (
        Path(args.json)
        if args.json
        else (
            Path(args.log_dir) / "backtest" / f"{args.league}_players.json"
            if args.log_dir
            else None
        )
    )
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_clean(out), separators=(",", ":")))
        print(f"Saved to {path}")
        # Per-player results for the app's Players view (kept out of data.json: it's big).
        detail = {
            "generated_at": out["generated_at"],
            "seasons": args.seasons,
            **pb.player_report(before),
        }
        dpath = path.with_name(f"{args.league}_players_detail.json")
        dpath.write_text(json.dumps(_clean(detail), separators=(",", ":")))
        print(f"Per-player results for {len(detail['players'])} players saved to {dpath}")
        gpath = path.with_name(f"{args.league}_goals.json")
        gpath.write_text(json.dumps(_clean(goals), separators=(",", ":")))
        print(f"Anytime goalscorer stage 1 saved to {gpath}")


def cmd_backfill_players(args: argparse.Namespace) -> None:
    from soccer_stats.player_data import season_matches
    from soccer_stats.player_odds import backfill

    now = pd.Timestamp.now(tz="UTC")
    ks = pd.concat([season_matches(args.league, y)["kickoff"] for y in _years(args.seasons)])
    ks = ks[(ks < now) & (ks >= pd.Timestamp("2023-05-03", tz="UTC"))]
    rep = backfill(
        ks,
        args.league,
        max_credits=args.max_credits,
        keep_credits=args.keep_credits,
        dry_run=args.dry_run,
    )
    print(f"{len(ks)} matches played in {args.seasons}")
    for line in rep.lines():
        print(line)
    if args.dry_run:
        print("Dry run: no API calls made.")


def cmd_player_odds_check(args: argparse.Namespace) -> None:
    from soccer_stats.player_odds import check

    for line in check(args.league):
        print(line)


def _goal_league_feats(league: str, open_forward: str):
    """Understat appearances 2021/22-2025/26 (and 2026/27 only when the forward window is
    opened) with the goal features and that league's match model."""
    from soccer_stats import player_backtest as pb
    from soccer_stats import player_goal_lab as gl
    from soccer_stats import player_goals as pg
    from soccer_stats.factors import build_features
    from soccer_stats.player_data import load_appearances

    years = range(2021, current_season() + 1 if open_forward else 2026)
    apps, missing = load_appearances(league, years)
    print(f"{league}: {len(apps)} appearances ({missing} match files missing)")
    matches, err = with_xg(load_matches([league], years))
    preds = backtest.walk_forward(
        matches,
        start="2022-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if not err else 0.0),
    )
    mi = pb.match_info(preds, apps)
    feats = gl.extra_features(pg.goal_features(build_features(apps, mi)))
    if not open_forward:  # nothing from the forward window is ever computed while locked
        feats = feats[feats["kickoff"] < gl.FORWARD_START]
    cover = feats["team_xg"].notna().mean() if "team_xg" in feats else 0.0
    print(
        f"{league}: match-model coverage {cover:.1%} of appearances" + (f" ({err})" if err else "")
    )
    return feats.reset_index(drop=True)


def cmd_goal_league(args: argparse.Namespace) -> None:
    """One league's goalscorer rows for the round-8 test (docs/player_props.md §10)."""
    import json

    from soccer_stats import player_goal_lab as gl
    from soccer_stats.lab.harness import Holdout
    from soccer_stats.publish import _clean

    feats = _goal_league_feats(args.league, args.open_forward)
    out = {"league": args.league}
    lo = pd.Timestamp("2023-07-01", tz="UTC")
    rows = gl.scored_rows(
        feats, args.league, lo, gl.FORWARD_START, Holdout(gl.FORWARD_START), gl.LEAGUE_SEASONS
    )
    out["history"] = gl.bench_tests(rows, gl.LEVEL_LEAGUE)
    _print_bench(args.league, out["history"])
    if args.rows:
        Path(args.rows).parent.mkdir(parents=True, exist_ok=True)
        rows.to_csv(args.rows, index=False)
    if args.open_forward:
        fh = gl.forward_holdout(args.open_forward)
        hi = feats["kickoff"].max() + pd.Timedelta(days=1)
        fr = gl.scored_rows(feats, args.league, gl.FORWARD_START, hi, fh)
        fr = fr[pd.to_datetime(fr["kickoff"], utc=True) >= gl.FORWARD_START]
        out["forward_log"] = fh.events
        if args.rows:
            fr.to_csv(Path(args.rows).with_name(f"forward_{args.league}.csv.gz"), index=False)
    if args.json:
        Path(args.json).write_text(json.dumps(_clean(out), indent=1, default=str))


def _print_bench(name: str, res: dict) -> None:
    import json

    a, b = res.get("all_before_lineups") or {}, res.get("starters_lineup_known") or {}
    if not a:
        print(f"{name}: no rows")
        return
    va, vb = a["vs"], b["vs"]
    print(
        f"{name}: {a['rows']} appearances, {a['matches']} matches | A before lineups: log loss "
        f"{a['model']['log_loss']}, gain vs season goals {va['season_goals']['gain']} "
        f"{va['season_goals']['range']}, vs season xG {va['season_xg']['gain']} "
        f"{va['season_xg']['range']} -> beats both: {a['beats_both']}"
    )
    print(
        f"{name}: {b['rows']} starters | B lineup known: log loss {b['model']['log_loss']}, "
        f"vs goals {vb['season_goals']['gain']} {vb['season_goals']['range']}, vs xG "
        f"{vb['season_xg']['gain']} {vb['season_xg']['range']}, vs A {b['vs_A']['gain']}, "
        f"AUC {b['model']['auc']}, 30%+ {b['model']['share_30plus']:.1%}, "
        f"tail {json.dumps(b['model']['tail'])}"
    )


def _print_bcal(res: dict) -> None:
    import json

    for key, c in res["coefficients"].items():
        print(f"B-cal {key}: {json.dumps(c)}")
    for name, v in [("Pooled", res["pooled"]), *res["leagues"].items()]:
        if not v.get("rows"):
            continue
        b, bc = v["b"], v
        print(
            f"B-cal {name}: {v['rows']} starters | log loss B {b['model']['log_loss']} -> "
            f"B-cal {bc['model']['log_loss']}, B-cal vs B {bc['vs_B']['gain']} "
            f"{bc['vs_B']['range']}, vs xG B {b['vs']['season_xg']['gain']} -> B-cal "
            f"{bc['vs']['season_xg']['gain']}, 30%+ B {b['model']['share_30plus']:.1%} -> "
            f"B-cal {bc['model']['share_30plus']:.1%}"
        )
        print(f"B-cal {name}: tail B {json.dumps(b['model']['tail'])}")
        print(f"B-cal {name}: tail B-cal {json.dumps(bc['model']['tail'])}")


def cmd_goal_pool(args: argparse.Namespace) -> None:
    """Pool the leagues' rows: the historical answer, and the forward check if present."""
    import json

    from soccer_stats import player_goal_lab as gl
    from soccer_stats.publish import _clean

    d = Path(args.rows_dir)
    hist = {
        p.name.split("_")[1].split(".")[0]: pd.read_csv(p) for p in sorted(d.rglob("rows_*.csv.gz"))
    }
    for r in hist.values():
        r["started"] = r["started"].astype(bool)
    out = {"history": gl.history_report(hist)}
    for lg, res in out["history"]["leagues"].items():
        _print_bench(lg, res)
    if "pooled_new_leagues" in out["history"]:
        _print_bench("Pooled SP1+D1+I1+F1", out["history"]["pooled_new_leagues"])
    print(
        "Beats both benchmarks (A, all appearances, 99.375%): "
        + json.dumps(out["history"]["answer"])
    )
    allh = pd.concat(list(hist.values()), ignore_index=True) if hist else pd.DataFrame()
    if not allh.empty:  # §12: B-cal on the seen seasons, descriptive
        allh["season"] = allh["season"].astype(str)
        out["b_cal_development"] = gl.bcal_development(allh)
        _print_bcal(out["b_cal_development"])
    fwd = sorted(d.rglob("forward_*.csv.gz"))
    if fwd:
        fr = pd.concat([pd.read_csv(p) for p in fwd], ignore_index=True)
        fr["started"] = fr["started"].astype(bool)
        fr["season"] = fr["season"].astype(str)
        if not allh.empty:  # B-cal for the window is fitted on the locked history only
            fr, out["b_cal_forward_coefficients"] = gl.add_bcal(fr, allh)
        out["forward"] = gl.forward_report(fr)
        print("Forward check: " + json.dumps(out["forward"].get("gate"), default=str))
        if "b_cal" in out["forward"]:
            print("Forward check, B-cal: " + json.dumps(out["forward"]["b_cal"]["gate"]))
    if args.json:
        Path(args.json).write_text(json.dumps(_clean(out), indent=1, default=str))


def cmd_goal_lab(args: argparse.Namespace) -> None:
    """Goalscorer model improvements (docs/player_props.md §8), print-only."""
    import json

    from soccer_stats import player_backtest as pb
    from soccer_stats import player_goal_lab as gl
    from soccer_stats import player_goals as pg
    from soccer_stats.factors import build_features
    from soccer_stats.lab.harness import Holdout
    from soccer_stats.player_data import load_appearances
    from soccer_stats.publish import _clean

    years = range(2021, current_season() + 1 if args.open_forward else 2026)
    apps, missing = load_appearances(args.league, years)
    print(f"{len(apps)} appearances ({missing} match files missing)")
    matches, err = with_xg(load_matches([args.league], years))
    preds = backtest.walk_forward(
        matches,
        start="2022-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if not err else 0.0),
    )
    feats = gl.extra_features(pg.goal_features(build_features(apps, pb.match_info(preds, apps))))
    fwd = feats["kickoff"] >= gl.FORWARD_START
    if not args.open_forward:
        feats = feats[~fwd]  # the forward window is never computed on while locked
    feats = feats.reset_index(drop=True)
    out = {"pre_registration": "docs/player_props.md section 8"}

    print("\n=== Development (2023/24-2024/25), starters, lineup known ===")
    dev, tuned = gl.predict_dev(feats, Holdout(gl.SEEN_START))
    res = gl.compare(dev, feats)
    out.update(development=res, gbm_tuning=tuned)
    for c, d in res["candidates"].items():
        t = res["tests"].get(c, {})
        print(
            f"  {c}: log loss {d['log_loss']} (season-xG {d.get('log_loss_season_xg')}), "
            f"Brier {d['brier']}, resolution {d['resolution']}, AUC {d['auc']}, "
            f"30%+ {d['share_30plus']:.1%}, tail ok {d['tail']['ok']}"
            + (
                f" | vs {t['vs']}: gain {t['gain']} range {t['range']} -> "
                f"{'PASS' if t['passes'] else 'no'}"
                if t
                else ""
            )
        )
    for c in ("A", "B"):
        print(f"  {c} tail: {json.dumps(res['candidates'][c]['tail'])}")
    chosen = res["chosen"]
    print(f"Chosen: {chosen} (B if none of C-H passed)")

    names = sorted({"A", "B", chosen})
    seen = Holdout(gl.SEEN_START)
    seen.unlock(
        "goal-lab: 2025/26 reported as 'seen' after selection (pre-registered in "
        "docs/player_props.md section 8); it chooses nothing"
    )
    slo, shi = gl.SEEN_START, pd.Timestamp("2026-07-01", tz="UTC")
    sp = pd.DataFrame({n: gl.predict(feats, n, slo, shi, seen, _gbm_cfg(tuned, n)) for n in names})
    out["seen_2526"] = gl.score_window(sp, feats, names)
    out["seen_2526"]["holdout_log"] = seen.events
    for c, d in out["seen_2526"]["candidates"].items():
        print(
            f"  seen 2025/26 {c}: log loss {d['log_loss']}, AUC {d['auc']}, "
            f"tail {json.dumps(d['tail'])}"
        )
    print("  seen 2025/26 vs B: " + json.dumps(out["seen_2526"]["vs_ref"], default=str))

    if args.pilot_log and Path(args.pilot_log).exists():
        out["pilot"] = _goal_pilot_describe(
            gl.parse_pilot_log(Path(args.pilot_log).read_text()), apps, feats, sp, names
        )
        print("Pilot (5 matches, descriptive only): " + json.dumps(out["pilot"]["summary"]))

    if args.open_forward:
        fh = gl.forward_holdout(args.open_forward)
        flo = gl.FORWARD_START
        fhi = feats["kickoff"].max() + pd.Timedelta(days=1)
        fp = pd.DataFrame(
            {n: gl.predict(feats, n, flo, fhi, fh, _gbm_cfg(tuned, n)) for n in names}
        )
        out["forward"] = gl.score_window(fp, feats, names)
        out["forward"]["matches"] = int(feats.loc[fp.index, "match_id"].nunique())
        out["forward"]["holdout_log"] = fh.events
        print("Forward check: " + json.dumps(out["forward"], default=str)[:2000])
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(_clean(out), indent=1, default=str))


def _gbm_cfg(tuned: dict, name: str):
    """H's settings for a later window: the ones chosen for the last development season."""
    if name != "H" or not tuned:
        return None
    return tuned[max(tuned)]["config"]


def _goal_pilot_describe(prices, apps, feats, sp, names) -> dict:
    """Model vs the best price per starter in the round-5 pilot matches (descriptive)."""
    from soccer_stats.odds_feed import _team
    from soccer_stats.player_data import match_in_fixture

    known = set(apps["team"])
    rows = []
    for (home, away), g in prices.groupby(["home", "away"]):
        h, a = _team(home, known), _team(away, known)
        game = feats[(feats["team"] == h) & (feats["opponent"] == a) & (feats["season"] == "2526")]
        if game.empty:
            continue
        rosters = {
            t: dict(
                game[game["team"] == t][["player_id", "player"]].itertuples(index=False, name=None)
            )
            for t in (h, a)
        }
        found, _ = match_in_fixture(g["player"].unique(), rosters)
        best = g.groupby("player")["yes"].max()
        for name, odds in best.items():
            if name not in found:
                continue
            pid, team = found[name]
            r = game[(game["player_id"] == pid)]
            if r.empty or not bool(r["started"].iloc[0]):
                continue
            i = r.index[0]
            row = {
                "match": f"{h} v {a}",
                "player": name,
                "best_odds": float(odds),
                "implied": round(1 / float(odds), 4),
                "scored": int(r["goals"].iloc[0] > 0),
            }
            for n in names:
                v = sp[n].get(i, np.nan) if n in sp else np.nan
                row[f"p_{n}"] = None if pd.isna(v) else round(float(v), 4)
            rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return {"summary": {"starters": 0}, "rows": []}
    summary = {
        "starters": len(df),
        "matches": int(df["match"].nunique()),
        "implied": round(float(df["implied"].mean()), 4),
        "scored": round(float(df["scored"].mean()), 4),
    }
    for n in names:
        c = f"p_{n}"
        d = df.dropna(subset=[c])
        ev = d[c] * d["best_odds"] - 1
        summary[n] = {
            "mean_p": round(float(d[c].mean()), 4),
            "model_above_price": int((d[c] > d["implied"]).sum()),
            "of_those_scored": int(d.loc[d[c] > d["implied"], "scored"].sum()),
            "edge_12pct": int((ev >= 0.12).sum()),
            "edge_12pct_scored": int(d.loc[ev >= 0.12, "scored"].sum()),
        }
    return {"summary": summary, "rows": df.to_dict("records")}


def _goal_predictions(league: str, holdout_reason: str):
    """Understat appearances (2022/23-2025/26) and the goalscorer model's walk-forward
    chances for 2025/26 with the lineup known (the pilot is priced at the close)."""
    from soccer_stats import player_backtest as pb
    from soccer_stats import player_goals as pg
    from soccer_stats.factors import build_features
    from soccer_stats.player_data import load_appearances

    years = range(2022, 2026)
    apps, missing = load_appearances(league, years)
    if apps.empty:
        raise SystemExit("No Understat appearances loaded: nothing to match the prices to.")
    matches, err = with_xg(load_matches([league], years))
    preds = backtest.walk_forward(
        matches,
        start="2023-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if not err else 0.0),
    )
    feats = pg.goal_features(build_features(apps, pb.match_info(preds, apps)))
    feats = feats[feats["kickoff"] < pd.Timestamp("2026-07-01", tz="UTC")]
    hold = pg.stage1_holdout(holdout_reason)
    gp = pg.walk_forward(feats, "2025-07-01", lineup_known=True, holdout=hold)
    print(f"{len(apps)} appearances ({missing} match files missing); {len(gp)} 2025/26 chances")
    return apps, gp


def cmd_goalscorer_pilot(args: argparse.Namespace) -> None:
    """Stage 0 (live) and the 5-match pilot for anytime goalscorer odds, within --cap."""
    import json

    from soccer_stats import player_goal_odds as go
    from soccer_stats.publish import _clean

    events = go.pilot_events(args.league)
    print(f"Pilot matches (cached FanDuel closes): {len(events)}")
    for e in events:
        print(f"  {e['kickoff']:%Y-%m-%d %H:%M} {e['home']} v {e['away']} (close {e['requested']})")
    if len(events) < 5:
        print("WARNING: fewer than the 5 pre-registered pilot matches are cached.")
    # Free part first, so a broken pipeline spends nothing.
    apps, gp = _goal_predictions(
        args.league,
        "goalscorer pilot: the 2025/26 stage-1 chances (pre-registered in "
        "docs/player_props.md, reported on every weekly run) for the 5 pilot matches",
    )
    res = go.run_calls(args.cap, events, args.league)
    print(f"\n=== Calls (cap {args.cap}) ===")
    for line in res["log"]:
        print("  " + line)
    print(f"Credits spent: {res['spent']} (left on the shared key: {res['left']})")
    print(f"Books found live: {res.get('books_found_live')}")
    print(f"Books requested for the pilot: {res.get('books_requested')}")
    live_books = [r for x in res["live"] for r in go.book_summary(x["prices"])]
    for x in res["live"]:
        print(f"  live {x['event']}: {go.book_summary(x['prices']) or 'no goalscorer prices'}")
    allp = (
        pd.concat([x["prices"] for x in res["pilot"]], ignore_index=True)
        if res["pilot"]
        else pd.DataFrame(columns=["event_id", "book", "player", "yes", "no"])
    )
    # Paid data first, to the log and the JSON, before anything that could fail.
    for x in res["pilot"]:
        print(f"  pilot {x['home']} v {x['away']}: {len(x['prices'])} prices")
        for r in x["prices"].itertuples(index=False):
            print(f"    {r.book:>14} {r.player:<28} yes {r.yes} no {r.no}")
    raw = {
        "spent": res["spent"],
        "left": res["left"],
        "log": res["log"],
        "books_found_live": res.get("books_found_live"),
        "books_requested": res.get("books_requested"),
        "live_prices": [
            {"event": x["event"], "prices": x["prices"].to_dict("records")} for x in res["live"]
        ],
        "pilot_prices": [
            {
                "event": f"{x['home']} v {x['away']}",
                "kickoff": str(x["kickoff"]),
                "prices": x["prices"].to_dict("records"),
            }
            for x in res["pilot"]
        ],
    }
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).with_name("goalscorer_prices.json").write_text(
            json.dumps(_clean(raw), indent=1)
        )
    books = go.book_summary(allp)
    print("Pilot books: " + json.dumps(books))
    known = set(apps["team"])
    m = go.match_players(res["pilot"], apps, gp, known)
    ev = go.evaluate(m)
    print("Pilot measures (starters, best price): " + json.dumps(ev, default=str))
    v = go.verdict(books, ev)
    print("Verdict (pre-registered rules, docs/player_props.md §5): " + json.dumps(v))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(
                _clean(
                    {
                        "spent": res["spent"],
                        "left": res["left"],
                        "log": res["log"],
                        "books_found_live": res.get("books_found_live"),
                        "books_requested": res.get("books_requested"),
                        "live_books": live_books,
                        "live_prices": [
                            {"event": x["event"], "prices": x["prices"].to_dict("records")}
                            for x in res["live"]
                        ],
                        "pilot_books": books,
                        "pilot_events": [{k: str(v) for k, v in e.items()} for e in events],
                        "measures": ev,
                        "verdict": v,
                        "lines": m.astype(str).to_dict("records") if not m.empty else [],
                    }
                ),
                indent=1,
            )
        )


def cmd_player_lab(args: argparse.Namespace) -> None:
    """Player shot lines through the lab's metrics, development seasons only."""
    import json

    from soccer_stats import player_lab as pl

    res = pl.run(pl.load_lines(args.lines))
    if args.players_json:
        res["backtest_dev"] = pl.backtest_dev_summary(
            json.loads(Path(args.players_json).read_text())
        )
    for name, st in res["strategies"].items():
        for mk, x in st.items() if isinstance(st, dict) and "fair_market" in st else []:
            b, bl = x.get("bets") or {}, x.get("blend") or {}
            print(
                f"{name:>13} {mk:>19}: {x.get('rows')} lines, log loss {x.get('log_loss')} vs "
                f"{x.get('market_log_loss')}, blend c {bl.get('c')} {bl.get('c_range')}, "
                f"{b.get('bets')} bets, CLV {b.get('clv')} {b.get('clv_range')}, "
                f"ROI {b.get('roi')} {b.get('roi_range')}, passes {x.get('passes')}"
            )
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=str))


def cmd_player_segments(args: argparse.Namespace) -> None:
    import json

    from soccer_stats import player_segments as ps

    lines = ps.load_lines(args.lines)
    a, b = args.train, args.test
    results = []
    for train, test in ((a, b), (b, a)):  # the reverse split is the robustness check
        res = ps.out_of_sample(lines, train, test, min_bets=args.min_bets)
        print(ps.format_report(res) + "\n")
        results.append(res)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1, default=str))


def _fmt(v, kind="num"):
    if v is None:
        return "–"
    if kind == "pct":
        return f"{v:+.1%}"
    if kind == "usd":
        return f"${v:+,.2f}"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def _print_report(out: dict) -> None:
    s = out["summary"]
    print(f"\n=== DraftKings backtest, {out['seasons']}, edge >= {out['threshold']:.0%} ===")
    print(f"Trades: {s.get('trades', 0)} ({s.get('settled', 0)} settled, {s.get('void', 0)} void)")
    if s.get("settled"):
        ci = s.get("roi_ci95")
        print(
            f"Staked ${s['staked']:,.0f}, profit {_fmt(s['profit'], 'usd')}, ROI "
            f"{_fmt(s['roi'], 'pct')} (se {s['roi_se'] or 0:.1%}"
            + (f", 95% interval {ci[0]:+.1%} to {ci[1]:+.1%})" if ci else ")")
        )
        print(
            f"Win rate {s['win_rate']:.1%} vs break-even {s['breakeven']:.1%}; "
            f"average claimed edge {s['avg_edge']:+.1%}"
        )
        print(
            f"Closing line value: DraftKings {_fmt(s.get('clv_dk'), 'pct')} "
            f"(beat close {s.get('beat_close_dk') or 0:.0%}), "
            f"Pinnacle {_fmt(s.get('clv_pinnacle'), 'pct')}"
        )
        print(f"Max drawdown ${s['max_drawdown']:,.2f}")
        c = out["summary_capped"]
        if c.get("settled"):
            print(f"With a 6.0 odds cap: {c['trades']} trades, ROI {_fmt(c['roi'], 'pct')}")
    ll = out.get("log_loss") or {}
    if ll:
        print(
            f"Log loss on {ll['matches']} matches: model {ll['model']:.4f}, "
            f"DraftKings {ll['draftkings']:.4f}"
        )
    for key, rows in out["breakdowns"].items():
        if not rows:
            continue
        print(f"\nBy {key.replace('_', ' ')}:")
        for r in rows:
            print(
                f"  {r['group']:>12}: {r['trades']:>4} trades, ROI {_fmt(r.get('roi'), 'pct')}, "
                f"CLV {_fmt(r.get('clv_dk'), 'pct')}"
            )
    print("\nThreshold sweep (trades / ROI / CLV vs DraftKings close):")
    for r in out["sweep"]:
        cap = f"cap {r['max_odds']:g}" if r["max_odds"] else "no cap"
        print(
            f"  {r['threshold']:>4.0%} {cap:>7}: {r['trades']:>4} / {_fmt(r.get('roi'), 'pct')} "
            f"/ {_fmt(r.get('clv_dk'), 'pct')}"
        )


def _pct(v) -> str:
    return "–" if v is None else f"{v:.1%}"


def _print_strategies(out: dict) -> None:
    print(f"\nLooks with a DraftKings over/under 2.5 price: {_pct(out.get('totals_priced'))}")
    ll = out.get("log_loss") or {}
    for key, name in (("h2h", "Home/draw/away"), ("totals", "Over/under 2.5")):
        x = ll if key == "h2h" else ll.get("totals") or {}
        if x.get("blend_matches"):
            print(
                f"{name} log loss on {x['blend_matches']} matches: model "
                f"{x['model_on_blend']:.4f}, blend {x['blend']:.4f}, "
                f"DraftKings close {x['draftkings_on_blend']:.4f}"
            )
    blend = out.get("blend") or {}
    for g, fits in blend.get("fits", {}).items():
        if fits:
            print(f"Blend fits ({g}): {len(fits)}, latest {fits[-1]}")
    for g, f in blend.get("live", {}).items():
        print(f"Live blend ({g}): {f}")
    for name, st in out.get("strategies", {}).items():
        print(f"\nStrategy {name}: {st['label']}")
        print("  edge   cap   bets     ROI        95% range  CLV DK  avg p    won")
        for r in st["sweep"]:
            ci = r.get("roi_ci95")
            rng = f"{ci[0]:+.0%} to {ci[1]:+.0%}" if ci else "-"
            cap = f"{r['max_odds']:g}" if r["max_odds"] else "none"
            print(
                f"  {r['threshold']:>4.0%} {cap:>5} {r['trades']:>5} "
                f"{_fmt(r.get('roi'), 'pct'):>7} {rng:>16} {_fmt(r.get('clv_dk'), 'pct'):>7} "
                f"{_pct(r.get('avg_p')):>6} {_pct(r.get('win_rate')):>6}"
            )


def cmd_match_markets(args: argparse.Namespace) -> None:
    """Asian handicap, O/U 2.5 and 1X2 against Pinnacle (football-data), out of sample."""
    import json

    from soccer_stats import match_markets as mm
    from soccer_stats.data import download, season_code
    from soccer_stats.publish import _clean

    years = _years(args.seasons)
    matches = load_matches([args.league], range(years[0] - args.burn_in, years[-1] + 1))
    matches, err = with_xg(matches)
    has_xg = matches["home_xg"].notna().any()
    print(err or f"xG attached to {matches['home_xg'].notna().mean():.0%} of matches")
    weight = args.xg_weight if has_xg else 0.0
    preds = backtest.walk_forward(
        matches,
        start=f"{years[0]}-07-01",
        model_factory=functools.partial(DixonColes, xg_weight=weight),
        keep_matrix=True,
    )
    print(f"{len(preds)} out-of-sample predictions (xg_weight {weight})")
    frames = []
    for y in years:
        raw = pd.read_csv(download(args.league, y), encoding="latin-1", on_bad_lines="skip")
        frames.append(mm.load_prices(raw, season=season_code(y)))
    joined = mm.join(preds, pd.concat(frames, ignore_index=True))
    if has_xg:
        feats = mm.xg_features(matches)
        feats["date"] = pd.to_datetime(feats["date"]).dt.normalize()
        joined = joined.merge(feats, on=["date", "home", "away"], how="left")
    print(f"{len(joined)} predictions matched to football-data prices")
    out = mm.run(joined)
    print("\nPrice check (rows blanked as implausible):")
    print(pd.DataFrame(out["price_check"]).round(4).to_string(index=False))
    for g in mm.GROUPS:
        _print_market(g, out[g])
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(_clean(out), indent=1, default=str))
        print(f"\nJSON written to {args.json}")


def _rng(ci) -> str:
    return f"{ci[0]:+.1%} to {ci[1]:+.1%}" if ci else "-"


def _rng4(ci) -> str:
    return f"{ci[0]:+.4f} to {ci[1]:+.4f}" if ci else "-"


def _print_market(group: str, r: dict) -> None:
    print(f"\n==================== {group} ====================")
    print(f"Matches priced: close {r['matches_close']}, early {r['matches_early']}")
    if "ah_line_moved" in r:
        print(
            f"AH line moved early -> close: {r['ah_line_moved']:.1%}; "
            f"matches with a push or half stake: {r['ah_push_share']:.1%}"
        )
    for key in ("log_loss", "log_loss_early_pinnacle"):
        if r.get(key):
            print(
                f"{key}: "
                + ", ".join(
                    f"{k} {v:.4f}" if isinstance(v, float) else f"{k} {v}"
                    for k, v in r[key].items()
                )
            )
    for k, f in (r.get("fits") or {}).items():
        print(f"{k}: {f['refits']} refits, model weight c range {f['c_range']}, last {f['last']}")
    print(f"Fit on every match (live): {r.get('live')}")
    for k, sig in (r.get("signals") or {}).items():
        d = sig["loss_diff_vs_blend"]
        print(
            f"{k} {sig['features']}: log loss vs blend "
            f"{d.get('diff', float('nan')):+.4f} (95% {_rng4(d.get('ci95'))}) "
            f"on {d['matches']} matches (negative = signal helps)"
        )
    print("source          strategy   edge  bets     ROI        95% range      CLV       CLV range")
    for s in r.get("sweep", []):
        if not s["bets"]:
            print(f"{s['source']:<15} {s['strategy']:<9} {s['threshold']:>4.0%} {0:>5}")
            continue
        clv = f"{s['clv']:+.1%}" if s.get("clv") is not None else "-"
        print(
            f"{s['source']:<15} {s['strategy']:<9} {s['threshold']:>4.0%} {s['bets']:>5} "
            f"{s['roi']:>+7.1%} {_rng(s['roi_ci95']):>18} {clv:>7} {_rng(s.get('clv_ci95')):>18}"
        )
    if r.get("by_season"):
        print("By season, Pinnacle early, 12% edge:")
        for s in r["by_season"]:
            clv = f"{s['clv']:+.1%}" if s.get("clv") is not None else "-"
            print(f"  {s['strategy']:<9} {s['season']} {s['bets']:>4} {s['roi']:>+7.1%} CLV {clv}")


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

    pub = sub.add_parser("publish", help="build the phone web app into a folder")
    pub.add_argument("--out", default="_site")
    pub.add_argument("--league", default="E0")
    pub.set_defaults(func=cmd_publish)

    log = sub.add_parser("log-news", help="append team-news changes to a history folder")
    log.add_argument("--snapshot", default="_site/news_snapshot.json")
    log.add_argument("--log-dir", required=True)
    log.set_defaults(func=cmd_log_news)

    lo = sub.add_parser("log-odds", help="append the build's DraftKings prices to odds_log/")
    lo.add_argument("--site", default="_site", help="folder written by publish")
    lo.add_argument("--log-dir", required=True, help="data-log checkout")
    lo.add_argument("--league", default="E0")
    lo.set_defaults(func=cmd_log_odds)

    ep = sub.add_parser("espn-probe", help="ESPN league slugs, team names and summary shape")
    ep.set_defaults(func=cmd_espn_probe)

    ltn = sub.add_parser("log-team-news", help="append the build's ESPN team news to team_news/")
    ltn.add_argument("--site", default="_site", help="folder written by publish")
    ltn.add_argument("--log-dir", required=True, help="data-log checkout")
    ltn.set_defaults(func=cmd_log_team_news)

    ltt = sub.add_parser("log-team-totals", help="append team-total rows and calls to odds_log/")
    ltt.add_argument("--pending", required=True, help="folder publish wrote rows/calls to")
    ltt.add_argument("--log-dir", required=True, help="data-log checkout")
    ltt.set_defaults(func=cmd_log_team_totals)

    ttr = sub.add_parser("team-totals-report", help="team-total CLV vs FanDuel's close (no key)")
    ttr.add_argument("--log-dir", required=True, help="data-log checkout")
    ttr.add_argument("--min-matches", type=int, default=50)
    ttr.add_argument("--after", default="", help="confirmation: only kickoffs after this time")
    ttr.add_argument(
        "--rule", default="", help="confirmation: the frozen rule, e.g. fanduel_anchor:0.05"
    )
    ttr.set_defaults(func=cmd_team_totals_report)

    ec = sub.add_parser(
        "estimate-credits", help="Odds API credits per league for a month (refresh rules)"
    )
    ec.add_argument("--month", default=pd.Timestamp.now(tz="UTC").strftime("%Y-%m"))
    ec.set_defaults(func=cmd_estimate_credits)

    ett = sub.add_parser(
        "estimate-team-totals", help="credits to log team-total prices (estimate, no key)"
    )
    ett.add_argument("--months", default="2026-10,2026-11,2026-12", help="YYYY-MM,YYYY-MM")
    ett.add_argument("--since", default="2026-10-09", help="start for the 4/8-week counts")
    ett.set_defaults(func=cmd_estimate_team_totals)

    pap = sub.add_parser("paper", help="update the paper-trade ledger and the app's portfolio")
    pap.add_argument("--site", default="_site", help="folder written by publish")
    pap.add_argument("--log-dir", help="data-log checkout holding paper_trades/")
    pap.add_argument("--league", default="E0")
    pap.set_defaults(func=cmd_paper)

    bf = sub.add_parser("backfill-odds", help="download historical DraftKings odds (paid plan)")
    bf.add_argument("--league", default="E0")
    bf.add_argument("--seasons", default="2025", help="start years, e.g. 2023-2025")
    bf.add_argument(
        "--looks",
        type=float,
        nargs="+",
        default=[48.0, 3.0],
        help="look times in hours before kickoff",
    )
    bf.add_argument("--max-credits", type=int, default=0, help="stop before spending more")
    bf.add_argument(
        "--keep-credits",
        type=int,
        default=1500,
        help="never let credits left fall below this (live refreshes)",
    )
    bf.add_argument("--dry-run", action="store_true", help="print the plan; no API calls")
    bf.set_defaults(func=cmd_backfill)

    dk = sub.add_parser("backtest-dk", help="replay the trade rule on historical DraftKings odds")
    dk.add_argument("--league", default="E0")
    dk.add_argument("--seasons", default="2023-2025", help="seasons to predict (start years)")
    dk.add_argument("--burn-in", type=int, default=2, help="seasons of training data before")
    dk.add_argument("--looks", type=float, nargs="+", default=[48.0, 3.0])
    dk.add_argument("--threshold", type=float, default=0.12)
    dk.add_argument("--max-odds", type=float, default=None)
    dk.add_argument("--xg-weight", type=float, default=0.7)
    dk.add_argument(
        "--blend-seasons",
        type=int,
        default=3,
        help="seasons of Pinnacle closing odds before the first one to fit the blend on",
    )
    dk.add_argument("--out", help="CSV path, one row per trade")
    dk.add_argument("--json", help="path for the app's backtest data")
    dk.add_argument("--log-dir", help="data-log checkout: writes backtest/<league>_dk.json")
    dk.set_defaults(func=cmd_backtest_dk)

    bp = sub.add_parser("backtest-players", help="walk-forward test of the player shot model")
    bp.add_argument("--league", default="E0")
    bp.add_argument(
        "--seasons",
        default="2023-now",
        help="seasons to predict (start years; 'now' = the season in progress)",
    )
    bp.add_argument("--burn-in", type=int, default=1, help="seasons of data before")
    bp.add_argument("--out", help="CSV of priced player trades (stage 2)")
    bp.add_argument("--json", help="path for the results (the gate the app reads)")
    bp.add_argument("--log-dir", help="data-log checkout: writes backtest/<league>_players.json")
    bp.add_argument(
        "--goal-forward-reason",
        default="",
        help="opens the goalscorer forward window (from 2026-10-10) in E0_goals.json; "
        "blank keeps it locked (docs/player_props.md §10b)",
    )
    bp.set_defaults(func=cmd_backtest_players)

    bfp = sub.add_parser("backfill-player-odds", help="historical FanDuel player shot odds")
    bfp.add_argument("--league", default="E0")
    bfp.add_argument("--seasons", default="2025")
    bfp.add_argument("--max-credits", type=int, default=0)
    bfp.add_argument("--keep-credits", type=int, default=1500)
    bfp.add_argument("--dry-run", action="store_true")
    bfp.set_defaults(func=cmd_backfill_players)

    poc = sub.add_parser("player-odds-check", help="diagnose player-prop coverage (~40-80 credits)")
    poc.add_argument("--league", default="E0")
    poc.set_defaults(func=cmd_player_odds_check)

    glg = sub.add_parser("goal-league", help="one league's goalscorer rows (round 8)")
    glg.add_argument("--league", required=True)
    glg.add_argument("--rows", help="CSV(.gz) of scored rows")
    glg.add_argument("--open-forward", default="", help="reason to open the forward window")
    glg.add_argument("--json", help="write the results as JSON")
    glg.set_defaults(func=cmd_goal_league)

    gpl = sub.add_parser("goal-pool", help="pool the leagues' goalscorer rows (round 8)")
    gpl.add_argument("--rows-dir", required=True)
    gpl.add_argument("--json", help="write the results as JSON")
    gpl.set_defaults(func=cmd_goal_pool)

    gl_ = sub.add_parser("goal-lab", help="goalscorer model improvements (pre-registered)")
    gl_.add_argument("--league", default="E0")
    gl_.add_argument("--pilot-log", help="the round-5 pilot's job log, for the description")
    gl_.add_argument(
        "--open-forward", default="", help="reason to open the locked forward window (once)"
    )
    gl_.add_argument("--json", help="write the results as JSON")
    gl_.set_defaults(func=cmd_goal_lab)

    gp_ = sub.add_parser(
        "goalscorer-pilot", help="anytime goalscorer odds: live probe + 5-match pilot (capped)"
    )
    gp_.add_argument("--league", default="E0")
    gp_.add_argument("--cap", type=int, default=0, help="most credits to spend (0 = none)")
    gp_.add_argument("--json", help="write the results as JSON")
    gp_.set_defaults(func=cmd_goalscorer_pilot)

    pl_ = sub.add_parser("player-lab", help="player shot lines through the lab's metrics")
    pl_.add_argument("--lines", required=True, help="E0_player_lines.csv(.gz) from data-log")
    pl_.add_argument("--players-json", help="E0_players.json, for the backtest's own numbers")
    pl_.add_argument("--json", help="write the results as JSON")
    pl_.set_defaults(func=cmd_player_lab)

    seg = sub.add_parser(
        "player-segments", help="out-of-sample segment search on the priced player lines"
    )
    seg.add_argument("--lines", required=True, help="E0_player_lines.csv(.gz) from data-log")
    seg.add_argument("--train", type=int, default=2024, help="season to pick on (2024 = 24/25)")
    seg.add_argument("--test", type=int, default=2025, help="season to report on")
    seg.add_argument("--min-bets", type=int, default=100)
    seg.add_argument("--out", help="write the results as JSON")
    seg.set_defaults(func=cmd_player_segments)

    mk = sub.add_parser(
        "match-markets", help="Asian handicap, O/U 2.5 and 1X2 vs Pinnacle (football-data)"
    )
    mk.add_argument("--league", default="E0")
    mk.add_argument("--seasons", default="2017-2025", help="seasons to predict (start years)")
    mk.add_argument("--burn-in", type=int, default=2, help="seasons of training data before")
    mk.add_argument("--xg-weight", type=float, default=0.7)
    mk.add_argument("--json", help="write the results as JSON")
    mk.set_defaults(func=cmd_match_markets)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
