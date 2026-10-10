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
    assert home["p_h2h"] == pytest.approx(tt.over_chance(1.6, 1.5), abs=0.01)
    # Older than DK_FRESH_HOURS: dropped as stale; none at all: "no quote".
    stale = tt.with_market(rows, pd.DataFrame([_dk("2026-10-16T07:00:00+00:00")]))
    assert (stale["dk_status"] == "stale").all() and stale["p_h2h"].isna().all()
    none = tt.with_market(rows, pd.DataFrame([_dk("2026-10-16T15:00:00+00:00")]))
    assert (none["dk_status"] == "no quote").all()


def test_with_market_skips_push_lines_and_other_books():
    rows = pd.DataFrame(_fd(line=2.0) + _fd())
    rows.loc[2, "book"] = "bovada"
    m = tt.with_market(rows, pd.DataFrame([_dk("2026-10-16T12:00:00+00:00")]))
    assert len(m) == 1 and m.iloc[0]["line"] == 1.5


def _totals(home, away, kickoff, seen, lh, la, line=2.5, books=("fanduel", "pinnacle")):
    """The match totals rows one team-total call returns (market "totals")."""
    p = tt.breakeven_over(tt._total_pmf(tt._score_probs(lh, la)), line)
    return [
        {
            "league": "E0",
            "home": home,
            "away": away,
            "kickoff": kickoff,
            "event_id": "e",
            "snapshot": "look",
            "book": b,
            "team": None,
            "side": "match",
            "line": line,
            "over": round(0.97 / p, 2),
            "under": round(0.97 / (1 - p), 2),
            "fair_over": p,
            "fair_under": 1 - p,
            "market": "totals",
            "fetched_at": seen,
            "downloaded_at": seen,
        }
        for b in books
    ]


def _league(n, seed=0, start="2026-10-17", anchored=True):
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
            if anchored and snap == "look":
                rows += _totals(h, a, ko.isoformat(), t.isoformat(), lh, la)
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
    assert out["stage"] == "development" and out["matches"]["fanduel_anchor"] == 60
    assert out["level"] == pytest.approx(1 - 0.05 / 12)
    assert set(out["candidates"]) == set(tt.MM_CANDIDATES + tt.MM_DESCRIPTIVE)
    for c in out["candidates"].values():
        assert set(c["rules"]) == {"0.02", "0.05", "0.10"}
    # The anchored chance recovers FanDuel's fair price here, so it can't clear the margin.
    assert out["candidates"]["fanduel_anchor"]["rules"]["0.02"]["bets"] == 0
    # h2h-only is descriptive: never a pass, never frozen.
    assert not any(b["pass"] for b in out["candidates"]["h2h"]["rules"].values())
    assert ("frozen_rule" in out) == bool(out["passes"])
    assert not any(x.startswith("h2h") for x in out["passes"])


def test_confirmation_uses_only_later_kickoffs():
    rows, dk, res = _league(200)
    after = pd.Timestamp("2026-10-17", tz="UTC") + pd.Timedelta(hours=3 * 49)
    out = tt.market_report(rows, dk, res, after=after, rule=("model", 0.05))
    assert out["stage"] == "confirmation" and out["matches"]["model"] == 150
    assert out["level"] == 0.95 and set(out["candidates"]) == {"model"}
    assert set(out["candidates"]["model"]["rules"]) == {"0.05"}
    short = tt.market_report(
        rows, dk, res, after=after + pd.Timedelta(hours=3), rule=("model", 0.05)
    )
    assert "not enough data yet" in short["note"]


def test_breakeven_over_handles_whole_and_quarter_lines():
    pmf = tt._total_pmf(tt._score_probs(1.5, 1.1))
    gt = lambda c: pmf[np.arange(len(pmf)) > c].sum()  # noqa: E731
    lt = lambda c: pmf[np.arange(len(pmf)) < c].sum()  # noqa: E731
    assert tt.breakeven_over(pmf, 2.5) == pytest.approx(gt(2.5))
    assert tt.breakeven_over(pmf, 3.0) == pytest.approx(gt(3) / (gt(3) + lt(3)))
    q = (gt(2.5) + gt(3)) / (gt(2.5) + gt(3) + lt(2.5) + lt(3))
    assert tt.breakeven_over(pmf, 2.75) == pytest.approx(q)


def test_anchored_means_take_the_total_from_the_totals_price():
    for lh, la, line in [(1.6, 1.0, 2.5), (1.1, 1.3, 2.75), (2.2, 0.8, 3.0)]:
        pmf = tt._total_pmf(tt._score_probs(lh, la))
        fh, fa = tt.anchored_means(_fair_h2h(lh, la), line, tt.breakeven_over(pmf, line))
        assert (fh, fa) == pytest.approx((lh, la), abs=0.03)
    # A higher total price moves the total, not just the split.
    pmf = tt._total_pmf(tt._score_probs(1.4, 1.0))
    base = sum(tt.anchored_means(_fair_h2h(1.4, 1.0), 2.5, tt.breakeven_over(pmf, 2.5)))
    up = sum(tt.anchored_means(_fair_h2h(1.4, 1.0), 2.5, tt.breakeven_over(pmf, 2.5) + 0.08))
    assert up > base + 0.2


def test_anchor_comes_only_from_the_same_call():
    seen = "2026-10-16T14:00:00+00:00"
    rows = pd.DataFrame(_fd(seen=seen) + _totals("A", "B", KO, seen, 1.6, 1.0, books=("fanduel",)))
    dk = pd.DataFrame([_dk("2026-10-16T12:00:00+00:00")])
    m = tt.with_market(rows, dk)
    assert len(m) == 2 and m["p_fanduel_anchor"].notna().all()
    assert m["p_pinnacle_anchor"].isna().all()
    # A totals row from another download is not this call's anchor.
    other = _totals("A", "B", KO, "2026-10-16T13:00:00+00:00", 1.6, 1.0, books=("pinnacle",))
    m = tt.with_market(pd.concat([rows, pd.DataFrame(other)]), dk)
    assert m["p_pinnacle_anchor"].isna().all()
    cov = tt.plumbing(rows, dk)["anchors"]
    assert cov["fanduel_anchor"] == {"rows": 2, "line_types": {"half": 2}, "median_line": 2.5}


def test_development_waits_for_both_anchors():
    rows, dk, res = _league(60, anchored=False)
    out = tt.market_report(rows, dk, res)
    assert "fanduel_anchor 0 of 50" in out["note"] and "candidates" not in out
    assert out["matches"]["model"] == 60


def test_the_older_report_waits_for_the_market_gate():
    rows, dk, res = _league(60, anchored=False)
    mm = tt.market_report(rows, dk, res)
    assert "candidates" not in mm  # no anchors yet: below the round-13 gate
    team = rows[tt.market_kind(rows) == "team_totals"]
    out = tt.report(team, res, min_matches=tt.gated_min_matches(mm))
    assert "not enough data yet" in out["note"] and "model" not in out
    # Once the gate is met the older report scores as before.
    rows, dk, res = _league(60)
    mm = tt.market_report(rows, dk, res)
    assert tt.gated_min_matches(mm) == tt.MIN_MATCHES


def test_confirmation_accepts_a_naive_after_time():
    rows, dk, res = _league(200)
    out = tt.market_report(rows, dk, res, after="2026-10-23T03:00:00", rule=("model", 0.05))
    assert out["matches"]["model"] == 150
