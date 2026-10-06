"""FanDuel's over-only player shot lines, sliced without the model.

Input is backtest/E0_player_lines.csv.gz (one row per priced over, with the result).
For each slice: how many lines, the average implied chance (1/odds, margin included),
how often the over won, and the return from backing every over in it. A slice whose
return is near zero is close to fair; the margin is the implied-minus-won gap.

Bets on one match share its game state, so ranges resample whole matches. Every
slice examined is counted, and the ranges are widened (Bonferroni) for that count.
The best slices on the earlier seasons are then checked on the latest season.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.edge.stats import bonferroni_level, bootstrap_mean

ODDS_BINS = [1.0, 1.25, 1.5, 2.0, 3.0, 5.0, 10.0, np.inf]
ODDS_LABELS = ["<=1.25", "1.25-1.5", "1.5-2", "2-3", "3-5", "5-10", ">10"]
ONE_WAY = ["market", "line", "odds_band", "kind", "position", "started", "move", "season"]
TWO_WAY = [
    ("market", "line"),
    ("market", "odds_band"),
    ("kind", "odds_band"),
    ("position", "market"),
    ("started", "kind"),
    ("move", "market"),
]


def prepare(lines: pd.DataFrame) -> pd.DataFrame:
    """Adds ret (return per unit on the over), odds_band, season, match and move.

    move compares a close line with the same player's look line: "shortened" when the
    over's odds fell, "drifted" when they rose, "same", or "no look" (look rows: "look").
    """
    d = lines.copy()
    d["kickoff"] = pd.to_datetime(d["kickoff"], utc=True)
    d["ret"] = d["won"] * d["odds"] - 1
    d["odds_band"] = pd.cut(d["odds"], ODDS_BINS, labels=ODDS_LABELS, right=True).astype(str)
    y = np.where(d["kickoff"].dt.month >= 7, d["kickoff"].dt.year, d["kickoff"].dt.year - 1)
    d["season"] = [f"{a % 100:02d}{(a + 1) % 100:02d}" for a in y]
    d["match"] = d["kickoff"].astype(str) + "|" + d["home"]
    key = ["match", "player", "market", "line"]
    look = d[d["kind"] == "look"].drop_duplicates(key)[[*key, "odds"]]
    d = d.merge(look.rename(columns={"odds": "look_odds"}), on=key, how="left")
    d["move"] = np.select(
        [
            d["kind"] == "look",
            d["look_odds"].isna(),
            d["odds"] < d["look_odds"],
            d["odds"] > d["look_odds"],
        ],
        ["look", "no look", "shortened", "drifted"],
        "same",
    )
    return d.drop(columns="look_odds")


def slice_table(d: pd.DataFrame, by, level: float = 0.95, seed: int = 0) -> pd.DataFrame:
    """One row per value of `by`: lines, matches, implied, won, gap, ROI and its range."""
    by = [by] if isinstance(by, str) else list(by)
    rows = []
    for key, x in d.groupby(by, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        ci = bootstrap_mean(x["ret"], clusters=x["match"], level=level, seed=seed, n=1000)
        rows.append(
            {
                "slice": " & ".join(f"{b}={v}" for b, v in zip(by, key, strict=True)),
                "lines": len(x),
                "matches": x["match"].nunique(),
                "implied": float(x["implied"].mean()),
                "won": float(x["won"].mean()),
                "gap": float(x["implied"].mean() - x["won"].mean()),
                "roi": float(x["ret"].mean()),
                "roi_lo": ci[0] if ci else None,
                "roi_hi": ci[1] if ci else None,
            }
        )
    return pd.DataFrame(rows)


def scan(d: pd.DataFrame, min_lines: int = 300, seed: int = 0) -> tuple[pd.DataFrame, int]:
    """Every one-way and two-way slice with at least `min_lines` lines.

    Returns (table sorted by ROI, number of slices examined). Ranges are Bonferroni
    widened for that number, so a slice whose upper end is above zero is not ruled out.
    """
    groups = [[c] for c in ONE_WAY] + [list(t) for t in TWO_WAY]
    sizes = [d.groupby(g, observed=True).size() for g in groups]
    tests = int(sum((s >= min_lines).sum() for s in sizes))
    level = bonferroni_level(tests)
    frames = [slice_table(d, g, level=level, seed=seed) for g in groups]
    t = pd.concat(frames, ignore_index=True)
    t = t[t["lines"] >= min_lines].sort_values("roi", ascending=False).reset_index(drop=True)
    return t, tests


def holdout(d: pd.DataFrame, holdout_season: str, top: int = 5, min_lines: int = 300) -> list:
    """The `top` slices by ROI on seasons before `holdout_season`, re-measured on it."""
    early = d[d["season"] < holdout_season]
    late = d[d["season"] == holdout_season]
    t, _ = scan(early, min_lines=min_lines)
    out = []
    for s in t.head(top)["slice"]:
        mask_e, mask_l = _mask(early, s), _mask(late, s)
        r = late.loc[mask_l, "ret"]
        ci = bootstrap_mean(r, clusters=late.loc[mask_l, "match"], n=1000)
        out.append(
            {
                "slice": s,
                "discovery_lines": int(mask_e.sum()),
                "discovery_roi": float(early.loc[mask_e, "ret"].mean()),
                "holdout_lines": int(mask_l.sum()),
                "holdout_roi": float(r.mean()) if len(r) else None,
                "holdout_ci95": ci,
            }
        )
    return out


def _mask(d: pd.DataFrame, s: str) -> pd.Series:
    m = pd.Series(True, index=d.index)
    for part in s.split(" & "):
        col, val = part.split("=", 1)
        m &= d[col].astype(str) == val
    return m
