"""The comeback research summary (lab/comeback_research.json) and its builder."""

from __future__ import annotations

import json

from soccer_stats.lab import comeback_research as cr

DATA = json.loads(cr.FILE.read_text())


def row(league="E0", season=2023, won=1, dk=5.0, fair=0.2, band="45%-55%", ko="2023-09-02T14:00Z"):
    return {
        "league": league,
        "season": season,
        "kickoff": ko,
        "home": "A",
        "away": "B",
        "favourite": "home",
        "ht": [0, 1],
        "band": band,
        "deficit": 1,
        "won": won,
        "dk_odds": dk,
        "bf_odds": None,
        "fair": fair,
        "fair_from": "pinnacle",
        "model": 0.15,
    }


def test_group_counts_bets_and_market_chance():
    rows = [row(won=1), row(won=0), row(won=0, dk=None, fair=0.3)]
    g = cr.group(rows)
    assert g["bets"] == 2 and g["wins"] == 1
    assert g["roi"] == round((4.0 - 1.0) / 2, 5)
    # the market's chance averages every row with a fair price, bet or not
    assert g["implied"] == round((0.2 + 0.2 + 0.3) / 3, 5)


def test_bet_rows_are_compact_and_skip_rows_without_a_price():
    rows = [row(), row(dk=None), row(fair=None, won=0)]
    bets = cr.bet_rows(rows, "C")
    assert len(bets) == 2
    assert bets[0] == {
        "date": "2023-09-02",
        "kickoff": "2023-09-02T14:00Z",
        "lg": "E0",
        "home": "A",
        "away": "B",
        "fav": "home",
        "ht": "0-1",
        "odds": 5.0,
        "implied": 0.2,
        "won": True,
        "design": "C",
    }
    assert bets[1]["implied"] is None and bets[1]["won"] is False


def test_check_names_every_difference():
    designs = {k: dict(v) for k, v in cr.EXPECTED.items()}
    assert cr.check(designs) == []
    designs["C"]["wins"] = 122
    assert cr.check(designs) == ["C wins: 122 != 123"]


def test_file_reproduces_the_published_runs():
    assert DATA["reproduces"] is True
    for key, want in cr.EXPECTED.items():
        for field, value in want.items():
            assert DATA["designs"][key][field] == value


def test_file_has_the_shape_the_app_reads():
    for key in (
        "title",
        "question",
        "rule",
        "verdict",
        "verdict_text",
        "closed_at",
        "cost",
        "runs",
        "preregistered",
        "designs",
        "bets",
        "phase1",
        "timelines",
        "notes",
    ):
        assert key in DATA
    assert DATA["verdict"] == "fail"
    assert DATA["designs"]["C"]["role"] == "judged"
    assert DATA["designs"]["B"]["role"] == "descriptive"
    assert DATA["designs"]["pooled"]["role"] == "descriptive"
    c = DATA["cost"]
    assert c["design_b"] + c["design_c"] + c["probes"] == c["total"]
    for d in DATA["designs"].values():
        for part in ("by_league", "by_season", "by_band"):
            for line in d[part]:
                assert {"bets", "wins", "win_rate", "implied", "roi", "roi_ci95"} <= set(line)


def test_file_totals_match_the_designs_and_the_bets():
    designs, bets = DATA["designs"], DATA["bets"]
    for key, d in designs.items():
        for part in ("by_league", "by_season", "by_band"):
            assert sum(x["bets"] for x in d[part]) == d["bets"]
            assert sum(x["wins"] for x in d[part]) == d["wins"]
        mine = [b for b in bets if key == "pooled" or b["design"] == key]
        assert len(mine) == d["bets"]
        assert sum(b["won"] for b in mine) == d["wins"]
    c, b, pooled = designs["C"], designs["B"], designs["pooled"]
    for field in ("slots", "matches", "bets", "wins", "not_priced"):
        assert c[field] + b[field] == pooled[field]


def test_file_designs_share_no_match_and_bets_are_in_kickoff_order():
    key = lambda x: (x["lg"], x["home"], x["away"], x["date"])  # noqa: E731
    c = {key(x) for x in DATA["bets"] if x["design"] == "C"}
    b = {key(x) for x in DATA["bets"] if x["design"] == "B"}
    assert c and b and not c & b
    kickoffs = [x["kickoff"] for x in DATA["bets"]]
    assert kickoffs == sorted(kickoffs)
    assert all(x["date"] == x["kickoff"][:10] for x in DATA["bets"])
