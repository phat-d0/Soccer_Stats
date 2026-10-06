"""Out-of-sample segment search on the priced player lines.

Is there any slice of FanDuel's player shot lines that pays? Searching many slices of
one season always finds a winner by luck, so the search is strictly out of sample:

1. every candidate segment (strategy x market x line x position x venue x odds band x
   minimum blended edge, see DIMENSIONS) is scored on the *train* season only;
2. the best one (highest ROI with at least `min_bets` bets) is then reported untouched
   on the *test* season, beside the top few and the share of train winners that still
   win in the test season.

Each priced line in a segment is a 1-unit bet. Lines of one player and match are
correlated (1+ and 2+ shots), so the 95% range is a bootstrap over matches. The input
is `backtest/E0_player_lines.csv.gz` on data-log (one row per priced over side, with
the walk-forward blend `p`); no network, no look-ahead beyond the season split.
"""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

# name -> list of (label, row filter); "any" keeps every row.
STRATEGIES = {
    "look_all": lambda d: d["kind"] == "look",  # 3 hours before, every player
    "close_starters": lambda d: (d["kind"] == "close") & d["started"],  # after lineups
}


def _odds_band(lo, hi):
    return lambda d: (d["odds"] >= lo) & (d["odds"] < hi)


def _is_position(pos):
    return lambda d: d["position"] == pos


def _edge_min(t):
    return lambda d: (d["p"] * d["odds"] - 1) >= t  # NaN blend (too early) never passes


DIMENSIONS: dict[str, list[tuple[str, object]]] = {
    "market": [
        ("any", None),
        ("shots", lambda d: d["market"] == "player_shots"),
        ("on target", lambda d: d["market"] == "player_shots_on_target"),
    ],
    "line": [
        ("any", None),
        ("1+", lambda d: np.ceil(d["line"]) == 1),
        ("2+", lambda d: np.ceil(d["line"]) == 2),
        ("3+ or more", lambda d: np.ceil(d["line"]) >= 3),
    ],
    "position": [("any", None)] + [(p, _is_position(p)) for p in ("DEF", "MID", "FWD")],
    "venue": [
        ("any", None),
        ("home", lambda d: d["team"] == d["home"]),
        ("away", lambda d: d["team"] == d["away"]),
    ],
    "odds": [
        ("any", None),
        ("<1.5", _odds_band(0, 1.5)),
        ("1.5-2.5", _odds_band(1.5, 2.5)),
        ("2.5-5", _odds_band(2.5, 5)),
        ("5+", _odds_band(5, np.inf)),
    ],
    "edge": [
        ("all lines", None),
        (">=0%", _edge_min(0.0)),
        (">=5%", _edge_min(0.05)),
        (">=12%", _edge_min(0.12)),
    ],
}


def season_of(kickoff: pd.Series) -> pd.Series:
    """Season start year (2024 = 2024/25)."""
    k = pd.to_datetime(kickoff, utc=True)
    return k.dt.year.where(k.dt.month >= 7, k.dt.year - 1)


def load_lines(path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["kickoff"] = pd.to_datetime(d["kickoff"], utc=True)
    d["started"] = d["started"].astype(str).str.lower().isin(["true", "1"])
    return d


def _masks(d: pd.DataFrame) -> dict[str, list[tuple[str, np.ndarray]]]:
    n = len(d)
    out = {"strategy": [(k, f(d).to_numpy(bool)) for k, f in STRATEGIES.items()]}
    for dim, opts in DIMENSIONS.items():
        out[dim] = [
            (lab, np.ones(n, bool) if f is None else np.asarray(f(d), dtype=bool))
            for lab, f in opts
        ]
    return out


def bootstrap_roi(
    ret: np.ndarray, cluster: np.ndarray, n_boot: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """95% range of ROI (mean return per unit), resampling whole matches."""
    if len(ret) == 0:
        return (np.nan, np.nan)
    codes, uniq = pd.factorize(cluster)
    k = len(uniq)
    s = np.bincount(codes, weights=ret, minlength=k)
    c = np.bincount(codes, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, k, size=(n_boot, k))
    roi = s[draws].sum(1) / c[draws].sum(1)
    return float(np.quantile(roi, 0.025)), float(np.quantile(roi, 0.975))


def summarise(d: pd.DataFrame, n_boot: int = 2000, seed: int = 0) -> dict:
    """Bets, ROI with a 95% match-bootstrap range, win rate vs breakeven, blend calibration."""
    if d.empty:
        return {"bets": 0}
    ret = (d["won"] * d["odds"] - 1).to_numpy(float)
    lo, hi = bootstrap_roi(ret, (d["kickoff"].astype(str) + d["home"]).to_numpy(), n_boot, seed)
    return {
        "bets": int(len(d)),
        "matches": int(d.groupby(["kickoff", "home"]).ngroups),
        "roi": round(float(ret.mean()), 4),
        "roi_95": [round(lo, 4), round(hi, 4)],
        "win_rate": round(float(d["won"].mean()), 4),
        "breakeven": round(float((1 / d["odds"]).mean()), 4),
        "blend_p": round(float(d["p"].mean()), 4) if d["p"].notna().any() else None,
    }


def score_segments(d: pd.DataFrame) -> pd.DataFrame:
    """Every candidate segment's bets, profit and ROI on `d` (one row per segment)."""
    masks = _masks(d)
    ret = (d["won"] * d["odds"] - 1).to_numpy(float)
    dims = list(masks)
    rows = []
    for combo in itertools.product(*(masks[k] for k in dims)):
        m = combo[0][1].copy()
        for _, mk in combo[1:]:
            m &= mk
        n = int(m.sum())
        prof = float(ret[m].sum()) if n else 0.0
        rows.append(
            {
                **{k: lab for k, (lab, _) in zip(dims, combo, strict=True)},
                "bets": n,
                "profit": prof,
                "roi": prof / n if n else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _mask_for(d: pd.DataFrame, seg: dict) -> np.ndarray:
    masks = _masks(d)
    m = np.ones(len(d), bool)
    for dim, opts in masks.items():
        m &= dict(opts)[seg[dim]]
    return m


def out_of_sample(
    lines: pd.DataFrame,
    train: int,
    test: int,
    min_bets: int = 100,
    top: int = 5,
    n_boot: int = 2000,
    seed: int = 0,
) -> dict:
    """Pick segments on season `train`, report them unchanged on season `test`."""
    season = season_of(lines["kickoff"])
    tr_d = lines[season == train].reset_index(drop=True)
    te_d = lines[season == test].reset_index(drop=True)
    seg_cols = ["strategy", *DIMENSIONS]
    s_tr = score_segments(tr_d)
    s_te = score_segments(te_d).set_index(seg_cols)
    eligible = s_tr[s_tr["bets"] >= min_bets].sort_values("roi", ascending=False)
    picked = []
    for r in eligible.head(top).itertuples(index=False):
        seg = {k: getattr(r, k) for k in seg_cols}
        picked.append(
            {
                "segment": seg,
                "train": summarise(tr_d[_mask_for(tr_d, seg)], n_boot, seed),
                "test": summarise(te_d[_mask_for(te_d, seg)], n_boot, seed),
            }
        )
    winners = eligible[eligible["roi"] > 0]
    te_roi = s_te.reindex(winners.set_index(seg_cols).index)
    te_ok = te_roi[te_roi["bets"] >= 1]
    return {
        "train": train,
        "test": test,
        "min_bets": min_bets,
        "segments_tried": int(len(s_tr)),
        "eligible": int(len(eligible)),
        "train_positive": int(len(winners)),
        "train_positive_still_positive": int((te_ok["roi"] > 0).sum()),
        "best": picked[0] if picked else None,
        "top": picked,
        "baseline": {
            k: summarise(te_d[f(te_d).to_numpy(bool)], n_boot, seed) for k, f in STRATEGIES.items()
        },
    }


def _seg_label(seg: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in seg.items() if v not in ("any", "all lines"))


def format_report(res: dict) -> str:
    out = [
        f"Pick on {res['train']}/{res['train'] + 1 - 2000}, report on "
        f"{res['test']}/{res['test'] + 1 - 2000}: {res['segments_tried']} segments tried, "
        f"{res['eligible']} with >= {res['min_bets']} bets, {res['train_positive']} profitable "
        f"in the pick season, {res['train_positive_still_positive']} of those profitable "
        "in the report season."
    ]
    for i, t in enumerate(res["top"], 1):
        a, b = t["train"], t["test"]
        out.append(f" {i}. {_seg_label(t['segment'])}")
        out.append(
            f"    pick:   {a['bets']} bets, ROI {a['roi']:+.1%} "
            f"[{a['roi_95'][0]:+.1%}, {a['roi_95'][1]:+.1%}]"
        )
        if b["bets"]:
            out.append(
                f"    report: {b['bets']} bets, ROI {b['roi']:+.1%} "
                f"[{b['roi_95'][0]:+.1%}, {b['roi_95'][1]:+.1%}], won {b['win_rate']:.1%} "
                f"vs breakeven {b['breakeven']:.1%}"
            )
        else:
            out.append("    report: no bets")
    for k, b in res["baseline"].items():
        if b["bets"]:
            out.append(
                f" every line, {k}: {b['bets']} bets, ROI {b['roi']:+.1%} "
                f"[{b['roi_95'][0]:+.1%}, {b['roi_95'][1]:+.1%}]"
            )
    return "\n".join(out)
