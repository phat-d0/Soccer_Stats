import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import lasso_residual as lr
from soccer_stats.lab.harness import Holdout, HoldoutLocked


def _market(n, rng):
    """Random fair 1X2 chances."""
    a = rng.dirichlet([4, 2.5, 3], size=n)
    return a


def _draw(p, rng):
    u = rng.random(len(p))
    return (u[:, None] > np.cumsum(p, axis=1)).sum(axis=1).clip(0, 2)


def test_fit_l1_keeps_a_planted_factor_and_drops_noise():
    rng = np.random.default_rng(1)
    n = 6000
    q = _market(n, rng)
    x = rng.normal(size=(n, 4))
    s = np.log(q)
    s[:, 0] += 0.5 * x[:, 0]  # x0 moves the home chance beyond the price
    p = np.exp(s) / np.exp(s).sum(axis=1, keepdims=True)
    y = _draw(p, rng)
    off = np.log(q)
    lmax = lr.lambda_max(x, off, y)
    w = lr.fit_l1(x, off, y, lmax * 0.05)
    assert abs(w[1, 0]) > 0.3
    assert np.all(np.abs(w[2:, :]) < 0.05)
    # At lambda_max nothing is kept.
    w0 = lr.fit_l1(x, off, y, lmax * 1.001)
    assert np.allclose(w0[1:], 0)


def test_pure_noise_is_mostly_dropped_by_the_inner_validation():
    rng = np.random.default_rng(2)
    n = 5000
    q = _market(n, rng)
    y = _draw(q, rng)
    dates = pd.date_range("2017-07-01", periods=n, freq="8h")
    df = pd.DataFrame({"date": dates, "y": y})
    for k, s in enumerate(lr.SIDES):
        df[f"q_{s}"] = q[:, k]
    feats = ["n1", "n2", "n3"]
    for f in feats:
        df[f] = rng.normal(size=n)
    fit = lr.choose_lambda(df, df["date"].max() + pd.Timedelta(days=1), feats)
    assert np.abs(fit["w"][1:]).sum() < 0.1


def _matches():
    """Two seasons of a 4-team league, one match a week, with prices and xG."""
    teams = ["A", "B", "C", "D"]
    rows = []
    d = pd.Timestamp("2017-08-05")
    rng = np.random.default_rng(3)
    for season in ("1718", "1819"):
        for i in range(30):
            h, a = teams[i % 4], teams[(i + 1 + i // 4) % 4]
            if h == a:
                a = teams[(i + 2) % 4]
            rows.append(
                {
                    "league": "E0",
                    "season": season,
                    "date": d,
                    "home": h,
                    "away": a,
                    "home_goals": int(rng.integers(0, 4)),
                    "away_goals": int(rng.integers(0, 3)),
                    "home_xg": float(rng.random() * 2),
                    "away_xg": float(rng.random() * 2),
                    "odds_home": 2.2,
                    "odds_draw": 3.3,
                    "odds_away": 3.4,
                    "close_home": 2.1,
                    "close_draw": 3.4,
                    "close_away": 3.6,
                    "p_home": 0.45,
                    "p_draw": 0.27,
                    "p_away": 0.28,
                    "home_hst": int(rng.integers(0, 8)),
                    "away_hst": int(rng.integers(0, 6)),
                    "home_red": int(rng.random() < 0.1),
                    "away_red": int(rng.random() < 0.1),
                }
            )
            d += pd.Timedelta(days=7)
        d += pd.Timedelta(days=60)
    return lr.add_early_fair(pd.DataFrame(rows))


def test_factors_use_only_results_before_the_decision_day():
    m = _matches()
    base = lr.build_factors(m)
    target = 40
    day = pd.Timestamp(m.loc[target, "date"]) - lr.EARLY_LEAD
    later = pd.to_datetime(m["date"]) >= day
    assert later.sum() > 0
    # Change every result, xG and price of matches kicked off on or after the decision day.
    m2 = m.copy()
    m2.loc[later, ["home_goals", "away_goals"]] = 9
    m2.loc[later, ["home_xg", "away_xg"]] = 0.0
    m2.loc[later, ["home_hst", "away_hst", "home_red", "away_red"]] = [9, 0, 1, 1]
    m2.loc[later, ["close_home", "close_draw", "close_away"]] = [1.2, 8.0, 15.0]
    m2.loc[later & (m2.index != target), ["odds_home", "odds_draw", "odds_away"]] = [9.0, 5.0, 1.3]
    m2 = lr.add_early_fair(m2)
    again = lr.build_factors(m2)
    assert base.loc[target, lr.FACTORS["motivation"]].notna().any()
    assert base.loc[target, ["home_sot", "home_red_last", "home_shock"]].notna().all()
    for f in lr.FEATURES:
        a, b = base.loc[target, f], again.loc[target, f]
        assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(b), f


def test_rest_days_and_newcomers():
    m = _matches()
    f = lr.build_factors(m)
    assert f["home_rest"].max() <= lr.REST_CAP
    assert (f.loc[f["season"] == "1718", "home_new"].isna()).all()  # no season before
    assert (f.loc[f["season"] == "1819", "home_new"] == 0).all()  # same four teams


def test_europe_flags_count_one_to_four_days_before():
    trips = {"E0:A": [(pd.Timestamp("2018-09-18"), True)]}
    assert lr._euro(trips["E0:A"], pd.Timestamp("2018-09-22")) == (1.0, 1.0)
    assert lr._euro(trips["E0:A"], pd.Timestamp("2018-09-23")) == (0.0, 0.0)
    assert lr._euro(trips["E0:A"], pd.Timestamp("2018-09-18")) == (0.0, 0.0)


def test_walk_trains_only_on_matches_before_the_block():
    rng = np.random.default_rng(4)
    n = 4000
    q = _market(n, rng)
    dates = pd.date_range("2017-07-01", periods=n, freq="16h")
    rows = pd.DataFrame({"date": dates, "y": _draw(q, rng), "league": "E0", "season": "x"})
    rows["day"] = rows["date"] - lr.EARLY_LEAD
    for k, s in enumerate(lr.SIDES):
        rows[f"q_{s}"] = q[:, k]
    rows["f1"] = rng.normal(size=n)
    seen = []
    orig = lr.choose_lambda

    def spy(train, lo, feats):
        seen.append((train["date"].max(), lo))
        return orig(train, lo, feats)

    lr.choose_lambda = spy
    try:
        preds, refits = lr.walk(rows, "2019-07-01", "2020-07-01", feats=["f1"])
    finally:
        lr.choose_lambda = orig
    assert len(refits) > 5 and len(preds) > 0
    assert all(mx < lo for mx, lo in seen)


def test_walk_refuses_the_locked_holdout():
    rng = np.random.default_rng(5)
    n = 3000
    q = _market(n, rng)
    dates = pd.date_range("2022-01-01", periods=n, freq="8h")
    rows = pd.DataFrame({"date": dates, "y": _draw(q, rng)})
    rows["day"] = rows["date"] - lr.EARLY_LEAD
    for k, s in enumerate(lr.SIDES):
        rows[f"q_{s}"] = q[:, k]
    rows["f1"] = 0.0
    with pytest.raises(HoldoutLocked):
        lr.walk(rows, "2024-01-01", "2025-01-01", Holdout(lr.LOCK), feats=["f1"])


def test_report_and_gate():
    rng = np.random.default_rng(6)
    n = 600
    q = _market(n, rng)
    df = pd.DataFrame({"y": _draw(q, rng), "league": "E0", "season": "1920",
                       "date": pd.date_range("2019-08-01", periods=n, freq="D")})  # fmt: skip
    for k, s in enumerate(lr.SIDES):
        df[f"q_{s}"] = q[:, k]
        df[f"p_l1_{s}"] = q[:, k]
        df[f"fair_close_{s}"] = q[:, k]
        df[f"odds_{s}"] = 1 / q[:, k] / 1.03
    refits = [{"selected": [], "coefs": {}}] * 3
    res = lr.report(df, refits, n_boot=200)
    assert res["log_loss"]["improvement"] == pytest.approx(0)
    assert res["gate"]["passes"] is False
    assert res["rule"]["bets"] == 0
    assert lr.summary_lines(res)


def test_cup_coverage_counts_only_the_cups_asked(monkeypatch):
    from soccer_stats import cups

    asked = []

    def fake_calendar(slug, start, end, **kw):
        asked.append(slug)
        return {"events": [{"home": "A FC", "away": "B FC"}], "mode": "test", "failures": 0}

    monkeypatch.setattr(cups, "season_calendar", fake_calendar)
    monkeypatch.setattr(cups, "match_team", lambda n, teams: {"league": "D1", "name": n})
    m = pd.DataFrame({"league": ["D1"], "home": ["A"], "away": ["B"]})
    rows = lr.cup_coverage(m, [2020], 60, ["DFBP"])
    assert asked == ["ger.dfb_pokal"]
    assert rows[0]["cup"] == "DFBP" and rows[0]["events_with_club"] == 1


def test_feature_list_is_the_frozen_one():
    assert len(lr.FACTORS) == 13 and len(lr.FEATURES) == 46
    assert len(set(lr.FEATURES)) == 46


def test_place_table():
    assert lr.places("E0", "1819") == (20, 4, 6, 18)
    assert lr.places("D1", "2021") == (18, 4, 6, 16)
    assert lr.places("F1", "2122") == (20, 4, 6, 18)
    assert lr.places("F1", "2223") == (20, 4, 6, 17)
    assert lr.places("F1", "2324") == (18, 4, 6, 16)
    assert lr.places("E1", "2324") == (24, 2, 6, 22)
    assert lr.places("E2", "1920") == (23, 2, 6, 21)
    assert lr.places("E3", "1819") == (24, 3, 7, 23)


def _table_league(results):
    """One E0 season: matches (home, away, hg, ag) a week apart from 1 Aug 2018."""
    rows = []
    d = pd.Timestamp("2018-08-01")
    for h, a, hg, ag in results:
        rows.append(
            {
                "league": "E0",
                "season": "1819",
                "date": d,
                "home": h,
                "away": a,
                "home_goals": hg,
                "away_goals": ag,
            }
        )
        d += pd.Timedelta(days=7)
    m = pd.DataFrame(rows)
    m["day"] = m["date"] - lr.EARLY_LEAD
    return m


def test_table_features_use_earlier_results_and_need_three_matches():
    teams = [f"T{i}" for i in range(20)]
    res = [(teams[0], teams[i], 3, 0) for i in range(1, 4)]  # T0 wins three
    res += [(teams[0], teams[4], 0, 0)]
    m = _table_league(res)
    t = lr.table_features(m)
    assert np.isnan(t[(2, "home")]["gap_hi"])  # T0 has played two before the 3rd match
    f = t[(3, "home")]  # T0: 9 points from 3; 35 games left
    assert f["games_left"] == 35
    assert f["gap_hi"] == pytest.approx((9 - 0) / (3 * 35 + 3))
    assert f["nothing"] == 0


def test_congestion_counts_before_kickoff_and_league_matches_after():
    days = np.array(
        pd.to_datetime(["2018-09-01", "2018-09-10", "2018-09-15", "2018-09-18"]),
        dtype="datetime64[ns]",
    )
    ko = pd.Timestamp("2018-09-15")
    lo, hi = ko - pd.Timedelta(days=14), ko + pd.Timedelta(days=4)
    assert lr._count(days, lo, ko) == 2  # 1 and 10 Sep; not the match itself
    assert lr._count(days, ko, hi, closed="right") == 1  # 18 Sep


def test_cup_days_map_clubs_and_drop_after_the_cutoff():
    events = [
        {"kickoff": "2018-09-25T18:45:00Z", "home": "Arsenal FC", "away": "Brentford"},
        {"kickoff": "2024-09-25T18:45:00Z", "home": "Arsenal FC", "away": "Bolton"},
    ]

    def match(name, teams):
        return {"name": "Arsenal"} if name.startswith("Arsenal") else None

    got, n = lr.cup_days(events, {"E0": {"Arsenal"}}, match, pd.Timestamp("2024-07-01", tz="UTC"))
    assert got == {"Arsenal": [pd.Timestamp("2018-09-25")]} and n == 1


def test_cups_flag_and_congestion_in_build_factors():
    m = _matches()
    a = lr.build_factors(m)
    assert (a["cups_covered"] == 0).all()
    ko = pd.Timestamp(m.loc[40, "date"])
    team = m.loc[40, "home"]
    cups = {team: [ko - pd.Timedelta(days=3)]}
    b = lr.build_factors(m, cups=cups)
    assert (b["cups_covered"] == 1).all()  # E0's cups are complete
    assert b.loc[40, "home_cong"] == a.loc[40, "home_cong"] + 1
    c = lr.build_factors(m, cups=cups, cups_incomplete={("E0", "1819")})
    assert (c.loc[c["season"] == "1819", "cups_covered"] == 0).all()
