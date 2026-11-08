"""Tuning phase 2: the ceiling analysis (docs/lab.md "Phase 2: ceiling analysis").

Every oracle here is descriptive and never tradable. The base is the Lean backtest as
`backtest-lean` builds it (all odds, entry at Pinnacle's early price), 2019/20-2023/24; 2024/25
is never loaded. Stages: (a) the blend's chance replaced by Pinnacle's fair close; (b) the
model's chance replaced by Poisson on the match's own post-match xG (refitted blend); (c) the
z cut swept in hindsight; (d) the same picks at the best of the ten books at the 48 h look;
(e) the same picks at DraftKings' 48 h price and Pinnacle's quoted close.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson

from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab.harness import Holdout
from soccer_stats.odds import devig_shin

H2H = ("home", "draw", "away")
Z_GRID = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
FROM_SEASON = 2019
BURN_IN = 2
MAX_GOALS = 10
ENTRIES = ("pin_early", "dk_48h", "best_48h", "pin_close")


def lean_bets(scored: pd.DataFrame, league: str, min_z: float = tr.LEAN_Z,
              prob: str = "pb") -> pd.DataFrame:  # fmt: skip
    """lean_backtest.bets with a chosen z cut and chance columns (<prob>_<side>): one row per
    bet with the match keys, side, entry odds (Pinnacle early), won, profit and CLV."""
    out = []
    for r in scored.to_dict("records"):
        sd = r.get("sigma")
        if sd is None or not np.isfinite(sd) or sd <= 0:
            continue
        odds = {m: float(r[f"odds_{m}"]) for m in H2H}
        p = {m: float(r[f"{prob}_{m}"]) for m in H2H}
        if not all(np.isfinite(v) for v in p.values()):
            continue
        pick = tr.lean_pick(p, odds, sd, min_z=min_z)
        if not pick:
            continue
        m = pick["market"]
        w = tr.won(m, int(r["home_goals"]), int(r["away_goals"]))
        close = {k: (float(r[f"close_{k}"]) if np.isfinite(r.get(f"close_{k}", np.nan)) else None)
                 for k in H2H}  # fmt: skip
        out.append(
            {
                "league": league,
                "season": str(r["season"]),
                "date": pd.Timestamp(r["date"]).normalize(),
                "home": r["home"],
                "away": r["away"],
                "market": m,
                "odds": pick["odds"],
                "z": pick["z"],
                "won": bool(w),
                "profit": (pick["odds"] - 1) if w else -1.0,
                "clv": tr.clv(pick["odds"], m, close),
                **{f"close_{k}": close[k] for k in H2H},
            }
        )
    cols = ["league", "season", "date", "home", "away", "market", "odds", "z", "won", "profit",
            "clv", *[f"close_{k}" for k in H2H]]  # fmt: skip
    return pd.DataFrame(out, columns=cols)


def with_close_chance(scored: pd.DataFrame) -> pd.DataFrame:
    """(a): pb_<side> replaced by Pinnacle's Shin-fair close (rows without a full close get
    NaN, so no bet)."""
    s = scored.copy()
    v = s[[f"close_{m}" for m in H2H]].to_numpy(float)
    ok = (np.isfinite(v) & (v > 1)).all(axis=1)
    fair = np.full(v.shape, np.nan)
    if ok.any():
        fair[ok] = np.vstack([devig_shin(r) for r in v[ok]])
    for i, m in enumerate(H2H):
        s[f"pb_{m}"] = fair[:, i]
    return s


def xg_oracle(home_xg, away_xg) -> np.ndarray:
    """(n, 3) independent-Poisson 1X2 on each match's own xG (NaN rows where xG is missing)."""
    hx, ax = np.asarray(home_xg, float), np.asarray(away_xg, float)
    out = np.full((len(hx), 3), np.nan)
    k = np.arange(MAX_GOALS + 1)
    for i, (a, b) in enumerate(zip(hx, ax, strict=True)):
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        m = np.outer(poisson.pmf(k, max(a, 1e-6)), poisson.pmf(k, max(b, 1e-6)))
        m /= m.sum()
        out[i] = [np.tril(m, -1).sum(), np.trace(m), np.triu(m, 1).sum()]
    return out


def replay(merged: pd.DataFrame, included: set[str]):
    """lean_backtest.league_report's steps: training rows, targets, walk-forward blend,
    restricted to the included seasons."""
    train = mc.training_rows(merged, "h2h") if not merged.empty else merged
    target = lb.target_rows(merged)
    scored, fits = lb.walk_forward(train, target)
    if len(scored):
        scored = scored[scored["season"].astype(str).isin(included)]
    return scored, fits


def best_at_look(near: pd.DataFrame, at: pd.Timestamp, books) -> dict:
    """{side: (odds, book)}: the best fresh quote per side among `books` at `at`."""
    from soccer_stats import dk_price_backtest as dkb

    best: dict = {}
    for b in books:
        q = dkb.quote(near, at, b)
        if not q:
            continue
        for m in H2H:
            if m not in best or q[m] > best[m][0]:
                best[m] = (q[m], b)
    return best


def entry_rows(base: pd.DataFrame, merged: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """(d)/(e): for each base bet on a match the ten-book history covers, the picked side's
    odds at each entry (Pinnacle early = the base, DraftKings at 48 h, the best of the ten
    books at 48 h, Pinnacle's quoted close), each with CLV against the fair close and $1
    profit."""
    from soccer_stats import dk_price_backtest as dkb
    from soccer_stats.odds_feed import BOOKMAKERS

    if base.empty or hist.empty:
        return pd.DataFrame()
    by_pair = {k: g.sort_values("snapshot_ts") for k, g in hist.groupby(["home", "away"])}
    rows = []
    for r in base.to_dict("records"):
        g = by_pair.get((r["home"], r["away"]))
        if g is None:
            continue
        day = pd.Timestamp(r["date"]).date()
        near = g[(g["kickoff"].dt.date - day).abs() <= pd.Timedelta(days=1)]
        if near.empty:
            continue
        kickoff = near["kickoff"].mode().iloc[0]
        look = kickoff - dkb.LOOK
        dk = dkb.quote(near, look, dkb.BOOKMAKER)
        best = best_at_look(near, look, BOOKMAKERS)
        m = r["market"]
        if dk is None or m not in best:
            continue
        close = {k: r[f"close_{k}"] for k in H2H}
        entries = {
            "pin_early": r["odds"],
            "dk_48h": dk[m],
            "best_48h": best[m][0],
            "pin_close": close[m],
        }
        row = {k: r[k] for k in ("league", "season", "date", "home", "away", "market", "won")}
        row["best_book"] = best[m][1]
        for name, o in entries.items():
            ok = o is not None and np.isfinite(o)
            row[f"odds_{name}"] = o if ok else np.nan
            row[f"clv_{name}"] = tr.clv(o, m, close) if ok else np.nan
            row[f"profit_{name}"] = ((o - 1) if r["won"] else -1.0) if ok else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def run_league(code: str, out_dir: Path, holdout: Holdout | None = None) -> dict:
    """Every stage for one league; writes ceiling_<code>.csv.gz (one row per bet and
    stage) and entries_<code>.csv.gz (d/e)."""
    from soccer_stats import backtest
    from soccer_stats import dk_price_backtest as dkb
    from soccer_stats import leagues as lgs
    from soccer_stats.data import load_matches, pinnacle_prices
    from soccer_stats.models import DixonColes
    from soccer_stats.publish import XG_WEIGHT
    from soccer_stats.xg import with_xg

    holdout = holdout or Holdout(tb.LOCK)
    years = range(FROM_SEASON - BURN_IN, tb.LAST_YEAR + 1)
    matches = load_matches([code], years)
    matches, err = with_xg(matches)
    if lgs.LEAGUES[code].understat and not matches["home_xg"].notna().any():
        raise RuntimeError(f"no Understat xG attached ({err})")
    matches = matches[matches["date"] < tb.LOCK]
    holdout.check(matches["date"])
    weight = XG_WEIGHT if matches["home_xg"].notna().any() else 0.0
    factory = functools.partial(DixonColes, xg_weight=weight)
    preds = backtest.walk_forward(matches, start=f"{FROM_SEASON}-07-01", model_factory=factory)
    pins = pinnacle_prices(code, range(FROM_SEASON, tb.LAST_YEAR + 1))
    pins = pins[pd.to_datetime(pins["date"]) < tb.LOCK]
    holdout.check(pins["date"])
    included = {c["season"] for c in lb.coverage(pins) if c["included"]}
    merged = lb.with_pinnacle(preds, pins)
    scored, _ = replay(merged, included)
    holdout.check(scored["date"])

    parts = []
    base = lean_bets(scored, code)
    for z in Z_GRID:
        b = base if z == tr.LEAN_Z else lean_bets(scored, code, min_z=z)
        parts.append(b.assign(stage=f"z{z:g}"))
    parts.append(lean_bets(with_close_chance(scored), code).assign(stage="a_close"))
    info = {"league": code, "xg_weight": weight, "seasons": sorted(included),
            "candidates": int(scored["sigma"].notna().sum()) if len(scored) else 0}  # fmt: skip
    if weight:
        xgm = matches[["season", "home", "away", "home_xg", "away_xg"]].copy()
        xgm["season"] = xgm["season"].astype(str)
        mg = merged.assign(season=merged["season"].astype(str)).merge(
            xgm.drop_duplicates(["season", "home", "away"]), on=["season", "home", "away"],
            how="left", suffixes=("", "_m"))  # fmt: skip
        hx = mg["home_xg_m"] if "home_xg_m" in mg else mg["home_xg"]
        ax = mg["away_xg_m"] if "away_xg_m" in mg else mg["away_xg"]
        o = xg_oracle(hx, ax)
        for i, m in enumerate(H2H):
            mg[f"p_{m}"] = o[:, i]
        mg = mg.dropna(subset=[f"p_{m}" for m in H2H])
        sc_b, _ = replay(mg, included)
        parts.append(lean_bets(sc_b, code).assign(stage="b_xg_oracle"))
        info["xg_oracle_candidates"] = int(sc_b["sigma"].notna().sum()) if len(sc_b) else 0
    bets = pd.concat(parts, ignore_index=True)
    entries = pd.DataFrame()
    if code in dkb.bought_leagues():
        hist = dkb.load_books(code, set(matches["home"]) | set(matches["away"]))
        hist = hist[hist["kickoff"] < pd.Timestamp(tb.LOCK, tz="UTC")]
        if len(hist):
            holdout.check(hist["kickoff"].dt.tz_convert(None))
            entries = entry_rows(base, merged, hist)
    info["entries"] = int(len(entries))
    out_dir.mkdir(parents=True, exist_ok=True)
    bets.to_csv(out_dir / f"ceiling_{code}.csv.gz", index=False)
    entries.to_csv(out_dir / f"entries_{code}.csv.gz", index=False)
    (out_dir / f"ceiling_{code}.json").write_text(json.dumps(info, default=str))
    return info


def metric(v, n_boot: int = tb.N_BOOT) -> dict:
    v = np.asarray(v, float)
    v = v[np.isfinite(v)]
    return {"n": int(len(v)), "mean": float(v.mean()) if len(v) else None,
            "ci95": tb.boot_mean(v, n_boot)}  # fmt: skip


def stage_block(b: pd.DataFrame, n_boot: int = tb.N_BOOT) -> dict:
    return {"bets": int(len(b)), "won": int(b["won"].sum()) if len(b) else 0,
            "avg_odds": float(b["odds"].mean()) if len(b) else None,
            "clv": metric(b["clv"], n_boot), "roi": metric(b["profit"], n_boot)}  # fmt: skip


def diff_independent(a, b, n_boot: int = tb.N_BOOT, seed: int = tb.SEED) -> list[float] | None:
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return None
    rng = np.random.default_rng(seed)
    d = a[rng.integers(0, len(a), (n_boot, len(a)))].mean(1) - b[
        rng.integers(0, len(b), (n_boot, len(b)))
    ].mean(1)
    return [float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))]


def pool(in_dir: Path, n_boot: int = tb.N_BOOT) -> dict:
    in_dir = Path(in_dir)
    bets = pd.concat([pd.read_csv(f) for f in sorted(in_dir.rglob("ceiling_*.csv.gz"))],
                     ignore_index=True)  # fmt: skip
    ent_files = [f for f in sorted(in_dir.rglob("entries_*.csv.gz")) if f.stat().st_size > 30]
    ents = []
    for f in ent_files:
        try:
            ents.append(pd.read_csv(f))
        except pd.errors.EmptyDataError:
            continue
    ent = pd.concat(ents, ignore_index=True) if ents else pd.DataFrame()
    infos = {json.loads(f.read_text())["league"]: json.loads(f.read_text())
             for f in sorted(in_dir.rglob("ceiling_*.json"))}  # fmt: skip
    base = bets[bets["stage"] == "z1"]
    stages = {s: stage_block(g, n_boot) for s, g in bets.groupby("stage")}
    for s, blk in stages.items():
        g = bets[bets["stage"] == s]
        ref = base[base["league"].isin(g["league"].unique())] if s == "b_xg_oracle" else base
        blk["clv_gain_vs_base"] = (blk["clv"]["mean"] - metric(ref["clv"])["mean"]
                                   if blk["clv"]["mean"] is not None else None)  # fmt: skip
        blk["clv_gain_ci95"] = diff_independent(g["clv"], ref["clv"], n_boot)
        blk["roi_gain_vs_base"] = (blk["roi"]["mean"] - metric(ref["profit"])["mean"]
                                   if blk["roi"]["mean"] is not None else None)  # fmt: skip
        blk["roi_gain_ci95"] = diff_independent(g["profit"], ref["profit"], n_boot)
        if s == "b_xg_oracle":
            blk["base_same_leagues"] = stage_block(ref, n_boot)
    timing = {}
    if len(ent):
        for e in ENTRIES:
            blk = {
                "clv": metric(ent[f"clv_{e}"], n_boot),
                "roi": metric(ent[f"profit_{e}"], n_boot),
            }
            if e != "pin_early":
                dc = ent[f"clv_{e}"] - ent["clv_pin_early"]
                dr = ent[f"profit_{e}"] - ent["profit_pin_early"]
                blk["clv_gain_vs_early"] = metric(dc, n_boot)
                blk["roi_gain_vs_early"] = metric(dr, n_boot)
            timing[e] = blk
        timing["bets"] = int(len(ent))
        timing["best_books"] = ent["best_book"].value_counts().to_dict()
    best_z = max((s for s in stages if s.startswith("z") and stages[s]["bets"] >= 300),
                 key=lambda s: stages[s]["clv"]["mean"] or -9, default=None)  # fmt: skip
    return {
        "kind": "tuning_ceiling",
        "development": True,
        "descriptive": True,
        "locked_from": str(tb.LOCK.date()),
        "leagues": infos,
        "stages": stages,
        "best_z_in_hindsight": best_z,
        "entries": timing,
        "configs": len(Z_GRID) + 4,
        "runs": {"tuning_ceiling": os.environ.get("GITHUB_RUN_ID")},
    }


def summary_lines(res: dict) -> list[str]:
    out = []
    for s, b in sorted(res["stages"].items()):
        c, r = b["clv"], b["roi"]
        if not b["bets"]:
            out.append(f"{s}: no bets")
            continue
        out.append(
            f"{s}: {b['bets']} bets, CLV {c['mean']:+.4f} {c['ci95']}, "
            f"ROI {r['mean']:+.4f} {r['ci95']}, CLV gain {b.get('clv_gain_vs_base')}"
        )
    for e, b in res["entries"].items():
        if isinstance(b, dict) and "clv" in b:
            out.append(f"entry {e}: CLV {b['clv']['mean']} ROI {b['roi']['mean']} "
                       f"gain {b.get('clv_gain_vs_early', {}).get('mean')}")  # fmt: skip
    out.append(f"best z in hindsight: {res['best_z_in_hindsight']}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="run one league into --out")
    p.add_argument("--pool", default="", help="pool every league's files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=tb.N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        print(f"TUNING_CEILING_LEAGUE {json.dumps(run_league(a.league, Path(a.out)), default=str)}")
        return
    res = pool(Path(a.pool or a.out), a.boot)
    print("\n".join(summary_lines(res)))
    print(f"TUNING_CEILING_JSON {json.dumps(res, default=str)}")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "tuning_ceiling.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
