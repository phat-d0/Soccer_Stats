"""Corner dispersion candidate f-oos beside model (f) (docs/totals.md "Corner dispersion:
candidate f-oos", pre-registered by the corners team before any 2025/26 read).

Development (2017/18-2023/24, descriptive): NB2 dispersion, PIT, 50%/90% coverage and the
summed binary log loss at team half lines 2.5-8.5. The judged read (2025/26, once, with a
logged reason, only after development is recorded): the per-match summed log loss of both
teams' half lines, f-oos minus (f), pooled over the eight corner leagues, 95% range from 2,000
match draws (seed 0); pass = the range below 0. 2024/25 is training only; 2026/27 is never
loaded. No key, 0 credits.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import nbinom

from soccer_stats.lab.harness import Holdout

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
LINES = (2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5)
DEV_FROM, LOCK = pd.Timestamp("2017-07-01"), pd.Timestamp("2024-07-01")
JUDGED_FROM, JUDGED_TO = pd.Timestamp("2025-07-01"), pd.Timestamp("2026-07-01")
N_BOOT, SEED = 2000, 0
EPS = 1e-12
REASON = ("Corner dispersion f-oos judged read on 2025/26, pre-registered in docs/totals.md "
          "by the corners team; development recorded first; lead's direction "
          "8 Nov 2026.")  # fmt: skip


def _nb(m, a):
    n = 1.0 / np.maximum(np.asarray(a, float), 1e-9)
    return n, n / (n + np.asarray(m, float))


def stacked(cw: pd.DataFrame, tag: str) -> pd.DataFrame:
    """One row per team-match: match key, count y, mean m, alpha a (model columns `tag`)."""
    key = cw["date"].astype(str) + "|" + cw["home"].astype(str) + "|" + cw["away"].astype(str)
    parts = []
    for side in ("home", "away"):
        parts.append(pd.DataFrame({"match": key, "league": cw["league"], "side": side,
                                   "y": cw[f"{side}_corners"].astype(float),
                                   "m": cw[f"m_{side}"].astype(float),
                                   "a": cw[f"alpha_{side}"].astype(float),
                                   "model": tag}))  # fmt: skip
    return pd.concat(parts, ignore_index=True)


def row_scores(s: pd.DataFrame) -> pd.DataFrame:
    """Per team-match: dispersion term, PIT (mid), inside 50%/90%, summed half-line log loss."""
    y, m, a = s["y"].to_numpy(float), s["m"].to_numpy(float), s["a"].to_numpy(float)
    n, p = _nb(m, a)
    var = m + a * m**2
    cdf_y = nbinom.cdf(y, n, p)
    pmf_y = nbinom.pmf(y, n, p)
    out = s[["match", "league", "side", "model"]].copy()
    out["disp"] = (y - m) ** 2 / var
    out["pit"] = cdf_y - 0.5 * pmf_y
    for lvl in (0.5, 0.9):
        lo, hi = nbinom.ppf((1 - lvl) / 2, n, p), nbinom.ppf((1 + lvl) / 2, n, p)
        out[f"in{int(lvl * 100)}"] = ((y >= lo) & (y <= hi)).astype(float)
    ll = np.zeros(len(s))
    for line in LINES:
        p_over = np.clip(1 - nbinom.cdf(np.floor(line), n, p), EPS, 1 - EPS)
        over = (y > line).astype(float)
        ll += -(over * np.log(p_over) + (1 - over) * np.log(1 - p_over))
    out["ll_lines"] = ll
    return out


def walk_both(cm: pd.DataFrame, start, end) -> pd.DataFrame:
    """(f) and f-oos walk-forward on identical blocks; keeps rows both predict."""
    from soccer_stats.edge.corner_alpha import CandidateFOos
    from soccer_stats.lab.model_assumptions import corner_walk

    keys = ["league", "date", "home", "away"]
    f = corner_walk(cm, start, end)
    o = corner_walk(cm, start, end, model=CandidateFOos)
    if f.empty or o.empty:
        return pd.DataFrame()
    cols = ["m_home", "m_away", "alpha_home", "alpha_away"]
    o = o[keys + cols].rename(columns={c: f"o_{c}" for c in cols})
    return f.merge(o, on=keys, how="inner")


def scored(both: pd.DataFrame) -> pd.DataFrame:
    """Row scores for both models, one row per team-match and model."""
    f = stacked(both, "f")
    o = both.copy()
    for c in ("m_home", "m_away", "alpha_home", "alpha_away"):
        o[c] = o[f"o_{c}"]
    return pd.concat([row_scores(f), row_scores(stacked(o, "f-oos"))], ignore_index=True)


def _boot(v: np.ndarray, groups: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED):
    codes, idx = np.unique(groups, return_inverse=True)
    sums = np.bincount(idx, weights=v, minlength=len(codes))
    cnt = np.bincount(idx, minlength=len(codes)).astype(float)
    rng = np.random.default_rng(seed)
    d = rng.integers(0, len(codes), size=(n_boot, len(codes)))
    means = sums[d].sum(1) / cnt[d].sum(1)
    return float(v.mean()), [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def summary(rs: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """Per model: dispersion, PIT deciles, coverage, mean per-match half-line log loss; and the
    paired per-match difference f-oos - (f) with its range."""
    out = {}
    for model, g in rs.groupby("model"):
        pit = np.clip(g["pit"].to_numpy(float), 0, 1 - 1e-9)
        dec = np.bincount((pit * 10).astype(int), minlength=10) / len(pit)
        per_match = g.groupby("match")["ll_lines"].sum()
        out[model] = {"team_matches": int(len(g)), "dispersion": float(g["disp"].mean()),
                      "pit_deciles": [round(float(x), 4) for x in dec],
                      "coverage_50": float(g["in50"].mean()),
                      "coverage_90": float(g["in90"].mean()),
                      "ll_lines_per_match": float(per_match.mean())}  # fmt: skip
    a = rs[rs["model"] == "f"].groupby("match")["ll_lines"].sum()
    b = rs[rs["model"] == "f-oos"].groupby("match")["ll_lines"].sum()
    j = pd.concat([a.rename("f"), b.rename("o")], axis=1).dropna()
    d = (j["o"] - j["f"]).to_numpy(float)
    m_, ci = _boot(d, j.index.to_numpy(), n_boot)
    out["matches"] = int(len(j))
    out["diff_foos_minus_f"] = m_
    out["diff_ci95"] = ci
    return out


def run_league(code: str, out_dir: Path, mode: str) -> dict:
    from soccer_stats.corners_live import corner_matches
    from soccer_stats.data import download

    if mode == "dev":
        years, start, end = range(DEV_FROM.year - 2, LOCK.year), DEV_FROM, LOCK
        holdout = Holdout(LOCK)
    else:
        years, start, end = (
            range(JUDGED_FROM.year - 3, JUDGED_FROM.year + 1),
            JUDGED_FROM,
            JUDGED_TO,
        )
        holdout = Holdout(JUDGED_FROM)
        holdout.unlock(REASON)
    for y in years:  # cached reads; nothing at or after 2026-07-01 is in these seasons
        try:
            download(code, y)
        except Exception:  # noqa: BLE001 - a missing season is reported by its row count
            pass
    cm = corner_matches(code, seasons=years)
    cm = cm[pd.to_datetime(cm["date"]) < end]
    holdout.check(cm["date"])
    both = walk_both(cm, start, end)
    d = pd.to_datetime(both["date"]) if len(both) else pd.Series(dtype="datetime64[ns]")
    if len(both) and ((d < start) | (d >= end)).any():
        raise RuntimeError("a row outside the scored window was predicted")
    both["league"] = code
    rs = scored(both) if len(both) else pd.DataFrame()
    out_dir.mkdir(parents=True, exist_ok=True)
    rs.to_csv(out_dir / f"foos_{mode}_{code}.csv.gz", index=False)
    info = {"league": code, "mode": mode, "matches": int(len(both)),
            "seasons_loaded": [int(y) for y in years]}  # fmt: skip
    (out_dir / f"foos_{mode}_{code}.json").write_text(json.dumps(info))
    return info


def pool(in_dir: Path, mode: str, n_boot: int = N_BOOT) -> dict:
    parts = []
    for f in sorted(Path(in_dir).rglob(f"foos_{mode}_*.csv.gz")):
        try:
            parts.append(pd.read_csv(f))
        except pd.errors.EmptyDataError:
            continue
    rs = pd.concat(parts, ignore_index=True)
    res = {"kind": "corner_foos", "mode": mode, "pooled": summary(rs, n_boot),
           "leagues": {lg: summary(g, n_boot) for lg, g in rs.groupby("league")},
           "runs": {"corner_foos": os.environ.get("GITHUB_RUN_ID")}}  # fmt: skip
    if mode == "judged":
        ci = res["pooled"]["diff_ci95"]
        res["passes"] = bool(ci and ci[1] < 0)
        res["verdict"] = "pass" if res["passes"] else "fail"
        res["reason"] = REASON
    return res


def summary_lines(res: dict) -> list[str]:
    p = res["pooled"]
    out = [f"f-oos {res['mode']}: {p['matches']} matches; per-match half-line log loss "
           f"(f) {p['f']['ll_lines_per_match']:.4f}, f-oos {p['f-oos']['ll_lines_per_match']:.4f}, "
           f"diff {p['diff_foos_minus_f']:+.5f} {p['diff_ci95']}"
           + (f" -> {res['verdict'].upper()}" if "verdict" in res else "")]  # fmt: skip
    for m in ("f", "f-oos"):
        b = p[m]
        out.append(f"  {m}: dispersion {b['dispersion']:.3f}, cover50 {b['coverage_50']:.3f}, "
                   f"cover90 {b['coverage_90']:.3f}, PIT deciles {b['pit_deciles']}")  # fmt: skip
    for lg, b in res["leagues"].items():
        out.append(f"  {lg}: {b['matches']} matches, diff {b['diff_foos_minus_f']:+.5f} "
                   f"{b['diff_ci95']}; disp f {b['f']['dispersion']:.3f} "
                   f"f-oos {b['f-oos']['dispersion']:.3f}")  # fmt: skip
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=("dev", "judged"), default="dev")
    p.add_argument("--league", default="", help="one league into --out")
    p.add_argument("--pool", default="", help="pool the files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        print(f"FOOS_LEAGUE {json.dumps(run_league(a.league, Path(a.out), a.mode))}")
        return
    res = pool(Path(a.pool or a.out), a.mode, a.boot)
    print("\n".join(summary_lines(res)))
    print(f"FOOS_{a.mode.upper()}_JSON {json.dumps(res, default=str)}")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / f"corner_foos_{a.mode}.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
