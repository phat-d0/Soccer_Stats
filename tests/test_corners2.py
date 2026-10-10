"""Corners bake-off 2 (edge/corners2.py) on a synthetic league (no network)."""

import numpy as np
import pandas as pd
import pytest
from test_corners import _league as _base

from soccer_stats.edge import corners2, totals
from soccer_stats.lab.harness import Holdout, HoldoutLocked


def _league(seed=0, seasons=range(2014, 2020)):
    df = _base(seed, seasons)
    rng = np.random.default_rng(seed + 1)
    n = len(df)
    for side in ("home", "away"):
        df[f"{side}_sot"] = rng.binomial(df[f"{side}_shots"].astype(int), 0.35)
        df[f"{side}_fouls"] = rng.poisson(11, n).astype(float)
        df[f"{side}_cards"] = rng.poisson(2, n).astype(float)
    ph = np.clip(0.45 + 0.3 * (df["exp_home"] - df["exp_away"]), 0.1, 0.8)
    pa = np.clip(0.8 - ph, 0.05, None)
    df["pinnacle_early_home"] = 0.95 / ph
    df["pinnacle_early_draw"] = 0.95 / 0.2
    df["pinnacle_early_away"] = 0.95 / pa
    return df


@pytest.fixture
def short_calendar(monkeypatch):
    """Development 2017/18-2018/19, test 2019/20 (the real ones are 2017-2023 and 2026)."""
    monkeypatch.setattr(corners2, "DEV_END", pd.Timestamp("2019-07-01"))
    monkeypatch.setattr(corners2, "TEST_START", pd.Timestamp("2019-07-01"))
    monkeypatch.setattr(corners2, "TEST_END", pd.Timestamp("2020-02-01"))
    monkeypatch.setattr(totals, "N_BOOT", 1000)


def test_eb_shrink_tracks_how_much_teams_differ():
    rng = np.random.default_rng(0)
    teams = pd.Series(np.repeat([f"T{i}" for i in range(20)], 40))
    same = rng.normal(1.0, 0.5, len(teams))
    differ = same + np.repeat(rng.normal(0, 0.3, 20), 40)
    assert corners2.eb_shrink(teams, differ) < 10 < corners2.eb_shrink(teams, same)


def test_style_features_use_earlier_matches_only():
    df = _league(seasons=range(2014, 2016))
    f = corners2.style_features(df)
    later = df.copy()
    cut = pd.Timestamp("2015-03-01")
    later.loc[later["date"] >= cut, ["home_fouls", "away_fouls"]] = 40.0
    g = corners2.style_features(later)
    early = (df["date"] < cut).to_numpy()
    pd.testing.assert_frame_equal(f[early], g[early])
    assert np.isfinite(f[corners2.H_FEATS].to_numpy()).mean() > 0.8


def test_development_scores_the_family_and_picks_a_passing_finalist(short_calendar):
    df = _league()
    res = corners2.run(df, level=0.95)
    assert res["stage"] == "development" and res["seasons"] == [2017, 2018]
    c = res["candidates"]
    assert set(c) == {"b", "f", "g", "h"} and not c["b"]["family"] and not c["b"]["pass"]
    assert set(c["f"]["lines"]) == {f"{s}_{x}" for s in ("home", "away") for x in (3.5, 4.5, 5.5)}
    assert c["f"]["gain"] > 0  # teams differ in the synthetic league
    assert res["coverage"]["g"] == 1.0 and res["coverage"]["h"] > 0.8
    fin = res["finalist"]
    assert fin is None or (fin in corners2.FAMILY and c[fin]["pass"])
    text = corners2.report("E0", res)
    assert "Finalist" in text and "nan" not in text.lower()


def test_finalist_needs_a_pass():
    r = lambda g, p: {"gain": g, "pass": p}  # noqa: E731
    assert corners2.finalist({"f": r(0.03, False), "g": r(0.01, True), "h": r(0.02, True)}) == "h"
    assert corners2.finalist({"f": r(0.03, False), "g": r(0.04, False), "h": r(0.0, False)}) is None
    assert corners2.finalist({"f": r(0.02, True), "g": r(0.02, True), "h": r(0.01, True)}) == "f"


def test_test_season_is_locked(short_calendar, monkeypatch):
    df = _league()
    data = corners2.frame(df)
    with pytest.raises(HoldoutLocked):
        corners2.predictions(
            data, corners2.TEST_START, corners2.TEST_END, Holdout(corners2.TEST_START)
        )
    # Not before the pre-registered opening date, and only for a family finalist.
    with pytest.raises(SystemExit):
        corners2.run(df, 0.95, "too early", "f")
    monkeypatch.setattr(corners2, "EARLIEST_OPEN", pd.Timestamp("2000-01-01", tz="UTC"))
    with pytest.raises(SystemExit):
        corners2.run(df, 0.95, "a reason", "b")
    res = corners2.run(df, 0.95, "a reason", "g")
    assert res["stage"] == "test" and res["holdout_log"]
    assert set(res["candidates"]) == {"b", "g"} and res["seasons"] == [2019]
    assert (pd.to_datetime(df["date"]) < corners2.TEST_END).any()
