import json

import numpy as np
import pandas as pd

from soccer_stats import player_segments as ps
from soccer_stats.cli import main


def synth_lines(seed=0, edge_fwd_home=1.0, n_matches=120):
    """Priced over lines for two seasons; win chance = edge / odds (1.0 = fair odds),
    except FWD at home on 1+ shots, which wins at `edge_fwd_home` / odds."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in (2024, 2025):
        start = pd.Timestamp(f"{season}-08-15", tz="UTC")
        for m in range(n_matches):
            ko = start + pd.Timedelta(days=2 * m)
            for team, home in (("H", True), ("A", False)):
                for pos in ("DEF", "MID", "FWD"):
                    for market in ("player_shots", "player_shots_on_target"):
                        for line in (1.0, 2.0, 3.0):
                            for kind in ("look", "close"):
                                odds = float(np.round(rng.uniform(1.3, 6.0), 2))
                                boost = (
                                    edge_fwd_home
                                    if (pos == "FWD" and home and line == 1.0)
                                    else 0.92
                                )
                                won = int(rng.random() < min(boost / odds, 0.99))
                                p = 1 / odds * rng.uniform(0.9, 1.15)
                                rows.append(
                                    {
                                        "kind": kind,
                                        "kickoff": ko,
                                        "home": f"H{m}",
                                        "away": f"A{m}",
                                        "player": f"{team}{pos}",
                                        "player_id": f"{team}{pos}{m}",
                                        "team": f"H{m}" if home else f"A{m}",
                                        "position": pos,
                                        "started": True,
                                        "market": market,
                                        "line": line,
                                        "side": "over",
                                        "odds": odds,
                                        "implied": 1 / odds,
                                        "p_model": p,
                                        "p": p,
                                        "won": won,
                                    }
                                )
    return pd.DataFrame(rows)


def test_bootstrap_roi_brackets_the_mean():
    rng = np.random.default_rng(1)
    ret = rng.choice([-1.0, 1.5], size=600)
    lo, hi = ps.bootstrap_roi(ret, np.repeat(np.arange(200), 3), n_boot=500)
    assert lo < ret.mean() < hi
    assert all(np.isnan(ps.bootstrap_roi(np.array([]), np.array([]))))


def test_no_edge_nothing_survives():
    d = synth_lines(seed=2)
    res = ps.out_of_sample(d, 2024, 2025, min_bets=100, n_boot=200)
    n_dims = len(ps.STRATEGIES) * np.prod([len(v) for v in ps.DIMENSIONS.values()])
    assert res["segments_tried"] == n_dims
    best = res["best"]
    assert best["train"]["bets"] >= 100
    # The pick season's winner is luck: its report-season ROI is far below its pick ROI
    assert best["test"]["roi"] < best["train"]["roi"]
    assert all(b["roi"] < 0 for b in res["baseline"].values())


def test_real_edge_is_found_out_of_sample():
    d = synth_lines(seed=3, edge_fwd_home=1.35)
    res = ps.out_of_sample(d, 2024, 2025, min_bets=100, n_boot=200)
    seg = res["best"]["segment"]
    assert seg["position"] == "FWD" and seg["venue"] in ("home", "any")
    assert res["best"]["test"]["roi"] > 0.1
    assert res["train_positive_still_positive"] >= 1


def test_seasons_are_split_by_kickoff():
    k = pd.Series(pd.to_datetime(["2025-07-01", "2025-06-30", "2026-01-01"], utc=True))
    assert list(ps.season_of(k)) == [2025, 2024, 2025]


def test_cli_writes_both_directions(tmp_path, capsys):
    d = synth_lines(seed=4, n_matches=40)
    path = tmp_path / "lines.csv.gz"
    d.to_csv(path, index=False)
    out = tmp_path / "seg.json"
    main(["player-segments", "--lines", str(path), "--min-bets", "30", "--out", str(out)])
    res = json.loads(out.read_text())
    assert [(r["train"], r["test"]) for r in res] == [(2024, 2025), (2025, 2024)]
    assert "segments tried" in capsys.readouterr().out


def test_nan_blend_never_passes_an_edge_filter():
    d = synth_lines(seed=5, n_matches=30)
    d["p"] = np.nan
    s = ps.score_segments(d[ps.season_of(d["kickoff"]) == 2024].reset_index(drop=True))
    assert (s.loc[s["edge"] != "all lines", "bets"] == 0).all()
