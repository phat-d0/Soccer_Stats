"""Corner Blend Lean: the development gate (docs/totals.md "Corner Blend Lean:
pre-registration", §4, with Amendment 1). Research lab, 12 Nov 2026; 0 credits, no key.

Implements §2 and Amendment 1 exactly:
- One row = one Pinnacle team half line at its first two-sided `look` quote, with Pinnacle's
  Shin-fair over (`fair_over`) and model (f)'s `p_model_over` as logged then.
- Outcome: over won = the team's corners > line (football-data's HC/AC; a match without a
  count yet is not settled and enters nothing).
- Blend: logit p_bet = a + b·logit(fair_over) + c·logit(p_model_over), a logistic fit on
  settled rows, pooled; σ = sd(p_bet − fair_over) on the fit rows.
- Fit leagues: SP1, D1, E1, E2, E3 only (E0, I1 and F1 are bake-off 2's locked test until
  3 Feb 2027; `LOCKED` never enters a fit, whatever the date passed in).
- Row rule (Amendment 1b, the backcast stand-in for settlement): a row enters a fit at time
  t only if its kickoff + 1 day is before t and its count is known.
- Minimum: 300 such lines from at least 150 matches; no fit, no picks.
- Timing (Amendment 1a): the first fit at the first look at which the minimum is met, then a
  refit every 28 days from that fit's date (at the first look on or after each date).

The gate (§4): c's 95% range from one fit on every eligible row now, resampling matches
(2,000 draws, seed 0). Descriptive only: the walk-forward replay of the rule (§3) on the
unlocked leagues' looks from 14 Oct: picks, strong picks, driver mix and CLV vs Pinnacle's
Shin-fair close at the same line. `--counts` prints the eligible lines and matches only
(whether results exist, not what they are).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

FIT_LEAGUES = ("SP1", "D1", "E1", "E2", "E3")
LOCKED = ("E0", "I1", "F1")  # bake-off 2's 2026/27 test, read 3 Feb 2027
START = pd.Timestamp("2026-10-14", tz="UTC")
REFIT = pd.Timedelta(days=28)
SETTLE_LAG = pd.Timedelta(days=1)
MIN_LINES, MIN_MATCHES = 300, 150
LEAN_Z, STRONG_Z = 1.0, 2.0
FRESH = pd.Timedelta(hours=3)
DRAWS, SEED = 2000, 0
EPS = 1e-6


def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# ---------- rows ----------


def look_rows(rows: list[dict]) -> pd.DataFrame:
    """Pinnacle's first two-sided look quote per (league, match, side, half line), downloaded
    before kickoff, kickoff from START; with the newest close quote before kickoff beside it."""
    look, close = {}, {}
    for r in rows:
        if (r.get("book") or "pinnacle") != "pinnacle" or r.get("market", "team_corners") != (
            "team_corners"
        ):
            continue
        try:
            line = float(r["line"])
            k, got = _ts(r["kickoff"]), _ts(r["downloaded_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if line == int(line) or got >= k or k < START:
            continue
        if r.get("over") is None or r.get("under") is None or r.get("fair_over") is None:
            continue
        key = (r.get("league"), r["home"], r["away"], k, r["side"], line)
        if r.get("snapshot") == "look":
            if r.get("p_model_over") is None:
                continue
            if key not in look or got < look[key][0]:
                look[key] = (got, r)
        elif r.get("snapshot") == "close":
            if key not in close or got > close[key][0]:
                close[key] = (got, r)
    out = []
    for key, (got, r) in look.items():
        c = close.get(key, (None, {}))[1]
        fetched = _ts(r.get("fetched_at") or r["downloaded_at"])
        out.append(
            {
                "league": key[0],
                "home": key[1],
                "away": key[2],
                "kickoff": key[3],
                "side": key[4],
                "line": key[5],
                "match": f"{key[0]}|{key[1]}|{key[2]}|{key[3].isoformat()}",
                "look_at": got,
                "age": got - fetched,
                "over": float(r["over"]),
                "under": float(r["under"]),
                "fair": float(r["fair_over"]),
                "model": float(r["p_model_over"]),
                "close_fair": float(c["fair_over"]) if c.get("fair_over") is not None else np.nan,
            }
        )
    cols = ["league", "home", "away", "kickoff", "side", "line", "match", "look_at", "age"]
    cols += ["over", "under", "fair", "model", "close_fair"]
    return pd.DataFrame(out, columns=cols)


def attach_counts(df: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """`count` = the quoted team's corners (NaN while unknown) and `won` = count > line."""
    from soccer_stats import corners_live as cl

    index = cl._result_index(results)
    df = df.copy()
    counts = []
    for r in df.to_dict("records"):
        n = cl._count(index, {**r, "kickoff": r["kickoff"].isoformat()})
        counts.append(np.nan if n is None else float(n))
    df["count"] = counts
    df["won"] = np.where(df["count"].notna(), (df["count"] > df["line"]).astype(float), np.nan)
    return df


# ---------- the fit ----------


def eligible(df: pd.DataFrame, at: pd.Timestamp) -> pd.DataFrame:
    """Rows a fit made at `at` may use: fit leagues only (never LOCKED), kickoff + 1 day
    before `at`, corner count known."""
    keep = df["league"].isin(FIT_LEAGUES) & ~df["league"].isin(LOCKED)
    keep &= (df["kickoff"] + SETTLE_LAG) < at
    keep &= df["count"].notna()
    return df[keep]


def meets_minimum(rows: pd.DataFrame) -> bool:
    return len(rows) >= MIN_LINES and rows["match"].nunique() >= MIN_MATCHES


def _design(fair, model) -> np.ndarray:
    return np.column_stack([np.ones(len(fair)), _logit(fair), _logit(model)])


def logistic(x: np.ndarray, y: np.ndarray, iters: int = 50) -> np.ndarray:
    """Maximum-likelihood logistic coefficients by Newton steps (a tiny ridge keeps the
    Hessian invertible on degenerate resamples)."""
    beta = np.zeros(x.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(x @ beta)))
        w = p * (1 - p)
        h = x.T @ (x * w[:, None]) + 1e-9 * np.eye(x.shape[1])
        step = np.linalg.solve(h, x.T @ (y - p))
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    return beta


def blend(coef, fair, model, c: float | None = None) -> np.ndarray:
    a, b, cc = coef
    cc = cc if c is None else c
    return 1 / (1 + np.exp(-(a + b * _logit(fair) + cc * _logit(model))))


def fit(rows: pd.DataFrame, at: pd.Timestamp) -> dict:
    """The blend fitted on `rows` (already eligible at `at`)."""
    coef = logistic(_design(rows["fair"], rows["model"]), rows["won"].to_numpy(float))
    p = blend(coef, rows["fair"], rows["model"])
    return {
        "fit_at": at.isoformat(),
        "a": float(coef[0]),
        "b": float(coef[1]),
        "c": float(coef[2]),
        "sigma": float(np.std(p - rows["fair"].to_numpy(float), ddof=1)),
        "lines": int(len(rows)),
        "matches": int(rows["match"].nunique()),
    }


def c_range(rows: pd.DataFrame, draws: int = DRAWS, seed: int = SEED) -> list[float]:
    """c's 95% range, resampling matches."""
    groups = [g for _, g in rows.groupby("match")]
    rng = np.random.default_rng(seed)
    cs = []
    for _ in range(draws):
        idx = rng.integers(0, len(groups), len(groups))
        s = pd.concat([groups[i] for i in idx])
        cs.append(logistic(_design(s["fair"], s["model"]), s["won"].to_numpy(float))[2])
    lo, hi = np.percentile(cs, [2.5, 97.5])
    return [float(lo), float(hi)]


# ---------- the rule, replayed ----------


def pick(row: dict, f: dict) -> dict | None:
    """§3 on one look row: the side with the larger z if z ≥ 1 (raw Pinnacle prices)."""
    if row["age"] > FRESH or row["look_at"] >= row["kickoff"]:
        return None
    coef = (f["a"], f["b"], f["c"])
    p = float(blend(coef, row["fair"], row["model"]))
    pm = float(blend(coef, row["fair"], row["model"], c=0.0))
    s = f["sigma"]
    z = {"over": (p - 1 / row["over"]) / s, "under": ((1 - p) - 1 / row["under"]) / s}
    zm = {"over": (pm - 1 / row["over"]) / s, "under": ((1 - pm) - 1 / row["under"]) / s}
    side = max(z, key=z.get)
    if z[side] < LEAN_Z:
        return None
    odds = row["over"] if side == "over" else row["under"]
    cf = row["close_fair"]
    fair_close = cf if side == "over" else 1 - cf
    return {
        "match": row["match"],
        "league": row["league"],
        "side_team": row["side"],
        "line": row["line"],
        "bet": side,
        "odds": odds,
        "p_bet": p if side == "over" else 1 - p,
        "z": z[side],
        "z_market": zm[side],
        "strong": z[side] >= STRONG_Z,
        "driver": "market" if zm[side] >= LEAN_Z else "model",
        "clv": odds * fair_close - 1 if not np.isnan(cf) else np.nan,
        "fit_at": f["fit_at"],
    }


def schedule(df: pd.DataFrame) -> list[dict]:
    """Amendment 1a on the look times: the first fit at the first look where the minimum is
    met, then a refit at the first look on or after each 28-day date from that fit."""
    fits, due = [], None
    for t in sorted(df["look_at"].unique()):
        t = _ts(t)
        if due is not None and t < due:
            continue
        rows = eligible(df, t)
        if not meets_minimum(rows):
            continue
        fits.append(fit(rows, t))
        due = (due or t) + REFIT
        while due <= t:
            due += REFIT
    return fits


def replay(df: pd.DataFrame, leagues=FIT_LEAGUES) -> tuple[list[dict], list[dict]]:
    """The walk-forward picks (descriptive): each look row in `leagues` priced by the latest
    fit made at or before its look; one bet per match × team, the line with the largest z."""
    fits = schedule(df)
    if not fits:
        return fits, []
    times = [_ts(f["fit_at"]) for f in fits]
    picks = []
    for row in df[df["league"].isin(leagues)].sort_values("look_at").to_dict("records"):
        live = [f for f, t in zip(fits, times, strict=True) if t <= row["look_at"]]
        if live:
            p = pick(row, live[-1])
            if p:
                picks.append(p)
    best: dict[tuple, dict] = {}
    for p in picks:
        k = (p["match"], p["side_team"])
        if k not in best or p["z"] > best[k]["z"]:
            best[k] = p
    return fits, list(best.values())


def _boot_mean(vals: pd.DataFrame, col: str, draws: int = DRAWS, seed: int = SEED):
    vals = vals[vals[col].notna()]
    groups = [g[col].to_numpy(float) for _, g in vals.groupby("match")]
    if len(groups) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(draws):
        idx = rng.integers(0, len(groups), len(groups))
        means.append(np.concatenate([groups[i] for i in idx]).mean())
    lo, hi = np.percentile(means, [2.5, 97.5])
    return [float(lo), float(hi)]


def summary(picks: list[dict], draws: int = DRAWS) -> dict:
    if not picks:
        return {"bets": 0}
    p = pd.DataFrame(picks)
    have = p[p["clv"].notna()]
    return {
        "bets": int(len(p)),
        "matches": int(p["match"].nunique()),
        "strong": int(p["strong"].sum()),
        "drivers": {k: int(v) for k, v in p["driver"].value_counts().items()},
        "sides": {k: int(v) for k, v in p["bet"].value_counts().items()},
        "avg_odds": float(p["odds"].mean()),
        "avg_z": float(p["z"].mean()),
        "clv_bets": int(len(have)),
        "clv": float(have["clv"].mean()) if len(have) else None,
        "clv_ci95": _boot_mean(p, "clv", draws),
        "by_driver": {
            d: {
                "bets": int(len(g)),
                "clv": float(g["clv"].mean()) if g["clv"].notna().any() else None,
            }
            for d, g in p.groupby("driver")
        },
        "by_league": {lg: int(len(g)) for lg, g in p.groupby("league")},
    }


# ---------- the read ----------


def counts(df: pd.DataFrame, now: pd.Timestamp) -> dict:
    rows = eligible(df, now)
    return {
        "now": now.isoformat(),
        "lines": int(len(rows)),
        "matches": int(rows["match"].nunique()),
        "minimum": {"lines": MIN_LINES, "matches": MIN_MATCHES},
        "met": bool(meets_minimum(rows)),
        "by_league": {lg: int(len(g)) for lg, g in rows.groupby("league")},
    }


def gate(df: pd.DataFrame, now: pd.Timestamp, draws: int = DRAWS) -> dict:
    out = {"counts": counts(df, now), "fit_leagues": list(FIT_LEAGUES), "locked": list(LOCKED)}
    rows = eligible(df, now)
    if not meets_minimum(rows):
        out["verdict"] = "minimum not met"
        return out
    f = fit(rows, now)
    ci = c_range(rows, draws)
    f["c_ci95"] = ci
    out["fit"] = f
    out["verdict"] = "c above 0" if ci[0] > 0 else "c not above 0"
    fits, picks = replay(df)
    out["replay"] = {
        "descriptive": True,
        "fits": fits,
        "picks": summary(picks, draws),
        "note": "Walk-forward on the unlocked leagues' looks from 14 Oct; fits under "
        "Amendment 1 (first fit when the minimum is met, then every 28 days; rows with kickoff "
        "+ 1 day before the fit). CLV = entry odds × Pinnacle's Shin-fair close at the same "
        "line − 1. Never judged.",
    }
    return out


def load(log_dir: Path) -> pd.DataFrame:
    from soccer_stats import corners_live as cl
    from soccer_stats.data import current_season, download

    df = look_rows(cl.load_rows(Path(log_dir)))
    year = current_season()
    for code in FIT_LEAGUES:  # never the locked leagues' results
        try:
            download(code, year)
        except Exception:  # noqa: BLE001 - that league's rows then stay unsettled
            pass
    return attach_counts(df, cl.corner_results(FIT_LEAGUES, seasons=[year]))


def report(res: dict) -> str:
    c = res["counts"]
    out = [
        "== Corner Blend Lean: development gate (docs/totals.md §4, Amendment 1) ==",
        f"Eligible now: {c['lines']} lines, {c['matches']} matches (minimum {MIN_LINES} / "
        f"{MIN_MATCHES}: {'met' if c['met'] else 'not met'}); by league {c['by_league']}",
    ]
    if "fit" in res:
        f = res["fit"]
        lo, hi = f["c_ci95"]
        out.append(
            f"Fit: a {f['a']:+.3f}, b {f['b']:.3f}, c {f['c']:+.3f} ({lo:+.3f} to {hi:+.3f}), "
            f"σ {f['sigma']:.4f}"
        )
        s = res["replay"]["picks"]
        out.append(f"Replay (descriptive): {len(res['replay']['fits'])} fits; {s}")
    out.append(f"VERDICT {res.get('verdict')}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m soccer_stats.lab.corner_lean")
    ap.add_argument("--log-dir", required=True, help="folder holding the corner price logs")
    ap.add_argument("--counts", action="store_true", help="eligible counts only, no fit")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    df = load(Path(args.log_dir))
    now = pd.Timestamp.now(tz="UTC")
    res = {"counts": counts(df, now)} if args.counts else gate(df, now)
    print(report(res) if not args.counts else json.dumps(res["counts"]))
    text = json.dumps(res, default=float)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("CORNER_LEAN_GATE_JSON " + text)


if __name__ == "__main__":
    main()
