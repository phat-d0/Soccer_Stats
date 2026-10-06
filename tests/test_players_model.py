import numpy as np
import pandas as pd
import pytest
from player_sim import simulate_players

from soccer_stats import player_backtest as pb
from soccer_stats import trades as tr
from soccer_stats.factors import FACTOR_GROUPS, build_features
from soccer_stats.models.player_counts import PlayerShotModel, nb_pmf, prob_over
from soccer_stats.player_data import match_names, norm, parse_match


@pytest.fixture(scope="module")
def sim():
    apps, truth = simulate_players()
    return build_features(apps), truth, apps


def test_features_use_only_earlier_matches(sim):
    feats, _, apps = sim
    cut = apps["kickoff"].sort_values().iloc[len(apps) // 2]
    apps2 = apps.copy()
    later = apps2["kickoff"] >= cut
    apps2.loc[later, "shots"] += 5
    apps2.loc[later, "team_shots"] += 50
    f2 = build_features(apps2)
    cols = [
        "rate",
        "sot_rate",
        "start_rate",
        "log_team_shots",
        "log_opp_conceded",
        "log_opp_pos",
        "pen_share",
        "absent_share",
        "start_minutes",
    ]
    early = feats["kickoff"] < cut
    pd.testing.assert_frame_equal(
        feats.loc[early, cols].reset_index(drop=True), f2.loc[early, cols].reset_index(drop=True)
    )


def test_rate_tracks_true_rate(sim):
    feats, truth, _ = sim
    last = feats.sort_values("kickoff").groupby("player_id").tail(1)
    m = last.merge(truth[["player_id", "rate"]], on="player_id", suffixes=("", "_true"))
    m = m[m["prev_apps"] > 20]
    assert np.corrcoef(np.log(m["rate"]), np.log(m["rate_true"]))[0, 1] > 0.8


def test_distributions_sum_to_one(sim):
    feats, _, _ = sim
    model = PlayerShotModel().fit(feats)
    d = model.distributions(feats.head(200))
    for k in ("shots", "sot"):
        assert np.allclose(d[k].sum(axis=1), 1)
        assert (d[k] >= 0).all()
    assert (prob_over(d["shots"], 0.5) >= prob_over(d["shots"], 1.5)).all()
    p = nb_pmf(np.array([1.3]), 0.4)
    assert (p * np.arange(p.shape[1])).sum() == pytest.approx(1.3, abs=1e-3)


@pytest.fixture(scope="module")
def backtest(sim):
    feats, _, _ = sim
    start = feats["kickoff"].min() + pd.Timedelta(days=200)
    preds = pb.walk_forward(feats, start, refit_every="28D")
    return feats, start, preds


def test_model_beats_season_average_baseline(backtest):
    _, _, preds = backtest
    s = pb.score(preds)
    assert s["appearances"] > 500
    for c in ("shots", "sot"):
        assert s[c]["beats_baseline"], s[c]
        assert len(s[c]["lines"]) == 3


def test_team_totals_reconcile(backtest):
    feats, start, _ = backtest
    known = pb.walk_forward(feats, start, lineup_known=True, refit_every="28D")
    r = pb.reconcile_team_totals(known)
    assert r["team_matches"] > 50
    assert r["within_tolerance"], r


def test_ablation_reports_every_group(backtest):
    feats, start, preds = backtest
    ab = pb.ablation(feats, start, pb.score(preds), refit_every="56D")
    assert [g["without"] for g in ab["groups"]] == list(FACTOR_GROUPS)
    assert "player" in ab["kept_groups"]  # his own shot rate must matter


def test_player_trade_rule():
    lines = pd.DataFrame(
        [
            # same player and market: two lines, keep the best edge only
            {
                "home": "A",
                "away": "B",
                "player": "P1",
                "market": "player_shots",
                "line": 0.5,
                "side": "over",
                "odds": 1.5,
                "p": 0.8,
            },
            {
                "home": "A",
                "away": "B",
                "player": "P1",
                "market": "player_shots",
                "line": 1.5,
                "side": "over",
                "odds": 2.6,
                "p": 0.5,
            },
            {
                "home": "A",
                "away": "B",
                "player": "P1",
                "market": "player_shots_on_target",
                "line": 0.5,
                "side": "over",
                "odds": 2.2,
                "p": 0.55,
            },
        ]
        + [
            {
                "home": "A",
                "away": "B",
                "player": f"Q{i}",
                "market": "player_shots",
                "line": 0.5,
                "side": "under",
                "odds": 2.0,
                "p": 0.6 + i / 100,
            }
            for i in range(5)
        ]
        + [
            {
                "home": "A",
                "away": "B",
                "player": "R",
                "market": "player_shots",
                "line": 0.5,
                "side": "over",
                "odds": 1.9,
                "p": 0.55,
            }
        ]
    )
    picks = tr.player_picks(lines)
    assert len(picks) == tr.MAX_PLAYER_TRADES  # capped per match
    assert not picks.duplicated(["player", "market"]).any()
    assert picks["edge"].is_monotonic_decreasing
    assert "R" not in set(picks["player"])  # 0.55 x 1.9 - 1 = 4.5%: below 12%
    p1 = tr.player_picks(lines[lines["player"] == "P1"])
    assert set(zip(p1["market"], p1["line"], strict=True)) == {
        ("player_shots", 1.5),
        ("player_shots_on_target", 0.5),
    }


def test_settle_player():
    t = {"line": 1.5, "side": "over", "odds": 2.5, "stake": 10.0}
    assert tr.settle_player(t, 2, True)["profit"] == 15.0
    assert tr.settle_player(t, 1, False) == {
        "status": "lost",
        "actual": 1,
        "started": False,
        "profit": -10.0,
    }
    assert tr.settle_player({**t, "side": "under"}, 1, True)["status"] == "won"
    assert tr.settle_player(t, None, None)["status"] == "void"


def test_name_matching_never_guesses():
    cands = {
        "1": "Martin Ødegaard",
        "2": "Gabriel Jesus",
        "3": "Gabriel Magalhães",
        "4": "Bukayo Saka",
        "5": "Jurriën Timber",
        "6": "Kai Havertz",
    }
    names = [
        "Martin Odegaard",
        "Bukayo Saka",
        "Saka",
        "Gabriel",
        "J. Timber",
        "Havertz",
        "Unknown Guy",
    ]
    got, missing = match_names(names, "Arsenal", cands)
    assert got == {
        "Martin Odegaard": "1",
        "Bukayo Saka": "4",
        "Saka": "4",
        "J. Timber": "5",
        "Havertz": "6",
    }
    assert set(missing) == {"Gabriel", "Unknown Guy"}  # two Gabriels: skipped, not guessed
    got, _ = match_names(
        ["Gabriel"], "Arsenal", cands, {("Arsenal", "gabriel"): "Gabriel Magalhães"}
    )
    assert got == {"Gabriel": "3"}
    assert norm("N'Golo Kanté") == "n golo kante"


def test_parse_match():
    data = {
        "shots": {
            "h": [
                {"player_id": "10", "result": "Goal", "situation": "Penalty"},
                {"player_id": "10", "result": "SavedShot", "situation": "OpenPlay"},
                {"player_id": "11", "result": "BlockedShot", "situation": "OpenPlay"},
                {"player_id": "20", "result": "OwnGoal", "situation": "OpenPlay"},
            ],
            "a": [{"player_id": "20", "result": "MissedShots", "situation": "OpenPlay"}],
        },
        "rosters": {
            "h": {
                "1": {
                    "player_id": "10",
                    "player": "Bukayo Saka",
                    "position": "AMR",
                    "time": "90",
                    "roster_in": "0",
                },
                "2": {
                    "player_id": "11",
                    "player": "Kai Havertz",
                    "position": "Sub",
                    "time": "25",
                    "roster_in": "5",
                },
                "3": {
                    "player_id": "12",
                    "player": "Unused",
                    "position": "Sub",
                    "time": "0",
                    "roster_in": "0",
                },
            },
            "a": {
                "4": {
                    "player_id": "20",
                    "player": "Joe Rodon",
                    "position": "DC",
                    "time": "90",
                    "roster_in": "0",
                }
            },
        },
    }
    meta = {
        "match_id": "99",
        "season": "2526",
        "kickoff": pd.Timestamp("2025-09-13 14:00", tz="UTC"),
        "home": "Arsenal",
        "away": "Leeds",
    }
    rows = {r["player"]: r for r in parse_match(data, meta)}
    assert set(rows) == {"Bukayo Saka", "Kai Havertz", "Joe Rodon"}
    assert (
        rows["Bukayo Saka"]["shots"],
        rows["Bukayo Saka"]["sot"],
        rows["Bukayo Saka"]["penalties"],
    ) == (2, 2, 1)
    assert rows["Kai Havertz"]["started"] is False and rows["Kai Havertz"]["shots"] == 1
    assert rows["Joe Rodon"]["shots"] == 1  # his own goal isn't his shot
    assert rows["Bukayo Saka"]["team_shots"] == 3 and rows["Joe Rodon"]["opp_shots"] == 3


def test_player_report_by_name(backtest):
    _, _, preds = backtest
    rep = pb.player_report(preds)
    rows = {r["player_id"]: r for r in rep["players"]}
    assert len(rows) == preds["player_id"].nunique()
    pid, r = next(iter(rows.items()))
    sub = preds[preds["player_id"] == pid]
    assert r["apps"] == len(sub) and r["shots"] == sub["shots"].sum()
    assert r["exp_shots"] == pytest.approx(sub["mean_shots"].sum(), abs=1e-2)
    assert len(rep["apps"][pid]) == len(sub)
    assert len(rep["apps"][pid][0]) == len(rep["fields"])
    total_exp = sum(r["exp_shots"] for r in rep["players"])
    assert total_exp == pytest.approx(preds["mean_shots"].sum(), rel=1e-3)
