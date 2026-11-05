"""Residual map (docs/lab.md "Residual map"): grids, ranges and factors on synthetic data."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import residual_map as rm


def test_bin_index_edges_and_nan():
    edges = (0.0, 0.1, 0.2, 1.0)
    idx = rm.bin_index([0.0, 0.05, 0.1, 0.19, 0.2, 0.99, np.nan], edges)
    assert idx.tolist() == [0, 0, 1, 1, 2, 2, -1]
    y = rm.bin_index([-0.2, -0.05, 0.0, 0.05, 0.2], rm.Y_EDGES)
    assert y.tolist() == [0, 2, 3, 5, 6]


def test_boot_range_resamples_matches():
    rng = np.random.default_rng(1)
    resid = rng.normal(0.1, 0.5, 600)
    lo, hi = rm.boot_range(resid, np.arange(600), n_boot=400)
    assert lo < 0.1 < hi
    # The same rows tied in pairs (one match each) give a wider range than independent rows.
    pair = np.repeat(rng.normal(0, 0.5, 300), 2)
    a = rm.boot_range(pair, np.repeat(np.arange(300), 2), n_boot=400)
    b = rm.boot_range(pair, np.arange(600), n_boot=400)
    assert (a[1] - a[0]) > (b[1] - b[0])
    assert rm.boot_range([0.3], [0]) is None


def _rows(n_matches: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n_matches):
        p = rng.dirichlet([4, 3, 3])
        y = rng.choice(3, p=p)
        for i, s in enumerate(rm.SIDES):
            out.append(
                {
                    "league": "E0" if k % 2 else "E1",
                    "season": "2122",
                    "date": pd.Timestamp("2021-08-14") + pd.Timedelta(days=k % 200),
                    "home": f"H{k}",
                    "away": f"A{k}",
                    "match": f"m{k}",
                    "side": s,
                    "p_model": float(np.clip(p[i] + rng.normal(0, 0.04), 0.01, 0.98)),
                    "p_blend": float(p[i]),
                    "fair_early": float(p[i]),
                    "fair_close": float(p[i]),
                    "won": float(y == i),
                }
            )
    df = pd.DataFrame(out)
    df["move"] = df["fair_close"] - df["fair_early"]
    return df


def test_surface_cells_partition_the_rows():
    rows = _rows()
    s = rm.surface(rows, "fair_close", n_boot=50)
    assert len(s["cells"]) == (len(rm.X_EDGES) - 1) * (len(rm.Y_EDGES) - 1)
    assert sum(c["n"] for c in s["cells"]) == len(rows) == s["rows"]
    for c in s["cells"]:
        assert c["thin"] == (c["n"] < rm.THIN)
        if c["n"]:
            assert rm.X_EDGES[c["xi"]] <= c["x"] <= rm.X_EDGES[c["xi"] + 1]


def test_slice_grid_totals_match_the_level_rows():
    rows = _rows()
    g = rm.slice_grid(rows, "league", ["E0", "E1"], "League", n_boot=50)
    for t in g["totals"]:
        n = (rows["league"] == t["level"]).sum()
        assert t["n"] == n
        assert sum(c["n"] for c in g["cells"] if c["level"] == t["level"]) == n
    assert len(g["cells"]) == 2 * len(rm.BAND_LABELS)


def _matches() -> pd.DataFrame:
    """Two seasons in E0 and E1: 'Up' is promoted into E0, 'Down' goes from E0 to E1."""
    rows = []
    start = {"1920": pd.Timestamp("2019-08-10"), "2021": pd.Timestamp("2020-08-10")}
    teams = {
        ("E0", "1920"): ["A", "B", "Down"],
        ("E0", "2021"): ["A", "B", "Up"],
        ("E1", "1920"): ["C", "D", "Up"],
        ("E1", "2021"): ["C", "D", "Down"],
    }
    for (lg, s), ts in teams.items():
        day = start[s]
        for r in range(12):
            for i, h in enumerate(ts):
                a = ts[(i + 1 + r) % len(ts)]
                if a == h:
                    continue
                rows.append(
                    {"league": lg, "season": s, "date": day, "home": h, "away": a,
                     "home_goals": 1, "away_goals": 0, "home_xg": 1.5, "away_xg": 0.5}
                )  # fmt: skip
            day += pd.Timedelta(days=7)
    return pd.DataFrame(rows)


def test_newcomers_promoted_and_relegated():
    new = rm.newcomers(_matches())
    assert new[("E0", "2021", "Up")] == "promoted"
    assert new[("E1", "2021", "Down")] == "relegated"
    assert ("E0", "1920", "A") not in new  # no season before is loaded


def test_after_europe_window_is_before_kickoff_only():
    day = pd.Timestamp("2021-09-18")
    assert rm.after_europe([pd.Timestamp("2021-09-15")], day)  # 3 days before
    assert not rm.after_europe([pd.Timestamp("2021-09-18")], day)  # same day
    assert not rm.after_europe([pd.Timestamp("2021-09-19")], day)  # after
    assert not rm.after_europe([pd.Timestamp("2021-09-13")], day)  # 5 days before
    assert not rm.after_europe(None, day)


def test_euro_dates_maps_clubs():
    ev = [{"kickoff": pd.Timestamp("2021-09-15 19:00", tz="UTC"), "home": "Arsenal FC",
           "away": "Unknown FC"}]  # fmt: skip
    got = rm.euro_dates(ev, {"Arsenal FC": "E0:Arsenal", "Unknown FC": None})
    assert got == {"E0:Arsenal": [pd.Timestamp("2021-09-15")]}


def test_add_factors_orients_sides_and_uses_earlier_matches():
    m = _matches()
    s2 = m[m["season"] == "2021"].copy()
    rows = []
    for r in s2.itertuples(index=False):
        key = f"{r.league}|{r.date.date()}|{r.home}"
        for i, side in enumerate(rm.SIDES):
            rows.append({"league": r.league, "season": r.season, "date": r.date,
                         "home": r.home, "away": r.away, "match": key, "side": side,
                         "p_model": 0.3, "p_blend": 0.3, "fair_early": 0.3,
                         "fair_close": 0.32, "won": float(i == 0)})  # fmt: skip
    rows = pd.DataFrame(rows)
    rows["move"] = rows["fair_close"] - rows["fair_early"]
    euro = {"E0:A": [pd.Timestamp("2020-08-14")]}  # A plays Europe a day before matchday 1
    lean = pd.DataFrame([{"league": "E0", "date": "2020-08-10", "home": "A",
                          "market": "home", "driver": "market"}])  # fmt: skip
    out = rm.add_factors(rows, m, lean, euro)
    assert len(out) == len(rows)
    draws = out[out["side"] == "draw"]
    assert draws[["rest_diff", "xg_luck", "new_team", "europe"]].isna().all().all()
    # 'Up' is new to E0 in 2020/21: its first 10 league matches are flagged, later ones not.
    up_home = out[(out["league"] == "E0") & (out["home"] == "Up") & (out["side"] == "home")]
    first = up_home.sort_values("date")
    assert first["new_team"].iloc[0] == "promoted, first 10"
    up_rows = out[(out["league"] == "E0") & (out["side"] == "away") & (out["home"] == "Up")]
    assert set(up_rows["new_team"]) <= {"opponent new", "neither"}
    a1 = out[(out["league"] == "E0") & (out["date"] == pd.Timestamp("2020-08-17"))]
    assert set(a1["europe"].dropna()) <= set(rm.EURO_LEVELS)
    # The price move is a label; month is August for the first matchday.
    assert set(out["price_move"]) == {"+0.01 to +0.04"}
    assert out[out["date"] == pd.Timestamp("2020-08-10")]["month"].eq("Aug").all()
    picked = out[(out["league"] == "E0") & (out["home"] == "A") & (out["side"] == "home")
                 & (out["date"] == pd.Timestamp("2020-08-10"))]  # fmt: skip
    assert picked["driver"].tolist() == ["Lean pick, market-driven"]
    assert out["driver"].isin(rm.DRIVER_LEVELS).all()


def test_europe_flag_counts_only_the_four_days_before():
    m = _matches()
    s2 = m[(m["season"] == "2021") & (m["league"] == "E0")]
    first_day = s2["date"].min()
    team = s2[s2["date"] == first_day].iloc[0]
    rows = pd.DataFrame(
        [
            {
                "league": "E0",
                "season": "2021",
                "date": first_day,
                "home": team["home"],
                "away": team["away"],
                "match": "x",
                "side": "home",
                "p_model": 0.5,
                "p_blend": 0.5,
                "fair_early": 0.5,
                "fair_close": 0.5,
                "won": 1.0,
            }
        ]  # fmt: skip
    )
    rows["move"] = 0.0
    euro = {f"E0:{team['home']}": [first_day - pd.Timedelta(days=3)]}
    out = rm.add_factors(rows, m, None, euro)
    assert out["europe"].iloc[0] == "team after Europe"
    euro = {f"E0:{team['away']}": [first_day - pd.Timedelta(days=2)]}
    assert rm.add_factors(rows, m, None, euro)["europe"].iloc[0] == "opponent after Europe"


def test_build_is_json_and_counts_cells():
    rows = _rows(300)
    for f in rm.FACTORS:
        if f["key"] not in rows:
            rows[f["key"]] = None
    res = rm.build(rows, n_boot=30, info={"locked_from": "2024-07-01"})
    text = json.dumps(res, default=str)
    assert '"exploratory": true' in text
    assert res["kind"] == "residual_map"
    assert res["slices"]["rest_diff"]["error"]
    assert res["slices"]["side"]["levels"] == list(rm.SIDES)
    mu = res["multiplicity"]
    assert mu["cells"] >= mu["cells_200_plus"] >= mu["range_excludes_0"]
    assert res["warning"].startswith("Exploratory")
    assert {f["key"] for f in res["factors"]} == {
        "side", "league", "month", "rest_diff", "europe", "xg_luck", "new_team",
        "price_move", "driver", "home_league",
    }  # fmt: skip


def test_main_refuses_rows_in_the_locked_holdout():
    from soccer_stats.lab.harness import Holdout, HoldoutLocked

    h = Holdout(pd.Timestamp("2024-07-01"))
    with pytest.raises(HoldoutLocked):
        h.check([pd.Timestamp("2024-08-10")])
    assert rm.LAST_YEAR + 1 == 2024  # the default cut-off is the strategy search's holdout


def test_print_file_round_trips(capsys):
    import base64
    import gzip

    text = json.dumps({"a": 1})
    rm.print_file(text)
    lines = capsys.readouterr().out.splitlines()
    blob = "".join(ln.split(" ", 2)[2] for ln in lines if ln.startswith("RESIDUAL_MAP_B64"))
    assert gzip.decompress(base64.b64decode(blob)).decode() == text
