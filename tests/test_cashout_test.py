import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import cashout_test as ct
from soccer_stats.lab import comeback_fetch as cf

KO = "2024-09-14T14:00Z"


def body(minutes, dk, updated_after=2, home="Ajax", away="PSV"):
    snap = pd.Timestamp(KO) + pd.Timedelta(minutes=minutes)
    upd = snap - pd.Timedelta(minutes=updated_after)
    ev = {
        "home_team": home,
        "away_team": away,
        "commence_time": pd.Timestamp(KO).isoformat(),
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": upd.isoformat(),
                        "outcomes": [
                            {"name": home, "price": dk[0]},
                            {"name": "Draw", "price": dk[1]},
                            {"name": away, "price": dk[2]},
                        ],
                    }
                ],
            }
        ],
    }
    return {"timestamp": snap.isoformat(), "data": [ev]}


def setup(tmp_path, ht_prices, m70_prices, stale_m70=False, ft=(0, 1), ht=(0, 1)):
    slot = {
        "league": "N1",
        "sport": "soccer_netherlands_eredivisie",
        "kickoff": KO,
        "matches": [
            {"home": "Ajax", "away": "PSV", "dog": "away", "dog_odds": 5.0, "price": "pinnacle"}
        ],
    }
    (tmp_path / "slots.json").write_text(json.dumps({"matches": [slot]}))
    cache = tmp_path / "cache"
    cache.mkdir()
    for minutes, prices, stale in ((55, ht_prices, False), (85, m70_prices, stale_m70)):
        if prices is None:
            continue
        at = (pd.Timestamp(KO) + pd.Timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%MZ")
        path = cf.body_path(cache, {"sport": slot["sport"], "request_at": at})
        path.write_text(json.dumps(body(minutes, prices, 9 if stale else 2)))
    fd = pd.DataFrame(
        [
            {
                "league": "N1",
                "season_start": 2024,
                "home": "Ajax",
                "away": "PSV",
                "fthg": ft[0],
                "ftag": ft[1],
                "hthg": ht[0],
                "htag": ht[1],
                "fh": 0.7,
                "fd": 0.18,
                "fa": 0.12,
                "f_over": 0.6,
            }
        ]
    )
    return ct.build(tmp_path / "slots.json", cache, fd)


def test_a_passing_half_time_quote_at_50_percent_is_cashed_out(tmp_path):
    pos, counts = setup(tmp_path, (3.4, 3.6, 1.7), None)
    p = pos[0]
    assert counts["positions"] == 1 and p["won"] == 1 and p["q_m70"] is None
    assert 0.5 < p["q_ht"] < 0.6 and p["live_ht"] == 1.7
    prof, at = ct.rule(p, 0.5, 0.05)
    assert at == "ht" and prof == pytest.approx(5.0 * p["q_ht"] * 0.95 - 1)
    assert ct.rule(p, 0.6, 0.05) == (4.0, None)  # holds, and the underdog won
    assert p["p_model"] is not None and 0 < p["p_model"] < 1


def test_a_stale_70th_minute_quote_offers_no_cash_out(tmp_path):
    pos, _ = setup(tmp_path, (1.5, 3.8, 6.0), (2.5, 3.0, 2.0), stale_m70=True)
    p = pos[0]
    assert p["q_ht"] < 0.4 and p["q_m70"] is None
    assert ct.rule(p, 0.5, 0.0) == (4.0, None)


def test_the_70th_minute_quote_is_used_when_half_time_is_below_the_threshold(tmp_path):
    pos, _ = setup(tmp_path, (1.5, 3.8, 6.0), (4.0, 3.0, 1.6), ft=(1, 1))
    prof, at = ct.rule(pos[0], 0.5, 0.10)
    assert at == "m70" and prof == pytest.approx(5.0 * pos[0]["q_m70"] * 0.9 - 1)
    assert ct.hold(pos[0]) == -1.0


def test_unmatched_events_and_blocked_league_seasons_are_left_out(tmp_path):
    pos, counts = setup(tmp_path, None, None)
    assert pos == [] and counts["unmatched"] == 1
    (tmp_path / "cache" / cf.GUARD_FILE).write_text(json.dumps({"N1 2024/25": {"blocked": True}}))
    pos, counts = ct.build(
        tmp_path / "slots.json",
        tmp_path / "cache",
        pd.DataFrame(columns=["league", "season_start", "home", "away"]),
    )
    assert counts["not_priced"] == 1


def test_kelly_keeps_everything_with_a_big_edge_and_nothing_without_one():
    p = {"entry": 5.0, "won": 1, "q_ht": 0.5, "live_ht": 1.9, "p_model": 0.9}
    assert ct.kelly(p, 0.05, 0.02) == pytest.approx(ct.hold(p))
    p["p_model"] = 0.3
    assert ct.kelly(p, 0.05, 0.02) == pytest.approx(5.0 * 0.5 * 0.95 - 1)


def test_growth_drawdown_and_the_verdict_rule():
    prof = np.array([1.0, -1.0, -1.0, 2.0])
    assert ct.drawdown(prof, 0.5) == pytest.approx(0.75)
    rng = np.random.default_rng(0)
    pos = [
        {
            "kickoff": f"2024-09-{1 + i % 28:02d}T14:00Z",
            "entry": 5.0,
            "won": int(rng.random() < 0.2),
            "q_ht": 0.6,
            "live_ht": 1.6,
            "q_m70": None,
            "p_model": None,
            "league": "N1",
            "season": 2024,
        }
        for i in range(320)
    ]
    res = ct.analyse(pos, {"positions": 320})
    v = res["verdict"]
    assert v["decisions"] == 320 and v["verdict"] in ("pass", "fail")
    few = ct.analyse(pos[:100], {"positions": 100})
    assert few["verdict"]["verdict"] == "inconclusive"
