"""Goalscorer improvements (docs/player_props.md §8): new features use only earlier
matches, candidates run through the lab harness, the pre-registered comparison and
tail rule, the locked forward window, and the pilot-log parser."""

import numpy as np
import pandas as pd
import pytest
from player_sim import add_goals, simulate_players

from soccer_stats import player_goal_lab as gl
from soccer_stats import player_goals as pg
from soccer_stats.factors import build_features
from soccer_stats.lab.harness import Holdout, HoldoutLocked

NEW = [*gl.C_FEATS, *gl.D_FEATS, "log_xg_share", *gl.F_FEATS]


def with_set_pieces(apps, seed=0):
    rng = np.random.default_rng(seed)
    pen = (rng.random(len(apps)) < 0.01) & apps["position"].isin(["FWD", "MID"])
    return apps.assign(
        sp_xg=np.round(apps["xg"] * rng.uniform(0, 0.3, len(apps)), 3),
        penalties=pen.astype(int),
        pen_xg=np.where(pen, 0.76, 0.0),
    )


@pytest.fixture(scope="module")
def apps():
    a, _ = simulate_players(n_teams=8, seasons=3, seed=21)
    return with_set_pieces(add_goals(a, seed=21))


@pytest.fixture(scope="module")
def feats(apps):
    return gl.extra_features(pg.goal_features(build_features(apps)))


def test_new_features_use_only_earlier_matches(apps):
    cut = apps["kickoff"].sort_values().iloc[len(apps) // 2]
    later = apps["kickoff"] >= cut
    rng = np.random.default_rng(1)
    ch = apps.copy()
    for col in ("xg", "sp_xg"):
        ch.loc[later, col] = rng.uniform(0, 2, later.sum())
    ch.loc[later, "goals"] = rng.integers(0, 3, later.sum())
    ch.loc[later, "penalties"] = rng.integers(0, 2, later.sum())
    a = gl.extra_features(pg.goal_features(build_features(apps)))
    b = gl.extra_features(pg.goal_features(build_features(ch)))
    keep = a["kickoff"] < cut
    pd.testing.assert_frame_equal(a.loc[keep, NEW], b.loc[keep, NEW])
    assert a[NEW].notna().all().all()


def test_penalty_taker_is_the_last_one_before(feats):
    f = feats.sort_values("kickoff")
    team = f[f["penalties"] > 0]["team"].iloc[0]
    t = f[f["team"] == team]
    first = t[t["penalties"] > 0].iloc[0]
    same = t[t["kickoff"] == first["kickoff"]]
    assert same["pen_taker"].sum() == 0  # not known at his own match
    after = t[(t["kickoff"] > first["kickoff"]) & (t["player_id"] == first["player_id"])]
    if not after.empty:
        nxt = t[(t["kickoff"] > first["kickoff"]) & (t["penalties"] > 0)]["kickoff"]
        upto = after[after["kickoff"] <= (nxt.min() if len(nxt) else after["kickoff"].max())]
        assert (upto["pen_taker"] == 1).all()


def test_candidates_through_the_harness(feats):
    lo = feats["kickoff"].min() + pd.Timedelta(days=400)
    hi = lo + pd.Timedelta(days=120)
    for name in ("A", "B", "G"):
        p = gl.predict(feats, name, lo, hi)
        assert len(p) and p.dropna().between(0, 1).all()
    h = gl.predict(feats, "H", lo, hi, gbm=gl.GBM_GRID[0])
    assert h.dropna().between(0, 1).all() and h.notna().sum() > 100


def test_candidate_never_sees_its_block(feats):
    lo = feats["kickoff"].min() + pd.Timedelta(days=400)
    hi = lo + pd.Timedelta(days=60)
    later = feats["kickoff"] >= lo
    ch = feats.copy()
    ch.loc[later, "goals"] = 5
    a = gl.predict(feats, "G", lo, hi)
    b = gl.predict(ch, "G", lo, hi)
    np.testing.assert_allclose(
        a.loc[a.index[: len(a) // 4]].fillna(-1), b.loc[a.index[: len(a) // 4]].fillna(-1)
    )


def test_locked_windows(feats):
    lo = feats["kickoff"].min() + pd.Timedelta(days=400)
    hi = feats["kickoff"].max() + pd.Timedelta(days=1)
    h = Holdout(lo + pd.Timedelta(days=60))
    with pytest.raises(HoldoutLocked):
        gl.predict(feats, "B", lo, hi, h)
    fh = gl.forward_holdout()
    assert not fh.unlocked and fh.start == gl.FORWARD_START
    fh2 = gl.forward_holdout("test reason")
    assert fh2.unlocked and "test reason" in fh2.events[0]


def test_compare_and_rules(feats):
    sim = feats.copy()
    sim["season"] = sim["season"].astype(str)
    dev, tuned = gl.predict_dev(sim, log=lambda *_: None)
    assert set(gl.CANDIDATES) <= set(dev.columns)
    res = gl.compare(dev, sim)
    assert set(res["tests"]) == set(gl.REFERENCE) and res["level"] == pytest.approx(
        0.9929, abs=1e-4
    )
    assert res["chosen"] in "BCDEFGH"
    b = res["candidates"]["B"]
    assert 0 < b["auc"] < 1 and b["resolution"] >= 0 and b["rows"] == res["rows"]
    # Knowing the lineup helps on starters.
    assert res["tests"]["B"]["gain"] > 0
    for t in res["tests"].values():
        assert t["passes"] == (t["gain_passes"] and t["tail_ok"])


def test_tail_rule():
    rng = np.random.default_rng(0)
    p = np.r_[np.full(2000, 0.25), np.full(2000, 0.4)]
    y = (rng.random(4000) < p).astype(int)
    assert gl.tail_rule(p, y)["ok"]
    assert not gl.tail_rule(p + 0.1, y)["ok"]  # 10 points too high


LOG = """2026-10-07T05:02:09.5259705Z   pilot Liverpool v Bournemouth: 2 prices
2026-10-07T05:02:09.5259910Z          betrivers Adam Smith                   yes 61.0 no nan
2026-10-07T05:02:09.5262720Z          betrivers Francisco Evanilson de Lima Barbosa yes 3.65 no nan
2026-10-07T05:02:09.5265746Z   pilot West Ham United v Wolverhampton Wanderers: 1 prices
2026-10-07T05:02:09.5869885Z         mybookieag Aaron Wan-Bissaka            yes 15.5 no nan
2026-10-07T05:02:09.5877243Z Pilot books: [{"book": "betrivers"}]
"""


def test_parse_pilot_log():
    df = gl.parse_pilot_log(LOG)
    assert len(df) == 3
    assert df.iloc[1]["player"] == "Francisco Evanilson de Lima Barbosa"
    assert df.iloc[2][["home", "away", "book", "yes"]].tolist() == [
        "West Ham United",
        "Wolverhampton Wanderers",
        "mybookieag",
        15.5,
    ]
