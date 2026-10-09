import json

import pandas as pd
import pytest
import requests

from soccer_stats import espn_news as en

NOW = pd.Timestamp("2026-10-17 10:00", tz="UTC")
K = NOW + pd.Timedelta(hours=5)  # 15:00 kickoff


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(en.time, "sleep", lambda s: None)


def card(home="Man City", away="Bournemouth", league="E0", kickoff=K):
    return {"league": league, "home": home, "away": away, "kickoff": kickoff.isoformat()}


def scoreboard(*events):
    return {
        "events": [
            {
                "id": i,
                "date": k.isoformat(),
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": h}},
                            {"homeAway": "away", "team": {"displayName": a}},
                        ]
                    }
                ],
            }
            for i, h, a, k in events
        ]
    }


def roster(team, n_start, n_subs=7):
    players = [
        {"starter": True, "athlete": {"displayName": f"{team} S{i}"}} for i in range(n_start)
    ]
    players += [
        {"starter": False, "athlete": {"displayName": f"{team} B{i}"}} for i in range(n_subs)
    ]
    return {"team": {"displayName": team}, "roster": players}


def summary(n_start=11, injuries=True):
    body = {"rosters": [roster("Manchester City", n_start), roster("AFC Bournemouth", n_start)]}
    if injuries:
        body["injuries"] = [
            {
                "team": {"displayName": "Manchester City"},
                "injuries": [
                    {
                        "athlete": {"displayName": "Rodri"},
                        "status": "Out",
                        "details": {"type": "Knee"},
                    }
                ],
            }
        ]
    return body


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def fake(calls, summ=None, board=None, status=200):
    board = board or scoreboard(("401", "Manchester City", "AFC Bournemouth", K))

    def get(url, params=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        if status != 200:
            return Resp({}, status)
        return Resp(
            board if url.endswith("scoreboard") else (summ if summ is not None else summary())
        )

    return get


def test_slugs_and_name_mapping():
    assert en.SLUGS == {
        "E0": "eng.1",
        "SP1": "esp.1",
        "D1": "ger.1",
        "I1": "ita.1",
        "F1": "fra.1",
        "E1": "eng.2",
    }
    known = {"Man City", "Bournemouth", "Ath Madrid", "Inter", "Wolves"}
    assert en.team_name("Manchester City", known) == "Man City"  # shared Odds API map
    assert en.team_name("AFC Bournemouth", known) == "Bournemouth"
    assert en.team_name("Atlético Madrid", known) == "Ath Madrid"
    assert en.team_name("Internazionale", known) == "Inter"
    ev = en.parse_scoreboard(
        scoreboard(("1", "Wolverhampton Wanderers", "Manchester City", K)), known
    )
    assert (ev[0]["home"], ev[0]["away"]) == ("Wolves", "Man City")
    assert ev[0]["espn_home"] == "Wolverhampton Wanderers"


# ESPN's spellings from the probe (run 37964999592) -> football-data's.
PROBED = {
    "AFC Bournemouth": "Bournemouth",
    "Brighton & Hove Albion": "Brighton",
    "Manchester United": "Man United",
    "Nottingham Forest": "Nott'm Forest",
    "Tottenham Hotspur": "Tottenham",
    "Leeds United": "Leeds",
    "Hull City": "Hull",
    "Athletic Club": "Ath Bilbao",
    "Atlético Madrid": "Ath Madrid",
    "Alavés": "Alaves",
    "Celta Vigo": "Celta",
    "Deportivo": "La Coruna",
    "Espanyol": "Espanol",
    "Málaga": "Malaga",
    "Rayo Vallecano": "Vallecano",
    "Real Betis": "Betis",
    "1. FC Union Berlin": "Union Berlin",
    "Bayer Leverkusen": "Leverkusen",
    "Borussia Dortmund": "Dortmund",
    "Borussia Mönchengladbach": "M'gladbach",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "FC Augsburg": "Augsburg",
    "FC Cologne": "FC Koln",
    "Hamburg SV": "Hamburg",
    "SC Paderborn 07": "Paderborn",
    "SV Elversberg": "Elversberg",
    "TSG Hoffenheim": "Hoffenheim",
    "AC Milan": "Milan",
    "AS Roma": "Roma",
    "Internazionale": "Inter",
    "AJ Auxerre": "Auxerre",
    "AS Monaco": "Monaco",
    "Le Havre AC": "Le Havre",
    "Paris Saint-Germain": "Paris SG",
    "Stade Rennais": "Rennes",
    "Queens Park Rangers": "QPR",
    "West Bromwich Albion": "West Brom",
    "Wolverhampton Wanderers": "Wolves",
    "Preston North End": "Preston",
}


def test_probed_espn_names_map_to_football_data():
    known = set(PROBED.values()) | {"Paris FC", "Bristol City", "West Ham"}
    wrong = {e: en.team_name(e, known) for e, f in PROBED.items() if en.team_name(e, known) != f}
    assert wrong == {}
    assert en.team_name("Paris FC", known) == "Paris FC"  # not taken for Paris SG


def test_parse_summary_and_confirmed():
    p = en.parse_summary(summary())
    assert p["lineups"]["Manchester City"]["starters"][0] == "Manchester City S0"
    assert len(p["lineups"]["AFC Bournemouth"]["subs"]) == 7
    assert p["injuries"]["Manchester City"] == [
        {"name": "Rodri", "status": "Out", "detail": "Knee"}
    ]
    assert en.confirmed(p["lineups"])
    assert not en.confirmed(en.parse_summary(summary(n_start=0))["lineups"])
    assert en.parse_summary(None) == {"lineups": {}, "injuries": {}, "updated": None}
    assert en.parse_summary({"rosters": "odd", "injuries": [{"team": None}]})["lineups"] == {}


def test_add_attaches_compact_team_news(tmp_path):
    calls, cards = [], [card()]
    s = en.add(cards, tmp_path, NOW, fake(calls, summary(n_start=0)))
    tn = cards[0]["team_news"]
    assert tn["source"] == "ESPN" and tn["event_id"] == "401"
    assert tn["injuries"]["home"][0]["name"] == "Rodri" and tn["injuries"]["away"] == []
    assert tn["lineup"]["confirmed"] is False and "first_confirmed_at" not in tn["lineup"]
    assert s["matches"] == 1 and s["confirmed"] == 0 and s["unmatched"] == []
    json.dumps(tn)  # JSON-safe for data.json


def test_windows_cache_and_first_confirmed(tmp_path):
    calls, cards = [], [card()]
    get = fake(calls, summary(n_start=0))
    en.add(cards, tmp_path, NOW, get)  # 5 h out: a scoreboard per day + the summary
    assert calls == ["scoreboard", "scoreboard", "summary"]
    en.add([card()], tmp_path, NOW + pd.Timedelta(hours=1), get)  # cached for 3 h
    assert calls == ["scoreboard", "scoreboard", "summary"]
    en.add([card()], tmp_path, NOW + pd.Timedelta(hours=3, minutes=5), get)  # refreshed
    assert calls.count("summary") == 2
    # Inside 90 minutes: every run until the XI is confirmed...
    t = K - pd.Timedelta(minutes=80)
    en.add([card()], tmp_path, t, get)
    en.add([card()], tmp_path, t + pd.Timedelta(minutes=15), get)
    assert calls.count("summary") == 4
    get = fake(calls, summary())
    c = [card()]
    en.add(c, tmp_path, t + pd.Timedelta(minutes=30), get)
    first = c[0]["team_news"]["lineup"]["first_confirmed_at"]
    assert (
        c[0]["team_news"]["lineup"]["confirmed"]
        and first == (t + pd.Timedelta(minutes=30)).isoformat()
    )
    # ...then never again, and the first-seen time stays.
    c = [card()]
    en.add(c, tmp_path, t + pd.Timedelta(minutes=45), get)
    assert (
        calls.count("summary") == 5 and c[0]["team_news"]["lineup"]["first_confirmed_at"] == first
    )


def test_out_of_window_and_unmatched(tmp_path):
    calls = []
    far = card(kickoff=NOW + pd.Timedelta(hours=40))
    en.add([far], tmp_path, NOW, fake(calls))
    assert calls == [] and "team_news" not in far
    board = scoreboard(
        ("401", "Manchester City", "AFC Bournemouth", K), ("402", "Leeds United", "Fulham", K)
    )
    s = en.add([card()], tmp_path, NOW, fake(calls, board=board))
    assert s["unmatched"] == ["E0: Leeds United v Fulham"]
    assert "unmatched: E0: Leeds United v Fulham" in en.summary_line(s)


def test_espn_failures_never_raise(tmp_path, monkeypatch):
    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError("down")

    c = [card()]
    s = en.add(c, tmp_path, NOW, boom)
    days = 2  # 10:00 + 36 h spans two UTC days: one scoreboard call each
    assert "team_news" not in c and s["failures"] == days
    assert s["requests"] == days * (en.RETRIES + 1)
    calls = []
    s = en.add([card()], tmp_path / "b", NOW, fake(calls, status=503))  # 5xx: retried
    assert len(calls) == days * (en.RETRIES + 1)
    calls = []
    s = en.add([card()], tmp_path / "c", NOW, fake(calls, status=404))  # 4xx: not retried
    assert calls == ["scoreboard"] * days
    # A broken body for one league never stops the others.
    monkeypatch.setattr(en, "parse_scoreboard", lambda *a: 1 / 0)
    s = en.add([card()], tmp_path / "d", NOW, fake([]))
    assert s["errors"] == ["E0: ZeroDivisionError"]


def test_log_dedups_and_marks_the_first_confirmed_xi(tmp_path):
    def data(n_start, at):
        c = [card()]
        en.add(c, tmp_path / "raw", at, fake([], summary(n_start=n_start)))
        return {"fixtures": c}

    early = data(0, NOW)
    rows = en.rows_from_data(early, NOW)
    assert [r["side"] for r in rows] == ["home", "away"] and rows[0]["team"] == "Man City"
    assert en.log(tmp_path / "log", rows) == 2
    assert en.log(tmp_path / "log", rows) == 0  # unchanged: nothing new
    t = K - pd.Timedelta(minutes=60)
    late = data(11, t)
    assert en.log(tmp_path / "log", en.rows_from_data(late, t)) == 2
    again = data(11, t + pd.Timedelta(minutes=15))  # cached, unchanged
    assert en.log(tmp_path / "log", en.rows_from_data(again, t)) == 0
    path = tmp_path / "log" / "team_news" / "E0_2026-10.jsonl"
    logged = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["first_confirmed"] for r in logged] == [False, False, True, True]
    assert logged[2]["first_confirmed_at"] == t.isoformat() and len(logged[2]["starters"]) == 11


def test_scoreboard_asks_one_date_per_call(tmp_path):
    """ESPN answers a range (dates=A-B) with 400 (probe run 37964327038): one day each."""
    seen = []

    def get(url, params=None, timeout=None):
        seen.append(params.get("dates"))
        return Resp(scoreboard(("401", "Manchester City", "AFC Bournemouth", K)))

    en.scoreboard("E0", tmp_path, pd.Timestamp("2026-10-17 22:00", tz="UTC"), get, {})
    assert seen == ["20261017", "20261018", "20261019"]
    assert all("-" not in d for d in seen)


def test_a_failing_or_slow_espn_stops_the_run_early(tmp_path, monkeypatch):
    """A hanging ESPN must not hold up publish (cancel-in-progress would then cancel runs
    before their data-log writes): MAX_FAILURES failures or RUN_SECONDS end the run's calls."""

    def boom(url, params=None, timeout=None):
        raise requests.Timeout("slow")

    leagues = ["E0", "SP1", "D1", "I1", "F1", "E1"]
    cards = [card(league=lg) for lg in leagues]
    s = en.add(cards, tmp_path, NOW, boom)
    assert s["failures"] == en.MAX_FAILURES
    assert s["requests"] == en.MAX_FAILURES * (en.RETRIES + 1)
    assert any("stopped early" in e for e in s["errors"])
    assert all("team_news" not in c for c in cards)

    clock = iter([0.0] + [en.RUN_SECONDS + 1.0] * 100)  # start, then past the budget
    monkeypatch.setattr(en.time, "monotonic", lambda: next(clock))
    calls = []
    s = en.add([card()], tmp_path / "b", NOW, fake(calls))
    assert calls == [] and s["requests"] == 0
    assert any("stopped early" in e for e in s["errors"])
