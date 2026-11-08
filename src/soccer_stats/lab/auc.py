"""ROC AUC for the match model, the blend, the prices, corners model (f) and goalscorer B.

Owner via the lead, 8 Nov; exploratory, 0 credits, no key (docs/lab.md "ROC AUC"). AUC is the
chance that a random outcome that happened was given a higher chance than a random one that
didn't: 0.5 is a coin, 1 is perfect. It measures ranking only, not calibration.

Every series in a comparison is scored on the same rows, one row per outcome (the
calibration.outcome_rows shape), with 95% ranges from one match-level bootstrap (2,000 draws,
seed 0) shared by every series, so differences are paired.

1. Match 1X2, eight leagues, 2019/20-2023/24 (residual_map's rows; 2024/25 on locked):
   model, blend (p_bet), Pinnacle's Shin-fair early and close; on the matches the bought
   ten-book history covers (2020/21-2023/24), DraftKings' Shin-fair price at the 48 h look too.
2. "Does the model add anything?": the AUC of the model (and the blend, and model minus
   price) within 5-point bands of Pinnacle's early fair chance, averaged by band size.
3. Corners model (f): the live log's newest pre-kickoff half lines, model vs each book's fair
   over, in the leagues outside bake-off 2's locked test (SP1, D1, E1, E2, E3).
4. Goalscorer B: the 2025/26 development rows (goalscorer-dev's judged_<league>.csv.gz): B,
   season goals, season xG and the best bookmaker's price (1/odds), starters only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

N_BOOT = 2000
SEED = 0
CHUNK = 50
LEVEL = 0.95
BAND = 0.05  # 5-point bands of the early fair chance
MIN_BAND = 50  # rows (and both classes) for a band to count
LOCKED_CORNER_LEAGUES = ("E0", "I1", "F1")  # bake-off 2's 2026/27 test, read 3 Feb 2027
CORNER_LEAGUES = ("SP1", "D1", "E1", "E2", "E3")
B64_CHUNK = 60000
NOTE = (
    "ROC AUC: the chance that a random outcome that happened had been given a higher chance "
    "than a random one that didn't (0.5 = a coin, 1 = perfect). It measures ranking only, "
    "not calibration. Every series in a block is scored on the same rows, one per outcome; "
    "95% ranges resample matches (2,000 draws, seed 0), shared by every series, so the "
    "differences are paired. Exploratory: no verdict, no bets."
)


# --- AUC with match-level bootstrap weights -------------------------------------------


class Ranked:
    """One score column's sort order and tie groups, reused for every bootstrap draw."""

    def __init__(self, score, y, match_idx):
        score = np.asarray(score, dtype=float)
        order = np.argsort(score, kind="mergesort")
        s = score[order]
        self.y = np.asarray(y, dtype=float)[order]
        self.m = np.asarray(match_idx)[order]
        self.starts = np.flatnonzero(np.r_[True, s[1:] != s[:-1]])

    def auc(self, counts: np.ndarray) -> np.ndarray:
        """AUC per row of `counts` (draws x matches: how often each match was drawn)."""
        w = counts[:, self.m]
        pos = np.add.reduceat(w * self.y, self.starts, axis=1)
        neg = np.add.reduceat(w * (1 - self.y), self.starts, axis=1)
        below = np.cumsum(neg, axis=1) - neg
        num = (pos * (below + 0.5 * neg)).sum(axis=1)
        den = pos.sum(axis=1) * neg.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return num / den


def auc(score, y) -> float | None:
    """Plain AUC (ties count half); None without both classes."""
    y = np.asarray(y, dtype=float)
    if y.min(initial=1) == y.max(initial=0):
        return None
    r = Ranked(score, y, np.zeros(len(y), dtype=int))
    return float(r.auc(np.ones((1, 1)))[0])


def draws(n_matches: int, n_boot: int = N_BOOT, seed: int = SEED, chunk: int = CHUNK):
    """Match multiplicities, `chunk` draws at a time; the same stream for every caller with
    the same seed, so blocks built on the same matches are paired."""
    rng = np.random.default_rng(seed)
    done = 0
    while done < n_boot:
        k = min(chunk, n_boot - done)
        idx = rng.integers(0, n_matches, size=(k, n_matches))
        out = np.zeros((k, n_matches))
        for i in range(k):
            out[i] = np.bincount(idx[i], minlength=n_matches)
        yield out
        done += k


def _ci(v: np.ndarray) -> list[float] | None:
    v = v[np.isfinite(v)]
    if len(v) < 10:
        return None
    lo, hi = np.percentile(v, [100 * (1 - LEVEL) / 2, 100 * (1 + LEVEL) / 2])
    return [round(float(lo), 4), round(float(hi), 4)]


def compare(
    df: pd.DataFrame,
    series: dict[str, str],
    groups: dict[str, np.ndarray],
    diffs: list[tuple[str, str]],
    n_boot: int = N_BOOT,
) -> dict:
    """AUC per group and series with ranges, and paired differences. `df` has `won` and
    `match`; `series` maps a name to a column; `groups` maps a name to a row mask."""
    codes, midx = np.unique(df["match"].to_numpy(), return_inverse=True)
    y = df["won"].to_numpy(dtype=float)
    ranked, point = {}, {}
    for g, mask in groups.items():
        if mask.sum() == 0 or y[mask].min() == y[mask].max():
            continue
        for s, col in series.items():
            r = Ranked(df[col].to_numpy(dtype=float)[mask], y[mask], midx[mask])
            ranked[(g, s)] = r
            point[(g, s)] = float(r.auc(np.ones((1, len(codes))))[0])
    boots = {k: [] for k in ranked}
    for counts in draws(len(codes), n_boot):
        for k, r in ranked.items():
            boots[k].append(r.auc(counts))
    boots = {k: np.concatenate(v) for k, v in boots.items()}
    out = {}
    for g, mask in groups.items():
        if (g, next(iter(series))) not in ranked:
            out[g] = {"rows": int(mask.sum()), "error": "fewer than two classes"}
            continue
        block = {
            "rows": int(mask.sum()),
            "matches": int(len(np.unique(midx[mask]))),
            "won": int(y[mask].sum()),
            "auc": {s: round(point[(g, s)], 4) for s in series},
            "auc_ci95": {s: _ci(boots[(g, s)]) for s in series},
            "diff": {},
        }
        for a, b in diffs:
            if (g, a) in ranked and (g, b) in ranked:
                block["diff"][f"{a} - {b}"] = {
                    "value": round(point[(g, a)] - point[(g, b)], 4),
                    "ci95": _ci(boots[(g, a)] - boots[(g, b)]),
                }
        out[g] = block
    return out


def within_bands(
    df: pd.DataFrame, price: str, series: dict[str, str], n_boot: int = N_BOOT
) -> dict:
    """Each series' AUC inside 5-point bands of `price`, averaged by band size (the bands
    with >= MIN_BAND rows and both classes), with a paired match bootstrap. 0.5 = no ranking
    beyond the price."""
    codes, midx = np.unique(df["match"].to_numpy(), return_inverse=True)
    y = df["won"].to_numpy(dtype=float)
    band = np.floor(df[price].to_numpy(dtype=float) / BAND).astype(int)
    bands = []
    for b in np.unique(band):
        m = band == b
        if m.sum() >= MIN_BAND and 0 < y[m].sum() < m.sum():
            bands.append((b, m))
    if not bands:
        return {"error": "no band with enough rows"}
    sizes = np.array([m.sum() for _, m in bands], dtype=float)
    wts = sizes / sizes.sum()
    ranked = {
        s: [Ranked(df[col].to_numpy(dtype=float)[m], y[m], midx[m]) for _, m in bands]
        for s, col in series.items()
    }
    ones = np.ones((1, len(codes)))
    point = {s: float(sum(w * r.auc(ones)[0] for w, r in zip(wts, rs, strict=True)))
             for s, rs in ranked.items()}  # fmt: skip
    boots = {s: [] for s in series}
    for counts in draws(len(codes), n_boot):
        for s, rs in ranked.items():
            vals = np.vstack([r.auc(counts) for r in rs])  # bands x draws
            boots[s].append(np.nansum(wts[:, None] * vals, axis=0))
    boots = {s: np.concatenate(v) for s, v in boots.items()}
    per_band = []
    for (b, m), _w in zip(bands, wts, strict=True):
        per_band.append(
            {
                "lo": round(b * BAND, 2),
                "hi": round((b + 1) * BAND, 2),
                "rows": int(m.sum()),
                "won": int(y[m].sum()),
                **{
                    s: round(auc(df[col].to_numpy(float)[m], y[m]) or 0.5, 4)
                    for s, col in series.items()
                },  # fmt: skip
            }
        )
    return {
        "price": price,
        "bands": len(bands),
        "rows": int(sizes.sum()),
        "auc": {s: round(v, 4) for s, v in point.items()},
        "auc_ci95": {s: _ci(v) for s, v in boots.items()},
        "above_half": {s: bool(_ci(v) and _ci(v)[0] > 0.5) for s, v in boots.items()},
        "per_band": per_band,
    }


# --- 1 and 2: match 1X2 -------------------------------------------------------------


MATCH_SERIES = {
    "model": "p_model",
    "blend": "p_blend",
    "pinnacle_early": "fair_early",
    "pinnacle_close": "fair_close",
}


def match_blocks(rows: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """Section 1 and 2 on residual_map's outcome rows (with an optional `fair_dk`)."""
    base = rows.dropna(subset=list(MATCH_SERIES.values())).reset_index(drop=True)
    groups = {"pooled": np.ones(len(base), dtype=bool)}
    for side in ("home", "draw", "away"):
        groups[side] = (base["side"] == side).to_numpy()
    for lg in sorted(base["league"].unique()):
        groups[f"league:{lg}"] = (base["league"] == lg).to_numpy()
    diffs = [("model", "pinnacle_early"), ("blend", "pinnacle_early"),
             ("pinnacle_close", "pinnacle_early")]  # fmt: skip
    out = {
        "rows": int(len(base)),
        "matches": int(base["match"].nunique()),
        "seasons": sorted(base["season"].astype(str).unique().tolist()),
        "main": compare(base, MATCH_SERIES, groups, diffs, n_boot),
    }
    if "fair_dk" in base and base["fair_dk"].notna().any():
        dk = base.dropna(subset=["fair_dk"]).reset_index(drop=True)
        series = {**MATCH_SERIES, "draftkings_48h": "fair_dk"}
        g = {"pooled": np.ones(len(dk), dtype=bool)}
        for side in ("home", "draw", "away"):
            g[side] = (dk["side"] == side).to_numpy()
        for lg in sorted(dk["league"].unique()):
            g[f"league:{lg}"] = (dk["league"] == lg).to_numpy()
        out["draftkings"] = {
            "rows": int(len(dk)),
            "matches": int(dk["match"].nunique()),
            "seasons": sorted(dk["season"].astype(str).unique().tolist()),
            **compare(
                dk,
                series,
                g,
                diffs + [("draftkings_48h", "pinnacle_early"), ("model", "draftkings_48h")],
                n_boot,
            ),  # fmt: skip
        }
    resid = base.assign(model_minus_price=base["p_model"] - base["fair_early"],
                        blend_minus_price=base["p_blend"] - base["fair_early"])  # fmt: skip
    out["within_bands"] = within_bands(
        resid,
        "fair_early",
        {
            "model": "p_model",
            "blend": "p_blend",
            "model_minus_price": "model_minus_price",
            "pinnacle_early": "fair_early",
            "pinnacle_close": "fair_close",
        },
        n_boot,
    )
    return out


def dk_fair(code: str, rows: pd.DataFrame, last: int) -> pd.DataFrame:
    """DraftKings' Shin-fair 1X2 at the 48 h look for this league's matches, from the bought
    ten-book history (dk_price_backtest.candidates), seasons up to `last` only. Columns:
    season, home, away, side, fair_dk."""
    from soccer_stats import dk_price_backtest as dpb

    known = set(rows["home"]) | set(rows["away"])
    hist = dpb.load_books(code, known)
    if hist.empty:
        return pd.DataFrame(columns=["season", "home", "away", "side", "fair_dk"])
    first = rows.drop_duplicates("match")
    merged = pd.DataFrame(
        {
            "date": first["date"],
            "season": first["season"],
            "home": first["home"],
            "away": first["away"],
            "home_goals": 0,
            "away_goals": 0,
        }
    )
    for side in ("home", "draw", "away"):
        p = rows[rows["side"] == side].drop_duplicates("match").set_index("match")["p_model"]
        merged[f"p_{side}"] = first["match"].map(p).to_numpy()
    seasons = {s for s in merged["season"].astype(str) if 2000 + int(s[:2]) <= last}
    cand, _ = dpb.candidates(merged, hist, seasons)
    if cand.empty:
        return pd.DataFrame(columns=["season", "home", "away", "side", "fair_dk"])
    parts = [
        pd.DataFrame(
            {
                "season": cand["season"].astype(str),
                "home": cand["home"],
                "away": cand["away"],
                "side": side,
                "fair_dk": cand[f"mkt_{side}"].astype(float),
            }
        )
        for side in ("home", "draw", "away")
    ]
    return pd.concat(parts, ignore_index=True)


# --- 3: corners model (f) ---------------------------------------------------------------


def corner_rows(rows: list[dict], results: pd.DataFrame, books=("pinnacle", "draftkings")):
    """One row per book, team half line (newest pre-kickoff quote) with both sides priced:
    book, league, match, model (p_model_over), price (fair_over), won (over)."""
    from soccer_stats import corners_live as cl

    index = cl._result_index(results)
    out = []
    for book in books:
        for q in cl.newest_quotes(rows, book=book).values():
            if q.get("league") in LOCKED_CORNER_LEAGUES:
                continue
            line = float(q["line"])
            if line == int(line) or q.get("fair_over") is None or q.get("p_model_over") is None:
                continue
            n = cl._count(index, q)
            if n is None:
                continue
            out.append(
                {
                    "book": book,
                    "league": q.get("league"),
                    "match": f"{q.get('league')}|{q['home']}|{q['away']}|{q['kickoff']}",
                    "model": float(q["p_model_over"]),
                    "price": float(q["fair_over"]),
                    "won": 1.0 if n > line else 0.0,
                }
            )
    return pd.DataFrame(out, columns=["book", "league", "match", "model", "price", "won"])


def corner_blocks(df: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    out = {
        "leagues": list(CORNER_LEAGUES),
        "left_out": list(LOCKED_CORNER_LEAGUES),
        "note": "E0, I1 and F1 are left out: their 2026/27 corners are bake-off 2's locked "
        "test (read once on 3 Feb 2027). Live log only: no historical corner prices exist.",
    }
    for book in ("pinnacle", "draftkings"):
        d = df[df["book"] == book].reset_index(drop=True)
        if d.empty:
            out[book] = {"error": "no settled half lines"}
            continue
        out[book] = compare(
            d,
            {"model_f": "model", "fair_price": "price"},
            {"pooled": np.ones(len(d), dtype=bool)},
            [("model_f", "fair_price")],
            n_boot,
        )["pooled"]
    return out


# --- 4: goalscorer B ------------------------------------------------------------------------


GOAL_SERIES = {
    "model_b": "p_B",
    "season_goals": "bench_goals",
    "season_xg": "bench_xg",
    "best_price": "price",
}


def goal_blocks(df: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    """B vs the season benchmarks vs the best bookmaker's price (1/odds), 2025/26 starters."""
    d = df.copy()
    d["price"] = 1.0 / d["entry_odds"].astype(float)
    d = d.dropna(subset=list(GOAL_SERIES.values()) + ["scored"]).reset_index(drop=True)
    d["won"] = d["scored"].astype(float)
    d["match"] = d["match_id"].astype(str)
    groups = {"pooled": np.ones(len(d), dtype=bool)}
    for lg in sorted(d["league"].unique()):
        groups[f"league:{lg}"] = (d["league"] == lg).to_numpy()
    res = compare(
        d,
        GOAL_SERIES,
        groups,
        [("model_b", "best_price"), ("model_b", "season_xg"), ("model_b", "season_goals")],
        n_boot,
    )
    return {
        "rows": int(len(d)),
        "season": "2025/26 (development only; §16's judged window is not read)",
        "price_note": "Best bookmaker's Yes price as 1/odds (no book offers a No price, so the "
        "margin can't be removed; AUC only needs the ranking).",
        **res,
    }


# --- loading (Actions only) -----------------------------------------------------------------


def load_match_rows(leagues, first: int, last: int, with_dk: bool = True):
    from soccer_stats.lab import residual_map as rm
    from soccer_stats.lab.harness import Holdout

    holdout = Holdout(pd.Timestamp(f"{last + 1}-07-01"))
    frames, reps = [], {}
    for code in leagues:
        try:
            rows, _m, _lean, rep = rm.load_league(code, holdout, first, last)
            if with_dk:
                try:
                    dk = dk_fair(code, rows, last)
                    rows = rows.merge(dk, on=["season", "home", "away", "side"], how="left")
                    rep["dk_rows"] = int(rows["fair_dk"].notna().sum())
                except Exception as exc:  # noqa: BLE001 - DraftKings is optional
                    rep["dk_error"] = f"{type(exc).__name__}: {exc}"[:160]
            holdout.check(rows["date"])
            frames.append(rows)
        except Exception as exc:  # noqa: BLE001 - one league failing is reported
            rep = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        reps[code] = rep
        print(f"{code}: {rep}", flush=True)
    rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return rows, reps


def load_corner_rows(log_dir: Path) -> pd.DataFrame:
    from soccer_stats import corners_live as cl
    from soccer_stats.data import current_season, download

    rows = cl.load_rows(log_dir)
    year = current_season()
    for code in CORNER_LEAGUES:
        try:
            download(code, year)
        except Exception:  # noqa: BLE001 - that league's results are then missing
            pass
    results = cl.corner_results(CORNER_LEAGUES, seasons=[year])
    return corner_rows(rows, results)


def load_goal_rows(goal_dir: Path) -> pd.DataFrame:
    files = sorted(Path(goal_dir).rglob("judged_*.csv.gz"))
    frames = [pd.read_csv(f) for f in files]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summary_lines(res: dict) -> list[str]:
    lines = ["== ROC AUC (exploratory) =="]
    m = res.get("match", {})
    if "main" in m:
        p = m["main"]["pooled"]
        lines.append(f"  match 1X2 pooled ({p['rows']} rows): {p['auc']}  diffs {p['diff']}")
        wb = m.get("within_bands", {})
        lines.append(f"  within 5-pt bands: {wb.get('auc')} ranges {wb.get('auc_ci95')}")
    if "draftkings" in m:
        p = m["draftkings"]["pooled"]
        lines.append(f"  DraftKings subset ({p['rows']} rows): {p['auc']}")
    for book in ("pinnacle", "draftkings"):
        c = res.get("corners", {}).get(book, {})
        lines.append(f"  corners {book}: {c.get('auc')} {c.get('diff')} rows {c.get('rows')}")
    g = res.get("goalscorer", {}).get("pooled", {})
    lines.append(f"  goalscorer: {g.get('auc')} {g.get('diff')} rows {g.get('rows')}")
    return lines


def print_file(text: str) -> None:
    print(f"AUC_SHA256 {hashlib.sha256(text.encode()).hexdigest()} ({len(text)} bytes)")
    blob = base64.b64encode(gzip.compress(text.encode(), mtime=0)).decode()
    parts = [blob[i : i + B64_CHUNK] for i in range(0, len(blob), B64_CHUNK)]
    for i, part in enumerate(parts):
        print(f"AUC_B64 {i + 1}/{len(parts)} {part}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--leagues", default="E0,SP1,D1,I1,F1,E1,E2,E3")
    p.add_argument("--first", type=int, default=2019)
    p.add_argument("--last", type=int, default=2023, help="last season (2024/25 is locked)")
    p.add_argument("--boot", type=int, default=N_BOOT)
    p.add_argument("--log-dir", default="", help="data-log checkout with odds_log/ (corners)")
    p.add_argument("--goal-dir", default="", help="folder with judged_<league>.csv.gz files")
    p.add_argument("--skip-match", action="store_true")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    if args.last >= 2024:
        raise SystemExit("2024/25 on is locked: --last must be 2023 or earlier")
    res: dict = {
        "kind": "auc_research",
        "title": "ROC AUC: how well each model ranks outcomes",
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exploratory": True,
        "note": NOTE,
        "runs": {"auc": os.environ.get("GITHUB_RUN_ID")},
    }
    if not args.skip_match:
        rows, reps = load_match_rows(
            [c for c in args.leagues.split(",") if c], args.first, args.last
        )
        res["match"] = {"leagues": reps, **match_blocks(rows, args.boot)} if len(rows) else {
            "leagues": reps, "error": "no rows"}  # fmt: skip
    if args.log_dir:
        try:
            res["corners"] = corner_blocks(load_corner_rows(Path(args.log_dir)), args.boot)
        except Exception as exc:  # noqa: BLE001
            res["corners"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    if args.goal_dir:
        try:
            g = load_goal_rows(Path(args.goal_dir))
            res["goalscorer"] = goal_blocks(g, args.boot) if len(g) else {"error": "no files"}
        except Exception as exc:  # noqa: BLE001
            res["goalscorer"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    print("\n".join(summary_lines(res)))
    text = json.dumps(res, default=str, separators=(",", ":"))
    print("AUC_JSON " + text)
    print_file(text)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)


if __name__ == "__main__":
    main()
