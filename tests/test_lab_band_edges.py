"""The price-band edge rule test (lab/band_edges.py; docs/lab.md, owner 20 Oct)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import band_edges as be
from soccer_stats.lab import edge12_filters as ef


def test_bands_by_price_with_the_draw_on_its_own():
    assert be.band("home", 1.99) == "fav"
    assert be.band("away", 2.00) == "mid"
    assert be.band("home", 3.50) == "mid"
    assert be.band("away", 3.51) == "dog"
    assert be.band("home", 6.00) == "dog"
    assert be.band("away", 6.01) == "long"
    assert be.band("draw", 1.50) == "draw" and be.band("draw", 9.0) == "draw"


def test_seasons_split_development_check_and_left_out():
    assert all(be.is_dev(s) for s in ("1920", "2021", "2122", "2223"))
    assert not be.is_dev("2324") and not be.is_dev("2425") and not be.is_dev("2526")
    assert be.is_check("2324") and be.is_check("2425")
    assert not be.is_check("2223") and not be.is_check("2526")


def test_pick_uses_each_bands_own_threshold_and_skips_bands_that_are_off():
    probs = {"home": 0.56, "draw": 0.20, "away": 0.24}
    odds = {"home": 1.90, "draw": 3.60, "away": 4.80}  # fav +6.4%, draw -28%, dog +15.2%
    # Underdogs need 20%: the favourite's 6.4% clears its 5% and is the pick.
    got = be.pick(probs, odds, {"fav": 0.05, "dog": 0.20, "draw": 0.12})
    assert got["market"] == "home" and got["band"] == "fav"
    assert got["edge"] == pytest.approx(0.064)
    # With the underdog bar at 12% the higher edge (away) wins.
    assert be.pick(probs, odds, {"fav": 0.05, "dog": 0.12})["market"] == "away"
    # A band that is off (missing or None) never bets; nothing else clears: no pick.
    assert be.pick(probs, odds, {"fav": None, "dog": None}) is None
    assert be.pick(probs, odds, {"fav": 0.08, "dog": 0.20}) is None
    # The threshold itself qualifies (EPS); ties keep the earlier side.
    assert be.pick({"home": 0.56}, {"home": 2.0}, {"mid": 0.12})["edge"] == pytest.approx(0.12)
    tie = be.pick({"home": 0.4, "away": 0.4}, {"home": 3.0, "away": 3.0}, {"mid": 0.1})
    assert tie["market"] == "home"


def test_grouped_range_resamples_matches_not_sides():
    rng = np.random.default_rng(3)
    vals = rng.normal(0, 1, 400)
    # Pairs of sides from the same match that move together widen the range.
    paired = np.repeat(vals[:200], 2)
    groups = np.repeat(np.arange(200), 2)
    lo, hi = be.grouped_range(paired, groups)
    lo1, hi1 = be.grouped_range(paired, np.arange(400))
    assert hi - lo > (hi1 - lo1) * 1.2
    assert be.grouped_range(np.array([1.0]), np.array([0])) is None


def _row(i, season, ph, pd_, pa, oh, od, oa, hg, ag):
    return {
        "date": pd.Timestamp(f"20{season[:2]}-09-01") + pd.Timedelta(days=i),
        "season": season,
        "home": f"H{i}",
        "away": f"A{i}",
        "home_goals": hg,
        "away_goals": ag,
        "p_home": ph,
        "p_draw": pd_,
        "p_away": pa,
        "odds_home": oh,
        "odds_draw": od,
        "odds_away": oa,
        # Pinnacle's close: the same prices, so CLV = margin only (negative).
        "close_home": oh,
        "close_draw": od,
        "close_away": oa,
    }


def test_sides_keep_every_side_with_an_edge_and_score_it():
    # Prices with a 5% margin; the close is the same price, so CLV is below 0.
    t = pd.DataFrame([_row(0, "2021", 0.60, 0.25, 0.15, 1.80, 3.40, 5.00, 2, 0)])
    s = be.sides(t, "E0")
    # home +8%, draw -15%, away -25%: only home has an edge.
    assert list(s["market"]) == ["home"]
    r = s.iloc[0]
    assert r["band"] == "fav" and r["won"] and r["profit"] == pytest.approx(0.80)
    assert r["edge"] == pytest.approx(0.08) and r["implied"] == pytest.approx(1 / 1.80)
    assert r["match"] == "E0|2021|H0|A0" and r["clv"] < 0


def test_select_picks_best_clv_with_enough_sides_and_switches_losers_off():
    table = {
        "fav": [
            {"t": 0.02, "sides": 500, "clv": -0.01},
            {"t": 0.05, "sides": 300, "clv": 0.004},
            {"t": 0.08, "sides": 150, "clv": 0.02},  # best, but too few sides
        ],
        "dog": [
            {"t": 0.12, "sides": 400, "clv": -0.05},
            {"t": 0.20, "sides": 250, "clv": -0.03},
        ],
        "mid": [{"t": 0.05, "sides": 300, "clv": 0.01}, {"t": 0.08, "sides": 300, "clv": 0.01}],
    }
    got = be.select(table, min_sides=200)
    assert got["fav"]["t"] == 0.05 and got["fav"]["on"]
    assert got["dog"]["t"] == 0.20 and not got["dog"]["on"]  # R* keeps 20%, R switches off
    assert got["mid"]["t"] == 0.08  # ties: the higher threshold
    assert got["long"]["t"] is None and not got["long"]["on"]  # no rows at all


def _season_rows(season, n, start=0):
    """A mix: favourites the model slightly likes, underdogs it likes a lot."""
    rng = np.random.default_rng(int(season))
    rows = []
    for i in range(n):
        hg, ag = (rng.integers(0, 3), rng.integers(0, 3))
        if i % 2:
            rows.append(_row(start + i, season, 0.58, 0.24, 0.18, 1.80, 3.60, 5.00, hg, ag))
        else:
            rows.append(_row(start + i, season, 0.40, 0.25, 0.35, 2.10, 3.40, 3.80, hg, ag))
    return rows


def test_run_freezes_the_rule_on_development_before_scoring_the_check():
    dev = _season_rows("2122", 400) + _season_rows("2223", 400, 400)
    chk = _season_rows("2324", 200, 900)
    late = _season_rows("2526", 50, 1200)  # never scored
    target = pd.DataFrame(dev + chk + late)
    out = be.run({"E0": target}, now=pd.Timestamp("2026-10-20", tz="UTC"))
    assert out["development"]["matches"] == 800
    assert out["check"]["matches"] == 200
    # Closing prices equal the early ones, so every CLV is the margin: every band off.
    assert all(v is None for v in out["rule"].values())
    assert out["decision"]["any_band_on"] is False and not out["decision"]["passes"]
    assert out["check"]["pooled"]["R"]["bets"] == 0
    # R* still bets the bands with a development threshold, on the check seasons only.
    star = out["check"]["pooled"]["R_star"]
    assert star["bets"] > 0
    # The references are the 19 Oct rules on the same check matches.
    base = ef.bets(pd.DataFrame(chk), "E0", "baseline")
    assert out["check"]["pooled"]["baseline"]["bets"] == len(base)
    # Changing only check-season outcomes never moves the development choice.
    flipped = pd.DataFrame(dev + [{**r, "home_goals": 0, "away_goals": 5} for r in chk])
    out2 = be.run({"E0": flipped}, now=pd.Timestamp("2026-10-20", tz="UTC"))
    assert out2["development"]["chosen"] == out["development"]["chosen"]
    assert out2["rule_star"] == out["rule_star"]


def test_a_band_with_positive_development_clv_is_switched_on_and_judged():
    # Closing prices shorter than the early ones on the favourite: positive CLV there.
    def row(i, season):
        r = _row(i, season, 0.58, 0.24, 0.18, 1.80, 3.60, 5.00, 1, 0)
        r.update({"close_home": 1.60, "close_draw": 4.20, "close_away": 6.50})
        return r

    dev = [row(i, "2122") for i in range(300)]
    chk = [row(400 + i, "2324") for i in range(320)]
    out = be.run({"E0": pd.DataFrame(dev + chk)}, now=pd.Timestamp("2026-10-20", tz="UTC"))
    assert out["rule"]["fav"] is not None
    dec = out["decision"]
    assert dec["any_band_on"] and dec["bets"] == 320
    assert dec["clv_above_zero"] and dec["enough_bets"] and dec["passes"]


def test_decide_needs_both_the_range_and_the_bets():
    m = {"bets": 250, "clv": 0.01, "clv_ci95": [0.002, 0.02]}
    d = be.decide(m, True)
    assert not d["passes"] and "only 250 bets" in d["verdict"]
    d = be.decide({**m, "bets": 400, "clv_ci95": [-0.01, 0.02]}, True)
    assert not d["passes"] and "doesn't lie above 0" in d["verdict"]
    assert be.decide({**m, "bets": 400}, True)["passes"]


def test_cli_registers_the_command(monkeypatch):
    from soccer_stats import cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_backtest_band_edges", lambda a: seen.update(vars(a)))
    cli.main(["backtest-band-edges", "--leagues", "E0", "--json", "x"])
    assert seen["leagues"] == ["E0"] and seen["from_season"] == 2019 and seen["burn_in"] == 2


def test_command_runs_end_to_end_on_stubbed_data(monkeypatch, tmp_path, capsys):
    """The whole command (load, rule, check, print, JSON) with the downloads stubbed."""
    import json

    from soccer_stats import cli
    from soccer_stats import data as data_mod

    target = pd.DataFrame(
        _season_rows("2122", 300) + _season_rows("2223", 300, 300) + _season_rows("2324", 200, 700)
    )
    monkeypatch.setattr(cli, "_prefetch_seasons", lambda *a, **k: None)
    monkeypatch.setattr(cli, "load_matches", lambda *a, **k: pd.DataFrame({"home_xg": [1.0]}))
    monkeypatch.setattr(cli, "with_xg", lambda m: (m, None))
    monkeypatch.setattr(cli.backtest, "walk_forward", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(data_mod, "pinnacle_prices", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(
        be,
        "league_target",
        lambda preds, pins: (
            {"seasons_scored": ["2021/22"], "seasons_left_out": [], "candidates": len(target)},
            target,
        ),
    )
    path = tmp_path / "band.json"
    cli.main(["backtest-band-edges", "--leagues", "E0", "SP1", "--json", str(path)])
    out = capsys.readouterr().out
    assert "=== Decision (pre-registered) ===" in out and "BAND_EDGES_JSON " in out
    saved = json.loads(path.read_text())
    assert saved["kind"] == "band_edges" and set(saved["leagues"]) == {"E0", "SP1"}
    assert saved["check"]["matches"] == 400  # 200 per stubbed league
