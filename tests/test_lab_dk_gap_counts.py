"""DraftKings vs Pinnacle gap counts (docs/lab.md draft): prices only, no outcomes."""

from __future__ import annotations

import pandas as pd

from soccer_stats.lab import dk_gap_counts as dg


def _snaps(dk_home: float, age_min: float = 10) -> pd.DataFrame:
    ko = pd.Timestamp("2022-09-10 14:00", tz="UTC")
    look = ko - pd.Timedelta(hours=48)
    upd = (look - pd.Timedelta(minutes=age_min)).isoformat()
    books = {
        "pinnacle": {"home": 2.0, "draw": 3.6, "away": 4.0, "last_update": upd},
        "draftkings": {"home": dk_home, "draw": 3.4, "away": 3.8, "last_update": upd},
    }
    close = ko - pd.Timedelta(minutes=10)
    return pd.DataFrame(
        [
            {"at": look, "snapshot_ts": look, "kickoff": ko, "home": "A", "away": "B",
             "books": books},
            {"at": close, "snapshot_ts": close, "kickoff": ko, "home": "A", "away": "B",
             "books": {}},
        ]
    )  # fmt: skip


def test_gap_is_dk_price_times_pinnacle_fair():
    rows = dg.look_rows(_snaps(2.3))
    r = rows.iloc[0]
    assert r["has_look"] and r["has_close"] and r["has_dk"] and r["has_pin"]
    fair = dg.devig_shin([2.0, 3.6, 4.0])
    assert r["side"] == "home"
    assert abs(r["gap"] - (2.3 * fair[0] - 1)) < 1e-9
    assert abs(r["dk_age"] - 10) < 1e-6


def test_counts_respect_threshold_and_freshness():
    good = dg.look_rows(_snaps(2.3, age_min=10))
    stale = dg.look_rows(_snaps(2.3, age_min=120))
    c = dg.counts(pd.concat([good, stale], ignore_index=True))
    assert c["both_priced"] == 2
    assert c["bets"]["any"]["0.05"] == 2
    assert c["bets"]["15"]["0.05"] == 1
    assert c["bets"]["180"]["0.05"] == 2
    low = dg.counts(dg.look_rows(_snaps(1.9)))
    assert low["bets"]["any"]["0.05"] == 0


def test_no_look_snapshot_counts_as_missing():
    s = _snaps(2.3)
    s = s.iloc[[1]]
    rows = dg.look_rows(s)
    assert not rows.iloc[0]["has_look"]
    rep = dg.report({"E0": rows})
    assert rep["leagues"]["E0"]["all"]["look"] == 0
