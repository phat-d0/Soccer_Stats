"""Draw no bet replayed on Pinnacle's early prices (soccer-stats backtest-dnb; owner, 24 Oct).

Pre-registered in docs/lab.md, "Draw no bet". Both live match rules as draw no bet (DNB:
home or away, the stake refunded on a draw), beside the same rules on 1X2, on the same
scored matches, 0 credits:

* price: DNB synthesised from Pinnacle's early 1X2 (football-data PSH/PSD/PSA), side price
  x (1 - 1/draw price), margin kept: what backing the side and covering the draw at the
  same book pays. Fair DNB = Shin-fair p_side / (p_home + p_away), early and close.
* chances: the model's walk-forward 1X2 and the Lean backtest's walk-forward blend, each
  renormalised without the draw (no new fit); sigma_DNB per fit from its own rows.
* DNB 12%: the side with the larger edge q x price - 1 of at least 12%. DNB Lean: the
  side with the higher z = (q_blend - 1/price) / sigma_DNB of at least 1.
* scored: won / drawn / lost, hit rate vs break-even, ROI, CLV against Pinnacle's fair DNB
  close, the noise per bet, odds bands, calibration; per league, season and pooled.
* check: where Pinnacle's own Asian handicap line is 0 (AHh), the real DNB price.

Pass: a DNB rule's pooled CLV range at 97.5% (two rules) lies above 0.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import bands as bd
from soccer_stats import calibration as cal
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.data import PIN_CLOSE, PIN_EARLY
from soccer_stats.lab import edge12_filters as ef
from soccer_stats.odds import devig_shin

KIND = "dnb_backtest"
LEAGUES = ef.LEAGUES  # the eight live domestic leagues
SIDES = ("home", "away")
RULES = ("dnb12", "dnb_lean", "x12", "x_lean")
RULE_LABELS = {
    "dnb12": "DNB 12%: the model's chance without the draw, 12% edge",
    "dnb_lean": "DNB Lean: the blend without the draw, 1 sigma",
    "x12": "1X2 Model 12% (the live rule), same matches",
    "x_lean": "1X2 Blend Lean (all odds), same matches",
}
DNB_RULES = ("dnb12", "dnb_lean")
BASELINE = {"dnb12": "x12", "dnb_lean": "x_lean"}
THRESHOLD = tr.PAPER_EDGE  # 12%
EPS = 1e-9
DECISION_LEVEL = 0.975  # two rules, Bonferroni at 95% family-wise
EDGE_TO_SHOW = 0.05  # monitoring: bets needed for a 5% edge to show at 95%
N_BOOT = 2000
STAKE = 1.0
AH_COLUMNS = {  # football-data: Pinnacle's Asian handicap line (home) and two-way prices
    "early": ("AHh", "PAHH", "PAHA"),
    "close": ("AHCh", "PCAHH", "PCAHA"),
}
BET_FIELDS = [
    "date",
    "season",
    "league",
    "home",
    "away",
    "rule",
    "market",
    "odds",
    "p",
    "edge",
    "z",
    "result",
    "profit",
    "clv",
    "clv_staked",
]
NOTE = (
    "Draw no bet replayed on Pinnacle's early prices from football-data: a side's DNB price "
    "is its 1X2 price x (1 - 1/draw price), what backing it and covering the draw pays; the "
    "model's and the blend's chances are their 1X2 chances with the draw taken out; a draw "
    "refunds the stake. CLV is against Pinnacle's margin-free DNB close (from its closing "
    "1X2), per stake not refunded. Beside each DNB rule, the same rule on 1X2 on the same "
    "matches."
)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


# ---------- prices ----------


def dnb_odds(odds_side: float, odds_draw: float) -> float | None:
    """The synthetic DNB decimal price: back the side and cover the draw so a draw returns
    the whole stake. side x (1 - 1/draw); None without both prices above 1."""
    s, d = _num(odds_side), _num(odds_draw)
    if s is None or d is None or s <= 1 or d <= 1:
        return None
    return s * (1 - 1 / d)


def fair_dnb(prices) -> dict | None:
    """{home, away}: Shin-fair 1X2 (home, draw, away) with the draw taken out."""
    vals = [_num(p) for p in prices]
    if any(v is None or v <= 1 for v in vals):
        return None
    f = devig_shin(np.array(vals, dtype=float))
    s = f[0] + f[2]
    return {"home": float(f[0] / s), "away": float(f[2] / s), "draw": float(f[1])}


def without_draw(p: dict) -> dict | None:
    """{home, away} = p_side / (p_home + p_away), or None."""
    h, a = _num(p.get("home")), _num(p.get("away"))
    if h is None or a is None or h + a <= 0:
        return None
    return {"home": h / (h + a), "away": a / (h + a)}


def ah_prices(raw: pd.DataFrame) -> pd.DataFrame:
    """home, away, ah_<when>_line / _home / _away from one football-data season file
    (Pinnacle's own Asian handicap line and prices; NaN where missing)."""
    df = raw.dropna(subset=["HomeTeam", "AwayTeam"])
    out = {"home": df["HomeTeam"].astype(str), "away": df["AwayTeam"].astype(str)}
    for when, cols in AH_COLUMNS.items():
        for name, c in zip(("line", "home", "away"), cols, strict=True):
            v = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
            out[f"ah_{when}_{name}"] = pd.Series(v, index=df.index, dtype=float)
    return pd.DataFrame(out).reset_index(drop=True)


# ---------- candidates ----------


def candidates(scored: pd.DataFrame) -> pd.DataFrame:
    """lean_backtest.walk_forward output plus the DNB columns: dnb_<side> (synthetic early
    price), fair_<side> (early fair DNB), close_fair_<side> and close_draw_p (Pinnacle's
    close), q_<side> (model), qb_<side> (blend; NaN without a fit)."""
    out = scored.copy()
    cols = {
        f"{k}_{s}": np.full(len(out), np.nan)
        for k in ("dnb", "fair", "close_fair", "q", "qb")
        for s in SIDES
    }
    cols["close_draw_p"] = np.full(len(out), np.nan)
    for i, r in enumerate(out.to_dict("records")):
        for s in SIDES:
            v = dnb_odds(r.get(f"odds_{s}"), r.get("odds_draw"))
            cols[f"dnb_{s}"][i] = np.nan if v is None else v
        early = fair_dnb([r.get(c) for c in PIN_EARLY])
        close = fair_dnb([r.get(c) for c in PIN_CLOSE])
        q = without_draw({s: r.get(f"p_{s}") for s in SIDES})
        qb = without_draw({s: r.get(f"pb_{s}") for s in SIDES})
        for s in SIDES:
            for key, src in (("fair", early), ("close_fair", close), ("q", q), ("qb", qb)):
                if src:
                    cols[f"{key}_{s}"][i] = src[s]
        if close:
            cols["close_draw_p"][i] = close["draw"]
    for k, v in cols.items():
        out[k] = v
    return out


def sigma_dnb(train: pd.DataFrame, fits: list[dict]) -> dict[str, float]:
    """fit start (ISO date) -> sigma_DNB: the sd (ddof 0) of the blend's DNB home chance
    minus the fair DNB (Pinnacle's close) over that fit's own training rows."""
    if train.empty or not fits:
        return {}
    mk, pc = [f"mkt_{m}" for m in lb.H2H], [f"p_{m}" for m in lb.H2H]
    tdate = pd.to_datetime(train["date"])
    out = {}
    for f in fits:
        rows = train[tdate < pd.Timestamp(f["from"])]
        b = mc.apply(f["coef"], rows[mk], rows[pc])
        m = rows[mk].to_numpy(dtype=float)
        qb = b[:, 0] / (b[:, 0] + b[:, 2])
        qm = m[:, 0] / (m[:, 0] + m[:, 2])
        gap = (qb - qm)[np.isfinite(qb - qm)]
        if len(gap):
            out[f["from"]] = float(gap.std(ddof=0))
    return out


# ---------- rules ----------


def pick_12(q: dict, odds: dict, threshold: float = THRESHOLD) -> dict | None:
    """DNB 12%: the side with the larger edge q x price - 1, if at least `threshold`;
    home first on a tie."""
    best = None
    for s in SIDES:
        p, o = _num(q.get(s)), _num(odds.get(s))
        if p is None or o is None or o <= 1:
            continue
        e = p * o - 1
        if e >= threshold - EPS and e > 0 and (best is None or e > best["edge"]):
            best = {"market": s, "odds": o, "p": p, "edge": e, "z": None}
    return best


def pick_lean(qb: dict, odds: dict, sigma: float | None, min_z: float = tr.LEAN_Z) -> dict | None:
    """DNB Lean: the side with the higher z = (q_blend - 1/price) / sigma, if at least
    `min_z`; home first on a tie."""
    sd = _num(sigma)
    if sd is None or sd <= 0:
        return None
    best = None
    for s in SIDES:
        p, o = _num(qb.get(s)), _num(odds.get(s))
        if p is None or o is None or o <= 1:
            continue
        z = (p - 1 / o) / sd
        if z >= min_z - EPS and (best is None or z > best["z"]):
            best = {"market": s, "odds": o, "p": p, "edge": p * o - 1, "z": z}
    return best


def result(market: str, home_goals: int, away_goals: int) -> str:
    """won / drawn / lost for a DNB bet on `market`."""
    if home_goals == away_goals:
        return "drawn"
    return "won" if tr.won(market, home_goals, away_goals) else "lost"


def _row(r: dict, league: str, rule: str, pk: dict, res: str, clv, clv_staked) -> dict:
    o = pk["odds"]
    return {
        "date": pd.Timestamp(r["date"]).date().isoformat(),
        "season": str(r["season"]),
        "league": league,
        "home": r["home"],
        "away": r["away"],
        "rule": rule,
        "market": pk["market"],
        "odds": float(o),
        "p": float(pk["p"]),
        "edge": float(pk["edge"]),
        "z": None if pk.get("z") is None else float(pk["z"]),
        "result": res,
        "profit": STAKE * (o - 1) if res == "won" else (0.0 if res == "drawn" else -STAKE),
        "clv": clv,
        "clv_staked": clv_staked,
    }


def dnb_bets(cand: pd.DataFrame, league: str, price: str = "dnb") -> pd.DataFrame:
    """Both DNB rules' bets on candidates(): rows with BET_FIELDS. `price` is the column
    prefix the bet is decided and paid at (dnb = synthetic; ah0 = the real AH-0 price)."""
    out = []
    for r in cand.to_dict("records"):
        odds = {s: r.get(f"{price}_{s}") for s in SIDES}
        close = {s: _num(r.get(f"close_fair_{s}")) for s in SIDES}
        draw = _num(r.get("close_draw_p"))
        picks = {
            "dnb12": pick_12({s: r.get(f"q_{s}") for s in SIDES}, odds),
            "dnb_lean": pick_lean({s: r.get(f"qb_{s}") for s in SIDES}, odds, r.get("sigma_dnb")),
        }
        for rule, pk in picks.items():
            if not pk:
                continue
            res = result(pk["market"], int(r["home_goals"]), int(r["away_goals"]))
            c = close[pk["market"]]
            clv = None if c is None else float(pk["odds"] * c - 1)
            staked = None if clv is None or draw is None else clv * (1 - draw)
            out.append(_row(r, league, rule, pk, res, clv, staked))
    return pd.DataFrame(out, columns=BET_FIELDS)


def x_bets(cand: pd.DataFrame, league: str) -> pd.DataFrame:
    """The 1X2 baselines on the same candidates: Model 12% (edge12_filters baseline) on
    every candidate, Lean (lean_backtest.bets) where the block has a fit."""
    out = []
    e12 = ef.bets(cand, league, "baseline")
    for r in e12.to_dict("records"):
        res = "won" if r["won"] else "lost"
        pk = {"market": r["market"], "odds": r["odds"], "p": r["model_p"], "edge": r["edge"]}
        out.append(_row(r, league, "x12", pk, res, r["clv"], r["clv"]))
    with_fit = cand[cand["sigma"].notna()] if len(cand) else cand
    lean = lb.bets(with_fit, league)
    for r in lean.to_dict("records"):
        res = "won" if r["won"] else "lost"
        pk = {
            "market": r["market"],
            "odds": r["odds"],
            "p": r["p_bet"],
            "edge": r["p_bet"] * r["odds"] - 1,
            "z": r["z"],
        }
        out.append(_row(r, league, "x_lean", pk, res, r["clv"], r["clv"]))
    return pd.DataFrame(out, columns=BET_FIELDS)


# ---------- metrics ----------


def boot_mean(values, seed: int = 0, level: float = 0.95, n: int = N_BOOT):
    """`level` percentile range of the mean, resampling bets (one bet per match and rule)."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), size=(n, len(v)))].mean(axis=1)
    a = (1 - level) / 2
    return [float(np.quantile(means, a)), float(np.quantile(means, 1 - a))]


def metrics(df: pd.DataFrame) -> dict:
    """Bets, won / drawn / lost, hit rate (won of won + lost) vs break-even (1/odds), the
    model's average chance, ROI per $1 staked and its range, CLV per stake not refunded
    (95% and 97.5% ranges) and per $1 staked, beat-the-close, average odds, the sd of
    profit per bet and the bets a 5% edge needs to show at 95%."""
    n = len(df)
    if n == 0:
        return {"bets": 0}
    res = df["result"].to_numpy()
    won, drawn, lost = (int((res == k).sum()) for k in ("won", "drawn", "lost"))
    profit = df["profit"].to_numpy(dtype=float)
    odds = df["odds"].to_numpy(dtype=float)
    decided = res != "drawn"
    clv = pd.to_numeric(df["clv"], errors="coerce").to_numpy(dtype=float)
    clv = clv[np.isfinite(clv)]
    staked = pd.to_numeric(df["clv_staked"], errors="coerce").to_numpy(dtype=float)
    staked = staked[np.isfinite(staked)]
    sd = float(profit.std(ddof=1)) if n > 1 else None
    return {
        "bets": n,
        "won": won,
        "drawn": drawn,
        "lost": lost,
        "hit_rate": won / (won + lost) if won + lost else None,
        "win_share": won / n,
        "breakeven": float((1 / odds[decided]).mean()) if decided.any() else None,
        "p": float(df["p"].astype(float).mean()),
        "roi": float(profit.sum() / (STAKE * n)),
        "roi_ci95": boot_mean(profit / STAKE),
        "profit": float(profit.sum()),
        "avg_odds": float(odds.mean()),
        "clv_bets": len(clv),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_ci95": boot_mean(clv, seed=1),
        "clv_ci975": boot_mean(clv, seed=1, level=DECISION_LEVEL),
        "clv_staked": float(staked.mean()) if len(staked) else None,
        "beat_close": float((clv > 0).mean()) if len(clv) else None,
        "profit_sd": sd,
        "bets_for_5pct": (
            int(np.ceil((1.96 * sd / EDGE_TO_SHOW) ** 2)) if sd is not None else None
        ),
        "by_market": {k: int(v) for k, v in df["market"].value_counts().items()},
    }


def odds_bands(df: pd.DataFrame) -> list[dict]:
    """bands.ODDS_BANDS (lo < odds <= hi): bets, share, hit rate, ROI, CLV."""
    out = []
    n = len(df)
    for lo, hi in bd.ODDS_BANDS:
        o = df["odds"].astype(float) if n else pd.Series(dtype=float)
        mask = (o > (lo or 0)) & (o <= (hi or np.inf))
        m = metrics(df[mask]) if n else {"bets": 0}
        keep = ("bets", "won", "drawn", "lost", "hit_rate", "breakeven", "roi", "roi_ci95")
        out.append(
            {
                "lo": lo,
                "hi": hi,
                **{k: m.get(k) for k in keep if k in m},
                "share": (m["bets"] / n) if n else 0.0,
                **({"clv": m.get("clv"), "clv_ci95": m.get("clv_ci95")} if m["bets"] else {}),
            }
        )
    return out


def rule_block(df: pd.DataFrame) -> dict:
    """metrics() plus odds bands and per-season metrics for one rule's bets."""
    seasons = []
    for season, g in df.groupby("season", sort=True) if len(df) else []:
        seasons.append({"season": str(season), "label": lb.season_label(season), **metrics(g)})
    return {**metrics(df), "by_odds_band": odds_bands(df), "by_season": seasons}


def rules_report(b: pd.DataFrame) -> dict:
    """{rule: rule_block} for every rule in RULES."""
    return {r: rule_block(b[b["rule"] == r] if len(b) else b) for r in RULES}


def calibration(cand: pd.DataFrame, b: pd.DataFrame) -> dict:
    """Every scored match without a draw: the home side's DNB chance from the model, the
    blend and the fair early price against whether home won; plus each DNB rule's own
    bets (chance vs 1/odds vs won, draws left out)."""
    if not len(cand):
        return {"all_outcomes": {}, "bets": {}}
    c = cand[cand["home_goals"] != cand["away_goals"]]
    won = (c["home_goals"] > c["away_goals"]).astype(float)
    series = {"model": "q_home", "blend": "qb_home", "price": "fair_home"}
    ok = c[list(series.values())].notna().all(axis=1)
    price = c.loc[ok, "fair_home"].astype(float)
    every = {
        k: cal.reliability(c.loc[ok, col].astype(float), won[ok], implied=price)
        for k, col in series.items()
    }
    bets = {}
    for rule in DNB_RULES:
        x = b[(b["rule"] == rule) & (b["result"] != "drawn")] if len(b) else b
        bets[rule] = (
            cal.reliability(
                x["p"].astype(float),
                (x["result"] == "won").astype(float),
                implied=1 / x["odds"].astype(float),
            )
            if len(x)
            else {"n": 0}
        )
    return {"all_outcomes": every, "bets": bets}


def ah_check(cand: pd.DataFrame, league: str) -> tuple[dict, pd.DataFrame]:
    """Matches where Pinnacle's early Asian handicap line is 0: the synthetic DNB's margin
    against the real AH 0's, the gap between their fair home chances, and both DNB rules
    decided and paid at the AH-0 price (CLV against the same fair DNB close)."""
    need = {"ah_early_line", "ah_early_home", "ah_early_away"}
    if not len(cand) or not need <= set(cand.columns):
        return {"matches": 0}, dnb_bets(cand.iloc[0:0], league)
    zero = cand[
        (cand["ah_early_line"] == 0)
        & (cand["ah_early_home"] > 1)
        & (cand["ah_early_away"] > 1)
        & cand["dnb_home"].notna()
    ].copy()
    if not len(zero):
        return {"matches": 0}, dnb_bets(zero, league)
    zero["ah0_home"] = zero["ah_early_home"]
    zero["ah0_away"] = zero["ah_early_away"]
    syn = 1 / zero["dnb_home"] + 1 / zero["dnb_away"] - 1
    ah = 1 / zero["ah0_home"] + 1 / zero["ah0_away"] - 1
    fair_ah = np.array(
        [
            devig_shin(np.array([h, a]))[0]
            for h, a in zip(zero["ah0_home"], zero["ah0_away"], strict=True)
        ]
    )
    gap = np.abs(fair_ah - zero["fair_home"].to_numpy(dtype=float))
    b = dnb_bets(zero, league, price="ah0")
    return {
        "matches": len(zero),
        "share": len(zero) / len(cand),
        "margin_synthetic": float(syn.median()),
        "margin_ah0": float(ah.median()),
        "fair_gap_mean": float(np.nanmean(gap)),
        "fair_gap_p90": float(np.nanquantile(gap, 0.9)),
    }, b


# ---------- one league ----------


def league_report(
    league: str, preds: pd.DataFrame, pins: pd.DataFrame, ah: pd.DataFrame | None = None
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """One league: coverage and the seasons scored (lean_backtest's rule), the candidates,
    every rule's bets on them (DNB and the 1X2 baselines), calibration and the AH-0 check.
    Returns (report, bets, AH-0 bets)."""
    merged = lb.with_pinnacle(preds, pins)
    train = mc.training_rows(merged, "h2h") if not merged.empty else merged
    cov = lb.coverage(pins)
    included = {c["season"] for c in cov if c["included"]}
    target = lb.target_rows(merged)
    scored, fits = lb.walk_forward(train, target)
    if len(scored):
        scored = scored[scored["season"].astype(str).isin(included)].reset_index(drop=True)
    sig = sigma_dnb(train, fits)
    starts = pd.to_datetime(scored["fit_from"]) if len(scored) else pd.Series(dtype=object)
    scored["sigma_dnb"] = [
        sig.get(d.date().isoformat(), np.nan) if pd.notna(d) else np.nan for d in starts
    ]
    cand = candidates(scored)
    if ah is not None and len(ah) and len(cand):
        keys = ["season", "home", "away"]
        cand = cand.merge(ah.drop_duplicates(keys), on=keys, how="left")
    b = pd.concat([dnb_bets(cand, league), x_bets(cand, league)], ignore_index=True)
    check, ah_b = ah_check(cand, league)
    vals = list(sig.values())
    rep = {
        "coverage": cov,
        "seasons_included": [c["label"] for c in cov if c["included"]],
        "seasons_excluded": [c["label"] for c in cov if not c["included"]],
        "candidates": len(cand),
        "with_blend": int(cand["qb_home"].notna().sum()) if len(cand) else 0,
        "draw_share": (
            float((cand["home_goals"] == cand["away_goals"]).mean()) if len(cand) else None
        ),
        "fits": len(fits),
        "sigma_dnb": {"min": min(vals), "max": max(vals)} if vals else None,
        "rules": rules_report(b),
        "calibration": calibration(cand, b),
        "ah0": {**check, "rules": {r: metrics(ah_b[ah_b["rule"] == r]) for r in DNB_RULES}}
        if len(ah_b)
        else check,
    }
    return rep, b, ah_b


def verdict(rules: dict) -> dict:
    """The pre-registered decision on the pooled rules: {rule: {passes, clv, clv_ci975}}."""
    out = {}
    for r in DNB_RULES:
        m = rules.get(r) or {}
        lo = (m.get("clv_ci975") or [None])[0]
        out[r] = {
            "passes": bool(lo is not None and lo > 0),
            "clv": m.get("clv"),
            "clv_ci975": m.get("clv_ci975"),
            "baseline": BASELINE[r],
            "baseline_clv": (rules.get(BASELINE[r]) or {}).get("clv"),
        }
    return out


def build(
    reports: dict[str, dict],
    league_bets: dict[str, pd.DataFrame],
    ah_bets: dict[str, pd.DataFrame],
    *,
    from_season: int,
    names: dict[str, str] | None = None,
    now: pd.Timestamp | None = None,
) -> dict:
    """The saved file (backtest/dnb_backtest.json): the rule, per-league reports, the
    pooled rules, calibration from the pooled bets and the verdict. No bet rows."""
    now = now or pd.Timestamp.now(tz="UTC")
    ok = [c for c, r in reports.items() if not r.get("error")]
    frames = [league_bets[c] for c in ok if len(league_bets.get(c, []))]
    pooled = pd.concat(frames, ignore_index=True) if frames else dnb_bets(pd.DataFrame(), "")
    ah_frames = [ah_bets[c] for c in ok if len(ah_bets.get(c, []))]
    ah_pooled = pd.concat(ah_frames, ignore_index=True) if ah_frames else pooled.iloc[0:0]
    rules = rules_report(pooled)
    by_league = {
        c: {r: metrics(league_bets[c][league_bets[c]["rule"] == r]) for r in RULES} for c in ok
    }
    bets_cal = {}
    for rule in DNB_RULES:
        x = pooled[(pooled["rule"] == rule) & (pooled["result"] != "drawn")]
        bets_cal[rule] = (
            cal.reliability(
                x["p"].astype(float),
                (x["result"] == "won").astype(float),
                implied=1 / x["odds"].astype(float),
            )
            if len(x)
            else {"n": 0}
        )
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "kind": KIND,
        "title": "Draw no bet replayed on Pinnacle's early prices",
        "note": NOTE,
        "rule": {
            "price": "Pinnacle early 1X2 (PSH/PSD/PSA): side x (1 - 1/draw), margin kept",
            "fair": "Shin-fair 1X2 with the draw taken out, p_side / (p_home + p_away)",
            "chance_12": "the model's walk-forward 1X2 with the draw taken out",
            "chance_lean": "the Lean backtest's walk-forward blend with the draw taken out",
            "sigma": "sd of blend DNB - fair DNB (Pinnacle's close), each fit's own rows",
            "threshold": THRESHOLD,
            "min_z": tr.LEAN_Z,
            "stake": STAKE,
            "from_season": lb.season_label(f"{from_season % 100:02d}{(from_season + 1) % 100:02d}"),
            "coverage_min": lb.COVERAGE_MIN,
            "clv": "entry DNB price x Pinnacle's fair DNB close - 1 (per stake not refunded); "
            "clv_staked = the same x the close's no-draw chance",
            "pass": "pooled CLV range at 97.5% above 0",
            "labels": RULE_LABELS,
        },
        "leagues": {c: {"name": (names or {}).get(c, c), **rep} for c, rep in reports.items()},
        "all": {
            "leagues": ok,
            "rules": rules,
            "by_league": by_league,
            "calibration_bets": bets_cal,
            "ah0": {r: metrics(ah_pooled[ah_pooled["rule"] == r]) for r in DNB_RULES},
        },
        "verdict": verdict(rules),
    }
