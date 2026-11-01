"""Priced goalscorer test: prices joined to B's chances, the T volume rule (no network)."""

import json

import numpy as np
import pandas as pd

from soccer_stats import player_goal_priced as gp
from soccer_stats.player_goal_odds import MARKET


def body(eid, offers):
    books = [
        {
            "key": b,
            "markets": [
                {
                    "key": MARKET,
                    "outcomes": [
                        {"name": "Yes", "description": n, "price": o} for n, o in players.items()
                    ],
                }
            ],
        }  # fmt: skip
        for b, players in offers.items()
    ]
    return {"timestamp": "x", "data": {"id": eid, "bookmakers": books}}


def write_cache(base):
    d = base / "E0"
    d.mkdir(parents=True)
    ev = {"id": "ev1", "home_team": "Arsenal", "away_team": "Chelsea",
          "commence_time": "2025-11-01T15:00:00Z"}  # fmt: skip
    (d / "events_20251101.json").write_text(json.dumps({"data": [ev]}))
    entry = {
        "draftkings": {"Bukayo Saka": 3.0, "Cole Palmer": 4.0, "No Scorer": 9.0},
        "betrivers": {"Bukayo Saka": 3.2, "Cole Palmer": 3.8},
        "betfair_ex_uk": {"Bukayo Saka": 1.5, "Cole Palmer": 50.0},  # never judged
    }
    close = {
        "draftkings": {"Bukayo Saka": 2.8, "Cole Palmer": 4.2},
        "betrivers": {"Bukayo Saka": 3.0},
    }
    (d / "ev1_entry.json").write_text(json.dumps(body("ev1", entry)))
    (d / "ev1_close.json").write_text(json.dumps(body("ev1", close)))


def rows():
    k = pd.Timestamp("2025-11-01T15:00:00Z")
    base = {"league": "E0", "season": "2526", "match_id": "E0|1", "kickoff": k, "bench_goals": 0.1,
            "bench_xg": 0.1, "p_A": 0.2}  # fmt: skip
    return pd.DataFrame(
        [
            {
                **base,
                "player_id": "1",
                "player": "Bukayo Saka",
                "team": "Arsenal",
                "home": True,
                "started": True,
                "scored": 1,
                "p_B": 0.40,
            },
            {
                **base,
                "player_id": "2",
                "player": "Cole Palmer",
                "team": "Chelsea",
                "home": False,
                "started": True,
                "scored": 0,
                "p_B": 0.20,
            },
            {
                **base,
                "player_id": "3",
                "player": "Sub Guy",
                "team": "Chelsea",
                "home": False,
                "started": False,
                "scored": 0,
                "p_B": 0.10,
            },
        ]  # fmt: skip
    )


def test_prices_join_best_of_nine_books_and_closes(tmp_path):
    write_cache(tmp_path)
    pr = gp.price_rows("E0", tmp_path)
    assert "No Scorer" not in set(pr["player"])
    j, counts = gp.join(rows(), pr)
    assert counts["events_linked"] == 1 and counts["names_matched"] == 2
    saka = j[j["player"] == "Bukayo Saka"].iloc[0]
    assert saka["entry_odds"] == 3.2 and saka["entry_book"] == "betrivers"  # not Betfair's
    assert saka["close_best"] == 3.0 and saka["close_same"] == 3.0
    palmer = j[j["player"] == "Cole Palmer"].iloc[0]
    assert palmer["entry_odds"] == 4.0 and palmer["close_best"] == 4.2
    assert palmer["close_same"] == 4.2
    assert len(j) == 2  # the substitute is not a judged row


def test_t_rule_takes_the_largest_threshold_with_enough_volume():
    # 10 matches; edges spread so 5% and 8% reach 0.3 bets a match but 12% doesn't.
    edges = [0.06] * 1 + [0.10] * 3 + [0.20] * 0
    j = pd.DataFrame(
        {
            "match_id": [f"m{i}" for i in range(10)],
            "entry_odds": 2.0,
            "p_B": [(1 + e) / 2 for e in edges] + [0.1] * (10 - len(edges)),
        }  # fmt: skip
    )
    v = gp.t_volume(j)
    per = {r["t"]: r["per_match"] for r in v["by_t"]}
    assert per[0.05] == 0.4 and per[0.08] == 0.3 and per[0.12] == 0.0
    assert v["T"] == 0.08
    assert gp.t_volume(j.assign(p_B=0.1))["T"] == gp.T_FALLBACK


def test_blend_c_finds_information_beyond_the_price():
    rng = np.random.default_rng(1)
    n = 3000
    price_p = rng.uniform(0.05, 0.4, n)
    true_p = np.clip(price_p + rng.normal(0, 0.05, n), 0.02, 0.6)
    scored = (rng.uniform(size=n) < true_p).astype(int)
    j = pd.DataFrame({"match_id": np.arange(n) // 20, "entry_odds": 1 / price_p,
                      "p_B": true_p, "scored": scored})  # fmt: skip
    c = gp.blend_c(j, draws=200)
    assert c["c"] > 0 and c["ci95"][0] > 0
    d = gp.describe(j.assign(close_best=j["entry_odds"], close_same=j["entry_odds"]), 0.12)
    assert d["baseline_clv"]["mean"] == 0 and d["bets"] > 0


def test_price_bands_split_the_rule_bets_into_quintiles():
    j = pd.DataFrame({"match_id": [f"m{i}" for i in range(10)],
                      "entry_odds": [float(o) for o in range(2, 12)], "p_B": 0.9})  # fmt: skip
    edges = gp.band_edges(j, 0.2)
    assert edges == [3.8, 5.6, 7.4, 9.2]
    # lo < odds <= hi: an odds equal to an edge sits in the lower band.
    assert list(gp.band_of([3.8, 3.81, 2.0, 50.0], edges)) == [0, 1, 0, 4]
    assert gp.band_edges(j.assign(p_B=0.01), 0.2) == []


def drift_rows():
    """Prices drift more on long shots; the rule bets only long shots, with the same drift."""
    rng = np.random.default_rng(3)
    rows = []
    for m in range(400):
        for odds, drift, p in ((2.5, 0.00, 0.30), (12.0, 0.08, 0.05), (12.0, 0.08, 0.20)):
            noise = rng.normal(0, 0.02)
            rows.append({"match_id": f"m{m}", "entry_odds": odds, "p_B": p,
                         "close_best": odds / (1 + drift + noise), "scored": 0})  # fmt: skip
    return pd.DataFrame(rows)


def test_matched_excess_removes_the_drift_of_the_rule_price_mix():
    j = drift_rows()
    edges = [3.0, 6.0, 9.0, 11.0]
    e = gp.matched_excess(j, 0.2, edges, draws=300)
    assert e["bets"] == 400 and e["matches"] == 400
    assert abs(e["rule_clv"] - 0.08) < 0.01
    assert abs(e["baseline_matched"] - 0.08) < 0.01  # same band, same drift
    assert abs(e["excess"]) < 0.005 and e["ci95"][0] < 0 < e["ci95"][1]
    # Unweighted, the long-shot drift looks like skill.
    assert e["excess_plain"] > 0.02 and e["excess_plain_ci95"][0] > 0
    shares = {b["lo"]: b["rule_share"] for b in e["by_band"]}
    assert shares[11.0] == 1.0


def test_matched_excess_finds_a_real_edge_within_a_band():
    j = drift_rows()
    picked = j["p_B"] == 0.20
    j.loc[picked, "close_best"] = (
        j.loc[picked, "entry_odds"] / 1.12
    )  # the rule's picks shorten more
    e = gp.matched_excess(j, 0.2, [3.0, 6.0, 9.0, 11.0], draws=300)
    assert e["excess"] > 0.015 and e["ci95"][0] > 0


def test_verdict_needs_c_and_the_matched_excess():
    c_ok, c_no = {"ci95": [0.1, 0.5]}, {"ci95": [-0.1, 0.5]}
    ex_ok = {"bets": 400, "ci95": [0.01, 0.05]}
    ex_no = {"bets": 400, "ci95": [-0.01, 0.05]}
    assert gp.verdict(c_ok, ex_ok) == "pass"
    assert gp.verdict(c_ok, ex_no) == "fail"
    assert gp.verdict(c_no, ex_ok) == "fail"
    assert gp.verdict(c_ok, {"bets": 200, "ci95": [0.01, 0.05]}) == "inconclusive"
