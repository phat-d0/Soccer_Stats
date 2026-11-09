import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import market_moves as mm
from soccer_stats.lab.harness import Holdout, HoldoutLocked


def _rows(n=1600, seed=0, signal=0.05):
    rng = np.random.default_rng(seed)
    dates = pd.Timestamp("2017-08-01") + pd.to_timedelta(np.sort(rng.integers(0, 2000, n)), "D")
    x = rng.normal(size=(n, 4))
    df = pd.DataFrame(x, columns=["s1", "s2", "n1", "n2"])
    df["date"] = dates
    df["day"] = dates - mm.EARLY_LEAD
    df["m_home"] = signal * df["s1"] + 0.05 * rng.normal(size=n)
    df["m_away"] = -signal * df["s2"] + 0.05 * rng.normal(size=n)
    return df


def test_group_lasso_keeps_signal_and_drops_noise():
    df = _rows(4000, signal=0.1)
    sc = mm.lr.Scaler(df[["s1", "s2", "n1", "n2"]].to_numpy(float))
    x = sc(df[["s1", "s2", "n1", "n2"]].to_numpy(float))
    y = mm.targets(df)
    groups = [np.array([0, 1]), np.array([2, 3])]
    lmax = mm.lambda_max(x, y, groups)
    w = mm.fit_group(x, y, groups, lmax * 1.01)
    assert np.allclose(w[1:], 0)
    w = mm.fit_group(x, y, groups, lmax * 0.2)
    assert np.abs(w[1:3]).sum() > 0.05
    assert np.allclose(w[3:], 0)


def test_ridge_near_zero_alpha_is_least_squares():
    df = _rows(500)
    x = df[["s1", "s2", "n1", "n2"]].to_numpy(float)
    y = df[["m_home", "m_away"]].to_numpy(float)
    w = mm.fit_ridge(x, y, 1e-9)
    xa = np.hstack([np.ones((len(x), 1)), x])
    ls = np.linalg.lstsq(xa, y, rcond=None)[0]
    assert np.allclose(w, ls, atol=1e-6)


def test_walk_is_out_of_sample_and_r2_positive_on_signal():
    df = _rows(3000, signal=0.1)
    groups = {"sig": ["s1", "s2"], "noise": ["n1", "n2"]}
    p, refits = mm.walk(df, "2019-07-01", "2022-01-01", groups, "group")
    assert len(p) and refits
    r = mm.r2_block(df.loc[p.index].join(p), n_boot=200)
    assert r["r2"] > 0.3 and r["ci975"][0] > 0
    # Changing every later target must not change the first block's predictions.
    lo = pd.Timestamp("2019-07-01")
    first = p.index[(df.loc[p.index, "day"] < lo + pd.Timedelta("28D"))]
    df2 = df.copy()
    later = df2["date"] >= lo
    df2.loc[later, ["m_home", "m_away"]] = 9.0
    p2, _ = mm.walk(df2, "2019-07-01", lo + pd.Timedelta("28D"), groups, "group")
    assert np.allclose(p.loc[first, "pred_home"], p2.loc[first, "pred_home"])


def test_walk_refuses_locked_rows():
    df = _rows(3000)
    with pytest.raises(HoldoutLocked):
        mm.walk(df, "2019-07-01", "2023-01-01", {"s": ["s1"]}, "ridge", Holdout("2020-01-01"))


def test_short_window_validates_on_last_third():
    df = _rows(900)
    inner, valid, how = mm.split_valid(df, pd.Timestamp("2018-06-01"))
    assert how == "last_third" and len(inner) + len(valid) == 900


def test_r2_block_perfect_and_null():
    df = pd.DataFrame({"m_home": [0.1, -0.2, 0.05], "m_away": [0.0, 0.1, -0.1]})
    perfect = df.assign(pred_home=df["m_home"], pred_away=df["m_away"], bar_home=0.0, bar_away=0.0)
    assert mm.r2_block(perfect, n_boot=50)["r2"] == pytest.approx(1.0)
    null = df.assign(pred_home=0.0, pred_away=0.0, bar_home=0.0, bar_away=0.0)
    assert mm.r2_block(null, n_boot=50)["r2"] == pytest.approx(0.0)


def _priced(pred_home=0.0, pred_away=0.0):
    e = np.array([0.5, 0.3, 0.2])
    c = np.array([0.55, 0.28, 0.17])
    row = {f"e_{s}": e[k] for k, s in enumerate(mm.SIDES)}
    row.update({f"c_{s}": c[k] for k, s in enumerate(mm.SIDES)})
    row.update({f"odds_{s}": 0.98 / e[k] for k, s in enumerate(mm.SIDES)})
    row.update({"pred_home": pred_home, "pred_away": pred_away, "league": "E0",
                "season": "2324", "date": pd.Timestamp("2024-01-01"), "y": 0})  # fmt: skip
    return pd.DataFrame([row])


def test_predicted_close_derives_the_draw():
    pc = mm.predicted_close(_priced(np.log(1.1), 0.0))
    assert pc[0, 0] == pytest.approx(0.55)
    assert pc[0, 1] == pytest.approx(1 - 0.55 - 0.2)


def test_rule_needs_the_move_to_cover_the_margin():
    assert mm.rule_bets(_priced(0.0, 0.0)).empty  # 0.98 x e = -2% predicted CLV
    b = mm.rule_bets(_priced(np.log(1.1), 0.0))
    assert list(b["side"]) == ["home"]
    assert b["clv"].iloc[0] == pytest.approx(0.98 / 0.5 * 0.55 - 1)
    assert b["profit"].iloc[0] == pytest.approx(0.98 / 0.5 - 1)


def test_verdict_needs_all_three():
    r2 = {"ci975": [0.001, 0.01]}
    rule = {"bets": 400, "clv_ci975": [0.002, 0.03]}
    assert mm.verdict(r2, rule)["passes"]
    assert not mm.verdict(r2, {**rule, "bets": 299})["passes"]
    assert not mm.verdict({"ci975": [-0.001, 0.01]}, rule)["passes"]


def test_days_early_collection_proxy():
    d = pd.Series(
        pd.to_datetime(["2023-08-12", "2023-08-14", "2023-08-15", "2023-08-17", "2023-08-11"])
    )  # fmt: skip  Sat, Mon, Tue, Thu, Fri
    assert list(mm.days_early(d)) == [1.0, 3.0, 0.0, 2.0, 0.0]


def test_book_features_against_pinnacle():
    pin = {"home": 2.0, "draw": 3.4, "away": 4.0}
    close = {"home": 1.9, "draw": 3.5, "away": 4.3}
    others = {"draftkings": {"home": 2.1, "draw": 3.3, "away": 3.6},
              "fanduel": {"home": 1.95, "draw": 3.4, "away": 4.1}}  # fmt: skip
    r = mm.book_features(pin, close, others, pd.Timestamp("2023-01-01", tz="UTC"))
    assert r["bk_count"] == 2
    assert r["best_home"] == 2.1 and r["best_away"] == 4.1
    assert r["bk_mean_home"] == pytest.approx((r["bk_dk_home"] + r["bk_fd_home"]) / 2)
    assert r["e48_home"] + r["e48_draw"] + r["e48_away"] == pytest.approx(1)


def test_snapshot_files_never_list_locked_files(tmp_path, monkeypatch):
    from soccer_stats import odds_history

    d = tmp_path / "E0"
    d.mkdir()
    for name in ("20240630T1200Z", "20240701T0000Z", "20250101T1200Z"):
        (d / f"{name}.json").write_text(json.dumps({"requested": name, "data": []}))
    monkeypatch.setattr(odds_history, "history_dir", lambda code, raw_dir: d)
    assert [p.stem for p in mm.snapshot_files("E0", tmp_path)] == ["20240630T1200Z"]


def test_news_features_games_in_hand():
    m = pd.DataFrame({
        "league": "E0", "season": "2324",
        "date": pd.to_datetime(["2023-08-01", "2023-08-01", "2023-08-20", "2023-08-20"]),
        "home": ["A", "C", "A", "B"], "away": ["B", "D", "C", "D"],
    })  # fmt: skip
    out = mm.news_features(m, None, {"B": [pd.Timestamp("2023-08-22")]})
    # By 16 Aug A, B, C, D have each played once: no games in hand.
    assert out.loc[2, "home_gih"] == 0 and out.loc[3, "away_gih"] == 0
    assert out.loc[3, "home_cup_next"] == 1.0 and out.loc[2, "home_cup_next"] == 0.0


def test_design_groups_frozen():
    a, b = mm.groups_for("A"), mm.groups_for("B")
    assert len(a) == len(b) == 16
    assert "cross_book_fd" in a and "cross_book_10" in b
    assert "days_early" in a["market_structure"] and "days_early" not in b["market_structure"]
