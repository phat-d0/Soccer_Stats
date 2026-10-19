"""Two filters on the live Model 12% match rule, replayed on history (owner, 19 Oct).

Pre-registered in docs/lab.md, "Model 12% filters". Three rules on the model's own raw
chances, one pick per match, $1 a bet:

* baseline: the live rule, `trades.best_pick` at 12% on home/draw/away;
* A (model favourites only): only sides whose model chance is above 50% can be picked;
* B (no long shots): only sides whose decimal price is 3.00 or shorter can be picked.

The filter is applied before the pick: each rule takes the best edge of at least 12%
among the sides that pass its filter (not the baseline's pick, filtered).

Main test: the model's walk-forward chances (`backtest.walk_forward`, as `lean_backtest`
builds them) bet at Pinnacle's early price (football-data PSH/PSD/PSA), CLV against
Pinnacle's Shin-fair close, seasons 2019/20-2024/25 where both prices cover 90% of
matches, eight leagues. Decision (pooled): a filter is worth keeping if its CLV range at
97.5% (Bonferroni, two filters) lies above 0 with at least MIN_BETS bets; if both pass,
the higher pooled CLV; if neither, no live change. ROI is reported, not judged.

Secondary, descriptive: the same rules on E0's DraftKings 2025/26 backtest candidates
(`backtest.dk_candidates`), first qualifying look per match.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import lean_backtest as lb
from soccer_stats import trades as tr

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
RULES = ("baseline", "A", "B")
FILTERS = ("A", "B")
RULE_LABELS = {
    "baseline": "Baseline: the live rule (model's raw chance, 12% edge, best edge per match)",
    "A": "A, model favourites only: the picked side's model chance above 50%",
    "B": "B, no long shots: the picked side's decimal price 3.00 or shorter",
}
MARKETS = tr.LEAN_MARKETS  # home, draw, away (the live DraftKings call is h2h only)
THRESHOLD = tr.PAPER_EDGE  # 12%
FAV_MIN = 0.50  # A: model chance strictly above this
MAX_ODDS = 3.00  # B: decimal price at most this
LAST_SEASON = "2425"  # 2025/26 and later are never scored
MIN_BETS = 300
DECISION_LEVEL = 0.975  # two filters, Bonferroni at 95% family-wise
N_BOOT = 2000
STAKE = 1.0
BET_FIELDS = [
    "date",
    "season",
    "league",
    "home",
    "away",
    "market",
    "odds",
    "model_p",
    "implied",
    "edge",
    "won",
    "profit",
    "clv",
]


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def pick(rule: str, probs: dict, odds: dict, threshold: float = THRESHOLD) -> dict | None:
    """The rule's pick for one match, or None: `trades.best_pick` (the live order, ties and
    threshold) over home/draw/away, after dropping the sides the rule's filter excludes.

    A drops sides whose model chance isn't above FAV_MIN; B drops sides priced above
    MAX_ODDS (best_pick's own max_odds). The baseline drops nothing."""
    if rule not in RULES:
        raise ValueError(f"unknown rule {rule!r}")
    p = {m: _num(probs.get(m)) for m in MARKETS}
    o = {m: _num(odds.get(m)) for m in MARKETS}
    if rule == "A":
        p = {m: (v if v is not None and v > FAV_MIN else None) for m, v in p.items()}
    return tr.best_pick(p, o, threshold, max_odds=MAX_ODDS if rule == "B" else None)


def season_ok(code: str, last: str = LAST_SEASON) -> bool:
    """football-data season code at or before `last` ("1920" <= "2425")."""
    code = str(code)
    return len(code) == 4 and code.isdigit() and int(code[:2]) <= int(last[:2])


def bets(target: pd.DataFrame, league: str, rule: str) -> pd.DataFrame:
    """One bet per candidate match where the rule picks a side: `target` is
    lean_backtest.target_rows output (model chances p_<side>, Pinnacle's early quoted
    odds_<side>, close_<side>, goals). $1 stakes; CLV against Pinnacle's fair close."""
    out = []
    for r in target.to_dict("records"):
        pk = pick(
            rule, {m: r.get(f"p_{m}") for m in MARKETS}, {m: r.get(f"odds_{m}") for m in MARKETS}
        )
        if not pk:
            continue
        m = pk["market"]
        w = tr.won(m, int(r["home_goals"]), int(r["away_goals"]))
        close = {k: _num(r.get(f"close_{k}")) for k in MARKETS}
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
            }
        )
    return pd.DataFrame(out, columns=BET_FIELDS)


def boot_range(
    values: np.ndarray, level: float = 0.95, seed: int = 0, n: int = N_BOOT
) -> list[float] | None:
    """Two-sided `level` percentile range of the mean, resampling the values (each bet is
    one match: every rule bets a match at most once)."""
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = values[rng.integers(0, len(values), size=(n, len(values)))].mean(axis=1)
    a = (1 - level) / 2
    return [float(np.quantile(means, a)), float(np.quantile(means, 1 - a))]


def metrics(df: pd.DataFrame, clv_col: str = "clv", extra_clv: tuple[str, ...] = ()) -> dict:
    """Bets, win rate beside the model's average chance and the average implied chance,
    ROI per $1 with its 95% range, CLV with its 95% and 97.5% ranges, beat-the-close share,
    average odds and bets per market. `extra_clv` adds the same CLV block for other
    columns (the DraftKings check's Pinnacle close)."""
    n = len(df)
    if n == 0:
        return {"bets": 0}
    profit = df["profit"].to_numpy(dtype=float)
    odds = df["odds"].to_numpy(dtype=float)
    won = df["won"].astype(bool).to_numpy()
    out = {
        "bets": n,
        "wins": int(won.sum()),
        "win_rate": float(won.mean()),
        "model_p": float(df["model_p"].astype(float).mean()),
        "implied": float((1 / odds).mean()),
        "roi": float(profit.sum() / (STAKE * n)),
        "roi_ci95": boot_range(profit / STAKE),
        "profit": float(profit.sum()),
        "avg_odds": float(odds.mean()),
        "by_market": {k: int(v) for k, v in df["market"].value_counts().items()},
        **_clv_block(df[clv_col]),
    }
    for col in extra_clv:
        out[col] = _clv_block(df[col]) if col in df else {}
    return out


def _clv_block(series: pd.Series) -> dict:
    clv = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
    return {
        "clv_bets": len(clv),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_ci95": boot_range(clv, 0.95, seed=1),
        "clv_ci975": boot_range(clv, DECISION_LEVEL, seed=1),
        "beat_close": float((clv > 0).mean()) if len(clv) else None,
    }


def decide(pooled: dict[str, dict], min_bets: int = MIN_BETS) -> dict:
    """The pre-registered rule on the pooled metrics: a filter passes when its CLV range at
    97.5% lies above 0 and it has at least `min_bets` bets; keep the passing filter with
    the higher pooled CLV, or none."""
    checks = {}
    for f in FILTERS:
        m = pooled.get(f) or {}
        rng = m.get("clv_ci975")
        enough = m.get("bets", 0) >= min_bets
        above = bool(rng) and rng[0] > 0
        checks[f] = {
            "bets": m.get("bets", 0),
            "clv": m.get("clv"),
            "clv_ci975": rng,
            "enough_bets": enough,
            "clv_above_zero": above,
            "passes": enough and above,
        }
    passing = [f for f in FILTERS if checks[f]["passes"]]
    keep = max(passing, key=lambda f: checks[f]["clv"]) if passing else None
    if keep:
        verdict = (
            f"Filter {keep} is worth keeping: pooled CLV range at 97.5% above 0 with "
            f"{checks[keep]['bets']} bets."
        )
    else:
        verdict = (
            "Neither filter passes: no pooled CLV range at 97.5% lies above 0 with at least "
            f"{min_bets} bets. No live change recommended."
        )
    return {
        "rule": f"pooled CLV range at {DECISION_LEVEL:.1%} above 0 and at least {min_bets} bets",
        "checks": checks,
        "keep": keep,
        "verdict": verdict,
    }


def league_bets(
    preds: pd.DataFrame, pins: pd.DataFrame, league: str
) -> tuple[dict, dict[str, pd.DataFrame]]:
    """One league: coverage, the seasons scored (covered and at or before LAST_SEASON) and
    each rule's bets. `preds` is backtest.walk_forward output, `pins`
    data.pinnacle_prices for the seasons predicted."""
    merged = lb.with_pinnacle(preds, pins)
    cov = lb.coverage(pins)
    scored = {c["season"] for c in cov if c["included"] and season_ok(c["season"])}
    target = lb.target_rows(merged)
    if len(target):
        target = target[target["season"].astype(str).isin(scored)]
    info = {
        "coverage": cov,
        "seasons_scored": [lb.season_label(s) for s in sorted(scored)],
        "seasons_left_out": [
            lb.season_label(c["season"]) for c in cov if c["season"] not in scored
        ],
        "candidates": len(target),
    }
    return info, {rule: bets(target, league, rule) for rule in RULES}


def by_season(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    return [
        {"season": str(s), "label": lb.season_label(s), **metrics(g)}
        for s, g in df.groupby("season", sort=True)
    ]


def build(
    infos: dict[str, dict],
    league_rule_bets: dict[str, dict[str, pd.DataFrame]],
    *,
    names: dict[str, str] | None = None,
    dk: dict | None = None,
    now: pd.Timestamp | None = None,
) -> dict:
    """The results: per league and pooled metrics for each rule, per season pooled, the
    decision and the DraftKings check."""
    now = now or pd.Timestamp.now(tz="UTC")
    pooled_bets = {}
    for rule in RULES:
        frames = [b[rule] for b in league_rule_bets.values() if len(b.get(rule, []))]
        pooled_bets[rule] = (
            pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=BET_FIELDS)
        )
    pooled = {rule: metrics(pooled_bets[rule]) for rule in RULES}
    leagues = {}
    for code, info in infos.items():
        rb = league_rule_bets.get(code, {})
        leagues[code] = {
            "name": (names or {}).get(code, code),
            **info,
            **({rule: metrics(rb[rule]) for rule in RULES} if rb else {}),
        }
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "kind": "edge12_filters",
        "preregistered": "docs/lab.md, Model 12% filters (owner, 19 Oct)",
        "rules": RULE_LABELS,
        "settings": {
            "threshold": THRESHOLD,
            "fav_min": FAV_MIN,
            "max_odds": MAX_ODDS,
            "last_season": lb.season_label(LAST_SEASON),
            "coverage_min": lb.COVERAGE_MIN,
            "price": "Pinnacle early (PSH/PSD/PSA), quoted",
            "clv": "entry odds x Pinnacle's Shin-fair close (PSCH/PSCD/PSCA) - 1",
            "stake": STAKE,
            "n_boot": N_BOOT,
        },
        "pooled": pooled,
        "by_season": {rule: by_season(pooled_bets[rule]) for rule in RULES},
        "leagues": leagues,
        "decision": decide(pooled),
        "draftkings_e0": dk,
    }


# ---------- secondary, descriptive: E0's DraftKings 2025/26 backtest ----------


def dk_bets(candidates: pd.DataFrame, rule: str, season: str | None = "2526") -> pd.DataFrame:
    """The rule on backtest.dk_candidates rows: teams with fewer than MIN_TEAM_MATCHES in
    the training window skipped (as the trade rule), the pick on home/draw/away at each
    look, a match trades at its first qualifying look (looks in their listed order, 48 h
    then 3 h), voids (no result, or moved) left out. CLV against DraftKings' close (`clv`)
    and Pinnacle's close from football-data (`clv_pinnacle`)."""
    cols = [*BET_FIELDS, "look", "clv_pinnacle"]
    if candidates.empty:
        return pd.DataFrame(columns=cols)
    df = candidates
    if season is not None:
        df = df[df["season"].astype(str) == season]
    order = {k: i for i, k in enumerate(dict.fromkeys(candidates["look"]))}
    rows = []
    for r in df.to_dict("records"):
        if min(r.get("home_n", np.inf), r.get("away_n", np.inf)) < tr.MIN_TEAM_MATCHES:
            continue
        pk = pick(
            rule, {m: r.get(f"p_{m}") for m in MARKETS}, {m: r.get(f"odds_{m}") for m in MARKETS}
        )
        if pk:
            rows.append({**r, **pk, "_o": order[r["look"]]})
    if not rows:
        return pd.DataFrame(columns=cols)
    picked = pd.DataFrame(rows).sort_values(["kickoff", "_o"])
    first = picked.groupby(["home", "away", "season"], sort=False).head(1)
    out = []
    for r in first.to_dict("records"):
        if r.get("void") or r.get("home_goals") is None or pd.isna(r.get("home_goals")):
            continue
        m = r["market"]
        w = tr.won(m, int(r["home_goals"]), int(r["away_goals"]))
        out.append(
            {
                "date": pd.Timestamp(r["kickoff"]).date().isoformat(),
                "season": str(r["season"]),
                "league": r.get("league") or "E0",
                "home": r["home"],
                "away": r["away"],
                "market": m,
                "odds": r["odds"],
                "model_p": r["model_p"],
                "implied": 1 / r["odds"],
                "edge": r["edge"],
                "won": bool(w),
                "profit": STAKE * (r["odds"] - 1) if w else -STAKE,
                "clv": tr.clv(r["odds"], m, r.get("close") or {}),
                "look": r["look"],
                "clv_pinnacle": tr.clv(r["odds"], m, r.get("pinnacle_close") or {}),
            }
        )
    return pd.DataFrame(out, columns=cols)


def dk_check(candidates: pd.DataFrame, season: str = "2526") -> dict:
    """Descriptive: each rule's metrics on the DraftKings candidates, CLV against
    DraftKings' close (`clv`) and Pinnacle's (`clv_pinnacle`)."""
    return {
        "label": "Descriptive: E0 DraftKings backtest, " + lb.season_label(season),
        "season": lb.season_label(season),
        "matches": int(
            candidates[candidates["season"].astype(str) == season][["home", "away"]]
            .drop_duplicates()
            .shape[0]
        )
        if len(candidates)
        else 0,
        **{
            rule: metrics(dk_bets(candidates, rule, season), extra_clv=("clv_pinnacle",))
            for rule in RULES
        },
    }
