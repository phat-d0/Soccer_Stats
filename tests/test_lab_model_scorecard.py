import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import model_scorecard as ms


def _rows(n=300, seed=0, league="E0"):
    rng = np.random.default_rng(seed)
    q = rng.dirichlet([5, 3, 4], size=n)
    y = np.array([rng.choice(3, p=r) for r in q])
    out = []
    for i in range(n):
        for k, s in enumerate(ms.SIDES):
            out.append(
                {
                    "league": league,
                    "season": "2021",
                    "date": "2020-09-01",
                    "home": f"H{i}",
                    "away": f"A{i}",
                    "match": f"{league}|{i}",
                    "side": s,
                    "p_model": 0.8 * q[i, k] + 0.2 / 3,
                    "p_blend": q[i, k],
                    "fair_early": q[i, k],
                    "fair_close": q[i, k],
                    "won": float(y[i] == k),
                }
            )
    rows = pd.DataFrame(out)
    pins = pd.DataFrame(
        {
            "league": league,
            "season": "2021",
            "home": [f"H{i}" for i in range(n)],
            "away": [f"A{i}" for i in range(n)],
            "odds_home": 0.95 / q[:, 0],
            "odds_draw": 0.95 / q[:, 1],
            "odds_away": 0.95 / q[:, 2],
        }
    )
    return rows, pins


def test_wide_keeps_matches_with_every_series():
    rows, _ = _rows(50)
    rows.loc[0, "p_blend"] = np.nan
    w = ms.wide(rows)
    assert len(w) == 49
    assert set(w["y"]) <= {0, 1, 2}


def test_match_card_counts_and_differences():
    rows, pins = _rows()
    c = ms.match_card(rows, pins, pd.DataFrame(), n_boot=200)
    s = c["series"]
    assert c["matches"] == 300
    assert s["pinnacle_early"]["log_loss"] == pytest.approx(s["pinnacle_close"]["log_loss"])
    assert s["blend"]["log_loss_minus_early"] == pytest.approx(0)
    assert np.sum(s["model"]["confusion"]["counts"]) == 300
    assert c["auc"]["auc"]["pinnacle_early"] > 0.5
    t = c["thresholds"]["model_12"]
    assert t["picked_won"] + t["picked_lost"] == t["picks"]
    assert t["picks"] + t["not_picked_won"] + t["not_picked_lost"] == 900
    assert c["thresholds"]["lean"]["picks"] == 0


def test_pick_12_needs_a_12_percent_edge():
    w = pd.DataFrame(
        {
            "match": ["a", "b"],
            "p_model_home": [0.6, 0.5],
            "p_model_draw": [0.2, 0.25],
            "p_model_away": [0.2, 0.25],
            "odds_home": [2.0, 2.0],
            "odds_draw": [3.0, 3.0],
            "odds_away": [4.0, 4.0],
        }
    )
    p = ms.pick_12(w)
    assert p.to_dict("records") == [{"match": "a", "side": "home"}]  # b's best edge is 0.0


def test_corner_card_beats_a_bad_baseline():
    rng = np.random.default_rng(1)
    n = 400
    mh, ma = rng.uniform(3, 8, n), rng.uniform(2, 6, n)
    cw = pd.DataFrame(
        {
            "date": pd.date_range("2018-01-01", periods=n).astype(str),
            "home": [f"H{i}" for i in range(n)],
            "away": "A",
            "home_corners": rng.poisson(mh),
            "away_corners": rng.poisson(ma),
            "m_home": mh,
            "m_away": ma,
            "alpha_home": 0.01,
            "alpha_away": 0.01,
            "b_home": 5.5,
            "b_away": 4.0,
        }
    )
    c = ms.corner_card(cw, n_boot=200)
    assert c["model_f"]["rmse"] < c["baseline"]["rmse"]
    assert c["mse_f_minus_baseline_ci95"][1] < 0
    assert 0.7 < c["model_f"]["coverage_80"] < 0.95


def test_corner_baseline_uses_only_earlier_matches():
    cm = pd.DataFrame(
        {
            "date": pd.to_datetime(["2018-06-01", "2018-07-10", "2018-08-20"]),
            "home": ["A", "B", "C"],
            "away": ["B", "C", "A"],
            "home_corners": [4, 10, 100],
            "away_corners": [2, 6, 100],
        }
    )
    b = ms.corner_baseline(cm, "2018-07-01", "2018-09-01")
    row = b[b["home"] == "C"].iloc[0]
    assert row["b_home"] == pytest.approx(7.0)  # June and July only, never its own 100


def test_pool_writes_a_scorecard(tmp_path):
    rows, pins = _rows(120)
    rows.to_csv(tmp_path / "score_rows_E0.csv.gz", index=False)
    pins.to_csv(tmp_path / "score_pins_E0.csv.gz", index=False)
    pd.DataFrame().to_csv(tmp_path / "score_lean_E0.csv.gz", index=False)
    (tmp_path / "score_E0.json").write_text(json.dumps({"league": "E0"}))
    res = ms.pool(tmp_path, n_boot=100)
    assert set(res["match"]) == {"pooled", "E0"}
    assert res["corners"] == {}
    assert ms.summary_lines(res)
    json.dumps(res, default=str)
