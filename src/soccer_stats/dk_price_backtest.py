"""Both live match rules replayed at DraftKings' own historical prices (lead, 28 Oct; 0 credits).

The method was written into docs/leagues.md ("Backtests at DraftKings' prices: method")
before this ran. In short:

* Candidates: every match of a league's seasons that the Pinnacle backtests score
  (lean_backtest.coverage) and the bought DraftKings history covers, where DraftKings priced
  the full 1X2 in the 48 h look snapshot (quote updated at most STALE_HOURS before it).
* Entry: DraftKings' quoted price at the look (break-even 1/decimal). Model 12% =
  trades.best_pick at 12% on the raw walk-forward model chance (edge12_filters' baseline);
  Blend Lean = trades.lean_pick on the blend (lean_backtest.walk_forward: fits on Pinnacle's
  close beside the model, every 28 days on earlier matches, the block set by the look time)
  fed DraftKings' Shin-fair look price, reported inside the live odds range and at every
  price.
* CLV: entry x Pinnacle's Shin-fair close from the same close snapshot - 1 (no fallback; a
  bet without it counts in clv_missing), and against DraftKings' own close (descriptive).
* The same rules on the same matches at football-data's Pinnacle early price, beside it.

App-ready (lead, 28 Oct): two data-log files in exactly the shape of lean_backtest.json and
edge12_backtest.json, built by their own `build` (bands, calibration, sizing, `all`, bets
newest first): backtest/lean_backtest_dk.json and backtest/edge12_backtest_dk.json. Each league
entry has `kind: "draftkings"`, its `coverage` rows say how much DraftKings priced at the look
(`early_share`) and Pinnacle at the close in the same body (`close_share`), and it carries
`clv_dk` and `pinnacle_same_matches` beside the usual blocks. The top level adds `book`,
`verdict` and `coverage_note`. Every run rebuilds both files from every league whose bodies
are in the cache, so a league's numbers update when its bodies land.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import edge12_backtest as eb
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import edge12_filters as ef
from soccer_stats.odds import devig_shin
from soccer_stats.odds_feed import BOOKMAKER, _team, book_prices
from soccer_stats.odds_history import history_dir

H2H = mc.H2H
LOOK = pd.Timedelta(hours=48)
STALE_HOURS = 6.0
SHARP = "pinnacle"
LEAN_FILE = "lean_backtest_dk.json"
EDGE12_FILE = "edge12_backtest_dk.json"
KIND = "draftkings"  # each league entry's kind (the Pinnacle files say "early" / "close")
NOTE = (
    "Blend Lean and Model 12% exactly as live, entered at DraftKings' own price 48 hours "
    "before kickoff from the bought ten-book history, CLV against Pinnacle's margin-free "
    "close in the same snapshot. Beside it, the same rules on the same matches at "
    "football-data's Pinnacle early price. Method fixed in docs/leagues.md before the run."
)


def unmatched_names(league: str, known: set[str], raw_dir: Path = RAW_DIR) -> dict[str, int]:
    """Feed names in the cached bodies that don't land on a football-data name in `known`,
    with how many event-snapshots they appear in: matches with such a team can't be
    candidates (a TEAM_NAMES gap, not a DraftKings one)."""
    out: dict[str, int] = {}
    for path in sorted(history_dir(league, raw_dir).glob("*.json")):
        for ev in json.loads(path.read_text()).get("data") or []:
            for n in (ev.get("home_team"), ev.get("away_team")):
                if n and _team(n, known) not in known:
                    out[n] = out.get(n, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def load_books(league: str, known: set[str] | None = None, raw_dir: Path = RAW_DIR):
    """Every cached snapshot as rows: at (requested), snapshot_ts, kickoff, home, away and
    `books` = {book: {home, draw, away, last_update}} (football-data names)."""
    rows = []
    for path in sorted(history_dir(league, raw_dir).glob("*.json")):
        snap = json.loads(path.read_text())
        at = pd.Timestamp(snap["requested"])
        ts = pd.Timestamp(snap.get("timestamp") or snap["requested"])
        for ev in snap.get("data") or []:
            books = book_prices(ev, known)
            if not books:
                continue
            rows.append(
                {
                    "at": at,
                    "snapshot_ts": ts,
                    "kickoff": pd.Timestamp(ev["commence_time"]).tz_convert("UTC"),
                    "home": _team(ev["home_team"], known),
                    "away": _team(ev["away_team"], known),
                    "books": books,
                }
            )
    cols = ["at", "snapshot_ts", "kickoff", "home", "away", "books"]
    return pd.DataFrame(rows, columns=cols)


def quote(snaps: pd.DataFrame, at: pd.Timestamp, book: str) -> dict | None:
    """`book`'s 1X2 in the latest snapshot taken at or before `at` that holds it, when the
    quote was updated at most STALE_HOURS before `at`; None otherwise. `snaps` is one
    match's rows, sorted by snapshot_ts."""
    h = snaps[snaps["snapshot_ts"] <= at]
    for books in reversed(list(h["books"])):
        q = (books or {}).get(book)
        if not q:
            continue
        upd = q.get("last_update")
        if upd is not None and at - pd.Timestamp(upd) > pd.Timedelta(hours=STALE_HOURS):
            return None
        return {m: float(q[m]) for m in H2H}
    return None


def candidates(
    merged: pd.DataFrame, hist: pd.DataFrame, seasons: set[str]
) -> tuple[pd.DataFrame, dict]:
    """lean_backtest.target_rows' shape at DraftKings' prices: one row per predicted match in
    `seasons` whose DraftKings 1X2 was in the 48 h look. odds_<side> = DraftKings' look
    (quoted), mkt_<side> its Shin-fair price, close_<side> Pinnacle's close in the same close
    snapshot, dk_close_<side> DraftKings' close, kickoff, day (the look time). Plus
    `coverage` per season: matches, look, close (DraftKings) and pinnacle_close counts."""
    by_pair = {k: g.sort_values("snapshot_ts") for k, g in hist.groupby(["home", "away"])}
    rows, cov = [], {}
    pool = merged[merged["season"].astype(str).isin(seasons)] if len(merged) else merged
    for r in pool.to_dict("records"):
        season = str(r["season"])
        c = cov.setdefault(
            season, {"matches": 0, "look": 0, "close": 0, "pinnacle_close": 0, "candidates": 0}
        )
        c["matches"] += 1
        if any(pd.isna(r.get(f"p_{m}")) for m in H2H):
            continue
        g = by_pair.get((r["home"], r["away"]))
        if g is None:
            continue
        day = pd.Timestamp(r["date"]).date()
        near = g[(g["kickoff"].dt.date - day).abs() <= pd.Timedelta(days=1)]
        if near.empty:
            continue
        kickoff = near["kickoff"].mode().iloc[0]
        look_at, close_at = kickoff - LOOK, kickoff
        dk_look = quote(near, look_at, BOOKMAKER)
        dk_close = quote(near, close_at, BOOKMAKER)
        pin_close = quote(near, close_at, SHARP)
        c["look"] += dk_look is not None
        c["close"] += dk_close is not None
        c["pinnacle_close"] += pin_close is not None
        if dk_look is None:
            continue
        c["candidates"] += 1
        fair = devig_shin(np.array([dk_look[m] for m in H2H], dtype=float))
        row = {
            "date": kickoff.tz_convert(None),
            "kickoff": kickoff,
            "day": look_at.tz_convert(None),
            "season": season,
            "home": r["home"],
            "away": r["away"],
            "home_goals": r["home_goals"],
            "away_goals": r["away_goals"],
            "y": int(mc.outcome_index([r["home_goals"]], [r["away_goals"]], "h2h")[0]),
        }
        for i, m in enumerate(H2H):
            row[f"p_{m}"] = float(r[f"p_{m}"])
            row[f"odds_{m}"] = dk_look[m]
            row[f"mkt_{m}"] = float(fair[i])
            row[f"close_{m}"] = pin_close[m] if pin_close else np.nan
            row[f"dk_close_{m}"] = dk_close[m] if dk_close else np.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    if len(df):
        df = df.sort_values("date").reset_index(drop=True)
    return df, cov


def _with_dk_clv(b: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    """Each bet's CLV against DraftKings' own close (`clv_dk`, descriptive)."""
    out = b.copy()
    out["clv_dk"] = np.nan
    if not len(b) or target.empty:
        return out
    t = target.assign(_d=pd.to_datetime(target["date"]).dt.date.astype(str))
    t = t.drop_duplicates(["_d", "home", "away"]).set_index(["_d", "home", "away"])
    for i, r in out.iterrows():
        k = (str(r["date"]), r["home"], r["away"])
        if k in t.index:
            close = {m: t.at[k, f"dk_close_{m}"] for m in H2H}
            close = {m: float(v) for m, v in close.items() if pd.notna(v)}
            v = tr.clv(float(r["odds"]), r["market"], close)
            out.at[i, "clv_dk"] = np.nan if v is None else v
    return out


def clv_dk_block(b: pd.DataFrame) -> dict:
    """CLV against DraftKings' own close (descriptive) and the bets without Pinnacle's close
    in the same body (no judged CLV)."""
    col = b["clv_dk"] if "clv_dk" in b else pd.Series(dtype=float)
    dk = pd.to_numeric(col, errors="coerce").dropna().to_numpy(dtype=float)
    pin = pd.to_numeric(b["clv"], errors="coerce") if len(b) else pd.Series(dtype=float)
    return {
        "clv": float(dk.mean()) if len(dk) else None,
        "clv_ci95": lb._boot_mean(dk, seed=2),
        "bets": len(dk),
        "pinnacle_close_missing": int(pin.isna().sum()) if len(b) else 0,
        "note": "CLV against DraftKings' own close (descriptive); the judged CLV is against "
        "Pinnacle's close in the same snapshot",
    }


def verdict(m: dict) -> dict:
    """The pass rule fixed in the method: the CLV range vs Pinnacle's close wholly above 0."""
    ci = (m or {}).get("clv_ci95")
    return {
        "passes": bool(ci and ci[0] > 0),
        "clv": (m or {}).get("clv"),
        "clv_ci95": ci,
        "bets": (m or {}).get("bets", 0),
        "rule": "pooled CLV vs Pinnacle's close in the same snapshot: 95% range above 0",
    }


def coverage_rows(dk_cov: dict, seasons: set[str]) -> list[dict]:
    """lean_backtest.coverage's shape: early_share = DraftKings priced at the 48 h look,
    close_share = Pinnacle's close in the same body, `included` = scored here."""
    out = []
    for season, c in sorted(dk_cov.items()):
        n = c["matches"]
        out.append(
            {
                "season": season,
                "label": lb.season_label(season),
                "matches": n,
                "early": c["look"],
                "close": c["pinnacle_close"],
                "dk_close": c["close"],
                "both": c["candidates"],
                "early_share": round(c["look"] / n, 4) if n else 0.0,
                "close_share": round(c["pinnacle_close"] / n, 4) if n else 0.0,
                "included": season in seasons,
            }
        )
    return out


def league_reports(league: str, preds: pd.DataFrame, pins: pd.DataFrame, hist: pd.DataFrame):
    """One league at DraftKings' prices, as lean_backtest.league_report and
    edge12_backtest.league_report shape them: (lean report, lean bets, edge12 report, edge12
    bets). Both reports add clv_dk and pinnacle_same_matches."""
    merged = lb.with_pinnacle(preds, pins)
    scored_by_pin = {c["season"] for c in lb.coverage(pins) if c["included"]}
    bought = {lb_season(k) for k in hist["kickoff"]} if len(hist) else set()
    seasons = scored_by_pin & bought
    target, dk_cov = candidates(merged, hist, seasons)
    train = mc.training_rows(merged, "h2h") if not merged.empty else merged
    cov = coverage_rows(dk_cov, seasons)
    base = {
        "kind": KIND,
        "coverage": cov,
        "seasons_included": [c["label"] for c in cov if c["included"]],
        "seasons_excluded": [c["label"] for c in cov if not c["included"]],
    }
    if target.empty:
        scored, fits = target, []
        lean_b, e12_b = lb.bets(pd.DataFrame(), league), eb.empty_bets(league)
    else:
        scored, fits = lb.walk_forward(train, target)
        lean_b = _with_dk_clv(lb.bets(scored, league), target)
        e12_b = _with_dk_clv(
            eb.with_blend(ef.bets(target, league, "baseline"), merged, target), target
        )
    # The same matches at Pinnacle's early price (football-data), the same rules.
    pin_t = lb.target_rows(merged)
    if len(pin_t) and len(target):
        keys = set(zip(target["season"].astype(str), target["home"], target["away"], strict=True))
        mask = [
            (str(s), h, a) in keys
            for s, h, a in zip(pin_t["season"], pin_t["home"], pin_t["away"], strict=True)
        ]
        pin_t = pin_t[mask]
        pin_scored, _ = lb.walk_forward(train, pin_t)
        pin_lean, pin_e12 = lb.bets(pin_scored, league), ef.bets(pin_t, league, "baseline")
    else:
        pin_t = pin_t.iloc[0:0]
        pin_lean, pin_e12 = lb.bets(pd.DataFrame(), league), eb.empty_bets(league)
    sig = [f["sigma"] for f in fits]
    lean_cal, lean_long = lb.calibration(scored, lean_b, lb.SERIES, "p_bet", league)
    lean_rep = {
        **base,
        "training_matches": len(train),
        "fits": len(fits),
        "first_fit": fits[0]["from"] if fits else None,
        "sigma": {"min": min(sig), "max": max(sig)} if sig else None,
        "candidates": int(scored["sigma"].notna().sum()) if len(scored) else 0,
        **lb.summary(lean_b),
        "calibration": lean_cal,
        "clv_dk": clv_dk_block(lean_b),
        "pinnacle_same_matches": {"candidates": int(len(pin_t)), **lb.summary(pin_lean)},
        lb.OUTCOMES: lean_long,
    }
    e12_cal, e12_long = lb.calibration(target, e12_b, eb.SERIES, "model_p", league)
    e12_rep = {
        **base,
        "candidates": int(len(target)),
        **eb.summary(e12_b),
        "calibration": e12_cal,
        "clv_dk": clv_dk_block(e12_b),
        "pinnacle_same_matches": {"candidates": int(len(pin_t)), **eb.summary(pin_e12)},
        lb.OUTCOMES: e12_long,
    }
    return lean_rep, lean_b, e12_rep, e12_b


def lb_season(kickoff: pd.Timestamp) -> str:
    """football-data's season code ('2425') for a kickoff."""
    return tr.season_label(pd.Timestamp(kickoff))


COVERAGE_NOTE = (
    "Coverage per league and season: early = DraftKings priced the match 48 hours before "
    "kickoff (only those are bet); close = Pinnacle's close was in the same snapshot (the "
    "judged CLV needs it). DraftKings history starts in 2020/21 (2021/22 for Ligue 1 and "
    "League Two); a season counts only where the Pinnacle backtests score it too."
)
PARTIAL_BELOW = 0.9  # a scored season under this share is named in the coverage note


def partial_coverage(leagues: dict) -> list[dict]:
    """Scored league-seasons where DraftKings priced under PARTIAL_BELOW of the matches
    (`priced`), or Pinnacle's close is missing on that share of the priced ones
    (`pinnacle`): {league, name, season, priced, pinnacle}. Read from each league
    report's coverage rows."""
    out = []
    for code, rep in leagues.items():
        for c in rep.get("coverage") or []:
            if not c.get("included") or not c.get("matches"):
                continue
            priced = c["early_share"]
            pin = c["both"] / c["early"] if c.get("early") else None
            if priced < PARTIAL_BELOW or (pin is not None and pin < PARTIAL_BELOW):
                out.append(
                    {
                        "league": code,
                        "name": rep.get("name") or code,
                        "season": c["label"],
                        "priced": round(priced, 4),
                        "pinnacle": None if pin is None else round(pin, 4),
                    }
                )
    return out


def coverage_note(leagues: dict) -> str:
    """COVERAGE_NOTE plus, by name, each partial season (partial_coverage)."""
    parts = []
    for p in partial_coverage(leagues):
        if p["priced"] == 0:
            what = "no DraftKings history bought yet"
        else:
            what = f"DraftKings priced {p['priced']:.0%} of matches"
        if p["pinnacle"] is not None and p["pinnacle"] < PARTIAL_BELOW:
            what += f", Pinnacle's close on {p['pinnacle']:.0%} of those"
        parts.append(f"{p['name']} {p['season']} ({what})")
    if not parts:
        return COVERAGE_NOTE
    return (
        COVERAGE_NOTE
        + " Partial seasons, which count only the matches priced: "
        + "; ".join(parts)
        + "."
    )


PRICE = "DraftKings' quoted 1X2 48 hours before kickoff (bought history), break-even = 1/decimal"
CLV = "entry odds x Pinnacle's margin-free (Shin) close in the same snapshot - 1"


def build_files(lean, edge12, *, from_season: int, names=None, now=None):
    """(lean_backtest_dk.json, edge12_backtest_dk.json) from each league's
    league_reports(): {code: (report, bets)} per rule. The Pinnacle files' own builders,
    relabelled for DraftKings."""
    now = now or pd.Timestamp.now(tz="UTC")
    lf = lb.build(
        {c: r for c, (r, _) in lean.items()},
        [b for _, b in lean.values()],
        from_season=from_season,
        names=names,
        now=now,
    )
    ef_ = eb.build(
        {c: r for c, (r, _) in edge12.items()},
        {c: b for c, (_, b) in edge12.items()},
        from_season=from_season,
        names=names,
        now=now,
    )
    for f, rule_name in ((lf, "lean"), (ef_, "edge12")):
        f["title"] = (
            "Blend Lean at DraftKings' own prices"
            if rule_name == "lean"
            else "Model 12% at DraftKings' own prices"
        )
        f["note"] = NOTE
        f["book"] = "draftkings"
        f["method"] = "docs/leagues.md, 'Backtests at DraftKings' prices: method'"
        f["coverage_note"] = coverage_note(f.get("leagues") or {})
        f["partial_coverage"] = partial_coverage(f.get("leagues") or {})
        f["rule"] = {**f.get("rule", {}), "price": PRICE, "clv": CLV}
        f["draftkings_replay"] = None
    lean_all = pd.concat([b for _, b in lean.values() if len(b)] or [lb.bets(pd.DataFrame(), "")])
    e12_all = pd.concat([b for _, b in edge12.values() if len(b)] or [eb.empty_bets()])
    inside = (
        lean_all[[tr.in_lean_range(o) for o in lean_all["odds"]]] if len(lean_all) else lean_all
    )
    lf["verdict"] = {
        "all_odds": verdict(lb.metrics(lean_all)),
        "live_rule": {**verdict(lb.metrics(inside)), "odds_range": list(tr.LEAN_ODDS_RANGE)},
    }
    lf["all"]["clv_dk"] = clv_dk_block(lean_all)
    ef_["verdict"] = verdict(eb.metrics(e12_all))
    ef_["all"]["clv_dk"] = clv_dk_block(e12_all)
    return lf, ef_


def headline(code: str, lean_rep: dict, e12_rep: dict) -> list[str]:
    """Plain lines for the job log."""
    out = []
    for c in lean_rep.get("coverage") or []:
        out.append(
            f"  {c['label']}: {c['matches']} matches, DraftKings at the look "
            f"{c['early_share']:.0%}, Pinnacle close in the body {c['close_share']:.0%}"
            + ("" if c["included"] else " (not scored)")
        )

    def f(v):
        return "n/a" if v is None else f"{v:+.1%}"

    def line(name, m):
        if not m or not m.get("bets"):
            return f"  {name}: 0 bets"
        r, c = m.get("roi_ci95") or [None, None], m.get("clv_ci95") or [None, None]
        return (
            f"  {name}: {m['bets']} bets, won {m['win_rate']:.1%} vs {m['breakeven']:.1%} "
            f"needed, ROI {f(m['roi'])} ({f(r[0])} to {f(r[1])}), CLV vs Pinnacle "
            f"{f(m.get('clv'))} ({f(c[0])} to {f(c[1])})"
        )

    out.append(line(f"{code} Blend Lean at DraftKings (all odds)", lean_rep.get("lean")))
    out.append(line(f"{code} Model 12% at DraftKings", e12_rep.get("edge12")))
    same = lean_rep.get("pinnacle_same_matches") or {}
    out.append(line(f"{code} Blend Lean, same matches at Pinnacle early", same.get("lean")))
    same = e12_rep.get("pinnacle_same_matches") or {}
    out.append(line(f"{code} Model 12%, same matches at Pinnacle early", same.get("edge12")))
    return out


def bought_leagues(raw_dir: Path = RAW_DIR) -> list[str]:
    """Domestic live leagues with ten-book bodies in the cache (some snapshot holds a book
    other than DraftKings: the original E0 run kept DraftKings alone)."""
    from soccer_stats import leagues as lgs

    out = []
    for code in lgs.live_codes():
        d = history_dir(code, raw_dir)
        if code == "UCL" or not d.is_dir():
            continue
        for path in sorted(d.glob("*.json"))[:50]:
            if '"pinnacle"' in path.read_text():
                out.append(code)
                break
    return out
