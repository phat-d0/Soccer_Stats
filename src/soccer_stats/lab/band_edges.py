"""A minimum edge per price band for the match rule, replayed on history (owner, 20 Oct).

Pre-registered in docs/lab.md, "Price-band edge rule". The owner's question: the match
rule bets whichever side shows an edge, but in a three-way market that is often an
underdog, and the model's underdog edges look like its mistakes. Would a stricter edge on
underdogs and a looser one on favourites win?

* Bands by the side's Pinnacle early quoted price: fav (< 2.00), mid (2.00-3.50),
  dog (3.50-6.00), long (> 6.00) for home and away; draw on its own.
* Development 2019/20-2022/23: every side with an edge, per band and claimed-edge
  bucket (how often it wins against the model's chance and the price's); then each
  band's threshold is the one with the highest mean CLV (at least MIN_DEV_SIDES sides),
  and a band whose best CLV is 0 or below is switched off.
* Check 2023/24-2024/25, once: the frozen rule R (one bet per match, the highest edge
  among sides clearing their band's threshold), pooled CLV range at 95% above 0 with at
  least MIN_CHECK_BETS bets. Beside it, descriptive: the live 12% baseline, the 19 Oct
  filters A and B, and R* (R without switching any band off).

Same data as lab/edge12_filters.py: the model's walk-forward chances at Pinnacle's early
price, CLV against Pinnacle's Shin-fair close, eight leagues.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import lean_backtest as lb
from soccer_stats import trades as tr
from soccer_stats.lab import edge12_filters as ef

LEAGUES = ef.LEAGUES
MARKETS = ef.MARKETS  # home, draw, away
BANDS = ("fav", "mid", "dog", "long", "draw")
BAND_LABELS = {
    "fav": "Favourite (home or away, price under 2.00)",
    "mid": "Middle (home or away, 2.00 to 3.50)",
    "dog": "Underdog (home or away, over 3.50 to 6.00)",
    "long": "Long shot (home or away, over 6.00)",
    "draw": "Draw (any price)",
}
FAV_MAX, MID_MAX, DOG_MAX = 2.00, 3.50, 6.00
THRESHOLDS = (0.02, 0.05, 0.08, 0.12, 0.15, 0.20, 0.25, 0.30)
EDGE_BUCKETS = ((0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, None))
DEV_LAST = "2223"  # development 2019/20-2022/23
CHECK_SEASONS = ("2324", "2425")
MIN_DEV_SIDES = 200
MIN_CHECK_BETS = 300
EPS = 1e-9
N_BOOT = 2000
STAKE = 1.0
SIDE_FIELDS = [
    "date",
    "season",
    "league",
    "match",
    "home",
    "away",
    "market",
    "band",
    "odds",
    "model_p",
    "implied",
    "edge",
    "won",
    "profit",
    "clv",
]


def band(market: str, odds: float) -> str:
    """The side's band: draw, else by its quoted decimal price."""
    if market == "draw":
        return "draw"
    if odds < FAV_MAX:
        return "fav"
    if odds <= MID_MAX:
        return "mid"
    if odds <= DOG_MAX:
        return "dog"
    return "long"


def is_dev(season) -> bool:
    s = str(season)
    return ef.season_ok(s, DEV_LAST)


def is_check(season) -> bool:
    return str(season) in CHECK_SEASONS


def league_target(preds: pd.DataFrame, pins: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """One league's candidate matches (lean_backtest.target_rows: model chances, Pinnacle's
    early quoted and close prices, goals) in the seasons scored: covered by Pinnacle's
    early and close prices and at or before 2024/25. As edge12_filters.league_bets."""
    merged = lb.with_pinnacle(preds, pins)
    cov = lb.coverage(pins)
    scored = {c["season"] for c in cov if c["included"] and ef.season_ok(c["season"])}
    target = lb.target_rows(merged)
    if len(target):
        target = target[target["season"].astype(str).isin(scored)].reset_index(drop=True)
    info = {
        "coverage": cov,
        "seasons_scored": [lb.season_label(s) for s in sorted(scored)],
        "seasons_left_out": [
            lb.season_label(c["season"]) for c in cov if c["season"] not in scored
        ],
        "candidates": len(target),
    }
    return info, target


def sides(target: pd.DataFrame, league: str) -> pd.DataFrame:
    """Every side with an edge above 0, one row each (a match can give two or three):
    its band, the model's chance, the implied chance (1/price), the edge, the result, $1
    profit and CLV against Pinnacle's fair close."""
    out = []
    for r in target.to_dict("records"):
        close = {k: ef._num(r.get(f"close_{k}")) for k in MARKETS}
        mid = f"{league}|{r['season']}|{r['home']}|{r['away']}"
        for m in MARKETS:
            p, o = ef._num(r.get(f"p_{m}")), ef._num(r.get(f"odds_{m}"))
            if p is None or o is None or o <= 1:
                continue
            edge = p * o - 1
            if edge <= 0:
                continue
            w = tr.won(m, int(r["home_goals"]), int(r["away_goals"]))
            out.append(
                {
                    "date": pd.Timestamp(r["date"]).date().isoformat(),
                    "season": str(r["season"]),
                    "league": league,
                    "match": mid,
                    "home": r["home"],
                    "away": r["away"],
                    "market": m,
                    "band": band(m, o),
                    "odds": o,
                    "model_p": p,
                    "implied": 1 / o,
                    "edge": edge,
                    "won": bool(w),
                    "profit": STAKE * (o - 1) if w else -STAKE,
                    "clv": tr.clv(o, m, close),
                }
            )
    return pd.DataFrame(out, columns=SIDE_FIELDS)


def grouped_range(
    values: np.ndarray, groups: np.ndarray, level: float = 0.95, seed: int = 0, n: int = N_BOOT
) -> list[float] | None:
    """Percentile range of the mean of `values`, resampling whole matches (`groups`), since
    sides of the same match aren't independent."""
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return None
    codes, uniq = pd.factorize(pd.Series(groups))
    k = len(uniq)
    if k < 2:
        return None
    sums = np.bincount(codes, weights=values, minlength=k)
    counts = np.bincount(codes, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n, k))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    a = (1 - level) / 2
    return [float(np.quantile(means, a)), float(np.quantile(means, 1 - a))]


def side_metrics(df: pd.DataFrame) -> dict:
    """Sides, matches, the model's average chance, the implied chance, the win rate, ROI
    per $1 and CLV, each with a match-resampled 95% range."""
    n = len(df)
    if n == 0:
        return {"sides": 0}
    profit = df["profit"].to_numpy(dtype=float)
    groups = df["match"].to_numpy()
    clv_df = df[pd.to_numeric(df["clv"], errors="coerce").notna()]
    clv = clv_df["clv"].to_numpy(dtype=float)
    return {
        "sides": n,
        "matches": int(df["match"].nunique()),
        "model_p": float(df["model_p"].astype(float).mean()),
        "implied": float(df["implied"].astype(float).mean()),
        "win_rate": float(df["won"].astype(bool).mean()),
        "avg_odds": float(df["odds"].astype(float).mean()),
        "roi": float(profit.mean() / STAKE),
        "roi_ci95": grouped_range(profit / STAKE, groups),
        "clv_sides": len(clv),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_ci95": grouped_range(clv, clv_df["match"].to_numpy(), seed=1),
    }


def buckets(df: pd.DataFrame) -> dict[str, list[dict]]:
    """Step 1: per band and claimed-edge bucket."""
    out = {}
    for b in BANDS:
        g = df[df["band"] == b]
        rows = []
        for lo, hi in EDGE_BUCKETS:
            sel = g[(g["edge"] >= lo) & ((g["edge"] < hi) if hi is not None else True)]
            rows.append({"edge_lo": lo, "edge_hi": hi, **side_metrics(sel)})
        out[b] = rows
    return out


def scan(df: pd.DataFrame) -> dict[str, list[dict]]:
    """Step 2's table: per band, every side with edge at or above each threshold."""
    out = {}
    for b in BANDS:
        g = df[df["band"] == b]
        out[b] = [{"t": t, **side_metrics(g[g["edge"] >= t - EPS])} for t in THRESHOLDS]
    return out


def select(table: dict[str, list[dict]], min_sides: int = MIN_DEV_SIDES) -> dict[str, dict]:
    """Each band's threshold: the t with the highest mean CLV among those with at least
    `min_sides` development sides (ties: the higher t). `on` is False when that best CLV
    is 0 or below (rule R switches the band off); `t` is kept either way (rule R*)."""
    out = {}
    for b in BANDS:
        ok = [
            r
            for r in table.get(b, [])
            if r.get("sides", 0) >= min_sides and r.get("clv") is not None
        ]
        if not ok:
            out[b] = {"t": None, "clv": None, "sides": 0, "on": False, "why": "too few sides"}
            continue
        best = max(ok, key=lambda r: (r["clv"], r["t"]))
        on = best["clv"] > 0
        out[b] = {
            "t": best["t"],
            "clv": best["clv"],
            "sides": best["sides"],
            "on": on,
            "why": "best CLV above 0" if on else "best CLV 0 or below: off",
        }
    return out


def pick(probs: dict, odds: dict, thresholds: dict[str, float | None]) -> dict | None:
    """One pick per match: among the sides whose edge clears their band's threshold (a
    band with None can't), the highest edge; ties keep the earlier side (home, draw,
    away), as trades.best_pick."""
    best = None
    for m in MARKETS:
        p, o = ef._num(probs.get(m)), ef._num(odds.get(m))
        if p is None or o is None or o <= 1:
            continue
        b = band(m, o)
        t = thresholds.get(b)
        if t is None:
            continue
        edge = p * o - 1
        if edge <= 0 or edge < t - EPS:
            continue
        if best is None or edge > best["edge"]:
            best = {"market": m, "odds": o, "model_p": p, "edge": edge, "band": b}
    return best


def rule_bets(target: pd.DataFrame, league: str, thresholds: dict) -> pd.DataFrame:
    """The band rule's bets on `target`, in edge12_filters' bet shape plus the band."""
    out = []
    for r in target.to_dict("records"):
        pk = pick(
            {m: r.get(f"p_{m}") for m in MARKETS},
            {m: r.get(f"odds_{m}") for m in MARKETS},
            thresholds,
        )
        if not pk:
            continue
        m = pk["market"]
        w = tr.won(m, int(r["home_goals"]), int(r["away_goals"]))
        close = {k: ef._num(r.get(f"close_{k}")) for k in MARKETS}
        out.append(
            {
                "date": pd.Timestamp(r["date"]).date().isoformat(),
                "season": str(r["season"]),
                "league": league,
                "home": r["home"],
                "away": r["away"],
                "market": m,
                "odds": pk["odds"],
                "model_p": pk["model_p"],
                "implied": 1 / pk["odds"],
                "edge": pk["edge"],
                "won": bool(w),
                "profit": STAKE * (pk["odds"] - 1) if w else -STAKE,
                "clv": tr.clv(pk["odds"], m, close),
                "band": pk["band"],
            }
        )
    return pd.DataFrame(out, columns=[*ef.BET_FIELDS, "band"])


def decide(m: dict, any_on: bool, min_bets: int = MIN_CHECK_BETS) -> dict:
    """The pre-registered decision on R's pooled check metrics."""
    rng = m.get("clv_ci95")
    bets = m.get("bets", 0)
    if not any_on:
        verdict = (
            "No rule survives development: every band's best CLV was 0 or below, so R "
            "makes no bets and there is nothing to check. No live change."
        )
        return {"any_band_on": False, "bets": 0, "passes": False, "verdict": verdict}
    enough = bets >= min_bets
    above = bool(rng) and rng[0] > 0
    passes = enough and above
    if passes:
        verdict = f"R passes: pooled CLV 95% range above 0 with {bets} bets."
    else:
        why = [] if above else ["its CLV range doesn't lie above 0"]
        if not enough:
            why.append(f"only {bets} bets (needs {min_bets})")
        verdict = "R doesn't pass: " + " and ".join(why) + ". No live change."
    return {
        "any_band_on": True,
        "bets": bets,
        "clv": m.get("clv"),
        "clv_ci95": rng,
        "enough_bets": enough,
        "clv_above_zero": above,
        "passes": passes,
        "verdict": verdict,
    }


def _concat(frames: list[pd.DataFrame], cols: list[str]) -> pd.DataFrame:
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


def run(
    targets: dict[str, pd.DataFrame],
    infos: dict[str, dict] | None = None,
    *,
    names: dict[str, str] | None = None,
    now: pd.Timestamp | None = None,
) -> dict:
    """The whole test from each league's target rows. Development sides decide the
    thresholds before any check-season row is read; the check then scores R once, with
    the baseline, A, B and R* beside it."""
    now = now or pd.Timestamp.now(tz="UTC")
    dev_t = {c: t[t["season"].map(is_dev)] for c, t in targets.items() if len(t)}
    check_t = {c: t[t["season"].map(is_check)] for c, t in targets.items() if len(t)}

    # Development: steps 1 and 2, then the frozen rule (step 3).
    dev_sides = _concat([sides(t, c) for c, t in dev_t.items()], SIDE_FIELDS)
    table = scan(dev_sides)
    chosen = select(table)
    rule = {b: (c["t"] if c["on"] else None) for b, c in chosen.items()}
    rule_star = {b: c["t"] for b, c in chosen.items()}
    any_on = any(v is not None for v in rule.values())

    # Check (step 4): read only now, with every threshold fixed.
    r_bets = {c: rule_bets(t, c, rule) for c, t in check_t.items()}
    rs_bets = {c: rule_bets(t, c, rule_star) for c, t in check_t.items()}
    refs = {rname: {c: ef.bets(t, c, rname) for c, t in check_t.items()} for rname in ef.RULES}
    cols = [*ef.BET_FIELDS, "band"]
    r_all = _concat(list(r_bets.values()), cols)
    rs_all = _concat(list(rs_bets.values()), cols)
    pooled = {
        "R": ef.metrics(r_all),
        "R_star": ef.metrics(rs_all),
        **{k: ef.metrics(_concat(list(v.values()), ef.BET_FIELDS)) for k, v in refs.items()},
    }
    by_band = {
        name: {b: ef.metrics(df[df["band"] == b]) for b in BANDS}
        for name, df in (("R", r_all), ("R_star", rs_all))
    }
    leagues = {}
    for c in targets:
        leagues[c] = {
            "name": (names or {}).get(c, c),
            **((infos or {}).get(c) or {}),
            "dev_matches": len(dev_t.get(c, [])),
            "check_matches": len(check_t.get(c, [])),
            "R": ef.metrics(r_bets.get(c, pd.DataFrame(columns=cols))),
            "baseline": ef.metrics(refs["baseline"].get(c, pd.DataFrame(columns=cols))),
        }
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "kind": "band_edges",
        "preregistered": "docs/lab.md, Price-band edge rule (owner, 20 Oct)",
        "bands": BAND_LABELS,
        "settings": {
            "thresholds": list(THRESHOLDS),
            "edge_buckets": [list(b) for b in EDGE_BUCKETS],
            "development": "2019/20-" + lb.season_label(DEV_LAST),
            "check": ", ".join(lb.season_label(s) for s in CHECK_SEASONS),
            "min_dev_sides": MIN_DEV_SIDES,
            "min_check_bets": MIN_CHECK_BETS,
            "price": "Pinnacle early (PSH/PSD/PSA), quoted",
            "clv": "entry odds x Pinnacle's Shin-fair close (PSCH/PSCD/PSCA) - 1",
            "stake": STAKE,
            "n_boot": N_BOOT,
        },
        "development": {
            "matches": int(sum(len(t) for t in dev_t.values())),
            "sides": side_metrics(dev_sides),
            "by_band": {b: side_metrics(dev_sides[dev_sides["band"] == b]) for b in BANDS},
            "buckets": buckets(dev_sides),
            "scan": table,
            "chosen": chosen,
        },
        "rule": rule,
        "rule_star": rule_star,
        "check": {
            "matches": int(sum(len(t) for t in check_t.values())),
            "pooled": pooled,
            "by_band": by_band,
        },
        "leagues": leagues,
        "decision": decide(pooled["R"], any_on),
    }
