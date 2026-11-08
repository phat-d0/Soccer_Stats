"""ROC AUC research (docs/lab.md "ROC AUC"): the weighted AUC, pairing and the blocks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import auc as A


def brute(score, y, w=None):
    score, y = np.asarray(score, float), np.asarray(y, float)
    w = np.ones(len(y)) if w is None else np.asarray(w, float)
    num = den = 0.0
    for i in np.flatnonzero(y == 1):
        for j in np.flatnonzero(y == 0):
            pair = w[i] * w[j]
            den += pair
            num += pair * (1.0 if score[i] > score[j] else 0.5 if score[i] == score[j] else 0.0)
    return num / den


def test_auc_matches_pair_counting_with_ties():
    rng = np.random.default_rng(3)
    s = rng.integers(0, 6, 60).astype(float)  # many ties
    y = (rng.random(60) < 0.4).astype(float)
    assert A.auc(s, y) == pytest.approx(brute(s, y))
    assert A.auc([0.1, 0.2], [1, 1]) is None


def test_weighted_auc_matches_brute_force_on_match_weights():
    rng = np.random.default_rng(4)
    n, m = 40, 12
    s, y = rng.random(n), (rng.random(n) < 0.5).astype(float)
    match = rng.integers(0, m, n)
    counts = rng.integers(0, 3, (1, m)).astype(float)
    got = A.Ranked(s, y, match).auc(counts)[0]
    assert got == pytest.approx(brute(s, y, counts[0, match]))


def _outcomes(n_matches=400, seed=0, noise=0.0):
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_matches):
        p = rng.dirichlet([4, 3, 3])
        y = rng.choice(3, p=p)
        for i, side in enumerate(("home", "draw", "away")):
            q = float(np.clip(p[i] + rng.normal(0, noise), 0.01, 0.98))
            rows.append({"league": "E0" if k % 2 else "E1", "season": "2122", "match": f"m{k}",
                         "side": side, "won": float(y == i), "p_model": q, "p_blend": p[i],
                         "fair_early": p[i], "fair_close": p[i]})  # fmt: skip
    return pd.DataFrame(rows)


def test_compare_pairs_differences_and_identical_series_differ_by_zero():
    df = _outcomes(noise=0.05)
    series = {"a": "fair_early", "b": "fair_close", "m": "p_model"}
    groups = {"pooled": np.ones(len(df), bool)}
    res = A.compare(df, series, groups, [("a", "b"), ("m", "a")], n_boot=60)
    p = res["pooled"]
    assert p["diff"]["a - b"]["value"] == 0
    assert p["diff"]["a - b"]["ci95"] == [0.0, 0.0]
    assert p["auc"]["a"] > 0.6  # the true chances rank well
    assert p["diff"]["m - a"]["value"] < 0  # noise only hurts


def test_within_bands_is_near_half_when_the_model_is_the_price_plus_noise():
    df = _outcomes(n_matches=1500, seed=1, noise=0.0)
    df["noise"] = np.random.default_rng(2).random(len(df))
    res = A.within_bands(df, "fair_early", {"noise": "noise"}, n_boot=60)
    assert res["bands"] >= 3
    lo, hi = res["auc_ci95"]["noise"]
    assert lo < 0.5 < hi
    assert res["above_half"]["noise"] is False


def test_match_blocks_shape_and_dk_subset():
    df = _outcomes(noise=0.03)
    df["date"] = pd.Timestamp("2021-09-01")
    df["home"], df["away"] = df["match"], "x"
    df["fair_dk"] = np.where(np.arange(len(df)) < 600, df["fair_early"], np.nan)
    res = A.match_blocks(df, n_boot=40)
    assert set(res["main"]) >= {"pooled", "home", "draw", "away", "league:E0", "league:E1"}
    assert res["draftkings"]["pooled"]["rows"] == 600
    assert "model - pinnacle_early" in res["main"]["pooled"]["diff"]
    assert res["within_bands"]["price"] == "fair_early"


def test_corner_rows_skip_locked_leagues_whole_lines_and_unsettled():
    ko = "2026-10-18T14:00:00+00:00"
    got = "2026-10-18T12:00:00+00:00"
    base = {"market": "team_corners", "kickoff": ko, "downloaded_at": got, "side": "home",
            "fair_over": 0.5, "p_model_over": 0.6, "over": 1.9, "under": 1.9}  # fmt: skip
    rows = [
        {**base, "league": "SP1", "home": "A", "away": "B", "line": 4.5, "book": "pinnacle"},
        {**base, "league": "SP1", "home": "A", "away": "B", "line": 5.0, "book": "pinnacle"},
        {**base, "league": "E0", "home": "C", "away": "D", "line": 4.5, "book": "pinnacle"},
        {**base, "league": "D1", "home": "E", "away": "F", "line": 4.5, "book": "pinnacle"},
    ]
    results = pd.DataFrame([
        {"league": "SP1", "home": "A", "away": "B", "date": "2026-10-18", "home_corners": 6,
         "away_corners": 3},
        {"league": "E0", "home": "C", "away": "D", "date": "2026-10-18", "home_corners": 6,
         "away_corners": 3},
    ])  # fmt: skip
    out = A.corner_rows(rows, results)
    assert out[["league", "won"]].values.tolist() == [["SP1", 1.0]]


def test_goal_blocks_uses_starters_rows_and_price():
    rng = np.random.default_rng(5)
    n = 900
    p = rng.uniform(0.05, 0.5, n)
    df = pd.DataFrame({"league": "E0", "match_id": rng.integers(0, 90, n), "p_B": p,
                       "bench_goals": p + rng.normal(0, 0.1, n), "bench_xg": p,
                       "entry_odds": 1 / np.clip(p, 0.02, 0.9),
                       "scored": (rng.random(n) < p).astype(int)})  # fmt: skip
    res = A.goal_blocks(df, n_boot=40)
    assert res["rows"] == n
    assert res["pooled"]["auc"]["model_b"] == pytest.approx(res["pooled"]["auc"]["best_price"])


def test_main_refuses_the_locked_season():
    with pytest.raises(SystemExit):
        A.main(["--last", "2024", "--skip-match"])
