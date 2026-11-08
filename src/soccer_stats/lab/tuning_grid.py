"""Tuning phase 3: grid search on the never-tuned settings (docs/lab.md "Phase 3").

Blocks, each varied one at a time from today's defaults: A (time decay x xG weight), R (the
ratings ridge), B (window x minimum team matches), C (the blend's refit cadence x minimum
rows). Every configuration runs walk-forward from 2017-07-01; for each season from 2019/20 the
configuration with the lowest log loss on the earlier seasons (per league) predicts it, and is
scored against the default on the same rows. 2024/25 is never loaded. No key, 0 credits.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab.harness import Holdout

FIRST_YEAR = 2014  # 1,095 days before 2017-07-01
XIS = (0.0010, 0.0015, 0.0019, 0.0025, 0.0035)
XG_WEIGHTS = (0.0, 0.3, 0.5, 0.7, 0.85, 1.0)
RIDGES = (0.0, 0.1, 0.3, 1.0, 3.0)
WINDOWS = (365, 540, 730, 1095)
MIN_TEAMS = (4, 6, 8)
RHOS = ("", "fixed:0", "penalty:1", "penalty:10", "penalty:100", "penalty:1000")
REFITS = (14, 28, 56)
MIN_ROWS = (200, 300, 500)
FIRST_SCORED = "1920"
KEY = ["season", "home", "away"]


def model_configs(code: str) -> dict[str, dict[str, tb.Config]]:
    """{block: {name: Config}} for blocks A, R and B; the default is named "default"."""
    base = tb.default_for(code)
    xg_league = code in tb.XG_LEAGUES

    def name(c: tb.Config) -> str:
        return "default" if c == base else c.key()

    a = {}
    for xi in XIS:
        for w in XG_WEIGHTS if xg_league else (base.xg_weight,):
            c = tb.Config(xi=xi, xg_weight=w, window=base.window, min_team=base.min_team)
            a[name(c)] = c
    r = {}
    for rg in RIDGES:
        c = tb.Config(xi=base.xi, xg_weight=base.xg_weight, window=base.window,
                      min_team=base.min_team, ridge=rg)  # fmt: skip
        r[name(c)] = c
    b = {}
    for win in WINDOWS:
        for mt in MIN_TEAMS:
            c = tb.Config(xi=base.xi, xg_weight=base.xg_weight, window=win, min_team=mt)
            b[name(c)] = c
    p = {}
    for rho in RHOS:
        c = tb.Config(xi=base.xi, xg_weight=base.xg_weight, window=base.window,
                      min_team=base.min_team, rho=rho)  # fmt: skip
        p[name(c)] = c
    return {"A": a, "R": r, "B": b, "P": p}


def blend_configs() -> dict[str, tuple[str, int]]:
    out = {}
    for rf in REFITS:
        for mr in MIN_ROWS:
            out["default" if (rf, mr) == (28, 300) else f"refit{rf}_min{mr}"] = (f"{rf}D", mr)
    return out


def ll_rows(preds: pd.DataFrame, prefix: str = "p") -> pd.DataFrame:
    p = preds[[f"{prefix}_{s}" for s in tb.SIDES]].to_numpy(float)
    ok = np.isfinite(p).all(axis=1)
    out = preds.loc[ok, KEY].copy()
    out["season"] = out["season"].astype(str)
    out["ll"] = tb.log_loss_rows(p[ok], preds.loc[ok, "y"].to_numpy(int))
    return out


def blend_rows(preds: pd.DataFrame, pins: pd.DataFrame) -> pd.DataFrame:
    """Block C: the blend at each cadence on the default model, scored on the early price
    (as bet). One row per (config, match)."""
    merged = lb.with_pinnacle(preds, pins)
    train = mc.training_rows(merged, "h2h")
    target = lb.target_rows(merged)
    target["y"] = target["y"].astype(int)
    parts = []
    for name, (rf, mr) in blend_configs().items():
        scored, _ = lb.walk_forward(train, target, refit=rf, min_rows=mr)
        part = ll_rows(scored, prefix="pb")
        part["block"], part["config"] = "C", name
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def run_league(code: str, out_dir: Path, blocks: tuple[str, ...] = ("A", "R", "B", "C")) -> dict:
    from soccer_stats.data import pinnacle_prices

    holdout = Holdout(tb.LOCK)
    matches, _pins_all, info = tb.load_league(code, FIRST_YEAR, holdout)
    parts, default_preds = [], None
    cfgs = model_configs(code)
    done: dict[str, pd.DataFrame] = {}
    cfgs = {b: c for b, c in cfgs.items() if b in blocks or (b == "A" and "C" in blocks)}
    for block, configs in cfgs.items():
        for name, cfg in configs.items():
            k = cfg.key()
            if k not in done:
                p, _ = tb.walk(matches, cfg, start=tb.BURN_FROM, insample_every=0,
                               keep_ratings=False, holdout=holdout)  # fmt: skip
                done[k] = p
                print(f"{code} {block} {name}: {len(p)} predictions", flush=True)
            if block in blocks:
                part = ll_rows(done[k])
                part["block"], part["config"] = block, name
                parts.append(part)
            if name == "default":
                default_preds = done[k]
    pins = pinnacle_prices(code, range(2017, tb.LAST_YEAR + 1))
    pins = pins[pd.to_datetime(pins["date"]) < tb.LOCK]
    holdout.check(pins["date"])
    if "C" in blocks:
        parts.append(blend_rows(default_preds, pins))
    rows = pd.concat(parts, ignore_index=True)
    rows.insert(0, "league", code)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "" if set(blocks) == {"A", "R", "B", "C"} else "_" + "".join(blocks)
    rows.to_csv(out_dir / f"grid_{code}{tag}.csv.gz", index=False)
    n = {b: len(c) for b, c in cfgs.items() if b in blocks}
    if "C" in blocks:
        n["C"] = len(blend_configs())
    res = {"league": code, "load": info, "configs": n, "rows": int(len(rows))}
    (out_dir / f"grid_{code}{tag}.json").write_text(json.dumps(res, default=str))
    return res


def nested(rows: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """One league and block: rows (season, home, away, config, ll). Keeps the matches every
    configuration predicts; for each scored season the configuration with the lowest mean log
    loss over the earlier seasons predicts it. Returns per-match (season, ll_default,
    ll_nested, gain) and the choices."""
    wide = rows.pivot_table(index=KEY, columns="config", values="ll", aggfunc="first")
    wide = wide.dropna()
    seasons = sorted(wide.index.get_level_values("season").unique())
    out, choices = [], []
    for s in seasons:
        if s < FIRST_SCORED:
            continue
        prev = wide[wide.index.get_level_values("season") < s]
        cur = wide[wide.index.get_level_values("season") == s]
        if prev.empty or cur.empty:
            continue
        means = prev.mean()
        pick = means.idxmin()
        choices.append({"season": s, "config": pick, "earlier_ll": float(means[pick]),
                        "default_earlier_ll": float(means.get("default", np.nan))})  # fmt: skip
        out.append(pd.DataFrame({"season": s, "ll_default": cur["default"].to_numpy(),
                                 "ll_nested": cur[pick].to_numpy()}))  # fmt: skip
    df = pd.concat(out, ignore_index=True) if out else pd.DataFrame(
        columns=["season", "ll_default", "ll_nested"])  # fmt: skip
    df["gain"] = df["ll_default"] - df["ll_nested"]
    return df, choices


def hindsight(rows: pd.DataFrame) -> dict:
    wide = rows.pivot_table(index=KEY, columns="config", values="ll", aggfunc="first").dropna()
    wide = wide[wide.index.get_level_values("season") >= FIRST_SCORED]
    if wide.empty:
        return {}
    m = wide.mean()
    best = m.idxmin()
    return {"config": best, "gain": float(m.get("default", np.nan) - m[best]),
            "matches": int(len(wide))}  # fmt: skip


def gain_block(d: pd.DataFrame, n_boot: int = tb.N_BOOT) -> dict:
    return {
        "matches": int(len(d)),
        "ll_default": float(d["ll_default"].mean()) if len(d) else None,
        "ll_nested": float(d["ll_nested"].mean()) if len(d) else None,
        "gain": float(d["gain"].mean()) if len(d) else None,
        "gain_ci95": tb.boot_mean(d["gain"].to_numpy(), n_boot),
    }


def pool(in_dir: Path, n_boot: int = tb.N_BOOT) -> dict:
    in_dir = Path(in_dir)
    files = sorted(in_dir.rglob("grid_*.csv.gz"))
    rows = pd.concat([pd.read_csv(f, dtype={"season": str}) for f in files], ignore_index=True)
    infos = [json.loads(f.read_text()) for f in sorted(in_dir.rglob("grid_*.json"))]
    blocks = {}
    for block, g in rows.groupby("block"):
        per, allg = {}, []
        for lg, h in g.groupby("league"):
            d, ch = nested(h)
            per[lg] = {**gain_block(d, n_boot), "choices": ch, "hindsight": hindsight(h)}
            allg.append(d)
        pooled = pd.concat(allg, ignore_index=True) if allg else pd.DataFrame()
        pb = gain_block(pooled, n_boot)
        pb["passes"] = bool(pb["gain_ci95"] and pb["gain_ci95"][0] > 0)
        blocks[block] = {"pooled": pb, "leagues": per}
    return {
        "kind": "tuning_grid",
        "development": True,
        "locked_from": str(tb.LOCK.date()),
        "blocks": blocks,
        "configs": int(sum(sum(i["configs"].values()) for i in infos)),
        "leagues": {i["league"]: i for i in infos},
        "runs": {"tuning_grid": os.environ.get("GITHUB_RUN_ID")},
    }


def summary_lines(res: dict) -> list[str]:
    out = [f"Configurations: {res['configs']}"]
    for b, blk in res["blocks"].items():
        p = blk["pooled"]
        out.append(f"Block {b}: {p['matches']} matches, default {p['ll_default']:.5f}, nested "
                   f"{p['ll_nested']:.5f}, gain {p['gain']:+.5f} {p['gain_ci95']} "
                   f"-> {'PASS' if p['passes'] else 'no'}")  # fmt: skip
        for lg, r in blk["leagues"].items():
            picks = ",".join(c["config"] for c in r["choices"])
            out.append(f"  {lg}: gain {r['gain']:+.5f} {r['gain_ci95']}; picks {picks}; "
                       f"hindsight {r['hindsight']}")  # fmt: skip
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="run one league into --out")
    p.add_argument("--pool", default="", help="pool every league's files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=tb.N_BOOT, help="bootstrap draws")
    p.add_argument("--blocks", default="A,R,B,C", help="comma list of blocks (A, R, B, C, P)")
    a = p.parse_args(argv)
    if a.league:
        blocks = tuple(b.strip() for b in a.blocks.split(",") if b.strip())
        res = run_league(a.league, Path(a.out), blocks)
        print(f"TUNING_GRID_LEAGUE {json.dumps(res, default=str)}")
        return
    res = pool(Path(a.pool or a.out), a.boot)
    print("\n".join(summary_lines(res)))
    print(f"TUNING_GRID_JSON {json.dumps(res, default=str)}")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "tuning_grid.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
