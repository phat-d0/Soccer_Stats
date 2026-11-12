"""Corner Blend Lean development gate: timing and row rules (no outcome read)."""

import numpy as np
import pandas as pd

from soccer_stats.lab import corner_lean as cl

T0 = pd.Timestamp("2026-10-15", tz="UTC")


def _rows(n_matches=200, leagues=cl.FIT_LEAGUES, seed=1, days=40):
    """Synthetic eligible-shaped rows: two half lines per match, with counts."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n_matches):
        lg = leagues[i % len(leagues)]
        k = T0 + pd.Timedelta(hours=int(i * days * 24 / n_matches))
        for side in ("home", "away"):
            fair = float(rng.uniform(0.3, 0.7))
            model = float(np.clip(fair + rng.normal(0, 0.05), 0.05, 0.95))
            count = float(rng.poisson(5))
            out.append(
                {
                    "league": lg,
                    "home": f"H{i}",
                    "away": f"A{i}",
                    "kickoff": k,
                    "side": side,
                    "line": 4.5,
                    "match": f"{lg}|{i}",
                    "look_at": k - pd.Timedelta(hours=20),
                    "age": pd.Timedelta(minutes=5),
                    "over": 1 / (fair + 0.035),
                    "under": 1 / (1 - fair + 0.035),
                    "fair": fair,
                    "model": model,
                    "close_fair": fair,
                    "count": count,
                    "won": float(count > 4.5),
                }
            )
    return pd.DataFrame(out)


def test_later_results_never_change_an_earlier_fit():
    df = _rows()
    at = T0 + pd.Timedelta(days=30)
    before = cl.fit(cl.eligible(df, at), at)
    later = df["kickoff"] + cl.SETTLE_LAG >= at
    assert later.any() and (~later).any()
    flipped = df.copy()
    flipped.loc[later, "won"] = 1 - flipped.loc[later, "won"]
    flipped.loc[later, "count"] = 99.0
    after = cl.fit(cl.eligible(flipped, at), at)
    assert before == after


def test_a_match_inside_the_settle_lag_is_left_out():
    df = _rows()
    at = df["kickoff"].iloc[100] + pd.Timedelta(hours=12)  # kicked off, not yet + 1 day
    rows = cl.eligible(df, at)
    assert (rows["kickoff"] + cl.SETTLE_LAG < at).all()
    assert df["match"].iloc[100] not in set(rows["match"])


def test_unsettled_rows_never_enter():
    df = _rows()
    df.loc[df.index[:10], "count"] = np.nan
    rows = cl.eligible(df, T0 + pd.Timedelta(days=60))
    assert rows["count"].notna().all()
    assert len(rows) == len(df) - 10


def test_locked_leagues_never_enter_a_fit():
    df = _rows()
    locked = _rows(n_matches=150, leagues=cl.LOCKED, seed=7)
    locked["match"] = "L" + locked["match"]
    locked["won"] = 1.0  # extreme outcomes: would move the fit if they entered
    locked["count"] = 99.0
    at = T0 + pd.Timedelta(days=60)
    both = pd.concat([df, locked], ignore_index=True)
    rows = cl.eligible(both, at)
    assert not rows["league"].isin(cl.LOCKED).any()
    assert cl.fit(rows, at) == cl.fit(cl.eligible(df, at), at)
    assert [f["c"] for f in cl.schedule(both)] == [f["c"] for f in cl.schedule(df)]


def test_first_fit_when_the_minimum_is_met_then_every_28_days():
    df = _rows(n_matches=300, days=80)
    fits = cl.schedule(df)
    assert fits
    first = pd.Timestamp(fits[0]["fit_at"])
    assert fits[0]["lines"] >= cl.MIN_LINES and fits[0]["matches"] >= cl.MIN_MATCHES
    # No earlier look met the minimum.
    earlier = sorted(t for t in df["look_at"].unique() if pd.Timestamp(t) < first)
    assert not any(cl.meets_minimum(cl.eligible(df, pd.Timestamp(t))) for t in earlier)
    for i, f in enumerate(fits[1:], 1):
        assert pd.Timestamp(f["fit_at"]) >= first + i * cl.REFIT


def test_minimum_not_met_gives_no_fit_and_no_picks():
    df = _rows(n_matches=60)
    assert cl.schedule(df) == []
    assert cl.replay(df) == ([], [])
    assert cl.gate(df, T0 + pd.Timedelta(days=90), draws=10)["verdict"] == "minimum not met"


def test_one_bet_per_match_and_team():
    df = _rows(n_matches=300, days=80)
    extra = df.copy()
    extra["line"] = 5.5
    _, picks = cl.replay(pd.concat([df, extra], ignore_index=True))
    keys = [(p["match"], p["side_team"]) for p in picks]
    assert len(keys) == len(set(keys))


def test_look_rows_keep_the_first_look_and_the_last_close():
    base = {
        "market": "team_corners",
        "league": "SP1",
        "home": "A",
        "away": "B",
        "kickoff": "2026-11-01T13:00:00+00:00",
        "side": "home",
        "line": 4.5,
        "over": 2.0,
        "under": 1.8,
        "p_model_over": 0.5,
    }
    rows = [
        {
            **base,
            "snapshot": "look",
            "downloaded_at": "2026-10-31T12:00:00+00:00",
            "fair_over": 0.45,
        },
        {
            **base,
            "snapshot": "look",
            "downloaded_at": "2026-10-31T18:00:00+00:00",
            "fair_over": 0.40,
        },
        {
            **base,
            "snapshot": "close",
            "downloaded_at": "2026-11-01T12:30:00+00:00",
            "fair_over": 0.5,
        },
        {
            **base,
            "snapshot": "close",
            "downloaded_at": "2026-11-01T12:45:00+00:00",
            "fair_over": 0.52,
        },
        {
            **base,
            "snapshot": "look",
            "line": 5.0,
            "downloaded_at": "2026-10-31T12:00:00+00:00",
            "fair_over": 0.4,
        },
        {
            **base,
            "snapshot": "look",
            "book": "draftkings",
            "downloaded_at": "2026-10-31T11:00:00+00:00",
            "fair_over": 0.3,
        },
    ]
    df = cl.look_rows(rows)
    assert len(df) == 1
    assert df["fair"].iloc[0] == 0.45 and df["close_fair"].iloc[0] == 0.52
