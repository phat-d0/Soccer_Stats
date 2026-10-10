"""Blend Lean replayed on Pinnacle's early prices, per league (soccer-stats backtest-lean).

The live Lean rule (trades.lean_pick) bets the side whose blended chance beats the
quoted price's break-even by at least one sigma. This replays it walk-forward on
football-data, 0 credits:

* Blend fits: every REFIT days, each league's h2h blend (match_calibration.fit) on the
  same Pinnacle-only rows as the live fit (the model's walk-forward chance beside
  Pinnacle's margin-free close, from `from_season`, at least MIN_ROWS matches), using
  only matches played before the block starts. Sigma comes from each fit's own rows.
* Bets: the blend fed Pinnacle's **early** margin-free price (PSH/PSD/PSA, Shin) and the
  model's chance; z per side against the early quoted price (1/decimal, margin kept);
  the highest z at 1 or more is a Lean bet, 2 or more Strong; $1, one per match. A block
  is assigned by the day the bet is decided (EARLY_LEAD before kickoff), so every
  training match had finished before the early price was taken.
* Driver: the same z with the blend's model term removed (c = 0, a_k and b kept;
  trades.market_side_z); "market" if that alone reaches 1 sigma, else "model".
* Scored: win rate vs break-even, ROI with a match-resampled 95% range, CLV against
  Pinnacle's fair (Shin) close; per league, season, tier and driver. Only seasons where
  Pinnacle's early and close prices both cover COVERAGE_MIN of the played matches.

This is a tougher test than the live one (Pinnacle early is sharper than DraftKings) and
not the live book: a Pinnacle-fitted blend fed Pinnacle prices has none of the b > 1 /
DraftKings mismatch live Lean has.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.data import PIN_CLOSE, PIN_EARLY
from soccer_stats.odds import devig_shin

H2H = mc.H2H
EARLY_LEAD = pd.Timedelta(days=4)  # football-data's early price is taken 2-3 days out
COVERAGE_MIN = 0.90
STAKE = 1.0
N_BOOT = 2000
MAX_BETS = 6000  # compact rows kept in the file (newest first); aggregates use every bet
DRIVERS = ("market", "model")
BET_FIELDS = [
    "date",
    "league",
    "home",
    "away",
    "market",
    "odds",
    "p_bet",
    "p_model",
    "z",
    "z_market",
    "driver",
    "tier",
    "won",
    "profit",
    "clv",
]
NOTE = (
    "Blend Lean replayed on Pinnacle's early prices from football-data, not on DraftKings: "
    "each league's blend refitted every 28 days on earlier matches only, sigma from each "
    "fit's own matches, $1 a bet on the side at least 1 sigma above the early price's "
    "break-even, CLV against Pinnacle's margin-free close. Pinnacle is a sharper price than "
    "DraftKings, so this is a tougher test than the live one, and it isn't the live book: a "
    "blend fitted on Pinnacle and fed Pinnacle's own prices doesn't have the mismatch live "
    "Lean has (fitted on Pinnacle, fed DraftKings' price, with b above 1 sharpening it)."
)
DK_NOTE = (
    "The same Lean rule on DraftKings' own historical prices (Premier League only; the "
    "DraftKings backtest's walk-forward blend and sigma). Few matches qualify."
)


def season_label(code: str) -> str:
    """football-data's season code to a label: "1920" -> "2019/20"."""
    code = str(code)
    return f"20{code[:2]}/{code[2:]}" if len(code) == 4 and code.isdigit() else code


def _priced(df: pd.DataFrame, cols) -> np.ndarray:
    if df.empty:
        return np.zeros(0, dtype=bool)
    vals = df[list(cols)].to_numpy(dtype=float)
    return (np.isfinite(vals) & (vals > 1)).all(axis=1)


def coverage(pins: pd.DataFrame, min_share: float = COVERAGE_MIN) -> list[dict]:
    """Per season: played matches, how many have Pinnacle's full early and close 1X2, the
    shares, and `included` (both shares at least `min_share`)."""
    out = []
    for season, g in pins.groupby("season", sort=True):
        early, close = _priced(g, PIN_EARLY), _priced(g, PIN_CLOSE)
        n = len(g)
        se = float(early.mean()) if n else 0.0
        sc = float(close.mean()) if n else 0.0
        out.append(
            {
                "season": str(season),
                "label": season_label(season),
                "matches": n,
                "early": int(early.sum()),
                "close": int(close.sum()),
                "both": int((early & close).sum()),
                "early_share": round(se, 4),
                "close_share": round(sc, 4),
                "included": bool(n and se >= min_share and sc >= min_share),
            }
        )
    return out


def with_pinnacle(preds: pd.DataFrame, pins: pd.DataFrame) -> pd.DataFrame:
    """The model's walk-forward chances with Pinnacle's own early and close prices (`pins`,
    data.pinnacle_prices) in place of load_matches' columns, which fall back to the market
    average or Bet365 where Pinnacle is missing."""
    keys = ["season", "home", "away"]
    base = preds.drop(columns=[*PIN_EARLY, *PIN_CLOSE], errors="ignore")
    if base.empty:
        return base
    return base.merge(pins.drop(columns="date").drop_duplicates(keys), on=keys, how="left")


def target_rows(merged: pd.DataFrame) -> pd.DataFrame:
    """One candidate per predicted match with Pinnacle's full early price: date, day (the
    bet is decided: date - EARLY_LEAD), season, home, away, goals, y, p_<side> (model),
    odds_<side> (early quoted), mkt_<side> (early, Shin), close_<side>."""
    cols = [
        "date",
        "day",
        "season",
        "home",
        "away",
        "home_goals",
        "away_goals",
        "y",
        *[f"p_{m}" for m in H2H],
        *PIN_EARLY,
        *[f"mkt_{m}" for m in H2H],
        *PIN_CLOSE,
    ]
    if merged.empty:
        return pd.DataFrame(columns=cols)
    df = merged[_priced(merged, PIN_EARLY)].copy()
    df = df.dropna(subset=[f"p_{m}" for m in H2H])
    if df.empty:
        return pd.DataFrame(columns=cols)
    fair = np.vstack([devig_shin(r) for r in df[list(PIN_EARLY)].to_numpy(dtype=float)])
    for i, m in enumerate(H2H):
        df[f"mkt_{m}"] = fair[:, i]
    df["date"] = pd.to_datetime(df["date"])
    df["day"] = df["date"] - EARLY_LEAD
    df["y"] = mc.outcome_index(df["home_goals"], df["away_goals"], "h2h")
    for c in PIN_CLOSE:
        if c not in df:
            df[c] = np.nan
    return df[cols].sort_values("date").reset_index(drop=True)


def walk_forward(
    train: pd.DataFrame,
    target: pd.DataFrame,
    refit: str = mc.REFIT,
    min_rows: int = mc.MIN_ROWS,
) -> tuple[pd.DataFrame, list[dict]]:
    """Blended chances for each candidate from a fit on earlier matches only.

    `train` is match_calibration.training_rows output (date = kickoff day, Pinnacle's
    margin-free close), `target` is target_rows output. Each block of `refit` days, by the
    day each bet is decided (`day`), uses a fit on `train` matches played before the block
    starts, and sigma from that fit's own rows. Adds pb_<side> (the blend on the early
    fair price), pm_<side> (the same with the model term removed), sigma, fit_from (block
    start), fit_to (last training match) and fit_rows. Blocks with fewer than `min_rows`
    earlier matches get NaN (no bet). Also returns one {from, to, matches, coef, sigma}
    per fit.
    """
    pb, pm = [f"pb_{m}" for m in H2H], [f"pm_{m}" for m in H2H]
    mk, pc = [f"mkt_{m}" for m in H2H], [f"p_{m}" for m in H2H]
    out = target.copy()
    for c in (*pb, *pm, "sigma"):
        out[c] = np.nan
    out["fit_from"] = pd.NaT
    out["fit_to"] = pd.NaT
    out["fit_rows"] = 0
    fits: list[dict] = []
    if target.empty or train.empty:
        return out, fits
    day = pd.to_datetime(target["day"])
    tdate = pd.to_datetime(train["date"])
    edges = pd.date_range(
        day.min().normalize(), day.max().normalize() + pd.Timedelta(refit), freq=refit
    )
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        idx = target.index[(day >= lo) & (day < hi)]
        if not len(idx):
            continue
        rows = train[tdate < lo]
        coef = mc.fit(rows[mk], rows[pc], rows["y"], min_rows)
        if coef is None:
            continue
        sd = mc.sigma(coef, rows[mk], rows[pc])
        if not sd:
            continue
        block = target.loc[idx]
        out.loc[idx, pb] = mc.apply(coef, block[mk], block[pc])
        out.loc[idx, pm] = mc.without_model(coef, block[mk])
        out.loc[idx, "sigma"] = sd
        last = pd.to_datetime(rows["date"]).max()
        out.loc[idx, "fit_from"] = lo
        out.loc[idx, "fit_to"] = last
        out.loc[idx, "fit_rows"] = len(rows)
        fits.append(
            {
                "from": lo.date().isoformat(),
                "to": last.date().isoformat(),
                "matches": len(rows),
                "coef": coef,
                "sigma": round(float(sd), 5),
            }
        )
    return out, fits


def bets(scored: pd.DataFrame, league: str) -> pd.DataFrame:
    """The Lean bet per candidate (trades.lean_pick on pb_<side> against the early quoted
    price, at that block's sigma), with its driver (pm_<side>), outcome, $1 profit and CLV
    against Pinnacle's fair close. One row per bet."""
    out = []
    for r in scored.to_dict("records"):
        sd = r.get("sigma")
        if sd is None or not np.isfinite(sd) or sd <= 0:
            continue
        odds = {m: float(r[f"odds_{m}"]) for m in H2H}
        pbet = {m: float(r[f"pb_{m}"]) for m in H2H}
        if not all(np.isfinite(v) for v in pbet.values()):
            continue
        zm = tr.side_z({m: float(r[f"pm_{m}"]) for m in H2H}, odds, sd)
        pick = tr.lean_pick(pbet, odds, sd, z_market=zm or {})
        if not pick:
            continue
        m = pick["market"]
        w = tr.won(m, int(r["home_goals"]), int(r["away_goals"]))
        close = {k: r.get(f"close_{k}") for k in H2H}
        close = {
            k: (float(v) if v is not None and np.isfinite(v) else None) for k, v in close.items()
        }
        out.append(
            {
                "date": pd.Timestamp(r["date"]).date().isoformat(),
                "season": str(r["season"]),
                "league": league,
                "home": r["home"],
                "away": r["away"],
                "market": m,
                "odds": pick["odds"],
                "p_bet": pick["p_bet"],
                "p_model": float(r[f"p_{m}"]),
                "z": pick["z"],
                "z_market": pick["z_market"],
                "driver": pick["driver"],
                "tier": pick["tier"],
                "sigma": float(sd),
                "won": bool(w),
                "profit": STAKE * (pick["odds"] - 1) if w else -STAKE,
                "clv": tr.clv(pick["odds"], m, close),
            }
        )
    cols = [*BET_FIELDS, "season", "sigma"]
    return pd.DataFrame(out, columns=cols) if out else pd.DataFrame(columns=cols)


def _boot_mean(values: np.ndarray, seed: int = 0, n: int = N_BOOT) -> list[float] | None:
    """95% percentile range of the mean, resampling bets (each bet is one match)."""
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = values[rng.integers(0, len(values), size=(n, len(values)))].mean(axis=1)
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def metrics(df: pd.DataFrame) -> dict:
    """Bets, wins, win rate vs break-even, ROI (per $1) with its match-resampled 95% range,
    profit, CLV vs Pinnacle's fair close with its range, beat-the-close share, average odds
    and z, z_realized (how many standard errors the win rate sits above break-even) and
    bets per market."""
    n = len(df)
    if n == 0:
        return {"bets": 0}
    profit = df["profit"].to_numpy(dtype=float)
    odds = df["odds"].to_numpy(dtype=float)
    won = df["won"].astype(bool).to_numpy()
    be = 1 / odds
    se = float(np.sqrt((be * (1 - be)).mean() / n))
    clv = pd.to_numeric(df["clv"], errors="coerce").dropna().to_numpy(dtype=float)
    out = {
        "bets": n,
        "wins": int(won.sum()),
        "win_rate": float(won.mean()),
        "breakeven": float(be.mean()),
        "roi": float(profit.sum() / (STAKE * n)),
        "roi_ci95": _boot_mean(profit / STAKE),
        "profit": float(profit.sum()),
        "avg_odds": float(odds.mean()),
        "avg_z": float(df["z"].astype(float).mean()),
        "z_realized": float((won.mean() - be.mean()) / se) if se > 0 else None,
        "clv_bets": len(clv),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_ci95": _boot_mean(clv, seed=1),
        "beat_close": float((clv > 0).mean()) if len(clv) else None,
        "by_market": {k: int(v) for k, v in df["market"].value_counts().items()},
    }
    return out


def _tier(df: pd.DataFrame) -> dict:
    return {
        **metrics(df),
        "by_driver": {d: metrics(df[df["driver"] == d]) for d in DRIVERS},
    }


def summary(df: pd.DataFrame) -> dict:
    """{lean (every bet, z >= 1), strong (z >= 2), each with by_driver; by_season: Lean
    metrics per season with its Strong, market and model counts}."""
    strong = df[df["tier"] == "strong"] if len(df) else df
    seasons = []
    for season, g in df.groupby("season", sort=True) if len(df) else []:
        seasons.append(
            {
                "season": str(season),
                "label": season_label(season),
                **metrics(g),
                "strong_bets": int((g["tier"] == "strong").sum()),
                "market_bets": int((g["driver"] == "market").sum()),
                "model_bets": int((g["driver"] == "model").sum()),
            }
        )
    return {"lean": _tier(df), "strong": _tier(strong), "by_season": seasons}


def league_report(
    league: str,
    preds: pd.DataFrame,
    pins: pd.DataFrame,
    refit: str = mc.REFIT,
    min_rows: int = mc.MIN_ROWS,
) -> tuple[dict, pd.DataFrame]:
    """One league's replay from the model's walk-forward chances (`preds`,
    backtest.walk_forward) and Pinnacle's prices (`pins`, data.pinnacle_prices for the
    seasons predicted): coverage, the seasons scored, fits, sigma range, candidates and
    summary(); plus its bets (only in seasons that pass the coverage filter)."""
    merged = with_pinnacle(preds, pins)
    train = mc.training_rows(merged, "h2h") if not merged.empty else merged
    cov = coverage(pins)
    target = target_rows(merged)
    scored, fits = walk_forward(train, target, refit, min_rows)
    included = {c["season"] for c in cov if c["included"]}
    scored = scored[scored["season"].astype(str).isin(included)] if len(scored) else scored
    b = bets(scored, league)
    sig = [f["sigma"] for f in fits]
    rep = {
        "coverage": cov,
        "seasons_included": [c["label"] for c in cov if c["included"]],
        "seasons_excluded": [c["label"] for c in cov if not c["included"]],
        "training_matches": len(train),
        "fits": len(fits),
        "first_fit": fits[0]["from"] if fits else None,
        "sigma": {"min": min(sig), "max": max(sig)} if sig else None,
        "candidates": int(scored["sigma"].notna().sum()) if len(scored) else 0,
        **summary(b),
    }
    return rep, b


def dk_replay(dk: dict | None) -> dict | None:
    """E0_dk.json -> tiers (the DraftKings backtest's Lean replay), labelled as such."""
    tiers = (dk or {}).get("tiers")
    if not isinstance(tiers, dict):
        return None
    seasons = (dk or {}).get("seasons")
    return {
        "label": "DraftKings replay, Premier League" + (f" (seasons {seasons})" if seasons else ""),
        "source": "backtest/E0_dk.json -> tiers",
        "book": "DraftKings",
        "lean": tiers.get("lean"),
        "strong": tiers.get("strong"),
        "sigma": tiers.get("sigma"),
        "note": DK_NOTE,
    }


def compact(df: pd.DataFrame) -> list[list]:
    """BET_FIELDS rows (about 100 bytes each), newest first."""
    if df.empty:
        return []
    df = df.sort_values(["date", "league", "home"], ascending=[False, True, True])
    rows = []
    for r in df.to_dict("records"):
        clv = r["clv"]
        zm = r["z_market"]
        rows.append(
            [
                r["date"],
                r["league"],
                r["home"],
                r["away"],
                r["market"],
                round(float(r["odds"]), 3),
                round(float(r["p_bet"]), 4),
                round(float(r["p_model"]), 4),
                round(float(r["z"]), 2),
                None if zm is None or not np.isfinite(zm) else round(float(zm), 2),
                r["driver"],
                r["tier"],
                int(bool(r["won"])),
                round(float(r["profit"]), 3),
                None if clv is None or not np.isfinite(clv) else round(float(clv), 4),
            ]
        )
    return rows


def build(
    reports: dict[str, dict],
    all_bets: list[pd.DataFrame],
    *,
    from_season: int,
    dk: dict | None = None,
    names: dict[str, str] | None = None,
    max_bets: int = MAX_BETS,
    now: pd.Timestamp | None = None,
) -> dict:
    """The saved file (backtest/lean_backtest.json): the rule, per-league reports, the
    pooled view across leagues, the DraftKings replay and the compact bets."""
    now = now or pd.Timestamp.now(tz="UTC")
    frames = [b for b in all_bets if len(b)]
    pooled = pd.concat(frames, ignore_index=True) if frames else bets(pd.DataFrame(), "")
    rows = compact(pooled)
    by_league = {code: summary(b)["lean"] for code, b in zip(reports, all_bets, strict=True)}
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "kind": "lean_backtest",
        "title": "Blend Lean replayed on Pinnacle's early prices",
        "note": NOTE,
        "rule": {
            "price": "Pinnacle early (football-data PSH/PSD/PSA), break-even = 1/decimal",
            "blend_fit": "Pinnacle's margin-free close beside the model's walk-forward chance",
            "refit": mc.REFIT,
            "min_rows": mc.MIN_ROWS,
            "from_season": season_label(f"{from_season % 100:02d}{(from_season + 1) % 100:02d}"),
            "min_z": tr.LEAN_Z,
            "strong_z": tr.STRONG_Z,
            "stake": STAKE,
            "coverage_min": COVERAGE_MIN,
            "clv": "entry odds x Pinnacle's margin-free (Shin) close - 1",
            "driver": "market = still at least 1 sigma with the model's term taken out of "
            "the fitted blend (c = 0, a and b kept as fitted, no refit); model = only with it",
            "driver_note": tr.DRIVER_NOTE,
        },
        "leagues": {
            code: {"name": (names or {}).get(code, code), **rep} for code, rep in reports.items()
        },
        "all": {**summary(pooled), "by_league": by_league},
        "draftkings_replay": dk_replay(dk),
        "bet_fields": BET_FIELDS,
        "bets_total": len(rows),
        "bets": rows[:max_bets],
    }


# ---------- what publish shows (paper.run -> portfolio.strategy_backtests.lean) ----------

APP_BETS = 200  # compact rows in data.json (newest first); every aggregate stays
MISSING_NOTE = (
    "The Lean backtest hasn't been run yet (backtest/lean_backtest.json is missing), so "
    "there is no history to show."
)


def app_section(found: dict | None, max_bets: int = APP_BETS) -> dict:
    """data.json's portfolio.strategy_backtests.lean: the file with its bets trimmed to the
    newest `max_bets` (`bets_shown`, `bets_total`), `available` True; without a file,
    {available: False, note}."""
    if not isinstance(found, dict) or found.get("kind") != "lean_backtest":
        return {"available": False, "note": MISSING_NOTE}
    rows = found.get("bets") or []
    return {
        **{k: v for k, v in found.items() if k != "bets"},
        "available": True,
        "bets": rows[:max_bets],
        "bets_shown": min(len(rows), max_bets),
        "bets_total": found.get("bets_total", len(rows)),
    }
