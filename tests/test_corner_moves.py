"""Corner price moves (edge/corner_moves.py) on a synthetic corner log."""

import json

import pytest

from soccer_stats.edge import corner_moves as cmv


def q(ev, snap, fair, p_model, fetched, line=4.5, book=None, kickoff="2026-11-01T15:00:00Z"):
    r = {
        "market": "team_corners",
        "league": "SP1",
        "event_id": ev,
        "team": "A",
        "line": line,
        "snapshot": snap,
        "over": 1 / fair,
        "under": 1 / (1 - fair),
        "fair_over": fair,
        "p_model_over": p_model,
        "fetched_at": fetched,
        "kickoff": kickoff,
    }
    if book:
        r["book"] = book
    return r


def write(tmp_path, rows):
    d = tmp_path / "odds_log"
    d.mkdir()
    (d / "SP1_corners_2026-11.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    return tmp_path


def test_moves_toward_the_model_side_and_filters(tmp_path):
    rows = []
    for i in range(10):
        ev = f"e{i}"
        up = i % 2 == 0  # model above the price on even events, below on odd
        rows += [
            q(ev, "look", 0.50, 0.60 if up else 0.40, "2026-10-31T10:00:00Z"),
            q(ev, "look", 0.51, 0.60 if up else 0.40, "2026-10-31T11:00:00Z"),  # newest look
            q(ev, "close", 0.53 if up else 0.49, 0.60, "2026-11-01T14:45:00Z"),
        ]
    rows.append(q("w", "look", 0.5, 0.6, "2026-10-31T10:00:00Z", line=5.0))  # whole line
    rows.append(q("w", "close", 0.6, 0.6, "2026-11-01T14:45:00Z", line=5.0))
    late = "2026-11-09T15:00:00Z"
    rows.append(q("late", "look", 0.5, 0.6, "2026-11-08T10:00:00Z", kickoff=late))
    rows.append(q("late", "close", 0.4, 0.6, "2026-11-09T14:45:00Z", kickoff=late))
    res = cmv.analyse(write(tmp_path, rows), draws=200)
    p = res["primary"]
    assert p["lines"] == 10 and p["matches"] == 10
    assert p["toward_mean"] == pytest.approx(0.02)
    assert res["verdict"] == "pass"
    assert p["moved_toward"] == 1.0
    assert res["draftkings"] == {"lines": 0}
    assert res["rule_bets"]["lines"] == 10  # 0.60 × 1/0.51 − 1 ≥ 12%
    assert "VERDICT pass" in cmv.report(res)


def test_no_movement_fails(tmp_path):
    rows = []
    for i in range(6):
        rows += [
            q(f"e{i}", "look", 0.5, 0.6, "2026-10-31T10:00:00Z"),
            q(f"e{i}", "close", 0.5 + (0.01 if i % 2 else -0.01), 0.6, "2026-11-01T14:45:00Z"),
        ]
    res = cmv.analyse(write(tmp_path, rows), draws=200)
    assert res["primary"]["toward_mean"] == pytest.approx(0.0, abs=1e-12)
    assert res["verdict"] == "fail"
