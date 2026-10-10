"""Round 13: FanDuel's team totals against DraftKings' main market (team_totals.py)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats import team_totals as tt


def _fair_h2h(lh, la):
    o = tt._outcomes(tt._score_probs(lh, la))
    return {k: o[k] for k in ("home", "draw", "away")}


def test_implied_means_recover_poisson_means():
    for lh, la in [(1.6, 1.0), (0.9, 1.4), (2.4, 0.7)]:
        fh, fa = tt.implied_means(_fair_h2h(lh, la))
        assert fh == pytest.approx(lh, abs=0.02) and fa == pytest.approx(la, abs=0.02)
    o = tt._outcomes(tt._score_probs(1.5, 1.2))
    fh, fa = tt.implied_means(_fair_h2h(1.5, 1.2), {"over25": o["over25"], "under25": o["under25"]})
    assert (fh, fa) == pytest.approx((1.5, 1.2), abs=0.02)


def test_over_chance():
    assert tt.over_chance(1.0, 0.5) == pytest.approx(1 - np.exp(-1.0))
    assert tt.over_chance(1.0, 1.5) == pytest.approx(1 - 2 * np.exp(-1.0))


KO = "2026-10-17T14:00:00+00:00"


def _fd(home="A", away="B", seen="2026-10-16T14:00:00+00:00", snapshot="look", line=1.5):
    return [
        {
            "league": "E0",
            "home": home,
            "away": away,
            "kickoff": KO,
            "event_id": "e",
            "snapshot": snapshot,
            "book": "fanduel",
            "team": t,
            "side": side,
            "line": line,
            "over": 1.9,
            "under": 1.9,
            "fair_over": 0.5,
            "fair_under": 0.5,
            "margin": 0.05,
            "fetched_at": seen,
            "downloaded_at": seen,
            "minutes_before": 1440.0,
            "p_model_over": 0.45,
        }
        for t, side in ((home, "home"), (away, "away"))
    ]


def _dk(downloaded, lh=1.6, la=1.0, home="A", away="B"):
    return {
        "league": "E0",
        "home": home,
        "away": away,
        "kickoff": pd.Timestamp(KO),
        "market": "h2h",
        "fair": _fair_h2h(lh, la),
        "fetched_at": pd.Timestamp(downloaded),
        "downloaded_at": pd.Timestamp(downloaded),
    }


def test_with_market_uses_only_earlier_fresh_quotes():
    rows = pd.DataFrame(_fd())
    # A quote downloaded after the FanDuel look must not be used.
    dk = pd.DataFrame(
        [_dk("2026-10-16T12:00:00+00:00"), _dk("2026-10-16T15:00:00+00:00", 3.0, 0.3)]
    )
    m = tt.with_market(rows, dk)
    assert (m["dk_status"] == "ok").all()
    home = m[m["side"] == "home"].iloc[0]
    assert home["lam_home"] == pytest.approx(1.6, abs=0.02)
    assert home["p_market"] == pytest.approx(tt.over_chance(1.6, 1.5), abs=0.01)
    # Older than DK_FRESH_HOURS: dropped as stale; none at all: "no quote".
    stale = tt.with_market(rows, pd.DataFrame([_dk("2026-10-16T07:00:00+00:00")]))
    assert (stale["dk_status"] == "stale").all() and stale["p_market"].isna().all()
    none = tt.with_market(rows, pd.DataFrame([_dk("2026-10-16T15:00:00+00:00")]))
    assert (none["dk_status"] == "no quote").all()


def test_with_market_skips_push_lines_and_other_books():
    rows = pd.DataFrame(_fd(line=2.0) + _fd())
    rows.loc[2, "book"] = "bovada"
    m = tt.with_market(rows, pd.DataFrame([_dk("2026-10-16T12:00:00+00:00")]))
    assert len(m) == 1 and m.iloc[0]["line"] == 1.5


def _league(n, seed=0, start="2026-10-17"):
    """n matches, each with a look, a close, a DraftKings quote and a result."""
    rng = np.random.default_rng(seed)
    rows, dk, res = [], [], []
    for i in range(n):
        ko = pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=3 * i)
        h, a = f"H{i}", f"A{i}"
        lh, la = rng.uniform(0.8, 2.2), rng.uniform(0.6, 1.6)
        seen = ko - pd.Timedelta(hours=24)
        for snap, t in (("look", seen), ("close", ko - pd.Timedelta(minutes=20))):
            for r in _fd(h, a, t.isoformat(), snap):
                lam = lh if r["side"] == "home" else la
                p = tt.over_chance(lam, 1.5)
                r.update(kickoff=ko.isoformat(), fair_over=p, fair_under=1 - p)
                r.update(over=round(0.95 / p, 2), under=round(0.95 / (1 - p), 2))
                rows.append(r)
        q = _dk(seen - pd.Timedelta(hours=1), lh, la, h, a)
        q["kickoff"] = ko
        dk.append(q)
        res.append(
            {
                "league": "E0",
                "home": h,
                "away": a,
                "date": ko.strftime("%Y-%m-%d"),
                "home_goals": rng.poisson(lh),
                "away_goals": rng.poisson(la),
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(dk), pd.DataFrame(res)


def test_below_the_gate_only_counts():
    rows, dk, res = _league(20)
    out = tt.market_report(rows, dk, res)
    assert "not enough data yet" in out["note"] and "candidates" not in out
    assert out["plumbing"]["look_rows"] == 40 and out["plumbing"]["dk_status"] == {"ok": 40}
    # The plumbing never reads results.
    assert tt.plumbing(rows, dk) == out["plumbing"]


def test_development_scores_every_rule_and_freezes_one():
    rows, dk, res = _league(60)
    out = tt.market_report(rows, dk, res)
    assert out["stage"] == "development" and out["matches"] == 60
    assert out["level"] == pytest.approx(1 - 0.05 / 9)
    assert set(out["candidates"]) == set(tt.MM_CANDIDATES)
    for c in out["candidates"].values():
        assert set(c["rules"]) == {"0.02", "0.05", "0.10"}
    # The market candidate equals FanDuel's fair price here, so it can't clear the margin.
    assert out["candidates"]["market"]["rules"]["0.02"]["bets"] == 0
    assert ("frozen_rule" in out) == bool(out["passes"])


def test_confirmation_uses_only_later_kickoffs():
    rows, dk, res = _league(200)
    after = pd.Timestamp("2026-10-17", tz="UTC") + pd.Timedelta(hours=3 * 49)
    out = tt.market_report(rows, dk, res, after=after, rule=("model", 0.05))
    assert out["stage"] == "confirmation" and out["matches"] == 150 and out["level"] == 0.95
    assert set(out["candidates"]) == {"model"}
    assert set(out["candidates"]["model"]["rules"]) == {"0.05"}
    short = tt.market_report(
        rows, dk, res, after=after + pd.Timedelta(hours=3), rule=("model", 0.05)
    )
    assert "not enough data yet" in short["note"]
