"""The match model on markets beyond DraftKings' 1X2, against Pinnacle (free data).

football-data.co.uk's season files carry, per Premier League match:
- 1X2 (Pinnacle early and close, market average and maximum);
- over/under 2.5 goals (the only totals line it has; Pinnacle from 2019/20);
- one Asian handicap line (AHh early, AHCh close) with Pinnacle's two-way prices at it
  (from 2019/20), plus the market average and maximum at the early line.

For each market this replays, out of sample:
- log loss of the model, Pinnacle's margin-free early and closing prices, and the blend
  (match_calibration, walk-forward fits on earlier matches only);
- the weight the model earns in the blend (c) and any extra signal's weight (d);
- the trade rule (one bet per match, the side with the larger edge) over the threshold
  sweep at each price source, with ROI, a bootstrap 95% range and CLV against
  Pinnacle's fair close.

Asian handicap: quarter lines are two half-stakes on the neighbouring lines, whole lines
push on the handicap. A side's "chance" is its win share among stakes that are not
pushed, (W + HW/2) / (W + HW/2 + L + HL/2): the price at which the bet breaks even.
Fits weight each match by its unpushed share (1, 0.5 or 0).

Everything here works on frames in memory; the CLI loads the data (in Actions).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.edge.stats import bootstrap_mean
from soccer_stats.odds import devig_shin

GROUPS = {"h2h": mc.H2H, "totals": mc.TOTALS, "ah": mc.AH}
WHEN = ("early", "close")
# (group, when, source) -> football-data columns, one per outcome in GROUPS order.
COLUMNS = {
    ("h2h", "early", "pinnacle"): ("PSH", "PSD", "PSA"),
    ("h2h", "early", "avg"): ("AvgH", "AvgD", "AvgA"),
    ("h2h", "early", "max"): ("MaxH", "MaxD", "MaxA"),
    ("h2h", "close", "pinnacle"): ("PSCH", "PSCD", "PSCA"),
    ("totals", "early", "pinnacle"): ("P>2.5", "P<2.5"),
    ("totals", "early", "avg"): ("Avg>2.5", "Avg<2.5"),
    ("totals", "early", "max"): ("Max>2.5", "Max<2.5"),
    ("totals", "close", "pinnacle"): ("PC>2.5", "PC<2.5"),
    ("ah", "early", "pinnacle"): ("PAHH", "PAHA"),
    ("ah", "early", "avg"): ("AvgAHH", "AvgAHA"),
    ("ah", "early", "max"): ("MaxAHH", "MaxAHA"),
    ("ah", "close", "pinnacle"): ("PCAHH", "PCAHA"),
}
AH_LINE = {"early": "AHh", "close": "AHCh"}  # the home side's handicap
SOURCES = {  # price source -> when it is taken
    "pinnacle_early": "early",
    "avg_early": "early",
    "max_early": "early",
    "pinnacle_close": "close",
}
# Extra signals per group, one blend variant each (columns from xg_features and
# soft_vs_sharp). The xG terms repeat the edge-finder's 1X2/totals tests (same 6-match
# window, no tuning); soft_vs_sharp on AH was the edge-finder's suggestion.
SIGNALS = {
    "h2h": {"blend_xg": ("x_xgd", "x_luck")},
    "ah": {"blend_xg": ("x_xgd", "x_luck"), "blend_svs": ("x_svs",)},
    "totals": {"blend_xg": ("x_xgt", "x_luckt")},
}
# Price sanity. A source's row is dropped when its overround is implausible (stale or
# mismatched quotes; a two-way market's maximum can't sit far under 100%), or, for the
# average and maximum, when its margin-free chance is far from Pinnacle's at the same
# time (a different line or a typo). Counts are reported in price_check.
OVERROUND = (-0.03, 0.20)
MAX_GAP = 0.10


# ---------- prices ----------


def load_prices(raw: pd.DataFrame, season: str | None = None) -> pd.DataFrame:
    """One row per played match: result, AH lines and `{group}_{when}_{source}_{m}` prices.

    Missing columns (older seasons, books not listed) are NaN.
    """
    df = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    out = {
        "date": pd.to_datetime(df["Date"], dayfirst=True, format="mixed").dt.normalize(),
        "home": df["HomeTeam"],
        "away": df["AwayTeam"],
        "home_goals": df["FTHG"].astype(int),
        "away_goals": df["FTAG"].astype(int),
    }
    for when, col in AH_LINE.items():
        out[f"line_{when}"] = pd.to_numeric(df[col], errors="coerce") if col in df else np.nan
    for (g, when, src), cols in COLUMNS.items():
        for m, c in zip(GROUPS[g], cols, strict=True):
            v = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
            v = pd.Series(v, index=df.index, dtype=float)
            out[f"{g}_{when}_{src}_{m}"] = v.where(v > 1)
    res = pd.DataFrame(out, index=df.index).reset_index(drop=True)
    res["season"] = season
    return res


def clean_prices(joined: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Blank implausible prices (see OVERROUND, MAX_GAP); one report row per source."""
    df = joined.copy()
    report = []
    for (g, when, src), _ in COLUMNS.items():
        cols = [f"{g}_{when}_{src}_{m}" for m in GROUPS[g]]
        if not set(cols) <= set(df.columns):
            continue
        odds = df[cols].to_numpy(dtype=float)
        priced = np.isfinite(odds).all(1)
        over = (1 / odds).sum(1) - 1
        bad = priced & ((over < OVERROUND[0]) | (over > OVERROUND[1]))
        gap_bad = np.zeros(len(df), dtype=bool)
        if src != "pinnacle":
            pin = df[[f"{g}_{when}_pinnacle_{m}" for m in GROUPS[g]]].to_numpy(dtype=float)
            gap = np.abs(_devig(odds)[:, 0] - _devig(pin)[:, 0])
            gap_bad = priced & ~bad & (gap > MAX_GAP)
        df.loc[bad | gap_bad, cols] = np.nan
        report.append(
            {
                "market": g,
                "when": when,
                "source": src,
                "priced": int(priced.sum()),
                "overround_median": float(np.nanmedian(over[priced])) if priced.any() else None,
                "dropped_overround": int(bad.sum()),
                "dropped_far_from_pinnacle": int(gap_bad.sum()),
                "max_odds": float(np.nanmax(odds[priced])) if priced.any() else None,
            }
        )
    return df, report


def soft_vs_sharp(joined: pd.DataFrame) -> pd.Series:
    """x_svs: log(home/away) from the market average's early 1X2 (Shin) minus the same
    from Pinnacle's early price. Positive = soft books rate the home side higher."""
    names = GROUPS["h2h"]
    avg = _devig(joined[[f"h2h_early_avg_{m}" for m in names]].to_numpy(dtype=float))
    pin = _devig(joined[[f"h2h_early_pinnacle_{m}" for m in names]].to_numpy(dtype=float))
    with np.errstate(divide="ignore", invalid="ignore"):
        x = np.log(avg[:, 0] / avg[:, 2]) - np.log(pin[:, 0] / pin[:, 2])
    return pd.Series(x, index=joined.index)


def join(preds: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Walk-forward predictions (with `matrix`) beside the prices, on date and teams."""
    p = preds.drop(columns=["home_goals", "away_goals", "season"], errors="ignore").copy()
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    q = prices.copy()
    q["date"] = pd.to_datetime(q["date"]).dt.normalize()
    return p.merge(q, on=["date", "home", "away"], how="inner").reset_index(drop=True)


# ---------- Asian handicap ----------


def ah_parts(line: float) -> list[tuple[float, float]]:
    """(handicap, share of stake): a quarter line is half on each neighbouring line."""
    line = float(line)
    if round(line * 4) % 2:  # x.25 or x.75
        return [(line - 0.25, 0.5), (line + 0.25, 0.5)]
    return [(line, 1.0)]


def ah_shares(m: np.ndarray, line: float) -> tuple[float, float]:
    """(win share, loss share) of a home bet at handicap `line` under score matrix `m`.

    Half-wins count 0.5 to the win share; pushes count to neither.
    """
    diff = np.subtract.outer(np.arange(m.shape[0]), np.arange(m.shape[1]))
    win = loss = 0.0
    for h, share in ah_parts(line):
        win += share * float(m[diff + h > 0].sum())
        loss += share * float(m[diff + h < 0].sum())
    return win, loss


def ah_settle(line: float, home_goals: int, away_goals: int) -> tuple[float, float]:
    """(win share, loss share) of a settled home bet: 1/0, 0.5/0 (half-win), 0/0 (push)..."""
    d = home_goals - away_goals
    win = sum(s for h, s in ah_parts(line) if d + h > 0)
    loss = sum(s for h, s in ah_parts(line) if d + h < 0)
    return float(win), float(loss)


def ah_chance(win: float, loss: float) -> float:
    """Break-even chance: the win share among unpushed stakes."""
    return win / (win + loss) if win + loss > 0 else np.nan


# ---------- candidates (one row per match, group and time) ----------


def _devig(odds: np.ndarray) -> np.ndarray:
    out = np.full(odds.shape, np.nan)
    for i in np.flatnonzero(np.isfinite(odds).all(1) & (odds > 1).all(1)):
        out[i] = devig_shin(odds[i])
    return out


def candidates(joined: pd.DataFrame, group: str, when: str) -> pd.DataFrame:
    """Rows for one market at one time: date, season, p_<m> (model), mkt_<m> (Pinnacle's
    margin-free price then), win_<m>/loss_<m> (settled shares), odds_<source>_<m> for the
    sources taken then, fair_close_<m> (for CLV; NaN where an AH line moved), y and w
    (the blend's outcome index and weight), and any x_* signal columns.
    """
    names = GROUPS[group]
    n = len(joined)
    hg = joined["home_goals"].to_numpy(dtype=int)
    ag = joined["away_goals"].to_numpy(dtype=int)
    out = pd.DataFrame(
        {"date": joined["date"].to_numpy(), "season": joined["season"].to_numpy()},
        index=joined.index,
    )
    out["home"], out["away"] = joined["home"], joined["away"]
    p = np.full((n, len(names)), np.nan)
    win = np.zeros((n, len(names)))
    loss = np.zeros((n, len(names)))
    if group == "ah":
        line = joined[f"line_{when}"].to_numpy(dtype=float)
        for i, (mtx, ln) in enumerate(zip(joined["matrix"], line, strict=True)):
            if not np.isfinite(ln):
                continue
            w_, l_ = ah_shares(mtx, ln)
            p[i] = [ah_chance(w_, l_), ah_chance(l_, w_)]
            sw, sl = ah_settle(ln, hg[i], ag[i])
            win[i], loss[i] = [sw, sl], [sl, sw]
        out["line"] = line
    else:
        p = joined[[f"p_{m}" for m in names]].to_numpy(dtype=float)
        y = mc.outcome_index(hg, ag, group)
        win = np.eye(len(names))[y]
        loss = 1 - win
    for j, m in enumerate(names):
        out[f"p_{m}"] = p[:, j]
        out[f"win_{m}"] = win[:, j]
        out[f"loss_{m}"] = loss[:, j]
    pin = joined[[f"{group}_{when}_pinnacle_{m}" for m in names]].to_numpy(dtype=float)
    mk = _devig(pin)
    close = _devig(joined[[f"{group}_close_pinnacle_{m}" for m in names]].to_numpy(dtype=float))
    if group == "ah":
        same = joined["line_early"].to_numpy(dtype=float) == joined["line_close"].to_numpy(
            dtype=float
        )
        close[~same] = np.nan
    for j, m in enumerate(names):
        out[f"mkt_{m}"] = mk[:, j]
        out[f"fair_close_{m}"] = close[:, j]
        for src, w in SOURCES.items():
            if w == when:
                book = src.rsplit("_", 1)[0]
                out[f"odds_{src}_{m}"] = joined.get(f"{group}_{when}_{book}_{m}", np.nan)
    # The blend's outcome index and weight: an AH match counts by its unpushed share.
    if group == "ah":
        out["y"] = np.where(win[:, 0] > 0, 0, 1)
        out["w"] = win[:, 0] + loss[:, 0]
    else:
        out["y"] = np.argmax(win, axis=1)
        out["w"] = 1.0
    for c in joined.columns:
        if c.startswith("x_"):
            out[c] = joined[c].to_numpy(dtype=float)
    keep = np.isfinite(p).all(1) & np.isfinite(mk).all(1)
    return out[keep].reset_index(drop=True)


# ---------- xG signals ----------


def xg_features(matches: pd.DataFrame, n: int = 6, min_games: int = 3) -> pd.DataFrame:
    """Per match, form signals from each side's previous `n` league matches only.

    x_xgd:   home (xG for - xG against per game) minus the away side's;
    x_luck:  the same for (xG difference - goal difference): positive = the home side
             has been unluckier than the away side, so its results understate it;
    x_xgt:   both sides' (xG for + xG against) per game, added (expected goals in play);
    x_luckt: both sides' (xG total - goals total) per game, added.
    Returns date, home, away and the four columns (NaN without `min_games` earlier games).
    """
    m = matches.sort_values("date").reset_index(drop=True)
    has = m["home_xg"].notna() & m["away_xg"].notna()
    side = []
    for is_home, team, xf, xa, gf, ga in (
        (True, "home", "home_xg", "away_xg", "home_goals", "away_goals"),
        (False, "away", "away_xg", "home_xg", "away_goals", "home_goals"),
    ):
        side.append(
            pd.DataFrame(
                {
                    "i": m.index,
                    "date": m["date"],
                    "team": m[team],
                    "is_home": is_home,
                    "xgd": (m[xf] - m[xa]).where(has),
                    "luck": ((m[xf] - m[xa]) - (m[gf] - m[ga])).where(has),
                    "xgt": (m[xf] + m[xa]).where(has),
                    "luckt": ((m[xf] + m[xa]) - (m[gf] + m[ga])).where(has),
                }
            )
        )
    long = pd.concat(side).sort_values(["date", "i"]).reset_index(drop=True)
    cols = ["xgd", "luck", "xgt", "luckt"]
    form = (
        long.groupby("team")[cols]
        .transform(lambda s: s.shift(1).rolling(n, min_periods=min_games).mean())
        .add_prefix("f_")
    )
    long = pd.concat([long, form], axis=1)
    h = long[long["is_home"]].set_index("i")
    a = long[~long["is_home"]].set_index("i")
    out = m[["date", "home", "away"]].copy()
    out["x_xgd"] = h["f_xgd"] - a["f_xgd"]
    out["x_luck"] = h["f_luck"] - a["f_luck"]
    out["x_xgt"] = h["f_xgt"] + a["f_xgt"]
    out["x_luckt"] = h["f_luckt"] + a["f_luckt"]
    return out


# ---------- blend, scoring, bets ----------


def blend(
    train: pd.DataFrame,
    target: pd.DataFrame,
    group: str,
    features: tuple[str, ...] = (),
    min_rows: int = mc.MIN_ROWS,
) -> tuple[pd.DataFrame, list[dict]]:
    """Walk-forward blended chances for `target` (fits on `train` rows played earlier)."""
    return mc.walk_forward(train, target, group, min_rows=min_rows, features=features)


def log_loss(cands: pd.DataFrame, probs: dict[str, pd.DataFrame], group: str) -> dict:
    """Weighted log loss of each probability set on rows where all are present."""
    names = GROUPS[group]
    sets = {"model": cands[[f"p_{m}" for m in names]].to_numpy(dtype=float)}
    sets["pinnacle_close"] = cands[[f"mkt_{m}" for m in names]].to_numpy(dtype=float)
    for k, v in probs.items():
        sets[k] = v[list(names)].to_numpy(dtype=float)
    ok = np.ones(len(cands), dtype=bool)
    for v in sets.values():
        ok &= np.isfinite(v).all(1)
    w = cands["w"].to_numpy(dtype=float)[ok]
    y = cands["y"].to_numpy(dtype=int)[ok]
    out = {"matches": int(ok.sum())}
    if not ok.any() or w.sum() == 0:
        return out
    for k, v in sets.items():
        p = np.clip(v[ok][np.arange(ok.sum()), y], 1e-12, 1)
        out[k] = float((-np.log(p) * w).sum() / w.sum())
    return out


def loss_diff(cands: pd.DataFrame, a: pd.DataFrame, b: pd.DataFrame, group: str) -> dict:
    """Out-of-sample log loss of `b` minus `a` per match, with a bootstrap 95% range
    (negative = `b` is better)."""
    names = list(GROUPS[group])
    pa, pb = a[names].to_numpy(dtype=float), b[names].to_numpy(dtype=float)
    ok = np.isfinite(pa).all(1) & np.isfinite(pb).all(1) & (cands["w"].to_numpy() > 0)
    if ok.sum() < 2:
        return {"matches": int(ok.sum())}
    y = cands["y"].to_numpy(dtype=int)[ok]
    w = cands["w"].to_numpy(dtype=float)[ok]
    idx = np.arange(ok.sum())
    d = -np.log(np.clip(pb[ok][idx, y], 1e-12, 1)) + np.log(np.clip(pa[ok][idx, y], 1e-12, 1))
    d = d * w / w.mean()
    return {"matches": int(ok.sum()), "diff": float(d.mean()), "ci95": bootstrap_mean(d)}


def bets(
    cands: pd.DataFrame,
    probs: pd.DataFrame,
    group: str,
    source: str,
    threshold: float,
    max_odds: float | None = None,
) -> pd.DataFrame:
    """The trade rule at one price source: per match, the side with the larger edge
    (chance x odds - 1) if it reaches `threshold`; 1 unit staked."""
    names = GROUPS[group]
    p = probs[list(names)].to_numpy(dtype=float)
    cols = [f"odds_{source}_{m}" for m in names]
    if not set(cols) <= set(cands.columns):
        return pd.DataFrame(columns=["date", "season", "market", "odds", "edge", "profit", "clv"])
    odds = cands[cols].to_numpy(dtype=float)
    edge = p * odds - 1
    if max_odds is not None:
        edge[odds > max_odds] = np.nan
    edge[~np.isfinite(edge)] = -np.inf
    j = edge.argmax(1)
    rows = np.arange(len(cands))
    best = edge[rows, j]
    pick = best >= threshold
    r, k = rows[pick], j[pick]
    win = np.column_stack([cands[f"win_{m}"] for m in names])[r, k]
    loss = np.column_stack([cands[f"loss_{m}"] for m in names])[r, k]
    fair = np.column_stack([cands[f"fair_close_{m}"] for m in names])[r, k]
    o = odds[r, k]
    return pd.DataFrame(
        {
            "date": cands["date"].to_numpy()[r],
            "season": cands["season"].to_numpy()[r],
            "match": (cands["home"] + " v " + cands["away"]).to_numpy()[r],
            "market": np.array(names)[k],
            "odds": o,
            "p": p[r, k],
            "edge": best[pick],
            "profit": win * (o - 1) - loss,
            "clv": o * fair - 1 if SOURCES[source] == "early" else np.full(len(r), np.nan),
        }
    )


def summarize(b: pd.DataFrame, seed: int = 0) -> dict:
    if b.empty:
        return {"bets": 0}
    clv = b["clv"].dropna()
    return {
        "bets": len(b),
        "roi": float(b["profit"].mean()),
        "roi_ci95": bootstrap_mean(b["profit"], seed=seed),
        "avg_odds": float(b["odds"].mean()),
        "avg_p": float(b["p"].mean()),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_ci95": bootstrap_mean(clv, seed=seed) if len(clv) > 1 else None,
        "clv_bets": len(clv),
    }


def _c_range(fits: list[dict], k: int) -> list[float] | None:
    """Smallest and largest model weight c across the walk-forward refits."""
    cs = [f["coef"][k] for f in fits]
    return [min(cs), max(cs)] if cs else None


def _live_fit(
    rows: pd.DataFrame, group: str, features: tuple[str, ...], min_rows: int
) -> list | None:
    """A fit on every settled row (what the live app would use)."""
    names = GROUPS[group]
    r = rows.dropna(subset=list(features)) if features else rows
    return mc.fit(
        r[[f"mkt_{m}" for m in names]],
        r[[f"p_{m}" for m in names]],
        r["y"],
        min_rows,
        weight=r["w"],
        extra=r[list(features)] if features else None,
    )


def run_group(
    joined: pd.DataFrame, group: str, thresholds=tr.SWEEP, min_rows: int = mc.MIN_ROWS
) -> dict:
    """Everything for one market: log loss, blend weights, signal test, sweeps."""
    close = candidates(joined, group, "close")
    early = candidates(joined, group, "early")
    out: dict = {"group": group, "matches_close": len(close), "matches_early": len(early)}
    if close.empty:
        return out
    if group == "ah":
        out["ah_line_moved"] = float(
            (joined["line_early"] != joined["line_close"])[joined["line_close"].notna()].mean()
        )
        out["ah_push_share"] = float((close["w"] < 1).mean())
    names = list(GROUPS[group])
    variants = {"blend": ()}
    for name, feats in SIGNALS[group].items():
        if all(f in close and close[f].notna().any() for f in feats):
            variants[name] = feats
    probs: dict[str, dict[str, pd.DataFrame]] = {"close": {}, "early": {}}
    fits: dict[str, list] = {}
    for name, f in variants.items():
        probs["close"][name], fits[name] = blend(close, close, group, f, min_rows)
        probs["early"][name], _ = blend(close, early, group, f, min_rows)
    for when, c in (("close", close), ("early", early)):
        probs[when]["model"] = c[[f"p_{m}" for m in names]].set_axis(names, axis=1)

    # Scores on the same matches: every variant covered, at the close.
    out["log_loss"] = log_loss(close, {k: probs["close"][k] for k in variants}, group)
    early_mk = early[["date", "home", "away", *[f"mkt_{m}" for m in names]]].rename(
        columns={f"mkt_{m}": f"early_{m}" for m in names}
    )
    both = close.merge(early_mk, on=["date", "home", "away"], how="left")
    pe = both[[f"early_{m}" for m in names]].set_axis(names, axis=1)
    out["log_loss_early_pinnacle"] = log_loss(
        both, {"pinnacle_early": pe, **{k: probs["close"][k] for k in variants}}, group
    )
    out["signals"] = {
        k: {
            "features": list(f),
            "loss_diff_vs_blend": loss_diff(
                close, probs["close"]["blend"], probs["close"][k], group
            ),
        }
        for k, f in variants.items()
        if f
    }
    out["fits"] = {
        k: {
            "refits": len(v),
            "last": v[-1] if v else None,
            "c_range": _c_range(v, len(names)),
        }
        for k, v in fits.items()
    }
    out["live"] = {k: _live_fit(close, group, f, min_rows) for k, f in variants.items()}

    # Sweeps: only matches the blend covers, so every strategy bets on the same set.
    sweeps = []
    by_season = []
    for src, when in SOURCES.items():
        c = close if when == "close" else early
        covered = probs[when]["blend"].notna().all(axis=1)
        for strat in ["model", *variants]:
            pr = probs[when][strat][covered]
            cc = c[covered]
            for t in thresholds:
                b = bets(cc, pr, group, src, t)
                sweeps.append({"source": src, "strategy": strat, "threshold": t, **summarize(b)})
                if t == tr.PAPER_EDGE and src == "pinnacle_early":
                    for s, g in b.groupby("season"):
                        by_season.append({"strategy": strat, "season": s, **summarize(g)})
    out["sweep"] = sweeps
    out["by_season"] = by_season
    return out


def run(
    joined: pd.DataFrame, groups=tuple(GROUPS), thresholds=tr.SWEEP, min_rows: int = mc.MIN_ROWS
) -> dict:
    """Clean the prices, add soft_vs_sharp, then each market's results (+ price_check)."""
    df, check = clean_prices(joined)
    df["x_svs"] = soft_vs_sharp(df)
    out = {g: run_group(df, g, thresholds, min_rows) for g in groups}
    out["price_check"] = check
    return out
