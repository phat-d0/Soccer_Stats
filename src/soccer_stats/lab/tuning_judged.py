"""The judged read of the tuning programme's model settings on 2025/26 (docs/lab.md "Model
settings: the judged read on 2025/26", frozen before this ran).

Per league, the candidate's settings are chosen on the development seasons 2017/18-2023/24
only (the ridge from block R2's grid; rho from block P's options only if P passed in
development). Then today's default and the candidate are run walk-forward over 2025/26 (weekly
refits as live) and compared on 1X2 log loss. 2024/25 enters only as training data and no row of
it is scored (checked); nothing from 2026/27 is loaded. No key, 0 credits.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab import tuning_grid as tg
from soccer_stats.lab.harness import Holdout

JUDGED_FROM = pd.Timestamp("2025-07-01")
JUDGED_TO = pd.Timestamp("2026-07-01")
DEV_FROM = tb.BURN_FROM  # 2017-07-01
DEV_TO = tb.LOCK  # 2024-07-01
FIRST_YEAR = 2015  # 730 days before 2017-07-01, plus a season
REASON = ("Tuning programme judged read on 2025/26 (model settings), frozen in docs/lab.md "
          "before this run; lead approval 8 Nov 2026.")  # fmt: skip
KEY = ["season", "home", "away"]


def dev_choice(matches: pd.DataFrame, code: str, options: dict[str, tb.Config]) -> dict:
    """The option with the lowest mean log loss over every development match all options
    predict (2017/18-2023/24)."""
    holdout = Holdout(DEV_TO)
    lls = {}
    for name, cfg in options.items():
        p, _ = tb.walk(matches, cfg, start=DEV_FROM, end=DEV_TO, insample_every=0,
                       keep_ratings=False, holdout=holdout)  # fmt: skip
        lls[name] = tg.ll_rows(p).set_index(KEY)["ll"]
    wide = pd.DataFrame(lls).dropna()
    means = wide.mean()
    pick = str(means.idxmin())
    return {"pick": pick, "config": options[pick], "dev_matches": int(len(wide)),
            "dev_ll": {k: float(v) for k, v in means.items()}}  # fmt: skip


def candidate_for(code: str, matches: pd.DataFrame, with_rho: bool) -> dict:
    cfgs = tg.model_configs(code)
    ridge = dev_choice(matches, code, cfgs["R2"])
    out = {"ridge": ridge}
    cand = ridge["config"]
    if with_rho:
        rho = dev_choice(matches, code, cfgs["P"])
        out["rho"] = rho
        cand = tb.Config(xi=cand.xi, xg_weight=cand.xg_weight, window=cand.window,
                         min_team=cand.min_team, ridge=cand.ridge,
                         rho=rho["config"].rho)  # fmt: skip
    out["candidate"] = cand
    return out


def judged_rows(matches: pd.DataFrame, pins: pd.DataFrame, default: tb.Config,
                cand: tb.Config) -> pd.DataFrame:  # fmt: skip
    """Per 2025/26 match both predict: ll_default, ll_candidate, and Pinnacle's fair early and
    close log loss where priced."""
    holdout = Holdout(JUDGED_FROM)
    holdout.unlock(REASON)
    parts = {}
    for name, cfg in (("default", default), ("candidate", cand)):
        p, _ = tb.walk(matches, cfg, start=JUDGED_FROM, end=JUDGED_TO, insample_every=0,
                       keep_ratings=False, holdout=holdout)  # fmt: skip
        d = pd.to_datetime(p["date"])
        if ((d >= DEV_TO) & (d < JUDGED_FROM)).any() or (d >= JUDGED_TO).any():
            raise RuntimeError("a row outside 2025/26 was predicted")
        parts[name] = p
    a = tg.ll_rows(parts["default"]).rename(columns={"ll": "ll_default"})
    b = tg.ll_rows(parts["candidate"]).rename(columns={"ll": "ll_candidate"})
    rows = a.merge(b, on=KEY, how="inner")
    pr = tb.with_prices(parts["default"][[*KEY, "date", "y"]], pins)
    for name in ("early", "close"):
        q = pr[[f"q_{name}_{s}" for s in tb.SIDES]].to_numpy(float)
        ok = np.isfinite(q).all(axis=1)
        ll = np.full(len(pr), np.nan)
        ll[ok] = tb.log_loss_rows(q[ok], pr["y"].to_numpy(int)[ok])
        pr[f"ll_{name}"] = ll
    pr["season"] = pr["season"].astype(str)
    rows = rows.merge(pr[[*KEY, "ll_early", "ll_close"]], on=KEY, how="left")
    rows["gain"] = rows["ll_default"] - rows["ll_candidate"]
    return rows


def run_league(code: str, out_dir: Path, with_rho: bool) -> dict:
    from soccer_stats import leagues as lgs
    from soccer_stats.data import load_matches, pinnacle_prices
    from soccer_stats.xg import with_xg

    matches = load_matches([code], range(FIRST_YEAR, 2026))
    matches, err = with_xg(matches)
    if lgs.LEAGUES[code].understat and not matches["home_xg"].notna().any():
        raise RuntimeError(f"no Understat xG attached ({err})")
    matches = matches[matches["date"] < JUDGED_TO]
    default = tb.default_for(code)
    cand = candidate_for(code, matches[matches["date"] < DEV_TO], with_rho)
    pins = pinnacle_prices(code, [2025])
    rows = judged_rows(matches, pins, default, cand["candidate"])
    rows.insert(0, "league", code)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows.to_csv(out_dir / f"judged_{code}.csv.gz", index=False)
    info = {
        "league": code,
        "default": default.key(),
        "candidate": cand["candidate"].key(),
        "ridge_choice": {k: v for k, v in cand["ridge"].items() if k != "config"},
        "rho_choice": (
            {k: v for k, v in cand["rho"].items() if k != "config"} if "rho" in cand else None
        ),  # fmt: skip
        "matches": int(len(rows)),
        "priced_early": int(rows["ll_early"].notna().sum()),
        "priced_close": int(rows["ll_close"].notna().sum()),
    }
    (out_dir / f"judged_{code}.json").write_text(json.dumps(info, default=str))
    return info


def block(r: pd.DataFrame, n_boot: int = tb.N_BOOT, level: float = 0.95) -> dict:
    g = r["gain"].to_numpy(float)
    ci = tb.boot_mean(g, n_boot)
    if level != 0.95 and len(g) > 1:
        rng = np.random.default_rng(tb.SEED)
        m = g[rng.integers(0, len(g), (n_boot, len(g)))].mean(axis=1)
        a = (1 - level) / 2
        ci = [float(np.quantile(m, a)), float(np.quantile(m, 1 - a))]
    priced = r.dropna(subset=["ll_close"])
    return {
        "matches": int(len(r)),
        "ll_default": float(r["ll_default"].mean()) if len(r) else None,
        "ll_candidate": float(r["ll_candidate"].mean()) if len(r) else None,
        "gain": float(g.mean()) if len(g) else None,
        "gain_ci": ci,
        "priced_close": int(len(priced)),
        "ll_close_priced": float(priced["ll_close"].mean()) if len(priced) else None,
        "ll_early_priced": (
            float(priced["ll_early"].mean()) if priced["ll_early"].notna().any() else None
        ),  # fmt: skip
        "ll_default_priced": float(priced["ll_default"].mean()) if len(priced) else None,
        "ll_candidate_priced": float(priced["ll_candidate"].mean()) if len(priced) else None,
    }


def pool(in_dir: Path, n_boot: int = tb.N_BOOT, level: float = 0.95) -> dict:
    in_dir = Path(in_dir)
    files = sorted(in_dir.rglob("judged_*.csv.gz"))
    rows = pd.concat([pd.read_csv(f, dtype={"season": str}) for f in files], ignore_index=True)
    infos = {json.loads(f.read_text())["league"]: json.loads(f.read_text())
             for f in sorted(in_dir.rglob("judged_*.json"))}  # fmt: skip
    pooled = block(rows, n_boot, level)
    passes = bool(pooled["gain_ci"] and pooled["gain_ci"][0] > 0)
    return {
        "kind": "tuning_judged",
        "season": "2025/26",
        "level": level,
        "pooled": pooled,
        "passes": passes,
        "verdict": "pass" if passes else "fail",
        "lower_leagues": block(rows[rows["league"].isin(["E1", "E2", "E3"])], n_boot, level),
        "leagues": {
            lg: {**infos.get(lg, {}), **block(g, n_boot, level)} for lg, g in rows.groupby("league")
        },  # fmt: skip
        "runs": {"tuning_judged": os.environ.get("GITHUB_RUN_ID")},
    }


def summary_lines(res: dict) -> list[str]:
    p = res["pooled"]
    out = [f"Judged read 2025/26: {p['matches']} matches, default {p['ll_default']:.5f}, "
           f"candidate {p['ll_candidate']:.5f}, gain {p['gain']:+.5f} {p['gain_ci']} "
           f"-> {res['verdict'].upper()}"]  # fmt: skip
    for lg, b in res["leagues"].items():
        out.append(f"  {lg}: {b.get('candidate')}: gain {b['gain']:+.5f} {b['gain_ci']}; "
                   f"priced {b['priced_close']} of {b['matches']}")  # fmt: skip
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="run one league into --out")
    p.add_argument("--pool", default="", help="pool every league's files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--with-rho", action="store_true", help="include block P's rho choice")
    p.add_argument("--level", type=float, default=0.95, help="range level (0.975 if two tests)")
    p.add_argument("--boot", type=int, default=tb.N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        res = run_league(a.league, Path(a.out), a.with_rho)
        print(f"TUNING_JUDGED_LEAGUE {json.dumps(res, default=str)}")
        return
    res = pool(Path(a.pool or a.out), a.boot, a.level)
    print("\n".join(summary_lines(res)))
    print(f"TUNING_JUDGED_JSON {json.dumps(res, default=str)}")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "tuning_judged.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
