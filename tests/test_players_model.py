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
                {"player_id": "10", "result": "Goal", "situation": "Penalty", "xG": "0.76"},
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
    assert rows["Bukayo Saka"]["goals"] == 1 and rows["Bukayo Saka"]["xg"] == pytest.approx(0.76)
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


def test_season_stats_per_player_and_club(sim):
    from soccer_stats.player_data import season_stats

    _, _, apps = sim
    apps = apps.assign(goals=0, xg=0.1)
    moved = apps["player_id"] == apps["player_id"].iloc[0]
    apps.loc[moved & (apps["season"] == "2425"), "team"] = "Elsewhere"  # a transfer
    rows = season_stats(apps)
    assert sum(r["shots"] for r in rows) == apps["shots"].sum()
    mine = [r for r in rows if r["player_id"] == apps["player_id"].iloc[0]]
    assert {r["team"] for r in mine} >= {"Elsewhere"}  # one row per club
    r = next(x for x in rows if x["shots"] > 0)
    sub = apps[
        (apps["player_id"] == r["player_id"])
        & (apps["season"] == r["season"])
        & (apps["team"] == r["team"])
    ]
    assert (r["apps"], r["minutes"], r["sot"]) == (len(sub), sub["minutes"].sum(), sub["sot"].sum())


def test_active_players_from_fpl():
    from soccer_stats.player_data import active_players

    apps = pd.DataFrame(
        {
            "season": ["2526", "2627", "2526", "2526", "2627"],
            "team": ["Arsenal", "Arsenal", "Arsenal", "Chelsea", "Arsenal"],
            "player_id": ["1", "1", "2", "3", "4"],
            "player": [
                "Bukayo Saka",
                "Bukayo Saka",
                "Thomas Partey",
                "Cole Palmer",
                "Unlisted Kid",
            ],
        }
    )
    fpl = pd.DataFrame(
        {
            "name": ["Saka", "Partey", "Palmer"],
            "full_name": ["Bukayo Saka", "Thomas Partey", "Cole Palmer"],
            "team": ["Arsenal", "Arsenal", "Man United"],  # Palmer moved within the league
            "status": ["a", "u", "i"],
        }
    )
    act = active_players(apps, fpl, "2627")
    assert act["1"]["active"] and act["1"]["team"] == "Arsenal"
    assert not act["2"]["active"]  # FPL: left / unavailable for the season
    assert act["3"]["active"] and act["3"]["team"] == "Man United"  # injured still counts
    assert act["4"]["active"] and act["4"]["team"] is None  # unmatched but playing this season
    assert not active_players(apps, None, "2627")["3"]["active"]  # no FPL, not seen this season


def test_whole_number_lines_mean_at_least():
    pmf = np.array([[0.5, 0.3, 0.2]])  # P(0), P(1), P(2)
    assert prob_over(pmf, 0.5)[0] == pytest.approx(0.5)
    assert prob_over(pmf, 1.0)[0] == pytest.approx(0.5)  # FanDuel "1+ shots"
    assert prob_over(pmf, 2.0)[0] == pytest.approx(0.2)
    t = {"line": 1.0, "side": "over", "odds": 1.8, "stake": 10.0}
    assert tr.settle_player(t, 1, True)["status"] == "won"
    assert tr.settle_player(t, 0, True)["status"] == "lost"


def test_calibration_shrinks_toward_price():
    from soccer_stats import player_calibration as cal

    rng = np.random.default_rng(1)
    n = 20000
    truth = rng.uniform(0.03, 0.6, n)
    implied = np.clip(truth * 1.1, 0.01, 0.95)  # the price: right, plus a margin
    model = np.clip(truth * np.exp(rng.normal(0.3, 0.5, n)), 0.01, 0.95)  # noisy, too high
    won = rng.uniform(size=n) < truth
    coef = cal.fit(implied, model, won)
    a, b, c = coef
    assert b > 0.7 and abs(c) < 0.15  # the price carries the weight
    p = cal.apply(coef, implied, model)
    assert abs(p.mean() - won.mean()) < 0.01  # margin taken out
    assert cal.fit(implied[:100], model[:100], won[:100]) is None  # too few lines
    lines = pd.DataFrame(
        {
            "kickoff": pd.date_range("2024-08-01", periods=n, freq="15min", tz="UTC"),
            "implied": implied,
            "p_model": model,
            "won": won.astype(int),
        }
    )
    out, fits = cal.walk_forward(lines)
    assert out.iloc[: cal.MIN_LINES].isna().all() and out.iloc[-100:].notna().all()
    assert all(f["lines"] >= cal.MIN_LINES for f in fits)
