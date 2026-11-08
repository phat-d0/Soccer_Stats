import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import tuning_ceiling as tc


def _scored(n=200, seed=0):
    rng = np.random.default_rng(seed)
    q = rng.dirichlet([4, 2.5, 3], size=n)
    rows = []
    for i in range(n):
        hg, ag = int(rng.poisson(1.4)), int(rng.poisson(1.1))
        r = {
            "date": pd.Timestamp("2020-08-01") + pd.Timedelta(days=i),
            "season": "2021",
            "home": f"H{i}",
            "away": f"A{i}",
            "home_goals": hg,
            "away_goals": ag,
            "sigma": 0.04,
        }
        for k, m in enumerate(tc.H2H):
            r[f"odds_{m}"] = 0.97 / q[i, k]
            r[f"close_{m}"] = 0.975 / q[i, k]
            r[f"pb_{m}"] = q[i, k] + (0.06 if m == "home" and i % 3 == 0 else -0.03 * (m == "home"))
        rows.append(r)
    return pd.DataFrame(rows)


def test_lean_bets_follow_the_z_cut():
    s = _scored()
    b1 = tc.lean_bets(s, "E0")
    b3 = tc.lean_bets(s, "E0", min_z=3.0)
    assert len(b1) > 0 and len(b3) < len(b1)
    assert (b1["z"] >= 1 - 1e-9).all()
    assert set(b1["market"]) <= set(tc.H2H)
    row = b1.iloc[0]
    assert row["profit"] == pytest.approx(row["odds"] - 1 if row["won"] else -1)


def test_close_chance_bets_have_positive_clv():
    b = tc.lean_bets(tc.with_close_chance(_scored()), "E0")
    assert len(b) == 0 or (b["clv"] > 0).all()


def test_xg_oracle_is_a_distribution():
    o = tc.xg_oracle([1.5, np.nan, 0.2], [0.8, 1.0, 2.5])
    assert np.allclose(o[[0, 2]].sum(axis=1), 1)
    assert np.isnan(o[1]).all()
    assert o[0, 0] > o[0, 2] and o[2, 2] > o[2, 0]


def test_entry_rows_take_the_best_fresh_book():
    base = tc.lean_bets(_scored(30), "E0")
    assert len(base)
    r = base.iloc[0]
    ko = pd.Timestamp(r["date"]).tz_localize("UTC") + pd.Timedelta(hours=15)
    look = ko - pd.Timedelta(hours=48)
    fresh = (look - pd.Timedelta(hours=1)).isoformat()
    books = {
        "draftkings": {"home": 2.0, "draw": 3.3, "away": 3.6, "last_update": fresh},
        "fanduel": {"home": 2.2, "draw": 3.2, "away": 3.5, "last_update": fresh},
        "betmgm": {
            "home": 2.5,
            "draw": 3.0,
            "away": 3.0,
            "last_update": (look - pd.Timedelta(hours=9)).isoformat(),
        },  # fmt: skip
    }
    hist = pd.DataFrame([{"at": look, "snapshot_ts": look - pd.Timedelta(minutes=5), "kickoff": ko,
                          "home": r["home"], "away": r["away"], "books": books}])  # fmt: skip
    e = tc.entry_rows(base.iloc[[0]], None, hist)
    assert len(e) == 1
    side = r["market"]
    exp = max(books["draftkings"][side], books["fanduel"][side])  # betmgm is stale
    assert e.iloc[0]["odds_best_48h"] == pytest.approx(exp)
    assert e.iloc[0]["odds_dk_48h"] == pytest.approx(books["draftkings"][side])


def test_pool_reads_league_files(tmp_path):
    s = _scored()
    parts = [tc.lean_bets(s, "E0").assign(stage=f"z{z:g}") for z in (1.0, 2.0)]
    parts.append(tc.lean_bets(tc.with_close_chance(s), "E0").assign(stage="a_close"))
    pd.concat(parts).to_csv(tmp_path / "ceiling_E0.csv.gz", index=False)
    pd.DataFrame().to_csv(tmp_path / "entries_E0.csv.gz", index=False)
    (tmp_path / "ceiling_E0.json").write_text('{"league": "E0"}')
    res = tc.pool(tmp_path, n_boot=100)
    assert res["stages"]["z1"]["clv_gain_vs_base"] == pytest.approx(0)
    assert res["stages"]["z2"]["bets"] <= res["stages"]["z1"]["bets"]
    assert tc.summary_lines(res)
