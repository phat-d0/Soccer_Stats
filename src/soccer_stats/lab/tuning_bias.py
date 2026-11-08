"""Tuning phase 1: bias vs variance (docs/lab.md "Phase 1: bias vs variance").

Per league (`run_league`): the walk-forward Dixon-Coles at four windows (365, 730, 1,095,
1,460 days, everything else at today's settings) and the live blend (28-day refits, on the
730-day model) with its training rows capped at 1-4 seasons or uncapped. Out-of-sample vs
in-sample log loss, against Pinnacle's fair close and early price on the same rows, and the
730-day fit's team-rating stability. `pool` reads every league's row file and gives the
pooled numbers. Development only: nothing at or after 2024-07-01 is loaded.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import match_calibration as mc
from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab.harness import Holdout, blocks

WINDOWS = (365, 730, 1095, 1460)
BLEND_CAPS = (1, 2, 3, 4, None)  # seasons of training rows (365 x k days); None = uncapped
FIRST_YEAR = 2015  # 1,460 days before 2019-07-01
EARLY_LEAD = pd.Timedelta(days=4)
SMALL_VARIANCE = 0.005
KEY = ["season", "home", "away"]


def _ll_cols(df: pd.DataFrame, prefix: str) -> np.ndarray:
    return tb.log_loss_rows(
        df[[f"{prefix}_{s}" for s in tb.SIDES]].to_numpy(float), df["y"].to_numpy(int)
    )


def model_rows(preds: dict[int, pd.DataFrame], prices: pd.DataFrame) -> pd.DataFrame:
    """One row per match predicted by every window and fully priced early and close:
    ll_w<window>, ll_close, ll_early."""
    base = None
    for w, p in preds.items():
        part = p[[*KEY, "date", "y", *[f"p_{s}" for s in tb.SIDES]]].copy()
        part["season"] = part["season"].astype(str)
        part[f"ll_w{w}"] = _ll_cols(part, "p")
        part = part.drop(columns=[f"p_{s}" for s in tb.SIDES])
        base = part if base is None else base.merge(part[[*KEY, f"ll_w{w}"]], on=KEY, how="inner")
    pr = tb.with_prices(base[[*KEY, "date", "y"]], prices)
    for name in ("early", "close"):
        q = pr[[f"q_{name}_{s}" for s in tb.SIDES]].to_numpy(float)
        ok = np.isfinite(q).all(axis=1)
        ll = np.full(len(pr), np.nan)
        ll[ok] = tb.log_loss_rows(q[ok], pr["y"].to_numpy(int)[ok])
        base[f"ll_{name}"] = ll
    return base[np.isfinite(base["ll_close"]) & np.isfinite(base["ll_early"])].reset_index(
        drop=True
    )


def blend_rows(pred730: pd.DataFrame, prices: pd.DataFrame, caps=BLEND_CAPS, refit="28D",
               min_rows: int = mc.MIN_ROWS, start=tb.SCORED_FROM, end=tb.LOCK):  # fmt: skip
    """The live blend on the 730-day model, its training rows capped per `caps`. Training
    rows: matches with the model's chance and Pinnacle's fair close (as live); a block of
    decision days (kickoff - 4 days) uses matches kicked off before it. Returns one row per
    match with every cap's fit: ll_b<cap>_close, ll_b<cap>_early, and per-cap in-sample
    records."""
    d = tb.with_prices(pred730, prices)
    d = d[np.isfinite(d[[f"q_close_{s}" for s in tb.SIDES]].to_numpy(float)).all(axis=1)]
    d = d[np.isfinite(d[[f"q_early_{s}" for s in tb.SIDES]].to_numpy(float)).all(axis=1)]
    d = d.sort_values("date").reset_index(drop=True)
    d["day"] = pd.to_datetime(d["date"]) - EARLY_LEAD
    pc = [f"p_{s}" for s in tb.SIDES]
    qc = [f"q_close_{s}" for s in tb.SIDES]
    qe = [f"q_early_{s}" for s in tb.SIDES]
    y = d["y"].to_numpy(int)
    dates = pd.to_datetime(d["date"])
    out = d[[*KEY, "date", "y"]].copy()
    insample: dict[str, list] = {}
    for cap in caps:
        name = f"b{cap or 'all'}"
        lc, le = np.full(len(d), np.nan), np.full(len(d), np.nan)
        recs = []
        for lo, hi in blocks(start, end, refit):
            idx = np.where((d["day"] >= lo) & (d["day"] < hi))[0]
            if not len(idx):
                continue
            keep = dates < lo
            if cap:
                keep &= dates >= lo - pd.Timedelta(days=365 * cap)
            tr = np.where(keep)[0]
            coef = mc.fit(d.iloc[tr][qc].to_numpy(), d.iloc[tr][pc].to_numpy(), y[tr], min_rows)
            if coef is None:
                continue
            fit_in = mc.apply(coef, d.iloc[tr][qc].to_numpy(), d.iloc[tr][pc].to_numpy())
            recs.append({"from": str(lo.date()), "rows": int(len(tr)),
                         "ll_in": float(tb.log_loss_rows(fit_in, y[tr]).mean())})  # fmt: skip
            pb_c = mc.apply(coef, d.iloc[idx][qc].to_numpy(), d.iloc[idx][pc].to_numpy())
            pb_e = mc.apply(coef, d.iloc[idx][qe].to_numpy(), d.iloc[idx][pc].to_numpy())
            lc[idx] = tb.log_loss_rows(pb_c, y[idx])
            le[idx] = tb.log_loss_rows(pb_e, y[idx])
        out[f"ll_{name}_close"] = lc
        out[f"ll_{name}_early"] = le
        insample[name] = recs
    cols = [c for c in out.columns if c.startswith("ll_b")]
    out = out[np.isfinite(out[cols].to_numpy(float)).all(axis=1)].reset_index(drop=True)
    return out, insample


def stability(refits: list[dict], lag: int = 4) -> dict:
    """Team ratings across weekly refits: median |change| week to week and the mean
    correlation of all teams' ratings between refits `lag` apart (attack, defence)."""
    steps = {"attack": [], "defence": []}
    cors = {"attack": [], "defence": []}
    rs = [r["ratings"] for r in refits if r.get("ratings")]
    for i in range(1, len(rs)):
        common = set(rs[i]) & set(rs[i - 1])
        for t in common:
            steps["attack"].append(abs(rs[i][t][0] - rs[i - 1][t][0]))
            steps["defence"].append(abs(rs[i][t][1] - rs[i - 1][t][1]))
    for i in range(lag, len(rs)):
        common = sorted(set(rs[i]) & set(rs[i - lag]))
        if len(common) < 6:
            continue
        for j, k in ((0, "attack"), (1, "defence")):
            a = [rs[i][t][j] for t in common]
            b = [rs[i - lag][t][j] for t in common]
            if np.std(a) > 0 and np.std(b) > 0:
                cors[k].append(float(np.corrcoef(a, b)[0, 1]))
    return {
        "refits": len(rs),
        "median_weekly_change": {k: float(np.median(v)) if v else None for k, v in steps.items()},
        "corr_28d": {k: float(np.mean(v)) if v else None for k, v in cors.items()},
        "rating_sd": {
            k: float(np.mean([np.std([r[t][j] for t in r]) for r in rs])) if rs else None
            for j, k in ((0, "attack"), (1, "defence"))
        },
    }


def _insample(refits: list[dict]) -> dict:
    rec = [r for r in refits if r.get("ll_in_recent") is not None]
    return {
        "refits": len(rec),
        "ll_in_recent": float(np.mean([r["ll_in_recent"] for r in rec])) if rec else None,
        "ll_in_all": float(np.mean([r["ll_in_all"] for r in rec])) if rec else None,
        "ll_in_recent_values": [r["ll_in_recent"] for r in rec],
        "n_train": float(np.mean([r["n_train"] for r in refits])) if refits else None,
    }


def _boot_diff_minus_const(rows: np.ndarray, const_values: list[float], n_boot=tb.N_BOOT,
                           seed=tb.SEED) -> list[float] | None:  # fmt: skip
    """Range of mean(rows) - mean(const_values), resampling rows (matches) and the
    in-sample refits independently."""
    rows = np.asarray(rows, float)
    cv = np.asarray(const_values, float)
    if len(rows) < 2 or len(cv) < 1:
        return None
    rng = np.random.default_rng(seed)
    a = rows[rng.integers(0, len(rows), (n_boot, len(rows)))].mean(axis=1)
    b = cv[rng.integers(0, len(cv), (n_boot, len(cv)))].mean(axis=1)
    d = a - b
    return [float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))]


def decompose(rows: pd.DataFrame, insample: dict[str, dict], n_boot: int = tb.N_BOOT) -> dict:
    """Per window: out-of-sample, in-sample, bias (vs close), variance, gap to early."""
    out = {"matches": int(len(rows)), "ll_close": float(rows["ll_close"].mean()),
           "ll_early": float(rows["ll_early"].mean()), "windows": {}}  # fmt: skip
    for w in WINDOWS:
        col = f"ll_w{w}"
        if col not in rows:
            continue
        ins = insample.get(str(w), {})
        oos = rows[col].to_numpy(float)
        bias = oos - rows["ll_close"].to_numpy(float)
        gap = oos - rows["ll_early"].to_numpy(float)
        rec = {
            "oos": float(oos.mean()),
            "in_recent": ins.get("ll_in_recent"),
            "in_all": ins.get("ll_in_all"),
            "n_train": ins.get("n_train"),
            "bias": float(bias.mean()),
            "bias_ci95": tb.boot_mean(bias, n_boot),
            "gap_early": float(gap.mean()),
            "gap_early_ci95": tb.boot_mean(gap, n_boot),
        }
        if ins.get("ll_in_recent") is not None:
            rec["variance"] = float(oos.mean() - ins["ll_in_recent"])
            rec["variance_ci95"] = _boot_diff_minus_const(oos, ins["ll_in_recent_values"], n_boot)
        out["windows"][str(w)] = rec
    w730 = out["windows"].get("730", {})
    b, v = w730.get("bias"), w730.get("variance")
    if b is not None and v is not None:
        out["dominant"] = "bias" if b > v else "variance"
    return out


def blend_decompose(rows: pd.DataFrame, insample: dict[str, list], n_boot=tb.N_BOOT) -> dict:
    out = {"matches": int(len(rows)), "caps": {}}
    for name, recs in insample.items():
        col = f"ll_{name}_close"
        if col not in rows:
            continue
        oos = rows[col].to_numpy(float)
        ins = [r["ll_in"] for r in recs]
        out["caps"][name] = {
            "oos_close": float(oos.mean()),
            "oos_early": float(rows[f"ll_{name}_early"].mean()),
            "in": float(np.mean(ins)) if ins else None,
            "variance": float(oos.mean() - np.mean(ins)) if ins else None,
            "variance_ci95": _boot_diff_minus_const(oos, ins, n_boot),
            "fits": len(recs),
            "mean_rows": float(np.mean([r["rows"] for r in recs])) if recs else None,
        }
    return out


def run_league(code: str, out_dir: Path, n_boot: int = tb.N_BOOT) -> dict:
    holdout = Holdout(tb.LOCK)
    matches, pins, info = tb.load_league(code, FIRST_YEAR, holdout)
    base = tb.default_for(code)
    preds, ins, stab = {}, {}, None
    for w in WINDOWS:
        cfg = tb.Config(xi=base.xi, xg_weight=base.xg_weight, window=w, min_team=base.min_team)
        p, refits = tb.walk(matches, cfg, start=tb.SCORED_FROM, keep_ratings=(w == 730),
                            holdout=holdout)  # fmt: skip
        preds[w] = p
        ins[str(w)] = _insample(refits)
        if w == 730:
            stab = stability(refits)
        print(f"{code} window {w}: {len(p)} predictions, {len(refits)} refits", flush=True)
    rows = model_rows(preds, pins)
    rows.insert(0, "league", code)
    brows, bins = blend_rows(preds[730], pins)
    brows.insert(0, "league", code)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows.to_csv(out_dir / f"model_rows_{code}.csv.gz", index=False)
    brows.to_csv(out_dir / f"blend_rows_{code}.csv.gz", index=False)
    res = {
        "league": code,
        "config": tb.config_dict(base),
        "load": info,
        "model": decompose(rows, ins, n_boot),
        "insample": {
            w: {k: v for k, v in d.items() if k != "ll_in_recent_values"} for w, d in ins.items()
        },  # fmt: skip
        "insample_values": {w: d["ll_in_recent_values"] for w, d in ins.items()},
        "blend": blend_decompose(brows, bins, n_boot),
        "blend_insample": bins,
        "stability": stab,
        "configs": len(WINDOWS) + len(BLEND_CAPS),
    }
    (out_dir / f"bias_{code}.json").write_text(json.dumps(res, default=str))
    return res


def pool(in_dir: Path, n_boot: int = tb.N_BOOT) -> dict:
    """Pooled decomposition from every league's files (in_dir searched recursively)."""
    in_dir = Path(in_dir)
    leagues = {}
    for f in sorted(in_dir.rglob("bias_*.json")):
        r = json.loads(f.read_text())
        leagues[r["league"]] = r
    mrows = pd.concat([pd.read_csv(f) for f in sorted(in_dir.rglob("model_rows_*.csv.gz"))],
                      ignore_index=True)  # fmt: skip
    brows = pd.concat([pd.read_csv(f) for f in sorted(in_dir.rglob("blend_rows_*.csv.gz"))],
                      ignore_index=True)  # fmt: skip
    # Pooled in-sample: every league's refit values together (each league's refits weigh
    # by count, as its rows do roughly).
    ins = {}
    for w in map(str, WINDOWS):
        vals = [v for r in leagues.values() for v in r["insample_values"].get(w, [])]
        ins[w] = {"ll_in_recent": float(np.mean(vals)) if vals else None,
                  "ll_in_recent_values": vals,
                  "ll_in_all": None, "n_train": None}  # fmt: skip
    bins = {}
    for r in leagues.values():
        for name, recs in r["blend_insample"].items():
            bins.setdefault(name, []).extend(recs)
    per = {
        code: {
            "dominant": r["model"].get("dominant"),
            "bias_730": r["model"]["windows"].get("730", {}).get("bias"),
            "variance_730": r["model"]["windows"].get("730", {}).get("variance"),
        }
        for code, r in leagues.items()
    }
    return {
        "kind": "tuning_bias",
        "development": True,
        "locked_from": str(tb.LOCK.date()),
        "leagues": leagues,
        "pooled": {
            "model": decompose(mrows, ins, n_boot),
            "blend": blend_decompose(brows, bins, n_boot),
        },
        "per_league": per,
        "configs": sum(r.get("configs", 0) for r in leagues.values()),
        "runs": {"tuning_bias": os.environ.get("GITHUB_RUN_ID")},
    }


def summary_lines(res: dict) -> list[str]:
    out = []

    nan = float("nan")

    def f(v):
        return nan if v is None else v

    def line(name, m):
        w = m["windows"].get("730", {})
        return (
            f"{name}: {m['matches']} matches; close {m['ll_close']:.4f}, "
            f"early {m['ll_early']:.4f}; 730 oos {f(w.get('oos')):.4f}, "
            f"in {f(w.get('in_recent')):.4f}, bias {f(w.get('bias')):+.4f}, "
            f"variance {f(w.get('variance')):+.4f} -> {m.get('dominant')}"
        )

    out.append(line("Pooled", res["pooled"]["model"]))
    for code, r in res["leagues"].items():
        out.append(line(code, r["model"]))
    for w, rec in res["pooled"]["model"]["windows"].items():
        out.append(
            f"  window {w}: oos {rec['oos']:.4f}, bias {rec['bias']:+.4f} {rec['bias_ci95']}"
        )
    for name, rec in res["pooled"]["blend"]["caps"].items():
        out.append(
            f"  blend {name}: oos close {rec['oos_close']:.4f}, "
            f"early {rec['oos_early']:.4f}, in {f(rec['in']):.4f}"
        )
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="run one league into --out")
    p.add_argument("--pool", default="", help="pool every league's files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=tb.N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        r = run_league(a.league, Path(a.out), a.boot)
        slim = {k: r[k] for k in ("league", "model", "blend", "stability")}
        print(f"TUNING_BIAS_LEAGUE {json.dumps(slim, default=str)}")
        return
    res = pool(Path(a.pool or a.out), a.boot)
    print("\n".join(summary_lines(res)))
    slim = {k: v for k, v in res.items() if k != "leagues"}
    print(f"TUNING_BIAS_JSON {json.dumps(slim, default=str)}")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "tuning_bias.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
