import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import corner_foos as cf


def _both(n=3000, alpha_true=0.08, alpha_f=0.0, alpha_o=0.08, seed=0):
    rng = np.random.default_rng(seed)
    mh, ma = rng.uniform(3.5, 7, n), rng.uniform(2.5, 6, n)

    def draw(m):
        r = 1 / alpha_true
        return rng.negative_binomial(r, r / (r + m))

    return pd.DataFrame(
        {
            "league": "E0",
            "date": pd.date_range("2020-01-01", periods=n).astype(str),
            "home": [f"H{i}" for i in range(n)],
            "away": "A",
            "home_corners": draw(mh),
            "away_corners": draw(ma),
            "m_home": mh,
            "m_away": ma,
            "alpha_home": alpha_f,
            "alpha_away": alpha_f,
            "o_m_home": mh,
            "o_m_away": ma,
            "o_alpha_home": alpha_o,
            "o_alpha_away": alpha_o,
        }
    )


def test_true_dispersion_scores_better_than_too_narrow():
    rs = cf.scored(_both())
    s = cf.summary(rs, n_boot=200)
    assert s["f-oos"]["dispersion"] == pytest.approx(1.0, abs=0.08)
    assert s["f"]["dispersion"] > s["f-oos"]["dispersion"]
    assert s["f-oos"]["coverage_90"] > s["f"]["coverage_90"]
    assert s["diff_foos_minus_f"] < 0 and s["diff_ci95"][1] < 0
    assert s["matches"] == 3000
    assert sum(s["f"]["pit_deciles"]) == pytest.approx(1, abs=1e-3)


def test_identical_models_give_zero_difference():
    b = _both(n=500, alpha_f=0.08, alpha_o=0.08)
    s = cf.summary(cf.scored(b), n_boot=100)
    assert s["diff_foos_minus_f"] == pytest.approx(0)


def test_half_line_log_loss_counts_seven_lines():
    b = _both(n=10)
    rs = cf.row_scores(cf.stacked(b, "f"))
    assert len(rs) == 20
    assert (rs["ll_lines"] > 0).all()
    assert len(cf.LINES) == 7


def test_pool_judged_verdict(tmp_path):
    rs = cf.scored(_both(n=800))
    rs["league"] = "E0"
    rs.to_csv(tmp_path / "foos_judged_E0.csv.gz", index=False)
    res = cf.pool(tmp_path, "judged", n_boot=100)
    assert res["verdict"] == "pass" and res["reason"] == cf.REASON
    assert cf.summary_lines(res)
