import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from soccer_stats.lab import model_scorecard2 as m2


def _matrix(lh, la, n=11):
    k = np.arange(n)
    mx = np.outer(poisson.pmf(k, lh), poisson.pmf(k, la))
    return mx / mx.sum()


def _preds(n=300, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        lh, la = rng.uniform(0.8, 2.2), rng.uniform(0.6, 1.8)
        rows.append(
            {
                "season": "2021",
                "date": pd.Timestamp("2020-09-01") + pd.Timedelta(days=i),
                "home": f"H{i}",
                "away": f"A{i}",
                "home_goals": int(rng.poisson(lh)),
                "away_goals": int(rng.poisson(la)),
                "exp_home": lh,
                "exp_away": la,
                "matrix": _matrix(lh, la),
            }
        )
    return pd.DataFrame(rows)


def test_goal_rows_distribution_sums_to_one():
    g = m2.goal_rows(_preds(20))
    t = g[[f"t{k}" for k in range(m2.MAX_T + 1)]].to_numpy()
    assert np.allclose(t.sum(axis=1), 1)
    assert np.allclose(g["p_over"], t[:, 3:].sum(axis=1))
    assert (g["p_hg"] > 0).all()


def test_rps_is_zero_for_a_certain_right_forecast():
    dist = np.zeros((1, m2.MAX_T + 1))
    dist[0, 2] = 1
    assert m2.rps(dist, np.array([2]))[0] == pytest.approx(0)
    assert m2.rps(dist, np.array([5]))[0] > 0
    assert np.allclose(m2.poisson_total(np.array([2.5])).sum(axis=1), 1)


def test_goals_card_model_beats_a_flat_baseline():
    g = m2.goal_rows(_preds(400))
    g["b_home"], g["b_away"] = 1.5, 1.2
    g["o_early"] = 1 / np.clip(g["p_over"] * 1.03, 0.05, 0.95)
    g["u_early"] = 1 / np.clip((1 - g["p_over"]) * 1.03, 0.05, 0.95)
    g["o_close"], g["u_close"] = g["o_early"], g["u_early"]
    g["league"] = "E0"
    c = m2.goals_card(g, n_boot=200)
    assert c["total_goals"]["rps_model"] < c["total_goals"]["rps_baseline"]
    assert c["team_goals"]["log_score_model"] > c["team_goals"]["log_score_baseline"]
    ou = c["over_under_25"]["series"]
    assert ou["model"]["log_loss_minus_ref"] == pytest.approx(0, abs=0.01)
    assert 0.6 < c["total_goals"]["coverage_80"] < 1


def test_cup_card_and_goalscorer_card():
    rng = np.random.default_rng(2)
    rows = []
    for i in range(200):
        q = rng.dirichlet([4, 2.5, 3])
        y = rng.choice(3, p=q)
        for k, s in enumerate(m2.H2H):
            rows.append(
                {
                    "match": str(i),
                    "season": "2425",
                    "side": s,
                    "p_model": q[k],
                    "fair_pin": q[k],
                    "fair_dk": q[k] if i % 2 else np.nan,
                    "won": float(y == k),
                }
            )
    c = m2.cup_card(pd.DataFrame(rows), n_boot=100)
    assert c["pinnacle"]["matches"] == 200 and c["draftkings"]["matches"] == 100
    assert c["pinnacle"]["p_model"]["minus_pinnacle"] == pytest.approx(0)
    n = 500
    p = rng.uniform(0.05, 0.5, n)
    gs = pd.DataFrame(
        {
            "p_B": p,
            "bench_goals": p,
            "bench_xg": p,
            "entry_odds": 1 / (p * 0.9),
            "scored": (rng.random(n) < p).astype(int),
            "match_id": np.arange(n) // 10,
        }
    )
    g = m2.goalscorer_card(gs, n_boot=100)
    t = g["threshold"]
    assert t["picks"] == t["picked_won"] + t["picked_lost"]
    assert set(g["series"]) == {"model_b", "season_goals", "season_xg", "best_price"}
