"""Command line entry point.

soccer-stats backtest --league E0 --seasons 2019-2024
soccer-stats backtest --league E0 --seasons 2019-2024 --xg-weight 0 0.5 0.7 1
soccer-stats publish --out _site
soccer-stats paper --site _site --log-dir ../log
soccer-stats backfill-odds --seasons 2025 --dry-run
soccer-stats backtest-dk --seasons 2023-2025 --out dk_trades.csv
"""

from __future__ import annotations

import argparse
import functools
import shutil
from pathlib import Path

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


def cmd_paper(args: argparse.Namespace) -> None:
    import json

    from soccer_stats import paper
    from soccer_stats.data import current_season
    from soccer_stats.publish import _clean

    site = Path(args.site)
    data = json.loads((site / "data.json").read_text())
    season = current_season()
    try:
        results = load_matches([args.league], [season - 1, season])
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


def cmd_backtest_players(args: argparse.Namespace) -> None:
    import json

    from soccer_stats import player_backtest as pb
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
        out["trades"] = (
            trades.sort_values("kickoff", ascending=False).to_dict("records")
            if not trades.empty
            else []
        )
        if args.out and not trades.empty:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            trades.to_csv(args.out, index=False)

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

    seg = sub.add_parser(
        "player-segments", help="out-of-sample segment search on the priced player lines"
    )
    seg.add_argument("--lines", required=True, help="E0_player_lines.csv(.gz) from data-log")
    seg.add_argument("--train", type=int, default=2024, help="season to pick on (2024 = 24/25)")
    seg.add_argument("--test", type=int, default=2025, help="season to report on")
    seg.add_argument("--min-bets", type=int, default=100)
    seg.add_argument("--out", help="write the results as JSON")
    seg.set_defaults(func=cmd_player_segments)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
