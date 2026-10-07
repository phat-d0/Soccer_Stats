"""A minimum edge learned from history, instead of fixed edge buttons.

Given settled backtest bets, each with the edge the model claimed when it bet
(chance x odds - 1), how did bets at each claimed edge actually do? The recommended
minimum edge is the smallest claimed edge t from which bets claiming t to t + 5 points
(a rolling band) have a realized return per unit staked whose lower range bound is
above 0, and so does every higher band, found on the
development part of the history and confirmed on the later part. When no level
qualifies the answer is `min_edge: None` with a plain-English reason, which the app
shows as it is. The rules were fixed in docs/lab.md before any run.

Input: one row per bet with `edge`, `p` (the chance the edge was measured with),
`odds` (decimal), `won` (1/0; NaN = void, left out), `group` (the match: ranges
resample whole matches), `season`, `time` (for ordering) and optionally `implied`
(the bookmaker's chance; 1 / odds when absent).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.lab.metrics import boot_range

CONFIDENCE = 0.95  # two-sided range; the rule uses its lower end
GRID = tuple(round(0.01 * i, 2) for i in range(31))  # claimed edge 0%, 1%, ..., 30%
BAND = 0.05  # each level is judged on bets claiming t to t + BAND (the smoothing)
MIN_BETS = 30  # a band with fewer development bets is not judged
BUCKETS = (0.0, 0.02, 0.05, 0.08, 0.12, 0.20, 0.30, None)  # by_bucket edges (None = open)
METHOD = (
    "Smallest claimed edge t (0-30% in 1% steps) such that past bets claiming t to "
    f"t+{BAND:.0%} returned more than 0 per unit staked at the 95% lower bound, and so "
    f"did every higher band with at least {MIN_BETS} bets; ranges resample whole "
    "matches; found on development seasons, then checked on the latest season (bets "
    "at t and up must have returned more than 0)."
)


def _prep(bets: pd.DataFrame) -> pd.DataFrame:
    b = bets.copy()
    b = b[pd.to_numeric(b["won"], errors="coerce").notna()]
    b = b[(b["edge"] > 0) & (b["odds"] > 1)]
    b["won"] = b["won"].astype(int)
    if "implied" not in b or b["implied"].isna().all():
        b["implied"] = 1 / b["odds"]
    b["implied"] = b["implied"].fillna(1 / b["odds"])
    b["ret"] = b["won"] * b["odds"] - 1  # profit per unit staked
    return b.sort_values("time").reset_index(drop=True)


def split(b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Development and check parts: the latest season is the check; with one season,
    the first and second halves by kickoff."""
    seasons = sorted(b["season"].astype(str).unique())
    if len(seasons) >= 2:
        dev = b[b["season"].astype(str) != seasons[-1]]
        chk = b[b["season"].astype(str) == seasons[-1]]
        return dev, chk, {"development": seasons[:-1], "check": [seasons[-1]]}
    cut = b["time"].iloc[len(b) // 2] if len(b) else None
    dev, chk = b[b["time"] < cut], b[b["time"] >= cut]
    label = seasons[0] if seasons else None
    return dev, chk, {"development": [f"{label} first half"], "check": [f"{label} second half"]}


def _level(b: pd.DataFrame, lo: float, hi: float | None = None, seed: int = 0) -> dict:
    s = b[(b["edge"] >= lo - 1e-9) & ((b["edge"] < hi) if hi is not None else True)]
    out = {"n": len(s)}
    if len(s) == 0:
        return out
    g = s["group"].to_numpy()
    rr = boot_range(s["ret"], g, level=CONFIDENCE, seed=seed)
    wr = boot_range(s["won"], g, level=CONFIDENCE, seed=seed)
    out.update(
        {
            "implied": float(s["implied"].mean()),
            "model": float(s["p"].mean()),
            "realized": float(s["won"].mean()),
            "realized_lo": wr[0] if wr else None,
            "realized_hi": wr[1] if wr else None,
            "roi": float(s["ret"].mean()),
            "roi_lo": rr[0] if rr else None,
            "roi_hi": rr[1] if rr else None,
        }
    )
    return out


def scan(b: pd.DataFrame) -> list[dict]:
    """Bets claiming each GRID level to level + BAND: count and return range."""
    return [{"edge": t, "edge_hi": round(t + BAND, 2), **_level(b, t, t + BAND)} for t in GRID]


def pick(rows: list[dict]) -> float | None:
    """The smallest band whose lower bound, and every judged higher band's, is above 0."""
    judged = [r for r in rows if r["n"] >= MIN_BETS]
    for i, r in enumerate(judged):
        if all((x.get("roi_lo") or -1) > 0 for x in judged[i:]):
            return r["edge"]
    return None


def _pct(x: float) -> str:
    return f"{x:.0%}" if abs(x * 100 - round(x * 100)) < 1e-9 else f"{x:.1%}"


def edge_threshold(bets: pd.DataFrame, label: str = "bets") -> dict:
    """The recommended minimum edge (or None and why), with the evidence behind it.

    Returns the contract the app reads: min_edge, confidence, method, n_bets, seasons,
    note and by_bucket (edge_lo, edge_hi, n, implied, model, realized, realized_lo,
    realized_hi, plus roi, roi_lo, roi_hi), and the development scan and check.
    """
    b = _prep(bets)
    base = {"confidence": CONFIDENCE, "method": METHOD, "n_bets": len(b)}
    if len(b) < 2 * MIN_BETS:
        return {
            **base,
            "min_edge": None,
            "seasons": {},
            "note": f"Too few settled {label} with a positive edge ({len(b)}) to learn a "
            "minimum edge.",
            "by_bucket": [],
        }
    dev, chk, seasons = split(b)
    rows = scan(dev)
    found = pick(rows)
    by_bucket = [
        {"edge_lo": lo, "edge_hi": hi, **_level(b, lo, hi)}
        for lo, hi in zip(BUCKETS[:-1], BUCKETS[1:], strict=True)
    ]
    by_bucket = [r for r in by_bucket if r["n"] > 0]
    out = {**base, "seasons": seasons, "by_bucket": by_bucket, "development_scan": rows}
    dev_n = len(dev)
    if found is None:
        judged = [r for r in rows if r["n"] >= MIN_BETS]
        best = max(judged, key=lambda r: r["roi"]) if judged else None
        why = (
            f" The best band was {_pct(best['edge'])}-{_pct(best['edge_hi'])}: {best['n']} "
            "bets returned "
            f"{best['roi']:+.1%} a bet (range {best['roi_lo']:+.1%} to {best['roi_hi']:+.1%})."
            if best
            else ""
        )
        return {
            **out,
            "min_edge": None,
            "note": f"No minimum edge works. At every claimed edge from 0% to 30%, the "
            f"{dev_n} past {label} lost money or could plausibly have lost it, so no "
            f"edge the model claims is large enough to trust.{why}",
        }
    c = _level(chk, found)
    out["check"] = {"edge": found, **c}
    if c["n"] == 0 or c["roi"] <= 0:
        return {
            **out,
            "min_edge": None,
            "note": f"A minimum edge of {_pct(found)} looked profitable on the earlier "
            f"{label} but did not hold up later: {c['n']} later bets at {_pct(found)} "
            f"and up returned {c.get('roi', 0):+.1%} a bet.",
        }
    return {
        **out,
        "min_edge": found,
        "note": f"Bets the model rates at {_pct(found)} edge or more have paid off: "
        f"{_level(dev, found)['n']} earlier {label} returned "
        f"{_level(dev, found)['roi']:+.1%} a bet, and {c['n']} later ones {c['roi']:+.1%}.",
    }


def from_trades(trades: pd.DataFrame, implied: str | None = None) -> pd.DataFrame:
    """Bets in edge_threshold's shape from trades.new_trade records (settled or void)."""
    if trades is None or trades.empty:
        return pd.DataFrame(columns=["edge", "p", "odds", "won", "group", "season", "time"])
    status = trades["status"]
    won = np.where(status == "won", 1.0, np.where(status == "lost", 0.0, np.nan))
    out = pd.DataFrame(
        {
            "edge": trades["edge"].astype(float),
            "p": trades["model_p"].astype(float),
            "odds": trades["odds"].astype(float),
            "won": won,
            "group": trades["season"].astype(str) + "|" + trades["home"] + "|" + trades["away"],
            "season": trades["season"].astype(str),
            "time": pd.to_datetime(trades["kickoff"], utc=True),
        }
    )
    if implied and implied in trades:
        out["implied"] = trades[implied].astype(float)
    return out
