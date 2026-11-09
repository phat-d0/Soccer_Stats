"""Corner twins under a club's old name (lead, 9 Nov): the later one is voided once by
paper.duplicate_sweep inside _run_corners; the first opened stands, whatever its result."""

import json

import pandas as pd

from soccer_stats import paper
from soccer_stats import trades as tr

OLD = "SP1|2627|Osasuna|Racing Santander|corners|home|5.5"
NEW = "SP1|2627|Osasuna|Santander|corners|home|5.5"
NOW = pd.Timestamp("2026-11-09 12:00", tz="UTC")


def opened(tid, at, side, odds, book="pinnacle"):
    return {
        "type": "open",
        "id": tid,
        "bet_type": "corners",
        "league": "SP1",
        "season": "2627",
        "opened_at": at,
        "kickoff": "2026-10-18T12:00:00+00:00",
        "home": "Osasuna",
        "away": tid.split("|")[3],
        "market": "team_corners",
        "team": "Osasuna",
        "team_side": "home",
        "line": 5.5,
        "side": side,
        "odds": odds,
        "stake": 10.0,
        "status": "open",
        "bookmaker": book,
        "strategy": book,
    }


def settled(tid, status, profit):
    return {"type": "update", "id": tid, "status": status, "profit": profit, "actual": 7}


def ledger_dir(tmp_path):
    d = tmp_path / "paper_trades"
    d.mkdir()
    rows = [
        opened(OLD, "2026-10-17T09:13:16+00:00", "under", 1.76),
        opened(NEW, "2026-10-18T11:45:46+00:00", "over", 2.02),
        opened(NEW + "|dk", "2026-10-18T11:45:46+00:00", "over", 1.95, book="draftkings"),
        settled(OLD, "lost", -10.0),
        settled(NEW, "won", 10.2),
        settled(NEW + "|dk", "won", 9.5),
    ]
    (d / "SP1_corners_2627.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return tmp_path


def test_the_later_twin_is_voided_and_the_first_stands(tmp_path):
    log = ledger_dir(tmp_path)
    led = paper.load_corner_ledger(log, "SP1")
    ev = paper.duplicate_sweep(led, NOW)
    assert [e["id"] for e in ev] == [NEW]  # the DraftKings line (|dk) is its own trade
    paper.append_corner_events(log, ev)
    again = paper.load_corner_ledger(log, "SP1")
    twin = again[NEW]
    assert twin["status"] == "void" and twin["profit"] == 0.0 and twin["push"] is False
    assert twin["void_reason"] == tr.DUPLICATE_REASON and twin["duplicate_of"] == OLD
    assert again[OLD]["status"] == "lost" and again[OLD]["profit"] == -10.0  # untouched
    assert again[NEW + "|dk"]["status"] == "won"
    assert paper.duplicate_sweep(again, NOW) == []  # once only


def test_run_corners_sweeps_before_updating(tmp_path):
    log = ledger_dir(tmp_path)
    n, trades = paper._run_corners({"fixtures": []}, log, ["SP1"], "SP1", NOW, None)
    assert n == 1
    by = {t["id"]: t for t in trades}
    assert by[NEW]["status"] == "void" and by[OLD]["status"] == "lost"
    n2, _ = paper._run_corners({"fixtures": []}, log, ["SP1"], "SP1", NOW, None)
    assert n2 == 0  # nothing swept twice
