"""Anytime goalscorer model: features before kickoff only, walk-forward, scoring."""

import numpy as np
import pandas as pd
import pytest
from player_sim import add_goals, simulate_players

from soccer_stats import player_goals as pg
from soccer_stats.factors import build_features
from soccer_stats.models.player_goals import GoalscorerModel, p_zero


@pytest.fixture(scope="module")
def apps():
    a, _ = simulate_players(n_teams=8, seasons=3, seed=11)
    return add_goals(a, seed=11)


@pytest.fixture(scope="module")
def feats(apps):
    return pg.goal_features(build_features(apps))


def test_p_zero_poisson_limit():
    m = np.array([0.0, 0.3, 1.0])
    assert p_zero(m, 0.0) == pytest.approx(np.exp(-m))
    assert p_zero(m, 1e-4) == pytest.approx(np.exp(-m), abs=1e-4)
    assert (p_zero(m, 0.5) >= np.exp(-m) - 1e-12).all()  # overdispersion: more zeros


def test_goal_features_use_only_earlier_matches(apps):
    cut = apps["kickoff"].sort_values().iloc[len(apps) // 2]
    later = apps["kickoff"] >= cut
    rng = np.random.default_rng(0)
    changed = apps.copy()
    changed.loc[later, "goals"] = rng.integers(0, 4, later.sum())
    changed.loc[later, "xg"] = rng.uniform(0, 2, later.sum())
    changed.loc[later, "shots"] = changed.loc[later, "goals"] + rng.integers(0, 5, later.sum())
    cols = ["log_xg_rate", "log_finish", "log_rate", "season_avg_goals", "season_avg_xg"]
    a = pg.goal_features(build_features(apps))
    b = pg.goal_features(build_features(changed))
    # Strictly before the cut (the simulator can give a team two matches at one kickoff).
    keep = a["kickoff"] < cut
    pd.testing.assert_frame_equal(a.loc[keep, cols], b.loc[keep, cols])


def test_walk_forward_predictions_do_not_see_the_future(feats, apps):
    start = feats["kickoff"].min() + pd.Timedelta(days=400)
    base = pg.walk_forward(feats, start, refit_every="28D")
    cut = base["kickoff"].quantile(0.5)
    later = apps["kickoff"] >= cut
    changed = apps.copy()
    changed.loc[later, "goals"] = 3
    changed.loc[later, "xg"] = 2.0
    other = pg.walk_forward(pg.goal_features(build_features(changed)), start, refit_every="28D")
    a = base[base["kickoff"] < cut].sort_values(["match_id", "player_id"]).reset_index(drop=True)
    b = other[other["kickoff"] < cut].sort_values(["match_id", "player_id"]).reset_index(drop=True)
    assert len(a) > 100
    np.testing.assert_allclose(a["p_model"], b["p_model"])
    np.testing.assert_allclose(a["p_base_goals"], b["p_base_goals"])


def test_model_beats_season_benchmarks_on_simulated_data(feats):
    start = feats["kickoff"].min() + pd.Timedelta(days=365)
    preds = pg.walk_forward(feats, start, refit_every="28D")
    assert preds["p_model"].between(0, 1).all()
    s = pg.score(preds)
    assert s["n"] == len(preds) and s["matches"] > 50
    # Lasting chance quality: the model (xG and shots history) beats noisy season averages.
    assert s["vs_season_goals"]["diff"] < 0 and s["vs_season_goals"]["beats"]
    lo, hi = s["vs_season_goals"]["range95"]
    assert lo <= s["vs_season_goals"]["diff"] <= hi
    assert abs(s["predicted_rate"] - s["scored_rate"]) < 0.03  # calibrated overall
    assert sum(r["n"] for r in s["calibration"]) == len(preds)


def test_lineup_known_mixture(feats):
    train = feats[feats["prev_apps"] >= 3].iloc[:4000]
    test = feats[feats["prev_apps"] >= 3].iloc[4000:4500]
    m = GoalscorerModel().fit(train)
    p_known = m.prob_score(test, lineup_known=True)
    starters, subs = test["started"].to_numpy(), ~test["started"].to_numpy()
    p_before = m.prob_score(test)
    # Starters play longer, so knowing he starts raises his chance; a sub's falls.
    assert (p_known[starters] >= p_before[starters] - 1e-9).all()
    assert (p_known[subs] <= p_before[subs] + 1e-9).all()


def test_report_splits_holdout_and_live(feats):
    start = feats["kickoff"].min() + pd.Timedelta(days=365)
    preds = pg.walk_forward(feats, start, refit_every="28D")
    seasons = sorted(preds["season"].unique())
    rep = pg.report(
        preds.assign(season=preds["season"].map({seasons[0]: "2425", seasons[-1]: "2526"}))
    )
    assert set(rep["before_lineups"]) == {"development", "holdout"}
    assert isinstance(rep["gate"]["passed"], bool) and rep["holdout"] == "2526"
    later = preds.assign(season="2627")
    assert set(pg.report(later)["before_lineups"]) == {"live"}
    assert pg.report(later)["gate"]["passed"] is False  # no holdout scored: not passed


def test_cli_prints_goal_report(feats, capsys):
    from soccer_stats import cli

    start = feats["kickoff"].min() + pd.Timedelta(days=365)
    preds = pg.walk_forward(feats, start, refit_every="28D")
    rep = pg.report(preds.assign(season="2526"), preds.assign(season="2526"))
    cli._print_goals(rep)
    out = capsys.readouterr().out
    assert "before_lineups" in out and "lineup_known" in out and "nan" not in out.lower()


def test_goal_walk_forward_respects_the_locked_holdout(feats):
    from soccer_stats.lab.harness import HoldoutLocked

    start = feats["kickoff"].min() + pd.Timedelta(days=365)
    cut = feats["kickoff"].max() - pd.Timedelta(days=60)
    locked = pg.stage1_holdout(None)
    locked.start = cut
    with pytest.raises(HoldoutLocked):
        pg.walk_forward(feats, start, refit_every="28D", holdout=locked)
    opened = pg.stage1_holdout("test: pre-registered")
    opened.start = cut
    p = pg.walk_forward(feats, start, refit_every="28D", holdout=opened)
    assert (p["kickoff"] >= cut).any() and opened.events[0].startswith("HOLDOUT OPENED")


def test_lab_metrics_on_goal_predictions(feats):
    start = feats["kickoff"].min() + pd.Timedelta(days=365)
    preds = pg.walk_forward(feats, start, refit_every="28D")
    r = pg.lab_metrics(preds, n_boot_blend=20)
    assert r["rows"] == len(preds) and r["gain"] > 0  # beats the season-xG benchmark
    lo, hi = r["gain_range95"]
    assert lo <= r["gain"] <= hi and r["blend_c"] > 0


def test_weekly_goals_never_see_the_locked_forward_window(apps, monkeypatch):
    """While the forward window is locked, no appearance on or after its start is fitted,
    scored or saved in E0_goals.json, and changing those matches changes nothing."""
    start = apps["kickoff"].min() + pd.Timedelta(days=365)
    cut = pd.Timestamp("2025-08-15", tz="UTC")  # inside the simulated 2025/26
    feats = pg.goal_features(build_features(apps))
    assert (feats["kickoff"] >= cut).sum() > 100

    seen = []
    real = pg.walk_forward

    def spy(f, *a, **k):
        seen.append(f["kickoff"].max())
        return real(f, *a, **k)

    monkeypatch.setattr(pg, "walk_forward", spy)
    locked = pg.stage1_goals(feats, start, None, cut)
    assert seen and max(seen) < cut  # nothing from the window reaches a fit or a score
    fw = locked["forward_window"]
    assert fw["open"] is False and fw["dropped"] == int((feats["kickoff"] >= cut).sum())
    assert fw["log"] == []

    # The window's own matches can change freely: the saved report is identical.
    later = apps["kickoff"] >= cut
    changed = apps.copy()
    changed.loc[later, "goals"] = 3
    changed.loc[later, "xg"] = 2.0
    other = pg.stage1_goals(pg.goal_features(build_features(changed)), start, None, cut)
    strip = ("holdout_log", "forward_window")
    assert {k: v for k, v in locked.items() if k not in strip} == {
        k: v for k, v in other.items() if k not in strip
    }

    # Opened with a reason, the window's rows come back and the opening is logged.
    seen.clear()
    opened = pg.stage1_goals(feats, start, "test: forward window opened", cut)
    assert max(seen) >= cut and opened["forward_window"]["open"] is True
    assert opened["forward_window"]["log"][0].startswith("HOLDOUT OPENED")
    n = lambda r: sum(v.get("n", 0) for v in r["before_lineups"].values())  # noqa: E731
    assert n(opened) > n(locked)


def test_forward_start_is_shared():
    from soccer_stats import player_goal_lab as gl

    assert pg.FORWARD_START == gl.FORWARD_START == pd.Timestamp("2026-10-10", tz="UTC")
