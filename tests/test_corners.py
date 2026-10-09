"""Corners bake-off (edge/corners.py) on a synthetic league (no network)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats.edge import corners, totals
from soccer_stats.lab.harness import Holdout, HoldoutLocked, blocks


def _league(seed=0, seasons=range(2014, 2020)):
    """A double round robin per season. Corners are NB2 with a team attack (for) and
    defence (against) effect and a home edge; shots follow the same tendencies."""
    rng = np.random.default_rng(seed)
    teams = [f"T{i}" for i in range(20)]
    att = dict(zip(teams, rng.normal(0, 0.25, 20), strict=True))
    dfn = dict(zip(teams, rng.normal(0, 0.15, 20), strict=True))
    rows = []
    for s in seasons:
        day = pd.Timestamp(f"{s}-08-10")
        pairs = [(h, a) for h in teams for a in teams if h != a]
        rng.shuffle(pairs)
        for i, (h, a) in enumerate(pairs):
            lh = np.exp(1.65 + att[h] + dfn[a])
            la = np.exp(1.45 + att[a] + dfn[h])
            r = 12.0
            rows.append(
                {
                    "date": day + pd.Timedelta(days=i // 2),
                    "home": h,
                    "away": a,
                    "season": f"{s % 100:02d}{(s + 1) % 100:02d}",
                    "home_corners": rng.negative_binomial(r, r / (r + lh)),
                    "away_corners": rng.negative_binomial(r, r / (r + la)),
                    "home_shots": rng.poisson(2.4 * lh),
                    "away_shots": rng.poisson(2.4 * la),
                    "exp_home": float(np.exp(0.3 + 0.5 * att[h])),
                    "exp_away": float(np.exp(0.1 + 0.5 * att[a])),
                }
            )
    df = pd.DataFrame(rows)
    # Float counts, as football-data loads them (and as read-only arrays under
    # copy-on-write): the ratings fit must not write into them.
    df[["home_corners", "away_corners"]] = df[["home_corners", "away_corners"]].astype(float)
    df["season_start"] = 2000 + df["season"].str[:2].astype(int)
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    return df


@pytest.fixture
def short_calendar(monkeypatch):
    """Development 2017/18-2018/19, holdout 2019/20 (the real ones are 2017-2024)."""
    monkeypatch.setattr(corners, "HOLDOUT_START", pd.Timestamp("2019-07-01"))
    monkeypatch.setattr(corners, "HOLDOUT_END", pd.Timestamp("2020-07-01"))
    monkeypatch.setattr(totals, "N_BOOT", 2000)


def test_mom_alpha():
    rng = np.random.default_rng(1)
    m = np.full(200_000, 5.0)
    y = rng.negative_binomial(4, 4 / 9, size=len(m))  # mean 5, alpha 1/4
    assert corners.mom_alpha(y, m) == pytest.approx(0.25, rel=0.05)
    assert corners.mom_alpha(np.full(10, 5), np.full(10, 5.0)) == 1e-4  # under-dispersed


def test_family_sizes_and_levels():
    assert corners.DEV_TESTS == 42 and corners.HOLDOUT_TESTS == 12


def test_parse_finalists():
    assert corners.parse_finalists("total=e, team=d") == {"total": "e", "team": "d"}
    for bad in ("total=e", "total=a,team=d", "team=e,total=d", ""):
        with pytest.raises(SystemExit):
            corners.parse_finalists(bad)


def test_predictions_use_earlier_matches_only():
    df = _league(seasons=range(2014, 2018))
    data = corners.frame(df)
    start, end = pd.Timestamp("2017-07-01"), pd.Timestamp("2018-07-01")
    p = corners.predictions(data, start, end, Holdout(pd.Timestamp("2030-01-01")))
    later = df.copy()
    cut = pd.Timestamp("2018-01-15")
    late = later["date"] >= cut
    later.loc[late, ["home_corners", "away_corners"]] = 25  # the future changes
    later.loc[late, ["home_shots", "away_shots"]] = 60
    q = corners.predictions(corners.frame(later), start, end, Holdout(pd.Timestamp("2030-01-01")))
    # Rows in blocks that open before the change can't see it.
    opened = [lo for lo, _ in blocks(start, end) if lo <= cut]
    safe = data.loc[p["index"], "time"] < opened[-1]
    assert safe.sum() > 100
    for c in corners.CANDIDATES:
        for part in range(3):
            np.testing.assert_allclose(p[c][part][safe.to_numpy()], q[c][part][safe.to_numpy()])
    # Every candidate's distributions are proper.
    for c in corners.CANDIDATES:
        for part in range(3):
            assert np.allclose(p[c][part].sum(axis=1), 1)


def test_locked_holdout_refuses_to_predict(short_calendar):
    df = _league()
    with pytest.raises(HoldoutLocked):
        corners.predictions(
            corners.frame(df),
            corners.HOLDOUT_START,
            corners.HOLDOUT_END,
            Holdout(corners.HOLDOUT_START),
        )


def test_development_drops_the_holdout_and_finds_team_effects(short_calendar):
    df = _league()
    res = corners.run(df, level=0.95)
    assert res["seasons"] == [2017, 2018] and res["rows"] > 600
    c = res["candidates"]
    assert set(c) == set(corners.CANDIDATES)
    assert "team" not in c["e"]["groups"] and set(c["d"]["groups"]) == {"total", "team"}
    # Corners depend on the teams: ratings beat the league average at the count level
    # and on the team lines, with slopes near 1.
    assert c["c"]["count_log_loss"]["home"] < c["a"]["count_log_loss"]["home"]
    assert c["c"]["groups"]["team"]["gain"] > 0
    assert all(0.6 < s < 1.5 for s in c["c"]["groups"]["team"]["slopes"].values())
    assert res["finalists"]["team"] in {"b", "c", "d"}
    text = corners.report("E0", res)
    assert "development" in text and "nan" not in text.lower()


def test_holdout_run_scores_only_the_finalists(short_calendar):
    df = _league()
    res = corners.run(df, 0.95, "test opening", {"total": "c", "team": "c"})
    assert res["seasons"] == [2019] and res["holdout_log"]
    assert set(res["candidates"]) == {"a", "c"}
    assert set(res["candidates"]["c"]["groups"]) == {"total", "team"}
