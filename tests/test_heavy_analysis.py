import json

import pandas as pd

from soccer_stats import cups
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import heavy_analysis as ha

KO = "2025-01-11T15:00Z"
SNAP = "2025-01-11T15:55:00Z"
UPD = "2025-01-11T15:53:00Z"


def book(key, home, away, prices):
    return {
        "key": key,
        "markets": [
            {
                "key": "h2h",
                "last_update": UPD,
                "outcomes": [
                    {"name": home, "price": prices[0]},
                    {"name": "Draw", "price": prices[1]},
                    {"name": away, "price": prices[2]},
                ],
            }
        ],
    }


def event(home, away, prices):
    books = ["draftkings", "pinnacle", "betfair_ex_eu", "fanduel"]
    return {
        "home_team": home,
        "away_team": away,
        "commence_time": "2025-01-11T15:00:00Z",
        "bookmakers": [book(b, home, away, prices) for b in books],
    }


def slot(league, sport, matches):
    return {"league": league, "sport": sport, "kickoff": KO, "request_at": "2025-01-11T15:55Z",
            "matches": matches}  # fmt: skip


def match(home, away, fav, espn_id=None, result_from="espn", source="H2P"):
    return {"home": home, "away": away, "espn_id": espn_id, "favourite": fav, "p_fav": 0.8,
            "ht": [0, 1] if fav == "home" else [1, 0], "price": "pinnacle", "source": source,
            "result_from": result_from}  # fmt: skip


def espn_day(tmp, slug, events):
    d = tmp / "espn" / cups.CACHE / "espn"
    d.mkdir(parents=True, exist_ok=True)
    body = {"events": [
        {"id": eid, "date": "2025-01-11T15:00Z", "status": {"type": {"completed": True}},
         "competitions": [{"competitors": [
             {"homeAway": "home", "team": {"id": "1", "displayName": h}, "score": str(s[0])},
             {"homeAway": "away", "team": {"id": "2", "displayName": a}, "score": str(s[1])}]}]}
        for eid, h, a, s in events
    ]}  # fmt: skip
    (d / f"{slug}_20250111.json").write_text(json.dumps(body))


def test_h2_rows_quote_checks_orientation_and_90_minute_results(tmp_path):
    ms = [match("Manchester City", "Salford City", "home", "7"),
          match("Tamworth", "Tottenham Hotspur", "away", "8")]  # fmt: skip
    s = slot("FAC", "soccer_fa_cup", ms)
    # City trail and are 1.9 in play (from ~85% before); the feed lists Spurs at home (swapped)
    evs = [event("Man City", "Salford City", [1.9, 3.6, 4.6]),
           event("Tottenham", "Tamworth", [2.0, 3.5, 4.2])]  # fmt: skip
    body = {"timestamp": SNAP, "data": evs}
    cf.body_path(tmp_path, s).write_text(json.dumps(body))
    espn_day(tmp_path, "eng.fa", [("7", "Manchester City", "Salford City", (2, 1)),
                                  ("8", "Tamworth", "Tottenham Hotspur", (1, 1))])  # fmt: skip
    rows, counts = ha.h2_rows([s], tmp_path, tmp_path / "espn")
    assert counts["unmatched"] == 0 and counts["no_result"] == 0 and len(rows) == 2
    city, spurs = rows
    assert city["dk_odds"] == 1.9 and city["won"] == 1 and city["fair_from"] == "pinnacle"
    assert spurs["dk_odds"] == 2.0 and spurs["won"] == 0  # the feed's home price, a draw
    assert spurs["ft"] == [1, 1] and spurs["band"] == "75%+"


def test_extra_league_results_come_from_football_data(tmp_path, monkeypatch):
    fd = pd.DataFrame({"date": pd.to_datetime(["2025-01-11"]), "home": ["Big"], "away": ["Small"],
                       "fthg": [1.0], "ftag": [2.0]})  # fmt: skip
    monkeypatch.setattr(ha.hm, "read_extra", lambda code, raw: fd)
    s = slot(
        "AUT",
        "soccer_austria_bundesliga",
        [match("Big", "Small", "home", result_from="football-data", source="extra")],
    )
    body = {"timestamp": SNAP, "data": [event("Big FC", "Small FC", [1.9, 3.6, 4.6])]}
    cf.body_path(tmp_path, s).write_text(json.dumps(body))
    rows, counts = ha.h2_rows([s], tmp_path, tmp_path / "espn")
    assert len(rows) == 1 and rows[0]["won"] == 0 and rows[0]["ft"] == [1, 2]


def test_a_snapshot_outside_half_time_and_a_blocked_season_count_apart(tmp_path):
    s = slot("FAC", "soccer_fa_cup", [match("A", "B", "home", "1")])
    body = {"timestamp": "2025-01-11T16:30:00Z", "data": [event("A", "B", [1.9, 3.6, 4.6])]}
    cf.body_path(tmp_path, s).write_text(json.dumps(body))
    blocked = {**s, "league": "CHN", "sport": "soccer_china_superleague"}
    (tmp_path / cf.GUARD_FILE).write_text(json.dumps({cf.guard_key(blocked): {"blocked": True}}))
    rows, counts = ha.h2_rows([s, blocked], tmp_path, tmp_path / "espn")
    assert rows == [] and counts["slots_off_half_time"] == 1
    assert counts["matches_not_priced"] == 1


def row(won, odds=2.0, sample="H", league="E0"):
    return {"league": league, "season": 2025, "kickoff": KO, "home": "A", "away": "B",
            "favourite": "home", "ht": [0, 1], "band": "75%+", "won": won, "dk_odds": odds,
            "bf_odds": None, "fair": 0.45, "fair_from": "pinnacle", "model": None,
            "sample": sample}  # fmt: skip


def test_verdict_needs_136_bets_and_a_range_above_zero():
    assert ha.verdict({"matches": 135, "roi_ci95": [0.1, 0.2]})[0] == "inconclusive"
    assert ha.verdict({"matches": 136, "roi_ci95": [0.01, 0.2]})[0] == "pass"
    assert ha.verdict({"matches": 200, "roi_ci95": [-0.1, 0.2]})[0] == "fail"
    assert ha.verdict({"matches": 200, "roi_ci95": [-0.3, -0.1]})[1].startswith("fail: the")


def test_analyse_pools_h_and_h2_and_keeps_cups_apart():
    h = ([row(1), row(0)], {"matches": 2, "matches_not_priced": 0, "unmatched": 0})
    h2 = ([row(1, league="FAC"), row(0, league="CHN")],
          {"matches": 3, "matches_not_priced": 1, "unmatched": 0})  # fmt: skip
    res = ha.analyse(h, h2)
    d = res["designs"]
    assert d["pooled"]["bets"] == 4 and d["pooled"]["role"] == "judged"
    assert d["pooled"]["matches"] == 5 and d["pooled"]["not_priced"] == 1
    assert d["cups"]["bets"] == 1 and d["leagues"]["bets"] == 3
    assert res["verdict"] == "inconclusive"
    app = ha.app_file(res, {"total": 700}, {"fetch": 1})
    assert app["verdict"] == "inconclusive" and len(app["bets"]) == 4
    assert {b["design"] for b in app["bets"]} == {"H", "H2"}
