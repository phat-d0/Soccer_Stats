import json

import pandas as pd
import pytest

from soccer_stats import cups
from soccer_stats.lab import heavy_more as hm


def event(eid, date, home, away, ft, ht=None, odds=None, details=None):
    def side(name, tid, score, half, where):
        c = {"homeAway": where, "team": {"id": tid, "displayName": name}, "score": str(score)}
        if half is not None:
            c["linescores"] = [{"value": half}, {"value": score - half}]
        return c

    comp = {
        "competitors": [
            side(home, "1", ft[0], None if ht is None else ht[0], "home"),
            side(away, "2", ft[1], None if ht is None else ht[1], "away"),
        ],
        "details": details or [],
    }
    if odds:
        comp["odds"] = [odds]
    return {"id": eid, "date": date, "status": {"type": {"completed": True}},
            "competitions": [comp]}  # fmt: skip


def test_american_odds_and_both_espn_shapes():
    assert hm.american("+300") == 4.0 and hm.american(-200) == 1.5 and hm.american("EVEN") == 2.0
    assert hm.american(None) is None and hm.american("x") is None
    old = {"homeTeamOdds": {"moneyLine": -400}, "awayTeamOdds": {"moneyLine": 900},
           "drawOdds": {"moneyLine": 450}}  # fmt: skip
    new = {"moneyline": {"home": {"open": {"odds": "-400"}}, "away": {"open": {"odds": "+900"}},
                         "draw": {"close": {"odds": "+450"}}}}  # fmt: skip
    for o in (old, new):
        p = hm.espn_prices({"odds": [o]})
        assert p == {"home": 1.25, "away": 10.0, "draw": 5.5}
    assert hm.espn_prices({"odds": [{"details": "x"}]}) is None
    side, chance = hm.favourite({"home": 1.25, "away": 10.0, "draw": 5.5})
    assert side == "home" and chance > 0.75


def test_half_time_from_linescores_else_goal_clock():
    body = {"events": [
        event("1", "2023-01-07T15:00Z", "Big", "Small", (2, 1), ht=(0, 1)),
        event("2", "2023-01-07T15:00Z", "C", "D", (1, 1), details=[
            {"scoringPlay": True, "clock": {"displayValue": "44'"}, "team": {"id": "2"}},
            {"scoringPlay": True, "clock": {"displayValue": "45'+2'"}, "team": {"id": "1"}},
        ]),
    ]}  # fmt: skip
    evs = {e["id"]: e for e in hm.events(body)}
    assert evs["1"]["ht"] == [0, 1] and evs["1"]["score90"] == [2, 1]
    assert evs["2"]["ht"] == [1, 1]
    assert (
        hm.trails([0, 1], "home") and not hm.trails([0, 1], "away") and not hm.trails(None, "home")
    )


def test_cup_season_counts_heavy_trailing_and_unpriced(tmp_path):
    heavy = {"homeTeamOdds": {"moneyLine": 900}, "awayTeamOdds": {"moneyLine": -400},
             "drawOdds": {"moneyLine": 450}}  # fmt: skip
    body = {"events": [
        event("1", "2023-01-07T15:00Z", "Small", "Big", (1, 2), ht=(1, 0), odds=heavy),
        event("2", "2023-01-07T15:00Z", "Tiny", "Huge", (0, 3), ht=(0, 0), odds=heavy),
        event("3", "2023-01-08T12:00Z", "E", "F", (2, 0), ht=(1, 0)),  # no odds, not level
        event("4", "2023-01-08T12:00Z", "G", "H", (0, 0), ht=(0, 0)),  # no odds, level
    ]}  # fmt: skip
    path = tmp_path / hm.SUBDIR / cups.CACHE / "espn" / "eng.fa_20230107.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body))

    class R:
        status_code = 404

    r = hm.cup_season("FAC", 2022, tmp_path, lambda *a, **k: R(), {},
                      pd.Timestamp("2026-10-28", tz="UTC"))  # fmt: skip
    assert r["matches"] == 4 and r["with_espn_odds"] == 2 and r["heavy"] == 2
    assert r["trailing"] == 1 and r["slots"] == 1 and r["credits"] == 10
    assert r["unpriced_matches"] == 2 and r["unpriced_not_level_at_ht"] == 1
    assert r["pre_match_slots"] == 1 and r["season"] == "2022/23"


def test_join_extra_needs_date_score_and_names():
    fd = pd.DataFrame({"date": pd.to_datetime(["2023-05-06", "2023-05-06"]),
                       "home": ["Rapid Vienna", "Sturm Graz"], "away": ["Salzburg", "LASK"],
                       "fthg": [1.0, 2.0], "ftag": [3.0, 0.0]})  # fmt: skip
    evs = [
        {"kickoff": pd.Timestamp("2023-05-07T00:30Z"), "home": "SK Rapid Wien", "away":
         "Red Bull Salzburg", "score90": [1, 3], "ht": [1, 0]},  # next UTC day, same match
        {"kickoff": pd.Timestamp("2023-05-06T15:00Z"), "home": "Sturm Graz", "away": "LASK",
         "score90": [2, 1], "ht": [0, 0]},  # score differs: not this match
    ]  # fmt: skip
    out, counts = hm.join_extra(fd, evs)
    assert counts == {"matches": 2, "joined": 1, "no_event": 1}
    assert out.loc[0, "ht"] == [1, 0] and pd.isna(out.loc[1, "kickoff"])


def test_paced_get_stops_when_the_budget_is_spent():
    calls = []
    paced = hm.Paced(seconds=-1, pause=0, get=lambda *a, **k: calls.append(1))
    with pytest.raises(hm.requests.ConnectionError):
        paced("u")
    assert calls == []
