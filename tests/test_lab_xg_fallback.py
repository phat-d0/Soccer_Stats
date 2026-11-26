import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import xg_fallback as xf


def _shots(n=400, seed=0):
    rng = np.random.default_rng(seed)
    hst, ast = rng.integers(0, 10, n), rng.integers(0, 10, n)
    hs, as_ = hst + rng.integers(0, 12, n), ast + rng.integers(0, 12, n)
    return pd.DataFrame({
        "home_hst": hst, "away_hst": ast, "home_hs": hs, "away_hs": as_,
        "home_goals": 0.3 * hst + 0.03 * (hs - hst), "away_goals": 0.3 * ast + 0.03 * (as_ - ast),
    })  # fmt: skip


def test_weight_is_the_live_xg_weight():
    from soccer_stats.publish import XG_WEIGHT

    assert xf.WEIGHT == XG_WEIGHT


def test_fit_proxy_recovers_the_conversion_rates():
    c = xf.fit_proxy(_shots())
    assert c["alpha"] == pytest.approx(0.3) and c["beta"] == pytest.approx(0.03)
    assert 0.3 < c["alpha_sot"] < 0.45  # shots on target alone also carry the other shots


def test_fit_proxy_never_gives_a_negative_weight():
    s = _shots()
    s["home_goals"] = 0.35 * s["home_hst"] - 0.02 * (s["home_hs"] - s["home_hst"]) + 0.1
    s["away_goals"] = 0.35 * s["away_hst"] - 0.02 * (s["away_hs"] - s["away_hst"]) + 0.1
    c = xf.fit_proxy(s)
    assert c["beta"] == 0.0 and c["alpha"] > 0
    assert (xf.proxy(s, c)[["home_xg", "away_xg"]] >= 0).all().all()


def test_proxy_falls_back_to_shots_on_target_then_to_goals():
    m = pd.DataFrame({"home_hst": [5.0, 5.0, np.nan], "away_hst": [2.0, 2.0, 2.0],
                      "home_hs": [10.0, np.nan, 10.0], "away_hs": [4.0, 4.0, 4.0]})  # fmt: skip
    c = {"alpha": 0.3, "beta": 0.03, "alpha_sot": 0.35}
    out = xf.proxy(m, c)
    assert out.loc[0, "home_xg"] == pytest.approx(0.3 * 5 + 0.03 * 5)
    assert out.loc[1, "home_xg"] == pytest.approx(0.35 * 5)
    assert np.isnan(out.loc[2, "home_xg"])
    assert xf.proxy(m, c, sot_only=True).loc[0, "home_xg"] == pytest.approx(0.35 * 5)


def _scored(n=3000, seed=1):
    rng = np.random.default_rng(seed)
    truth = rng.dirichlet([4, 3, 3], n)
    y = np.array([rng.choice(3, p=p) for p in truth])
    df = pd.DataFrame({"home_goals": np.where(y == 0, 1, 0), "away_goals": np.where(y == 2, 1, 0)})
    noise = {"a": 0.0, "b": 0.6, "c": 0.15, "c_sot": 0.3}
    for f, s in noise.items():
        p = truth * np.exp(rng.normal(0, s, truth.shape))
        p /= p.sum(axis=1, keepdims=True)
        for k, side in enumerate(xf.SIDES):
            df[f"p_{f}_{side}"] = p[:, k]
    return df


def test_ll_report_orders_the_forms_and_recovers_most():
    r = xf.ll_report(_scored(), "p", xf.FORMS, n_boot=300)
    ll = r["log_loss"]
    assert ll["a"] < ll["c"] < ll["c_sot"] < ll["b"]
    assert r["diffs"]["c-b"]["ci95"][1] < 0
    assert r["recovery"]["xg_gain_range_above_0"] and r["recovery"]["r"] > 0.5


def test_ll_report_uses_only_rows_every_form_scores():
    df = _scored(200)
    df.loc[:9, "p_c_home"] = np.nan
    assert xf.ll_report(df, "p", xf.FORMS, n_boot=50)["matches"] == 190


def test_proposal_needs_both_conditions():
    good_model = {"recovery": {"r": 0.6, "xg_gain_range_above_0": True}}
    blend_ok = {"diffs": {"c-b": {"ci95": [-0.004, -0.001]}}}
    blend_no = {"diffs": {"c-b": {"ci95": [-0.004, 0.001]}}}
    assert xf.proposal(good_model, blend_ok, blend_no)["understat_leagues"]["propose"]
    assert not xf.proposal(good_model, blend_no, blend_no)["understat_leagues"]["propose"]
    low = {"recovery": {"r": 0.4, "xg_gain_range_above_0": True}}
    assert not xf.proposal(low, blend_ok, blend_no)["understat_leagues"]["propose"]
    assert xf.proposal(low, blend_no, blend_ok)["goals_only_leagues"]["propose"]


def test_same_picks_counts_matching_sides():
    b = pd.DataFrame({"league": "E0", "season": "2324", "home": ["A", "C", "E"],
                      "away": ["B", "D", "F"], "market": ["home", "draw", "away"],
                      "form": "a"})  # fmt: skip
    c = b.iloc[:2].assign(form="c", market=["home", "away"])
    s = xf.same_picks(pd.concat([b, c]), "c")
    assert s == {"a_bets": 3, "form_bets": 2, "same_match": 2, "same_side": 1}


def _bad_train():
    return pd.DataFrame({"date": pd.to_datetime(["2020-01-01", "2020-01-01"]),
                         "home": ["A", "A"], "away": ["B", "B"],
                         "home_goals": [1, np.nan], "away_goals": [0, 1],
                         "home_xg": [np.inf, 1.0], "away_xg": [0.5, 0.5]})  # fmt: skip


def test_checked_factory_reports_a_failed_fit(capsys, monkeypatch):
    from soccer_stats import dashboard

    class Failing:
        def fit(self, train, as_of=None):
            raise RuntimeError("Dixon-Coles fit failed: ABNORMAL")

    monkeypatch.setattr(dashboard, "match_model", lambda code, xg_weight: Failing())
    model = xf.checked_factory("E0", "a", 0.7)()
    with pytest.raises(RuntimeError):
        model.fit(_bad_train(), as_of=pd.Timestamp("2020-01-02"))
    out = capsys.readouterr().out
    assert "XG_FALLBACK_FIT_FAILED" in out and '"form": "a"' in out and '"inf": 1' in out


def test_describe_train_counts_gaps():
    d = xf.describe_train(_bad_train())
    assert d["duplicates"] == 1 and d["home_goals"]["nan"] == 1 and d["home_xg"]["inf"] == 1
