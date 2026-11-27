"""B-espn, frozen by the §16 amendment of 26 Nov 2026: identical chances from identical
rows in any order, its settings pinned, and the amendment's check rule."""

import numpy as np
import pandas as pd

from soccer_stats import espn_players as ep
from soccer_stats import player_assist_lab as al
from soccer_stats.lab import harness
from soccer_stats.models.player_goals import FIT_OPTIONS


def test_b_espn_settings_are_frozen():
    assert ep.B_ESPN_FACTORS == [
        "log_rate",
        "sot_rate",
        "log_goal_rate",
        "pos_def",
        "pos_fwd",
        "log_team_xg",
        "log_opp_conceded",
        "home",
        "game_state",
    ]
    assert (ep.DEFAULT_START_OFF, ep.DEFAULT_SUB_ON) == (70, 20)
    assert (al.REFIT, al.LOOKBACK_DAYS, al.MIN_PREV_APPS) == ("28D", 730, 3)
    assert FIT_OPTIONS == {"ftol": 0.0, "gtol": 1e-8, "maxiter": 20000}
    assert (ep.MODEL_SOURCE, ep.FEATURE_SOURCE) == ("B-espn", "espn")


def feats(n_players=120, n_matches=160, seed=0):
    rng = np.random.default_rng(seed)
    d0 = pd.Timestamp("2023-08-01", tz="UTC")
    rows = []
    for m in range(n_matches):
        for k in rng.choice(n_players, 22, replace=False):
            x = {f: rng.normal() for f in ep.B_ESPN_FACTORS}
            started = bool(rng.random() < 0.8)
            minutes = int(rng.integers(60, 91)) if started else int(rng.integers(5, 30))
            rate = np.exp(-2.2 + 0.5 * x["log_goal_rate"] + 0.3 * x["log_team_xg"])
            # Overdispersed, so α is interior (on Poisson-like counts α runs to 0 and
            # L-BFGS-B stops at a different point per row order, B's fit too).
            mean = rng.gamma(0.2, 5.0) * rate * minutes / 90
            rows.append({**x, "match_id": f"m{m}", "player_id": f"p{k}",
                         "kickoff": d0 + pd.Timedelta(days=4 * m), "started": started,
                         "minutes": minutes, "goals": int(rng.poisson(mean)),
                         "prev_apps": 5, "start_minutes": 85, "sub_minutes": 20})  # fmt: skip
    return pd.DataFrame(rows)


def chances(df):
    pr = harness.walk_forward(
        df,
        lambda **kw: al.CountCandidate(**kw),
        {"target": "goals", "factors": ep.B_ESPN_FACTORS},
        pd.Timestamp("2024-06-01", tz="UTC"),
        pd.Timestamp("2025-01-01", tz="UTC"),
        time_col="kickoff",
        refit=al.REFIT,
        min_train=1,
    )
    key = df.loc[pr.index, ["match_id", "player_id"]].astype(str).agg("|".join, axis=1)
    return pd.Series(pr["p_1"].to_numpy(), index=key.to_numpy()).sort_index()


def test_b_espn_chances_agree_across_row_orders():
    df = feats()
    a = chances(df)
    b = chances(df.sample(frac=1, random_state=3).reset_index(drop=True))
    assert len(a) > 500 and a.notna().all()
    assert (a.round(6) == b.loc[a.index].round(6)).all()


def test_check_rows_join_by_name_and_verdict_rule():
    judged = pd.DataFrame(
        {
            "match_id": ["SP1|7", "SP1|7", "SP1|7"],
            "season": ["2526"] * 3,
            "started": [True] * 3,
            "player_id": ["11", "12", "13"],
            "player": ["Lamine Yamal", "Robert Lewandowski", "Pedri"],
            "team": ["Barcelona"] * 3,
            "scored": [1, 0, 0],
            "p_B": [0.3, 0.4, 0.1],
        }
    )
    fe = pd.DataFrame(
        {
            "match_id": ["7", "7", "7"],
            "player_id": ["e1", "e2", "e3"],
            "player": ["Lamine Yamal", "Robert Lewandowski", "Someone Else"],
            "team": ["Barcelona"] * 3,
            "started": [True] * 3,
            "p_B_espn": [0.31, 0.39, 0.2],
        }
    )
    rows, counts = ep.check_rows(fe, judged, "SP1")
    assert counts["rescued_rows"] == 3 and counts["joined_rows"] == 2
    assert set(rows["player_id"]) == {"11", "12"}
    assert ep.check_verdict(rows, {"join_share": 0.4})["verdict"] == "not checked"
    big = pd.concat([rows.assign(match_id=f"x{i}") for i in range(50)], ignore_index=True)
    worse = big.assign(p_B_espn=np.where(big["scored"] == 1, 0.05, 0.9))
    assert ep.check_verdict(worse, {"join_share": 1.0})["verdict"] == "fail"
    assert ep.check_verdict(big, {"join_share": 1.0})["verdict"] == "pass"


def test_match_xg_weight_keeps_xg_when_one_season_lacks_it():
    m = pd.DataFrame({"home_xg": [1.2, np.nan, 0.8], "away_xg": [0.9, np.nan, 1.1]})
    assert ep.match_xg_weight(m) == 0.7
    assert ep.match_xg_weight(m.assign(home_xg=np.nan)) == 0.0
    assert ep.match_xg_weight(m.drop(columns=["home_xg", "away_xg"])) == 0.0
