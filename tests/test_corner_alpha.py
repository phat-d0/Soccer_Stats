"""Candidate f-oos (edge/corner_alpha.py): (f)'s means with an out-of-sample alpha."""

import numpy as np
import pandas as pd

from soccer_stats.corners_live import TeamCornersF
from soccer_stats.edge import corner_alpha as ca
from soccer_stats.lab.model_assumptions import corner_walk


def season(seed=0, days=900, teams=16, alpha=0.08):
    rng = np.random.default_rng(seed)
    names = [f"T{i}" for i in range(teams)]
    strength = dict(zip(names, rng.normal(1, 0.2, teams), strict=True))
    rows = []
    start = pd.Timestamp("2018-07-01")
    for d in range(0, days, 3):
        for _ in range(4):
            h, a = rng.choice(names, 2, replace=False)
            out = []
            for mu in (5.5 * strength[h], 4.5 * strength[a]):
                lam = rng.gamma(1 / alpha, alpha * mu)  # NB2 with this alpha
                out.append(rng.poisson(lam))
            rows.append(
                {
                    "date": start + pd.Timedelta(days=d),
                    "home": h,
                    "away": a,
                    "home_corners": out[0],
                    "away_corners": out[1],
                }
            )
    return pd.DataFrame(rows)


def test_same_means_and_a_wider_out_of_sample_alpha():
    m = season()
    f = TeamCornersF().fit(m)
    c = ca.CandidateFOos().fit(m)
    np.testing.assert_allclose(
        c.rates(["T1", "T2"], ["T3", "T4"]), f.rates(["T1", "T2"], ["T3", "T4"])
    )
    assert c.alpha_in_ == f.alpha_
    assert c.alpha_source_ == "oos" and c.oos_matches_ >= ca.MIN_OOS
    # In-sample ratings soak up noise, so the out-of-sample alpha is larger.
    assert c.alpha_[0] > f.alpha_[0] and c.alpha_[1] > f.alpha_[1]
    info = c.info()
    assert info["model"] == "f-oos" and info["alpha_source"] == "oos"
    h, a = c.pmfs(["T1"], ["T2"])
    assert np.isclose(h.sum(axis=-1), 1, atol=1e-3).all()


def test_no_look_ahead_and_fallback():
    m = season()
    now = pd.Timestamp("2020-06-01", tz="UTC")
    a = ca.CandidateFOos().fit(m, now=now)
    b = ca.CandidateFOos().fit(m[m["date"] < "2020-06-01"])
    assert a.alpha_ == b.alpha_
    later = m.copy()
    later.loc[later["date"] >= "2020-06-01", "home_corners"] = 30  # changed future
    assert ca.CandidateFOos().fit(later, now=now).alpha_ == a.alpha_
    short = m[m["date"] < "2019-04-15"]  # about 300 matches: no inner fit is possible
    s = ca.CandidateFOos().fit(short)
    assert s.alpha_source_ == "in_sample" and s.alpha_ == s.alpha_in_


def test_corner_walk_takes_the_candidate():
    m = season(days=800)
    base = corner_walk(m, "2020-07-01", "2020-09-01")
    cand = corner_walk(m, "2020-07-01", "2020-09-01", model=ca.CandidateFOos)
    assert len(base) == len(cand) > 0
    np.testing.assert_allclose(base["m_home"], cand["m_home"])
    assert (cand["alpha_home"] > base["alpha_home"]).all()
