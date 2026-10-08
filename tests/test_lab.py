"""Research-lab harness and bake-off candidates on synthetic data (no network)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import features, metrics
from soccer_stats.lab.harness import Holdout, HoldoutLocked, blocks, nested, walk_forward
from soccer_stats.lab.models import GBM, HierPoisson, Logit, one_x_two

# ---------- synthetic data ----------


def _probs(r, draw=0.26):
    h = 1 / (1 + np.exp(-r))
    return np.c_[(1 - draw) * h, np.full(len(r), draw), (1 - draw) * (1 - h)]


def _rows(n=3000, hidden_sd=0.6, seed=0):
    """Events where the early market misses `hidden` and feature x1 carries it.

    The close knows it (so a model using x1 has positive CLV at the early price);
    x2 is noise.
    """
    rng = np.random.default_rng(seed)
    base = rng.normal(0.3, 0.6, n)
    hidden = rng.normal(0, hidden_sd, n)
    p_true, p_early = _probs(base + hidden), _probs(base)
    u = rng.random(n)
    y = (u[:, None] > p_true.cumsum(1)).sum(1)
    t = pd.Timestamp("2016-08-01") + pd.to_timedelta(np.arange(n) // 3, "D")
    df = pd.DataFrame(
        {
            "time": t,
            "season": [d.year if d.month >= 7 else d.year - 1 for d in t],
            "group": np.arange(n),
            "y": y,
            "x0": base,
            "x1": hidden + rng.normal(0, 0.1, n),
            "x2": rng.normal(0, 1, n),
        }
    )
    for k in range(3):
        df[f"mkt_{k}"] = p_early[:, k]
        df[f"odds_{k}"] = 1 / (1.03 * p_early[:, k])
        df[f"fair_{k}"] = p_true[:, k]
    return df


class Spy:
    """Records what each fit saw; predicts the training base rates."""

    calls: list = []

    def __init__(self, **params):
        self.params = params

    def fit(self, train):
        Spy.calls.append({"train_max": train["time"].max(), "params": self.params})
        self.rates = np.bincount(train["y"], minlength=3) / len(train)

    def predict(self, test):
        Spy.calls[-1]["test_min"] = test["time"].min()
        return np.tile(self.rates, (len(test), 1))


# ---------- harness ----------


def test_blocks_cover_the_range():
    b = blocks("2020-01-01", "2020-03-15", "28D")
    assert b[0][0] == pd.Timestamp("2020-01-01") and b[-1][1] >= pd.Timestamp("2020-03-15")
    assert all(hi == lo2 for (_, hi), (lo2, _) in zip(b[:-1], b[1:], strict=True))


def test_walk_forward_and_nested_never_train_on_the_rows_they_predict():
    df = _rows(2400)
    Spy.calls = []
    pr = walk_forward(df, Spy, {}, "2017-07-01", "2018-07-01")
    assert len(pr) > 300 and np.allclose(pr.sum(axis=1), 1)
    assert all(c["train_max"] < c["test_min"] for c in Spy.calls)
    Spy.calls = []
    grid = [{"k": 1}, {"k": 2}]
    pr, chosen = nested(df, Spy, grid, [2017, 2018], period_col="season")
    assert [c["period"] for c in chosen] == [2017, 2018]
    assert all(c["train_max"] < c["test_min"] for c in Spy.calls)
    # Tuning for 2018 used only rows from before 2018's first match.
    first_2018 = df.loc[df["season"] == 2018, "time"].min()
    tune_2018 = [c for c in Spy.calls if c["test_min"] < first_2018 and c["params"] in grid]
    assert tune_2018 and all(c["train_max"] < first_2018 for c in tune_2018)


def test_holdout_is_locked_until_opened_with_a_reason(tmp_path, capsys):
    df = _rows(2400)
    h = Holdout("2018-07-01", log_path=tmp_path / "holdout.log")
    walk_forward(df, Spy, {}, "2017-07-01", "2018-07-01", holdout=h)  # before it: fine
    with pytest.raises(HoldoutLocked):
        walk_forward(df, Spy, {}, "2017-07-01", "2019-01-01", holdout=h)
    with pytest.raises(HoldoutLocked):
        nested(df, Spy, [{}], [2018], period_col="season", holdout=h)
    with pytest.raises(ValueError):
        h.unlock("  ")
    h.unlock("final scoring of pre-registered finalists")
    assert "HOLDOUT OPENED" in capsys.readouterr().out
    assert "pre-registered" in (tmp_path / "holdout.log").read_text()
    assert len(walk_forward(df, Spy, {}, "2018-07-01", "2019-01-01", holdout=h)) > 0
    assert (h.development(df)["time"] < h.start).all()


# ---------- metrics ----------


def _evaluate(df, p, level=0.95):
    k = range(3)
    return metrics.evaluate(
        p,
        df["y"],
        df[[f"mkt_{i}" for i in k]].to_numpy(),
        df[[f"odds_{i}" for i in k]].to_numpy(),
        df[[f"fair_{i}" for i in k]].to_numpy(),
        df["group"],
        level=level,
        n_boot_blend=100,
    )


def test_planted_edge_passes_and_the_market_plus_noise_does_not():
    df = _rows(3600)
    pr, _ = nested(
        df, lambda **kw: _FeatureLogit(["x0", "x1"]), [{}], [2017, 2018], period_col="season"
    )
    ok = pr.index
    good = _evaluate(df.loc[ok], pr.to_numpy())
    assert good["blend"]["c_range"][0] > 0
    assert good["bets"]["bets"] > 50 and good["bets"]["clv_range"][0] > 0
    assert good["gain_vs_market"]["range"][0] > 0
    assert metrics.passes(good)

    rng = np.random.default_rng(9)
    mk = df.loc[ok, [f"mkt_{i}" for i in range(3)]].to_numpy()
    noisy = mk * np.exp(rng.normal(0, 0.15, mk.shape))
    noisy /= noisy.sum(1, keepdims=True)
    bad = _evaluate(df.loc[ok], noisy)
    assert not metrics.passes(bad)
    assert bad["bets"]["clv"] < 0


class _FeatureLogit:
    def __init__(self, cols):
        self.cols = cols

    def fit(self, train):
        from sklearn.linear_model import LogisticRegression

        self.m = LogisticRegression(C=10, max_iter=1000).fit(train[self.cols], train["y"])

    def predict(self, test):
        return self.m.predict_proba(test[self.cols])


def test_scores_and_calibration_basics():
    p = np.array([[0.5, 0.3, 0.2], [0.2, 0.3, 0.5]])
    y = np.array([0, 2])
    assert metrics.log_loss_rows(p, y) == pytest.approx(-np.log([0.5, 0.5]))
    assert metrics.brier_rows(p, y)[0] == pytest.approx(0.25 + 0.09 + 0.04)
    t = metrics.calibration_table(p, y, bins=5)
    assert t["n"].sum() == 6
    rows, k = metrics.pick_bets(p, np.array([[2.5, 3.0, 4.0], [5.0, 3.0, 1.5]]), 0.12)
    # Row 0: home edge 0.25. Row 1: best edge 0.2 * 5 - 1 = 0, under 12%.
    assert list(rows) == [0] and list(k) == [0]
    rows, _ = metrics.pick_bets(p, np.array([[2.0, 3.0, 4.0], [5.0, 3.0, 1.5]]), 0.12)
    assert list(rows) == []
    # Clustered ranges: duplicated rows in one group are not extra information.
    v = np.r_[np.ones(50), np.zeros(50)]
    wide = metrics.boot_range(v, np.repeat(np.arange(10), 10))
    narrow = metrics.boot_range(v)
    assert wide[1] - wide[0] > narrow[1] - narrow[0]


def test_walk_forward_blend_uses_earlier_rows_only():
    df = _rows(2400)
    mk = df[[f"mkt_{i}" for i in range(3)]].to_numpy()
    out = metrics.walk_forward_blend(mk, mk, df["y"], df["time"], min_rows=300)
    first = np.flatnonzero(np.isfinite(out).all(1))[0]
    assert first >= 300  # nothing before enough earlier rows exist
    # Changing later outcomes leaves earlier blended chances unchanged.
    y2 = df["y"].to_numpy().copy()
    y2[1500:] = 0
    out2 = metrics.walk_forward_blend(mk, mk, y2, df["time"], min_rows=300)
    t = df["time"]
    cut = (t < t.iloc[1500] - pd.Timedelta("28D")).to_numpy()
    np.testing.assert_allclose(out[cut], out2[cut], equal_nan=True)


# ---------- features ----------


def _league(seasons=3, teams=12, seed=1):
    rng = np.random.default_rng(seed)
    names = [f"T{i}" for i in range(teams)]
    strength = rng.normal(0, 0.35, teams)
    rows = []
    for s in range(seasons):
        day = pd.Timestamp(f"{2014 + s}-08-09")
        fixtures = [(h, a) for h in range(teams) for a in range(teams) if h != a]
        rng.shuffle(fixtures)
        for j in range(0, len(fixtures), teams // 2):
            for h, a in fixtures[j : j + teams // 2]:
                lh = np.exp(0.25 + strength[h] - strength[a])
                la = np.exp(0.05 + strength[a] - strength[h])
                rows.append(
                    {
                        "date": day,
                        "season": f"{14 + s}{15 + s}",
                        "home": names[h],
                        "away": names[a],
                        "home_goals": int(rng.poisson(lh)),
                        "away_goals": int(rng.poisson(la)),
                        "home_xg": float(rng.gamma(4, lh / 4)),
                        "away_xg": float(rng.gamma(4, la / 4)),
                    }
                )
                day += pd.Timedelta(days=1)  # one match a day: a team never plays twice
    return pd.DataFrame(rows), dict(zip(names, strength, strict=True))


def test_features_use_only_earlier_matches():
    m, _ = _league()
    f = features.build(m)
    assert set(features.FEATURES) <= set(f.columns)
    last_day = m["date"].max()
    m2 = m.copy()
    late = m2["date"] == last_day
    m2.loc[late, ["home_goals", "away_goals", "home_xg", "away_xg"]] = [9, 0, 7.0, 0.1]
    f2 = features.build(m2)
    pd.testing.assert_frame_equal(f[features.FEATURES], f2[features.FEATURES])
    # A team's first match: default Elo and no form yet.
    first = f.index[0]
    assert f.loc[first, "elo_home"] == features.ELO_NEW
    assert np.isnan(f.loc[first, "home_xgf6"]) and f.loc[first, "home_rest"] == 14


# ---------- candidates ----------


def _match_frame(seasons=3):
    m, strength = _league(seasons)
    df = features.build(m)
    df["time"] = df["date"]
    return df, strength


def test_hier_poisson_recovers_team_order():
    df, strength = _match_frame()
    hp = HierPoisson(half_life=365, prior_sd=0.4, home_sd=0.1)
    hp.fit(df)
    est = pd.Series(hp.att_) - pd.Series(hp.def_)
    true = pd.Series(strength)
    assert np.corrcoef(est[true.index], true)[0, 1] > 0.8
    assert hp.h_ > 0
    p = hp.predict(df.head(20))
    assert p.shape == (20, 3) and np.allclose(p.sum(1), 1)


def test_one_x_two_and_feature_models_give_valid_chances():
    p = one_x_two(np.array([1.5, 0.5]), np.array([0.5, 1.5]))
    assert np.allclose(p.sum(1), 1) and p[0, 0] > p[0, 2] and p[1, 2] > p[1, 0]
    df, _ = _match_frame()
    train, test = df[df["season"] != "1617"], df[df["season"] == "1617"]
    for model in (GBM(n_estimators=30), Logit(C=0.1)):
        model.fit(train)
        q = model.predict(test)
        assert q.shape == (len(test), 3) and np.allclose(q.sum(1), 1)
        assert np.isfinite(q).all()


@pytest.mark.parametrize("league", ["E0", "E1", "SP1"])
def test_bake_off_runs_end_to_end_and_keeps_the_holdout_shut(league, monkeypatch, tmp_path, capsys):
    import json

    from soccer_stats.lab import models, run

    m, _ = _league(seasons=5)  # 2014/15-2018/19
    df = features.build(m)
    df["time"] = df["date"]
    df["season_start"] = 2000 + df["season"].str[:2].astype(int)
    rng = np.random.default_rng(4)
    elo_p = _probs((df["elo_diff"].to_numpy() + 60) / 250)
    for i, k in enumerate(("home", "draw", "away")):
        noisy = elo_p[:, i] * np.exp(rng.normal(0, 0.05, len(df)))
        df[f"dc_p_{k}"] = elo_p[:, i]
        df[f"mkt_{k}"] = noisy
        df[f"fair_{k}"] = elo_p[:, i]
    mk = df[[f"mkt_{k}" for k in ("home", "draw", "away")]]
    for k in ("home", "draw", "away"):
        df[f"mkt_{k}"] = df[f"mkt_{k}"] / mk.sum(axis=1)
        df[f"pinnacle_early_{k}"] = 1 / (1.03 * df[f"mkt_{k}"])
        df[f"pinnacle_close_{k}"] = 1 / (1.03 * df[f"fair_{k}"])
    if league == "E1":
        # No xG at all (the goals-only path must not ask for it), and one season
        # without Pinnacle prices, which must be left out of scoring.
        df = df.drop(columns=[c for c in df.columns if "xg" in c])
        df.loc[df["season_start"] == 2016, "pinnacle_close_home"] = np.nan
    df["match"] = df.index.astype(str)
    seen = {}

    def fake_load(holdout, league="E0", last=None):
        seen["locked"] = not holdout.unlocked
        seen["league"] = league
        seen["last"] = last
        seen["holdout_start"] = holdout.start
        return df

    monkeypatch.setattr(run, "load", fake_load)
    monkeypatch.setattr(run, "has_xg", lambda lg: lg == "E0")
    monkeypatch.setattr(run, "WARMUP", 2015)
    monkeypatch.setattr(run, "FIRST_SCORED", 2016)
    monkeypatch.setattr(run, "LAST", 2019)
    monkeypatch.setattr(run, "STACK_MIN_ROWS", 100)
    small = {
        "a_dixon_coles": [{}],
        "b_hier_poisson": [{"half_life": 365.0}],
        "c_gbm": [{"n_estimators": 30}, {"n_estimators": 60}],
        "d_logit": [{"C": 0.1}],
    }
    monkeypatch.setattr(models, "GRIDS", small)
    monkeypatch.setattr(run, "GRIDS", small)
    out = tmp_path / "lab.json"
    extra = ["--holdout-season", "2018"] if league == "SP1" else []
    run.main(["--league", league, *extra, "--json", str(out)])
    text = capsys.readouterr().out
    assert seen["locked"] and seen["league"] == league
    assert "DEVELOPMENT" in text and "HOLDOUT OPENED" not in text
    res = json.loads(out.read_text())
    assert set(res["results"]) == {*small, "e_stack"}
    assert res["holdout_events"] == []
    if league == "E1":
        assert res["not_scored"] == [2016]
        assert res["level"] == pytest.approx(1 - 0.05 / 30, abs=1e-5)  # 3 leagues, 1 family
        assert res["coverage"]["2016"] < run.MIN_COVERAGE
        # --edge-only: candidate a and its learned minimum edge, nothing else.
        edge_out = tmp_path / "edge.json"
        run.main(["--league", league, "--edge-only", "--json", str(edge_out)])
        text2 = capsys.readouterr().out
        assert "['a_dixon_coles']" in text2 and "Stack base" not in text2
        assert "Learned minimum edge" in text2 and seen["locked"]
        e = json.loads(edge_out.read_text())
        assert e["mode"] == "edge-only" and e["not_scored"] == [2016] and "results" not in e
        assert e["edge_threshold"]["n_bets"] > 0 and "min_edge" in e["edge_threshold"]
        assert set(e["edge_threshold"]["seasons"]["development"]) <= {"2017", "2018"}
    elif league == "SP1":
        # Bake-off 3: four leagues in one family; the holdout moved to 2018/19, so
        # development ends at 2017/18 and 2018/19 is never scored.
        assert res["level"] == pytest.approx(1 - 0.05 / 40, abs=1e-5)
        assert seen["last"] == 2018 and str(seen["holdout_start"].date()) == "2018-07-01"
        assert "DEVELOPMENT 2016/17-2017/18" in text
        assert set(res["by_season"]) <= {"2016", "2017"}
        et = res["edge_threshold"]
        assert et["n_bets"] > 0 and "min_edge" in et and et["note"]
    else:
        assert res["not_scored"] == [] and res["level"] == pytest.approx(0.995)
    for name, r in res["results"].items():
        assert r["rows"] > (60 if name == "e_stack" else 120) and "c_range" in r["blend"]
    with pytest.raises(SystemExit):
        run.main(["--open-holdout", "--reason", "x", "--finalists", "nope"])


def test_goals_only_features_and_league_families():
    from soccer_stats.lab import run

    assert features.FEATURES_GOALS and not [f for f in features.FEATURES_GOALS if "xg" in f]
    assert set(features.FEATURES_GOALS) < set(features.FEATURES)
    assert run.family("E0") == 1 and run.family("E2") == 3 and run.family("SP1") == 4
    assert run.has_xg("E0") and not run.has_xg("E1")


def test_league_levels_are_read_from_the_committed_file():
    from soccer_stats.lab import thresholds as th

    levels = th.league_levels()
    assert {"E1", "SP1", "D1", "I1", "F1"} <= set(levels) and "E0" not in levels
    for code, lv in levels.items():
        assert {"min_edge", "note", "method", "n_bets", "seasons", "source"} <= set(lv), code
        assert lv["min_edge"] is None or 0 <= lv["min_edge"] <= 0.3
    assert th.league_levels(Path("/no/such/file.json")) == {}
