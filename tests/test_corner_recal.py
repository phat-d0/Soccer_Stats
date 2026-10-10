"""Team-corner recalibration (edge/corner_recal.py), no network."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats.edge import corner_recal, totals


def test_coverage_counts_played_matches_with_both_counts():
    raw = pd.DataFrame(
        {
            "HomeTeam": ["A", "B", "C", "D"],
            "AwayTeam": ["E", "F", "G", "H"],
            "FTHG": [1, 0, 2, np.nan],  # the last one isn't played
            "FTAG": [0, 0, 1, np.nan],
            "HC": [5, np.nan, 7, np.nan],
            "AC": [3, 4, 2, np.nan],
        }
    )
    assert corner_recal.coverage_counts(raw) == {"played": 3, "with_corners": 2, "share": 0.6667}
    assert corner_recal.coverage_counts(raw.drop(columns="HC"))["with_corners"] == 0


def test_fit_recovers_an_over_confident_model():
    rng = np.random.default_rng(2)
    p_true = rng.uniform(0.15, 0.85, 40_000)
    y = (rng.uniform(size=len(p_true)) < p_true).astype(int)
    # Over-confident: the model's logit is 1.6 times the truth's.
    p_model = corner_recal.expit(1.6 * corner_recal._logit(p_true) + 0.1)
    a, b = corner_recal.fit(p_model, y)
    assert abs(b - 1 / 1.6) < 0.03 and abs(a + 0.1 / 1.6) < 0.03
    fixed = corner_recal.apply(p_model, (a, b))
    assert np.abs(fixed - p_true).max() < 0.03


def _league(seed=0, seasons=range(2014, 2021)):
    """Double round robins; corners NB2 with team attack/defence effects."""
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
            lh, la = np.exp(1.65 + att[h] + dfn[a]), np.exp(1.45 + att[a] + dfn[h])
            rows.append(
                {
                    "date": day + pd.Timedelta(days=i // 2),
                    "home": h,
                    "away": a,
                    "season": f"{s % 100:02d}{(s + 1) % 100:02d}",
                    "home_corners": float(rng.negative_binomial(12, 12 / (12 + lh))),
                    "away_corners": float(rng.negative_binomial(12, 12 / (12 + la))),
                    "home_shots": rng.poisson(2.4 * lh),
                    "away_shots": rng.poisson(2.4 * la),
                    "exp_home": float(np.exp(0.3 + 0.5 * att[h])),
                    "exp_away": float(np.exp(0.1 + 0.5 * att[a])),
                }
            )
    df = pd.DataFrame(rows)
    df["season_start"] = 2000 + df["season"].str[:2].astype(int)
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    return df


@pytest.fixture
def short_calendar(monkeypatch):
    """Test season 2019/20 (the real one is 2025/26)."""
    monkeypatch.setattr(corner_recal, "TEST_SEASON", 2019)
    monkeypatch.setattr(corner_recal, "TEST_START", pd.Timestamp("2019-07-01"))
    monkeypatch.setattr(corner_recal, "TEST_END", pd.Timestamp("2020-07-01"))
    monkeypatch.setattr(totals, "N_BOOT", 1000)
    monkeypatch.setattr(corner_recal, "N_BOOT", 1000)


def test_development_run_never_predicts_the_test_season(short_calendar, monkeypatch):
    seen = []
    real = corner_recal.corners.predictions

    def spy(data, start, end, holdout):
        seen.append((data["date"].max(), end))
        return real(data, start, end, holdout)

    monkeypatch.setattr(corner_recal.corners, "predictions", spy)
    res = corner_recal.run(_league(), "E0", 0.95)
    assert "test" not in res
    assert seen[0][0] < corner_recal.TEST_START and seen[0][1] == corner_recal.TEST_START
    assert res["development"]["seasons"] == [2018]


def test_test_run_fits_on_earlier_seasons_only(short_calendar):
    df = _league()
    res = corner_recal.run(df, "E0", 0.95, "test opening")
    assert res["holdout_log"] and res["test"]["raw"]["matches"] > 300
    later = df.copy()
    late = later["season_start"] == 2019
    later.loc[late, ["home_corners", "away_corners"]] = 20.0  # only the test season changes
    res2 = corner_recal.run(later, "E0", 0.95, "test opening")
    assert res2["fit"] == res["fit"]  # the recalibration never sees 2019/20
    t = res["test"]
    assert set(t["raw"]["lines"]) == {f"{s}_{x}" for s in ("home", "away") for x in (3.5, 4.5, 5.5)}
    text = corner_recal.report(res)
    assert "Test 2019/20" in text and "nan" not in text.lower()


def test_family_size_and_finalists():
    assert corner_recal.TESTS == 12
    assert corner_recal.FINALISTS == {
        "E0": "d",
        "SP1": "c",
        "D1": "d",
        "I1": "c",
        "F1": "d",
        "E1": "d",
    }
