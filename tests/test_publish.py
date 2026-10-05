import json

import numpy as np
import pandas as pd

from soccer_stats.publish import _clean, build_data


def test_clean_makes_json_safe():
    out = _clean({"a": np.float64("nan"), "b": np.int64(3), "c": [np.float32(0.123456)]})
    assert out == {"a": None, "b": 3, "c": [0.1235]}
    json.dumps(out, allow_nan=False)


def test_build_data_shape(league):
    df, _ = league
    # Shift the synthetic league so its last season is the "current" one.
    df = df.copy()
    shift = pd.Timestamp.now().normalize() - pd.Timedelta(days=30) - df["date"].max()
    df["date"] = df["date"] + shift
    df["league"], df["season"] = "E0", "x"
    for col in ["odds_home", "odds_draw", "odds_away", "close_home", "close_draw", "close_away"]:
        df[col] = 3.0
    fixtures = pd.DataFrame(
        {
            "kickoff": [pd.Timestamp.now().normalize() + pd.Timedelta(days=3)],
            "home": ["T00"],
            "away": ["T01"],
            "odds_home": [2.0],
            "odds_draw": [3.4],
            "odds_away": [4.0],
            "odds_over25": [1.9],
            "odds_under25": [1.9],
        }
    )
    data = build_data(df, fixtures, xg_error=None)
    json.dumps(data, allow_nan=False)  # must be strict JSON for the browser

    assert data["xg_weight"] == 0.7
    assert set(data["params"]) == {"intercept", "home_adv", "rho", "attack", "defence"}
    fx = data["fixtures"][0]
    assert abs(fx["p"]["home"] + fx["p"]["draw"] + fx["p"]["away"] - 1) < 1e-3
    assert len(fx["matrix"]) == 6 and len(fx["matrix"][0]) == 6
    assert data["ratings"][0]["goal_diff"] >= data["ratings"][-1]["goal_diff"]
    assert {"model", "goals_only"} <= set(data["record"])
