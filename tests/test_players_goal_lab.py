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


# ---------- round 8: five leagues ----------


def _rows(feats, league):
    sim = feats.copy()
    lo = sim["kickoff"].min() + pd.Timedelta(days=400)
    hi = sim["kickoff"].max() + pd.Timedelta(days=1)
    return gl.scored_rows(sim, league, lo, hi)


def test_scored_rows_and_bench_tests(feats):
    r = _rows(feats, "SP1")
    assert len(r) > 500 and set(r["league"]) == {"SP1"}
    assert r["match_id"].str.startswith("SP1|").all()
    assert r[["p_A", "p_B", "bench_goals", "bench_xg"]].apply(lambda c: c.between(0, 1)).all().all()
    res = gl.bench_tests(r, gl.LEVEL_LEAGUE)
    a, b = res["all_before_lineups"], res["starters_lineup_known"]
    assert a["rows"] == len(r) and b["rows"] == int(r["started"].sum())
    assert set(a["vs"]) == {"season_goals", "season_xg"}
    for v in a["vs"].values():
        lo_, hi_ = v["range"]
        assert lo_ <= v["gain"] <= hi_ and v["beats"] == (lo_ > 0)
    assert a["beats_both"] == all(v["beats"] for v in a["vs"].values())
    assert res["level"] == pytest.approx(0.99375)


def test_history_and_forward_reports(feats):
    r = _rows(feats, "SP1")
    rows = {lg: r.assign(league=lg, match_id=lg + "|" + r["match_id"]) for lg in ("E0", "D1")}
    rep = gl.history_report(rows)
    assert set(rep["leagues"]) == {"E0", "D1"} and set(rep["answer"]) == {"E0", "D1"}
    assert rep["pooled_new_leagues"]["all_before_lineups"]["rows"] == len(r)  # D1 only
    few = rows["D1"][rows["D1"]["match_id"].isin(rows["D1"]["match_id"].unique()[:20])]
    fr = gl.forward_report(few)
    assert fr["matches"] == 20 and not fr["enough"] and fr["gate"]["passes"] is False
    big = gl.forward_report(pd.concat(rows.values(), ignore_index=True))
    assert big["enough"] and set(big["leagues"]) == {"E0", "D1"}
    assert big["gate"]["passes"] == (big["gate"]["beats_both"] and big["gate"]["tail_ok"])


def test_forward_window_stays_locked(feats):
    lo = feats["kickoff"].min() + pd.Timedelta(days=400)
    hi = feats["kickoff"].max() + pd.Timedelta(days=1)
    h = Holdout(lo + pd.Timedelta(days=90))
    with pytest.raises(HoldoutLocked):
        gl.scored_rows(feats, "E0", lo, hi, h)
    assert gl.FORWARD_START == pd.Timestamp("2026-10-10", tz="UTC")
    assert gl.FORWARD_LEAGUES == ("E0", "SP1", "D1", "I1", "F1")


def test_scored_rows_do_not_see_the_future(feats):
    lo = feats["kickoff"].min() + pd.Timedelta(days=400)
    hi = lo + pd.Timedelta(days=60)
    later = feats["kickoff"] >= lo
    ch = feats.copy()
    ch.loc[later, "goals"] = 4
    a = gl.scored_rows(feats, "D1", lo, hi)
    b = gl.scored_rows(ch, "D1", lo, hi)
    first = a["kickoff"] < lo + pd.Timedelta(days=20)  # the first 28-day block
    np.testing.assert_allclose(a.loc[first, "p_B"], b.loc[first, "p_B"])
    np.testing.assert_allclose(a.loc[first, "p_A"], b.loc[first, "p_A"])


def test_goal_pool_cli(feats, tmp_path, capsys):
    from soccer_stats import cli

    r = _rows(feats, "SP1")
    for lg in ("SP1", "I1"):
        d = tmp_path / f"goal-{lg}"
        d.mkdir()
        r.assign(league=lg, match_id=lg + "|" + r["match_id"]).to_csv(
            d / f"rows_{lg}.csv.gz", index=False
        )
    cli.main(["goal-pool", "--rows-dir", str(tmp_path), "--json", str(tmp_path / "p.json")])
    out = capsys.readouterr().out
    assert "Pooled SP1+D1+I1+F1" in out and "Beats both benchmarks" in out
    import json

    rep = json.loads((tmp_path / "p.json").read_text())
    assert set(rep["history"]["leagues"]) == {"SP1", "I1"} and "forward" not in rep


def test_goal_league_locked_never_loads_the_forward_season(monkeypatch):
    """Locked, the round-8 CLI never loads 2026/27 at all (dropped before computation);
    only a reason widens the seasons loaded."""
    from soccer_stats import cli, player_data

    seen = []

    class Stop(Exception):
        pass

    def fake_load(league, years):
        seen.append(list(years))
        raise Stop

    monkeypatch.setattr(player_data, "load_appearances", fake_load)
    monkeypatch.setattr(cli, "current_season", lambda: 2026)
    for lg in gl.FORWARD_LEAGUES:
        with pytest.raises(Stop):
            cli._goal_league_feats(lg, "")
        assert max(seen[-1]) == 2025
    with pytest.raises(Stop):
        cli._goal_league_feats("SP1", "opened for the test")
    assert max(seen[-1]) == 2026


# ---------- §12: B-cal ----------


def _cal_rows(seed=0, n=3000, leagues=("D1", "E0"), seasons=("2324", "2425", "2526"), k=1.4):
    """Synthetic scored rows where B is over-confident: p_B = expit(k * logit(q))."""
    from scipy.special import expit, logit

    rng = np.random.default_rng(seed)
    out = []
    for lg in leagues:
        for s in seasons:
            q = rng.beta(1.2, 6, n)
            y = (rng.random(n) < q).astype(int)
            out.append(
                pd.DataFrame(
                    {
                        "league": lg,
                        "season": s,
                        "match_id": [f"{lg}|{s}|{j // 20}" for j in range(n)],
                        "kickoff": pd.Timestamp(f"20{s[:2]}-09-01", tz="UTC")
                        + pd.to_timedelta(np.arange(n) // 20, "D"),
                        "started": True,
                        "scored": y,
                        "p_A": q,
                        "p_B": expit(k * logit(q)),
                        "bench_goals": np.full(n, y.mean()),
                        "bench_xg": np.full(n, y.mean()),
                    }
                )
            )
    return pd.concat(out, ignore_index=True)


def test_bcal_fits_earlier_seasons_of_its_own_league_only():
    r = _cal_rows()
    out, coefs = gl.add_bcal(r, r)
    assert out.loc[out["season"] == "2324", "p_Bcal"].isna().all()  # no earlier season
    assert coefs["D1 2324"] is None and coefs["D1 2526"]["fit_on"] == ["2324", "2425"]
    assert 0.55 < coefs["D1 2526"]["b"] < 0.9  # shrinks the over-confident B (true 1/1.4)
    # Changing a season's (or another league's) outcomes never moves that season's B-cal.
    ch = r.copy()
    later = (ch["season"] == "2526") | (ch["league"] == "E0")
    ch.loc[later, "scored"] = 1 - ch.loc[later, "scored"]
    out2, _ = gl.add_bcal(ch, ch)
    keep = (out["league"] == "D1") & (out["season"] != "2324")
    np.testing.assert_allclose(out.loc[keep, "p_Bcal"], out2.loc[keep, "p_Bcal"])


def test_bcal_floor_and_development_view():
    r = _cal_rows(n=500)  # 500 earlier starters < CAL_MIN_ROWS: B unchanged
    out, coefs = gl.add_bcal(r, r)
    nxt = out["season"] == "2425"
    np.testing.assert_allclose(out.loc[nxt, "p_Bcal"], out.loc[nxt, "p_B"].clip(1e-6, 1 - 1e-6))
    assert coefs["E0 2425"]["floor"] is True
    dev = gl.bcal_development(_cal_rows())
    pooled = dev["pooled"]
    assert pooled["rows"] == 2 * 2 * 3000  # 2024/25 and 2025/26, two leagues
    assert pooled["vs_B"]["gain"] > 0  # recalibration helps an over-confident B
    assert pooled["model"]["log_loss"] < pooled["b"]["model"]["log_loss"]
    assert set(dev["leagues"]) == {"D1", "E0"}


def test_bcal_forward_uses_locked_history_only(tmp_path, capsys):
    from soccer_stats import cli

    hist = _cal_rows()
    fwd = _cal_rows(seed=1, seasons=("2627",))
    fwd["kickoff"] = fwd["kickoff"] + pd.Timedelta(days=40)  # after 10 Oct
    out, coefs = gl.add_bcal(fwd, hist)
    assert coefs["D1 2627"]["fit_on"] == ["2324", "2425", "2526"]
    flipped = fwd.assign(scored=1 - fwd["scored"])  # forward outcomes never reach the fit
    out2, _ = gl.add_bcal(flipped, hist)
    np.testing.assert_allclose(out["p_Bcal"], out2["p_Bcal"])
    rep = gl.forward_report(out)
    assert rep["gate"]["passes"] == (rep["gate"]["beats_both"] and rep["gate"]["tail_ok"])
    g = rep["b_cal"]["gate"]
    assert g["passes"] == (rep["enough"] and g["beats_both"] and g["tail_ok"])
    assert "vs_B" in rep["b_cal"]["pooled"] and set(rep["b_cal"]["leagues"]) == {"D1", "E0"}
    for lg in ("D1", "E0"):
        hist[hist["league"] == lg].to_csv(tmp_path / f"rows_{lg}.csv.gz", index=False)
        fwd[fwd["league"] == lg].to_csv(tmp_path / f"forward_{lg}.csv.gz", index=False)
    cli.main(["goal-pool", "--rows-dir", str(tmp_path), "--json", str(tmp_path / "p.json")])
    printed = capsys.readouterr().out
    assert "Forward check, B-cal:" in printed and "B-cal Pooled:" in printed
    import json

    rep = json.loads((tmp_path / "p.json").read_text())
    assert rep["b_cal_forward_coefficients"]["E0 2627"]["fit_on"] == ["2324", "2425", "2526"]
    assert "b_cal" in rep["forward"] and "b_cal_development" in rep
