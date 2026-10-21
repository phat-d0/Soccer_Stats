"""The Champions League's match backtests at the close (owner, 21 Oct; 0 credits).

The cup has no early prices on football-data, only the closes bought on 13 Oct (data-log
odds_history/UCL_closes.jsonl, Pinnacle and DraftKings, one snapshot per kickoff slot).
So both live rules are replayed at DraftKings' close, on the cross-league model's
walk-forward chances (cup_model.history: a fit every Monday on matches played before it):

* Model 12%: the live rule (`trades.best_pick` at 12% on the raw model chance) against
  DraftKings' quoted close, $1, one bet per match (lab/edge12_filters.bets, baseline);
* Blend Lean: lean_backtest.walk_forward on the live fit's own rows (the model beside
  Pinnacle's margin-free close; match_calibration.training_rows), refitted every 28 days
  on matches before each block, sigma from each fit's rows, MIN_ROWS unchanged; the blend
  fed DraftKings' margin-free close, z against its quoted close (as live: fitted on
  Pinnacle, fed DraftKings).

Both skip what the live paper step skips: a club the model doesn't cover and low-data
matches. Results are the 90-minute score; CLV is entry odds x Pinnacle's Shin-fair close - 1.
Betting at the close leaves no time for the price to move, so this CLV only measures how
far DraftKings' close sits from Pinnacle's, not whether the rule beats a moving price.
Nothing here downloads odds: without the closes file a report is {error}.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import cup_model as cm
from soccer_stats import lean_backtest as lb
from soccer_stats import match_calibration as mc
from soccer_stats.odds import devig_shin

H2H = mc.H2H
KIND = "close"
CLOSES = "odds_history/UCL_closes.jsonl"  # on data-log (cup_odds.py)
PRICE = "DraftKings' close (bought, The Odds API), break-even = 1/decimal"
CLV = "entry odds x Pinnacle's margin-free (Shin) close - 1"
NOTE = (
    "Champions League, priced at the close, not early: the only cup prices we hold are the "
    "closes bought for 2024/25 and 2025/26 (Pinnacle and DraftKings, one snapshot a kickoff "
    "slot). Bets are taken at DraftKings' close on the cross-league model's walk-forward "
    "chances; CLV compares that price with Pinnacle's margin-free close at the same moment, "
    "so it shows how far DraftKings sits from Pinnacle, not price movement. Clubs the model "
    "doesn't cover and low-data matches are skipped, as in the live paper test."
)
NO_FILE = (
    "No Champions League closes file (data-log " + CLOSES + "), so there is nothing to "
    "replay; this backtest never buys prices."
)
COLS = [
    "espn_id",
    "date",
    "day",
    "season",
    "home",
    "away",
    "home_goals",
    "away_goals",
    "y",
    *[f"p_{m}" for m in H2H],
    *[f"odds_{m}" for m in H2H],
    *[f"mkt_{m}" for m in H2H],
    *[f"close_{m}" for m in H2H],
]


def _ok(df: pd.DataFrame, cols) -> np.ndarray:
    if df.empty:
        return np.zeros(0, dtype=bool)
    v = df[list(cols)].to_numpy(dtype=float)
    return (np.isfinite(v) & (v > 1)).all(axis=1)


def joined(preds: pd.DataFrame, pin: pd.DataFrame, dk: pd.DataFrame) -> pd.DataFrame:
    """cup_model.walk_forward rows with DraftKings' close as odds_<side> and Pinnacle's as
    close_<side> (cup_model.load_closes frames, ESPN's home and away)."""
    if preds.empty:
        return preds
    dk = dk.rename(columns={f"close_{m}": f"odds_{m}" for m in H2H})
    out = preds.merge(dk, on="espn_id", how="left").merge(pin, on="espn_id", how="left")
    for c in (*[f"odds_{m}" for m in H2H], *[f"close_{m}" for m in H2H]):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def coverage(df: pd.DataFrame) -> list[dict]:
    """Per season: finished matches with a 90-minute score, how many the model covers (and
    not low-data), how many have DraftKings' and Pinnacle's closes, and the candidates
    (all of those)."""
    out = []
    if df.empty:
        return out
    for season, g in df.groupby("season", sort=True):
        played = g["home_goals"].notna().to_numpy()
        model = (g["covered"].astype(bool) & ~g["low_data"].astype(bool)).to_numpy()
        dkp = _ok(g, [f"odds_{m}" for m in H2H])
        pinp = _ok(g, [f"close_{m}" for m in H2H])
        out.append(
            {
                "season": str(season),
                "label": lb.season_label(season),
                "matches": int(played.sum()),
                "model": int((played & model).sum()),
                "draftkings": int((played & dkp).sum()),
                "pinnacle": int((played & pinp).sum()),
                "candidates": int((played & model & dkp).sum()),
                "included": bool((played & model & dkp).any()),
            }
        )
    return out


def target_rows(df: pd.DataFrame) -> pd.DataFrame:
    """One candidate per finished, covered, not low-data match with DraftKings' full close:
    p_<side> (model), odds_<side> (DraftKings' quoted close), mkt_<side> (its Shin-fair
    price, what the blend is fed), close_<side> (Pinnacle's, for CLV), day = the kickoff
    date (the bet is taken at the close)."""
    if df.empty:
        return pd.DataFrame(columns=COLS)
    keep = (
        df["covered"].astype(bool)
        & ~df["low_data"].astype(bool)
        & df["home_goals"].notna()
        & df[[f"p_{m}" for m in H2H]].notna().all(axis=1)
    ).to_numpy() & _ok(df, [f"odds_{m}" for m in H2H])
    t = df[keep].copy()
    if t.empty:
        return pd.DataFrame(columns=COLS)
    fair = np.vstack([devig_shin(r) for r in t[[f"odds_{m}" for m in H2H]].to_numpy(dtype=float)])
    for i, m in enumerate(H2H):
        t[f"mkt_{m}"] = fair[:, i]
    t["date"] = pd.to_datetime(t["date"])
    t["day"] = t["date"]
    t["y"] = mc.outcome_index(t["home_goals"], t["away_goals"], "h2h")
    return t[COLS].sort_values("date").reset_index(drop=True)


def load(closes_path: Path | str | None) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """(Pinnacle, DraftKings) close frames, or None without the file."""
    if not closes_path or not Path(closes_path).exists():
        return None
    return cm.load_closes(closes_path, "pinnacle"), cm.load_closes(closes_path, "draftkings")


def base(preds: pd.DataFrame, pin: pd.DataFrame, dk: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """The shared part of both reports: kind, note, price, coverage, seasons, candidates."""
    df = joined(preds, pin, dk)
    cov = coverage(df)
    target = target_rows(df)
    return {
        "kind": KIND,
        "note": NOTE,
        "price": PRICE,
        "clv": CLV,
        "coverage": cov,
        "seasons_included": [c["label"] for c in cov if c["included"]],
        "seasons_excluded": [c["label"] for c in cov if not c["included"]],
        "candidates": len(target),
    }, target


def error_report(error: str) -> dict:
    return {"kind": KIND, "note": NOTE, "error": error}


def lean_report(
    preds: pd.DataFrame,
    pin: pd.DataFrame,
    dk: pd.DataFrame,
    refit: str = mc.REFIT,
    min_rows: int = mc.MIN_ROWS,
) -> tuple[dict, pd.DataFrame]:
    """Blend Lean at DraftKings' close: lean_backtest.walk_forward on the live fit's rows
    (match_calibration.training_rows on the model beside Pinnacle's close, the rows
    fit-match-blends uses), each block fitted on matches before it, at least `min_rows`."""
    rep, target = base(preds, pin, dk)
    train = mc.training_rows(cm.blend_preds(preds, pin), "h2h") if len(preds) else preds
    scored, fits = lb.walk_forward(train, target, refit, min_rows)
    b = lb.bets(scored, "UCL")
    sig = [f["sigma"] for f in fits]
    out_of_sample = int(scored["sigma"].notna().sum()) if len(scored) else 0
    rep.update(
        {
            "training_matches": len(train),
            "min_rows": min_rows,
            "fits": len(fits),
            "first_fit": fits[0]["from"] if fits else None,
            "sigma": {"min": min(sig), "max": max(sig)} if sig else None,
            "scored": out_of_sample,
            **lb.summary(b),
        }
    )
    rep["note"] = NOTE + " " + lean_note(len(train), len(target), out_of_sample, len(b), min_rows)
    return rep, b


def lean_note(train: int, candidates: int, scored: int, bets: int, min_rows: int) -> str:
    """Plain English: how many matches the blend could be fitted for out of sample."""
    if not scored:
        return (
            f"Blend Lean: the blend needs {min_rows} earlier matches with a model chance and "
            f"Pinnacle's close before its first fit, and there are {train} in all, so none of "
            f"the {candidates} candidate matches comes after a fit: 0 bets. The minimum isn't "
            "lowered for the backtest."
        )
    return (
        f"Blend Lean: the blend needs {min_rows} earlier matches before its first fit ({train} "
        f"in all), so only {scored} of the {candidates} candidate matches come after a fit, "
        f"giving {bets} bet{'s' if bets != 1 else ''}: far too few to judge."
    )
