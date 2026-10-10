"""Team-corner recalibration, round 12 (docs/totals.md, "Team-corner recalibration").

Pre-registered before any 2025/26 corner outcome was read. Per league, the 2024/25
holdout finalist from the corners bake-off (unchanged walk-forward) gives each team's
over 3.5/4.5/5.5 corner chances. One logistic recalibration per league, pooled across
the six team lines, logit p = a + b*logit(p_model), is fitted on 2017/18-2024/25 and
applied unchanged to 2025/26. Raw and recalibrated are scored side by side against the
league average with the bake-off's pass rule. Every run that does not open 2025/26 drops
it before anything is computed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import expit

from soccer_stats.edge import corners
from soccer_stats.edge.corners import LEAGUES, SLOPE_BAND, TEAM_LINES
from soccer_stats.edge.totals import N_BOOT, over_from_pmf, recal_slope, score_line
from soccer_stats.lab import metrics
from soccer_stats.lab.harness import Holdout

TEST_SEASON = 2025  # 2025/26
TEST_START = pd.Timestamp("2025-07-01")
TEST_END = pd.Timestamp("2026-07-01")
FIRST_FIT = 2017
FINALISTS = {"E0": "d", "SP1": "c", "D1": "d", "I1": "c", "F1": "d", "E1": "d"}
TESTS = 2 * len(LEAGUES)  # raw and recalibrated in each league: 12
LINES = [(side, part, line) for side, part in (("home", 0), ("away", 1)) for line in TEAM_LINES]


# ---------- coverage (counts only) ----------


def coverage_counts(raw: pd.DataFrame) -> dict:
    """Played matches and how many carry both corner counts (HC and AC)."""
    played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    has = pd.Series(True, index=played.index)
    for col in ("HC", "AC"):
        has &= pd.to_numeric(played[col], errors="coerce").notna() if col in played else False
    n = int(len(played))
    k = int(has.sum())
    return {"played": n, "with_corners": k, "share": round(k / n, 4) if n else None}


def coverage(leagues=LEAGUES, season: int = TEST_SEASON) -> dict:
    from soccer_stats.data import download

    out = {}
    for lg in leagues:
        raw = pd.read_csv(download(lg, season), encoding="latin-1", on_bad_lines="skip")
        out[lg] = coverage_counts(raw)
    return out


# ---------- recalibration ----------


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def fit(p, y) -> tuple[float, float]:
    """Maximum-likelihood logistic fit of y on logit(p): returns (a, b)."""
    X = np.column_stack([np.ones(len(p)), _logit(p)])
    y = np.asarray(y, float)
    w = np.array([0.0, 1.0])
    for _ in range(50):
        mu = expit(X @ w)
        h = (X * (mu * (1 - mu))[:, None]).T @ X + 1e-9 * np.eye(2)
        step = np.linalg.solve(h, X.T @ (y - mu))
        w += step
        if np.abs(step).max() < 1e-10:
            break
    return float(w[0]), float(w[1])


def apply(p, coef) -> np.ndarray:
    a, b = coef
    return expit(a + b * _logit(p))


def long_rows(data: pd.DataFrame, pred: dict, cand: str) -> pd.DataFrame:
    """One row per match and team line: the finalist's raw chance, the league average's,
    the outcome, the match (bootstrap group) and the season."""
    idx = pred["index"]
    d = data.loc[idx]
    counts = {0: d["home_corners"].to_numpy(int), 1: d["away_corners"].to_numpy(int)}
    out = []
    for side, part, line in LINES:
        out.append(
            pd.DataFrame(
                {
                    "line": f"{side}_{line}",
                    "p_raw": over_from_pmf(pred[cand][part], line),
                    "p_base": over_from_pmf(pred["a"][part], line),
                    "y": (counts[part] > line).astype(int),
                    "match": d["match"].to_numpy(),
                    "season": d["season_start"].to_numpy(int),
                    "row": np.arange(len(d)),
                }
            )
        )
    return pd.concat(out, ignore_index=True)


def score(rows: pd.DataFrame, col: str, level: float) -> dict:
    """Each line (score_line vs the league average) and the group: per-match mean gain
    across the six lines with its range at `level`, the slopes and the pass rule."""
    lines = {}
    gains = []
    for name, x in rows.groupby("line", sort=False):
        s = score_line(x[col], x["p_base"], x["y"], x["match"], level)
        s["brier"] = float(((x[col] - x["y"]) ** 2).mean())
        s["baseline_brier"] = float(((x["p_base"] - x["y"]) ** 2).mean())
        lines[name] = s
        two = lambda p: np.column_stack([p, 1 - p])  # noqa: E731
        yy = 1 - x["y"].to_numpy()
        g = metrics.log_loss_rows(two(x["p_base"].to_numpy()), yy) - metrics.log_loss_rows(
            two(x[col].to_numpy()), yy
        )
        gains.append(pd.Series(g, index=x["row"].to_numpy()))
    per_match = pd.concat(gains, axis=1).mean(axis=1)
    groups = rows.drop_duplicates("row").set_index("row").loc[per_match.index, "match"]
    rng = metrics.boot_range(per_match.to_numpy(), groups.to_numpy(), n_boot=N_BOOT, level=level)
    slopes = {k: v["slope"] for k, v in lines.items()}
    in_band = all(SLOPE_BAND[0] <= s <= SLOPE_BAND[1] for s in slopes.values())
    return {
        "matches": int(len(per_match)),
        "gain": float(per_match.mean()),
        "gain_range": rng,
        "slopes": slopes,
        "slopes_in_band": in_band,
        "pass": bool(rng is not None and rng[0] > 0 and in_band),
        "lines": lines,
    }


def development_view(rows: pd.DataFrame, level: float = 0.95) -> dict:
    """Descriptive only: each season from FIRST_FIT + 1 recalibrated with a fit on the
    seasons before it; raw and recalibrated scored on those seasons together."""
    seasons = sorted(rows["season"].unique())
    parts, coefs = [], {}
    for s in seasons:
        if s <= FIRST_FIT:
            continue
        train = rows[rows["season"] < s]
        coef = fit(train["p_raw"], train["y"])
        coefs[int(s)] = coef
        x = rows[rows["season"] == s].copy()
        x["p_recal"] = apply(x["p_raw"], coef)
        parts.append(x)
    if not parts:
        return {}
    dev = pd.concat(parts, ignore_index=True)
    return {
        "seasons": [int(s) for s in sorted(dev["season"].unique())],
        "coefs": coefs,
        "raw": score(dev, "p_raw", level),
        "recal": score(dev, "p_recal", level),
        "slope_raw_pooled": recal_slope(dev["p_raw"], dev["y"]),
    }


def run(df: pd.DataFrame, league: str, level: float, open_reason: str | None = None) -> dict:
    """Development view (open_reason None; 2025/26 dropped first) or the 2025/26 test."""
    cand = FINALISTS[league]
    holdout = Holdout(TEST_START)
    if open_reason is None:
        df = df[df["date"] < TEST_START]
        end = TEST_START
    else:
        df = df[df["date"] < TEST_END]
        holdout.unlock(open_reason)
        end = TEST_END
    data = corners.frame(df.sort_values("date").reset_index(drop=True))
    pred = corners.predictions(data, corners.DEV_START, end, holdout)
    rows = long_rows(data, pred, cand)
    res: dict = {"league": league, "finalist": cand, "name": corners.NAMES[cand], "level": level}
    res["development"] = development_view(rows[rows["season"] < TEST_SEASON])
    if open_reason is not None:
        train = rows[rows["season"] < TEST_SEASON]
        coef = fit(train["p_raw"], train["y"])
        test = rows[rows["season"] == TEST_SEASON].copy()
        test["p_recal"] = apply(test["p_raw"], coef)
        res["fit"] = {"a": coef[0], "b": coef[1], "rows": int(len(train))}
        res["test"] = {
            "season": TEST_SEASON,
            "raw": score(test, "p_raw", level),
            "recal": score(test, "p_recal", level),
        }
        res["holdout_log"] = holdout.events
    return res


# ---------- report ----------


def _rng(v, f="{:+.4f}"):
    return f"{f.format(v[0])}..{f.format(v[1])}" if v else "–"


def _group(label: str, g: dict) -> list[str]:
    out = [
        f"  {label}: {g['matches']} matches, gain {g['gain']:+.4f} ({_rng(g['gain_range'])}), "
        f"slopes {min(g['slopes'].values()):.2f}-{max(g['slopes'].values()):.2f}, "
        f"in band {g['slopes_in_band']}: PASS {g['pass']}"
    ]
    for name, s in g["lines"].items():
        out.append(
            f"    {name:>9}: over {s['over_rate']:.3f} vs predicted {s['predicted']:.3f}, "
            f"slope {s['slope']:.2f} ({_rng(s['slope_range'], '{:.2f}')}), gain {s['gain']:+.4f}, "
            f"Brier {s['brier']:.4f} (baseline {s['baseline_brier']:.4f})"
        )
    return out


def report(res: dict) -> str:
    out = [
        f"== {res['league']}: team-corner recalibration, finalist ({res['finalist']}) "
        f"{res['name']} (level {res['level']:.3%}) =="
    ]
    out += res.get("holdout_log", [])
    dev = res.get("development") or {}
    if dev:
        coefs = ", ".join(f"{s}: a {a:+.3f} b {b:.3f}" for s, (a, b) in dev["coefs"].items())
        out.append(f"Development view (descriptive, 95%), seasons {dev['seasons']}; fits {coefs}")
        out += _group("raw", dev["raw"])
        out += _group("recalibrated", dev["recal"])
    if "test" in res:
        f = res["fit"]
        out.append(
            f"Fit on 2017/18-2024/25 ({f['rows']} rows): a {f['a']:+.3f}, b {f['b']:.3f}. "
            f"Test {TEST_SEASON}/{(TEST_SEASON + 1) % 100:02d}:"
        )
        out += _group("raw", res["test"]["raw"])
        out += _group("recalibrated", res["test"]["recal"])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    from pathlib import Path

    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.corner_recal")
    ap.add_argument("--coverage-only", action="store_true")
    ap.add_argument("--league", default="E0", choices=LEAGUES)
    ap.add_argument("--reason", default="")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    if args.coverage_only:
        res = coverage()
        for lg, r in res.items():
            n, k = r["played"], r["with_corners"]
            print(f"{lg} 2025/26: {n} played, {k} with HC and AC ({r['share']})")
        print("COVERAGE_JSON " + json.dumps(res))
        return
    reason = args.reason.strip() or None
    cut = TEST_END if reason else TEST_START
    df = corners.load(args.league, cut, last=TEST_SEASON if reason else corners.LAST_DATA)
    res = run(df, args.league, bonferroni_level(TESTS), reason)
    print(report(res))
    text = json.dumps(_jsonable(res), default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("RECAL_JSON " + text)


if __name__ == "__main__":
    main()
